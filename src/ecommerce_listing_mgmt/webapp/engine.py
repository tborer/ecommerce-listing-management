"""Discovery runs: eBay (Deal/Browse API) -> CJ match -> criteria -> review
queue, with optional auto-listing.

Serverless-shaped: a run is a small state machine persisted in the DB
(phase discover -> match -> list -> done) and `advance_run()` does as much
as fits in a time budget, then returns. The dashboard keeps calling
"continue" while it's open, and the cron tick advances anything still
running -- so no single request has to fit a whole run.
"""
from __future__ import annotations

import math
import time
import traceback
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ecommerce_listing_mgmt.ebay import browse as ebay_browse
from ecommerce_listing_mgmt.pipeline import (
    _excluded_reason,
    build_query_variations,
    judge_match,
)
from ecommerce_listing_mgmt.suppliers.cj import (
    CJClient,
    CJError,
    CJTokens,
    cheapest_option_within,
)
from ecommerce_listing_mgmt.webapp import ebay_account
from ecommerce_listing_mgmt.webapp.config import get_settings
from ecommerce_listing_mgmt.webapp.criteria import evaluate
from ecommerce_listing_mgmt.webapp.crypto import decrypt_json, encrypt_json
from ecommerce_listing_mgmt.webapp.models import (
    Candidate,
    Credential,
    Run,
    User,
    UserSettings,
    utcnow,
)
from ecommerce_listing_mgmt.webapp.schemas import UserSettingsModel

CJ_SEARCH_PAGE_SIZE = 20
# A few CJ errors in a row usually means auth/rate-limit trouble, not a bad item.
MAX_CONSECUTIVE_CJ_ERRORS = 3


class RunBlocked(RuntimeError):
    """Run can't proceed until the user fixes something (e.g. no CJ key)."""


# --- settings / credentials --------------------------------------------------

def load_user_settings(db: Session, user: User) -> UserSettingsModel:
    row = db.get(UserSettings, user.id)
    return UserSettingsModel.model_validate(row.data if row else {})


def save_user_settings(db: Session, user: User, s: UserSettingsModel) -> None:
    row = db.get(UserSettings, user.id) or UserSettings(user_id=user.id)
    row.data = s.model_dump()
    db.add(row)
    db.commit()


def get_credential(db: Session, user: User, provider: str) -> Credential | None:
    return db.scalar(select(Credential).where(Credential.user_id == user.id, Credential.provider == provider))


def make_cj_client(db: Session, user: User) -> CJClient:
    """CJ client for this user, persisting refreshed tokens back (encrypted)."""
    cred = get_credential(db, user, "cj")
    if not cred:
        raise RunBlocked("add your CJdropshipping API key under Connections")
    secret = decrypt_json(cred.secret_enc)

    def save_tokens(tokens: CJTokens) -> None:
        secret["tokens"] = tokens.to_dict()
        cred.secret_enc = encrypt_json(secret)
        cred.meta = {**(cred.meta or {}), "token_ok_at": utcnow().isoformat(), "last_error": None}
        db.commit()

    return CJClient(secret["api_key"], tokens=CJTokens.from_dict(secret.get("tokens")), on_tokens=save_tokens)


# --- runs --------------------------------------------------------------------

def active_run(db: Session, user: User) -> Run | None:
    return db.scalar(select(Run).where(Run.user_id == user.id, Run.status == "running")
                     .order_by(Run.id.desc()))


def start_run(db: Session, user: User, trigger: str) -> Run:
    existing = active_run(db, user)
    if existing:
        return existing
    run = Run(user_id=user.id, trigger=trigger, stats={})
    db.add(run)
    db.commit()
    return run


def _bump(run: Run, key: str, n: int = 1) -> None:
    run.stats = {**(run.stats or {}), key: (run.stats or {}).get(key, 0) + n}


def _note_error(run: Run, msg: str) -> None:
    errors = list((run.stats or {}).get("errors", []))[-19:] + [msg]
    run.stats = {**(run.stats or {}), "errors": errors}


def _finish(db: Session, run: Run, status: str = "done", error: str | None = None) -> None:
    run.status, run.phase, run.error, run.finished_at = status, "done", error, utcnow()
    db.commit()


def advance_run(db: Session, run: Run, budget_seconds: float | None = None) -> Run:
    """Do as much of `run` as fits in the budget. Safe to call repeatedly."""
    deadline = time.monotonic() + (budget_seconds or get_settings().run_budget_seconds)
    user = db.get(User, run.user_id)
    s = load_user_settings(db, user)
    try:
        if run.phase == "discover":
            _discover(db, run, user, s)
            run.phase = "match"
            db.commit()
        if run.phase == "match":
            if not _match_pending(db, run, user, s, deadline):
                return run
            run.phase = "list"
            db.commit()
        if run.phase == "list":
            if not _auto_list(db, run, user, s, deadline):
                return run
            _finish(db, run)
    except RunBlocked as e:
        _finish(db, run, "error", str(e))
    except CJError as e:
        _finish(db, run, "error", f"CJdropshipping: {e}")
    except Exception as e:  # keep the run visible instead of stuck "running"
        traceback.print_exc()
        db.rollback()
        _finish(db, run, "error", f"{type(e).__name__}: {e}")
    return run


def _discover(db: Session, run: Run, user: User, s: UserSettingsModel) -> None:
    env = get_settings().ebay_env
    sources = [("deals", u) for u in s.deal_categories] + [("keyword", k) for k in s.keywords]
    if not sources:
        raise RunBlocked("pick at least one eBay deal category or keyword in Settings")
    per_source = max(10, math.ceil(2 * s.items_per_run / len(sources)))
    seen_ids = set(db.scalars(select(Candidate.ebay_item_id).where(Candidate.user_id == user.id)))
    found: list[tuple[str, ebay_browse.ApiItem]] = []
    for kind, value in sources:
        try:
            if kind == "deals":
                items = ebay_browse.discover_deals_url(value, per_source, s.max_ebay_price, env=env,
                                                       credential_mode="env")
                label = f"deals: {value.rstrip('/').rsplit('/', 1)[-1]}"
            else:
                items = ebay_browse.discover_keyword(value, per_source, s.max_ebay_price, env=env,
                                                     credential_mode="env")
                label = f"keyword: {value}"
        except ebay_browse.EbayApiError as e:
            _note_error(run, f"{kind} {value}: {e}")
            continue
        found.extend((label, it) for it in items)
    # The same listing can show up under several categories/keywords.
    unique: dict[str, tuple[str, ebay_browse.ApiItem]] = {}
    for label, it in found:
        unique.setdefault(it.id, (label, it))
    found = list(unique.values())
    _bump(run, "found", len(found))

    added = 0
    # Interleave sources so one busy category doesn't take every slot.
    for label, it in _round_robin(found):
        if added >= s.items_per_run:
            break
        if it.id in seen_ids or not (s.min_ebay_price <= it.priceNum < s.max_ebay_price):
            continue
        if _excluded_reason(it.title):
            _bump(run, "excluded")
            continue
        seen_ids.add(it.id)
        db.add(Candidate(user_id=user.id, run_id=run.id, source=label, ebay_item_id=it.id,
                         ebay_title=it.title[:300], ebay_price=it.priceNum, ebay_url=it.url,
                         ebay_image_url=it.image_url, ebay_category_id=it.category_id,
                         ebay_discount_pct=it.discount_pct))
        added += 1
    _bump(run, "new_candidates", added)
    db.commit()


def _round_robin(found: list[tuple[str, ebay_browse.ApiItem]]) -> list[tuple[str, ebay_browse.ApiItem]]:
    buckets: dict[str, list] = {}
    for label, it in found:
        buckets.setdefault(label, []).append((label, it))
    out = []
    while any(buckets.values()):
        for label in list(buckets):
            if buckets[label]:
                out.append(buckets[label].pop(0))
    return out


def _match_pending(db: Session, run: Run, user: User, s: UserSettingsModel, deadline: float) -> bool:
    """Match discovered candidates until done (True) or out of time (False)."""
    pending = list(db.scalars(select(Candidate).where(
        Candidate.user_id == user.id, Candidate.run_id == run.id, Candidate.status == "discovered")
        .order_by(Candidate.id)))
    if not pending:
        return True
    cj = make_cj_client(db, user)
    consecutive_errors = 0
    for cand in pending:
        if time.monotonic() > deadline:
            return False
        try:
            match_candidate(cj, cand, s)
            consecutive_errors = 0
        except CJError as e:
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_CJ_ERRORS:
                raise
            cand.status, cand.passes, cand.error = "no_match", False, f"CJ error: {e}"
            _note_error(run, f"item {cand.ebay_item_id}: {e}")
        _bump(run, f"status_{cand.status}")
        db.commit()
    return True


def match_candidate(cj: CJClient, cand: Candidate, s: UserSettingsModel) -> None:
    """Find the best CJ product for one eBay candidate, quote shipping, and
    evaluate the criteria. Sets cand.status to no_match / rejected / passed."""
    warehouse = "US" if s.cj_warehouse == "US" else None
    best = None
    products = {}
    for query in build_query_variations(cand.ebay_title, 2):
        for p in cj.search_products(query.replace("-", " "), CJ_SEARCH_PAGE_SIZE, warehouse):
            products[p.pid] = p
        best = judge_match(cand.ebay_title, cand.ebay_price, [
            {"id": p.pid, "title": p.title, "prices": [f"${p.price:.2f}"] if p.price is not None else [],
             "url": p.url, "imageUrl": p.image_url} for p in products.values()])
        if best and best.match_score >= s.min_match_score:
            break

    if not best or not best.id or best.verdict == "NONE" or best.match_score < s.min_match_score:
        cand.status = "no_match"
        cand.match_score = best.match_score if best else 0
        cand.match_reasons = best.reasons if best else ["no CJ results"]
        if best and best.id:
            cand.cj_pid, cand.cj_title, cand.cj_url = best.id, best.title[:500], best.url
        cand.passes = False
        return

    detail = cj.get_product(best.id)
    variant = detail.cheapest_variant()
    cost = variant.price if variant and variant.price is not None else detail.price
    cand.cj_pid, cand.cj_title, cand.cj_url = detail.pid, (detail.title or best.title)[:500], detail.url
    cand.cj_image_url = (variant.image_url if variant else None) or detail.image_url or best.imageUrl
    cand.cj_vid = variant.vid if variant else None
    cand.cj_variant_name = variant.name[:300] if variant else None
    cand.cj_cost = cost
    cand.match_score, cand.match_reasons = best.match_score, best.reasons

    option, options = None, []
    if variant:
        options = cj.freight_quote(variant.vid, "US", "US" if s.cj_warehouse == "US" else "CN")
        option = cheapest_option_within(options, s.max_delivery_days)
    cand.shipping_cost = option.price if option else None
    cand.shipping_method = option.name if option else None
    cand.shipping_days_max = option.max_days if option else None
    cand.details = {
        "cj_description": detail.description[:5000],
        "variant_count": len(detail.variants),
        "freight_options": [{"name": o.name, "price": o.price, "days": [o.min_days, o.max_days]}
                            for o in options[:8]],
    }

    ev = evaluate(ebay_price=cand.ebay_price, match_score=cand.match_score, cj_cost=cost,
                  shipping_cost=cand.shipping_cost, shipping_days_max=cand.shipping_days_max, s=s)
    if len(detail.variants) > 1:
        ev.rules.append({"rule": "Variants", "ok": True,
                         "detail": f"{len(detail.variants)} variants on CJ; priced/listed as '{cand.cj_variant_name}'"})
    cand.list_price, cand.profit, cand.margin_pct = ev.list_price, ev.profit, ev.margin_pct
    cand.criteria, cand.passes = ev.rules, ev.passes
    cand.status = "passed" if ev.passes else "rejected"


def _auto_list(db: Session, run: Run, user: User, s: UserSettingsModel, deadline: float) -> bool:
    if not s.auto_list_enabled or s.auto_list_max_per_run <= 0 or get_settings().auto_list_globally_disabled:
        return True
    already = (run.stats or {}).get("auto_list_attempted", 0)
    remaining = s.auto_list_max_per_run - already
    if remaining <= 0:
        return True
    picks = list(db.scalars(select(Candidate).where(
        Candidate.user_id == user.id, Candidate.run_id == run.id, Candidate.status == "passed")
        .order_by(Candidate.profit.desc()).limit(remaining)))
    for cand in picks:
        if time.monotonic() > deadline:
            return False
        _bump(run, "auto_list_attempted")
        list_candidate(db, user, cand, s, auto=True)
    return True


def list_candidate(db: Session, user: User, cand: Candidate, s: UserSettingsModel, auto: bool = False) -> Candidate:
    cand.status, cand.auto_listed, cand.error = "listing_queued", auto, None
    db.commit()
    try:
        listing_id = ebay_account.publish_candidate(db, user, cand, s)
        cand.status, cand.ebay_listing_id = "listed", listing_id
    except (ebay_account.ListingError, ebay_account.EbayNotConnected) as e:
        cand.status, cand.error = "list_failed", str(e)
    except Exception as e:
        cand.status, cand.error = "list_failed", f"{type(e).__name__}: {e}"
    db.commit()
    return cand


# --- schedule ----------------------------------------------------------------

def last_slot(now: datetime, hour_utc: int) -> datetime:
    """Most recent scheduled time at or before `now`."""
    slot = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return slot if slot <= now else slot - timedelta(days=1)


def is_due(db: Session, user: User, s: UserSettingsModel, now: datetime | None = None) -> bool:
    if not s.schedule_enabled:
        return False
    now = now or datetime.now(timezone.utc)
    slot = last_slot(now, s.schedule_hour_utc)
    latest = db.scalar(select(func.max(Run.started_at)).where(Run.user_id == user.id, Run.trigger == "schedule"))
    if latest is not None and latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return latest is None or latest < slot


def cron_tick(db: Session, budget_seconds: float | None = None) -> dict:
    """Start due scheduled runs and advance every running run, within budget."""
    deadline = time.monotonic() + (budget_seconds or get_settings().run_budget_seconds)
    started = advanced = 0
    for user in db.scalars(select(User)):
        s = load_user_settings(db, user)
        if is_due(db, user, s) and not active_run(db, user):
            start_run(db, user, "schedule")
            started += 1
    for run in list(db.scalars(select(Run).where(Run.status == "running").order_by(Run.id))):
        left = deadline - time.monotonic()
        if left <= 1:
            break
        advance_run(db, run, left)
        advanced += 1
    return {"started": started, "advanced": advanced}
