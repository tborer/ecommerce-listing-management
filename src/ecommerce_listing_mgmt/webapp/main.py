"""FastAPI app: JSON API under /api, consumed by the Next.js dashboard
(web/) through a same-origin rewrite so the session cookie stays first-party."""
from __future__ import annotations

import hmac
import secrets
import traceback
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ecommerce_listing_mgmt.ebay.browse import DEALS_PAGE_CATEGORIES
from ecommerce_listing_mgmt.suppliers.cj import CJError
from ecommerce_listing_mgmt.webapp import ebay_account, engine
from ecommerce_listing_mgmt.webapp.config import get_settings
from ecommerce_listing_mgmt.webapp.crypto import EncryptionNotConfigured, encrypt_json
from ecommerce_listing_mgmt.webapp.db import get_db
from ecommerce_listing_mgmt.webapp.models import (
    Candidate,
    Credential,
    Run,
    User,
    UserSession,
    utcnow,
)
from ecommerce_listing_mgmt.webapp.schemas import (
    AuthIn,
    CJCredentialIn,
    UserSettingsModel,
    deal_category_label,
)
from ecommerce_listing_mgmt.webapp.security import (
    SESSION_COOKIE,
    create_session,
    current_session,
    current_user,
    hash_password,
    token_hash,
    verify_password,
)

app = FastAPI(title="SourceSnap API", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.exception_handler(EncryptionNotConfigured)
def _encryption_error(_: Request, exc: EncryptionNotConfigured) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=503)


# --- serializers -------------------------------------------------------------

def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def run_out(run: Run | None) -> dict | None:
    if run is None:
        return None
    return {"id": run.id, "trigger": run.trigger, "status": run.status, "phase": run.phase,
            "stats": run.stats or {}, "error": run.error,
            "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at)}


CANDIDATE_FIELDS = (
    "id", "run_id", "status", "source", "ebay_item_id", "ebay_title", "ebay_price", "ebay_url",
    "ebay_image_url", "ebay_category_id", "ebay_discount_pct", "cj_pid", "cj_vid", "cj_title",
    "cj_variant_name", "cj_url", "cj_image_url", "cj_cost", "shipping_cost", "shipping_method",
    "shipping_days_max", "match_score", "match_reasons", "list_price", "profit", "margin_pct",
    "criteria", "passes", "auto_listed", "ebay_listing_id", "error")


def candidate_out(c: Candidate) -> dict:
    out = {f: getattr(c, f) for f in CANDIDATE_FIELDS}
    out["ebay_listing_url"] = f"https://www.ebay.com/itm/{c.ebay_listing_id}" if c.ebay_listing_id else None
    out["updated_at"] = _iso(c.updated_at)
    out["freight_options"] = (c.details or {}).get("freight_options", [])
    return out


# --- auth --------------------------------------------------------------------

def _signup_allowed(db: Session) -> bool:
    return get_settings().allow_signup or db.scalar(select(func.count(User.id))) == 0


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/auth/config")
def auth_config(db: Session = Depends(get_db)) -> dict:
    return {"signup_allowed": _signup_allowed(db)}


@app.post("/api/auth/signup")
def signup(body: AuthIn, response: Response, db: Session = Depends(get_db)) -> dict:
    if not _signup_allowed(db):
        raise HTTPException(403, "sign-ups are closed")
    if db.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(409, "an account with that email already exists")
    user = User(email=body.email, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    create_session(db, user, response)
    return {"email": user.email}


@app.post("/api/auth/login")
def login(body: AuthIn, response: Response, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.email == body.email))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "wrong email or password")
    create_session(db, user, response)
    return {"email": user.email}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        sess = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
        if sess:
            db.delete(sess)
            db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"email": user.email}


# --- settings ----------------------------------------------------------------

@app.get("/api/settings")
def get_user_settings(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return {
        "settings": engine.load_user_settings(db, user).model_dump(),
        "options": {"deal_categories": [{"value": u, "label": deal_category_label(u)}
                                        for u in DEALS_PAGE_CATEGORIES]},
        "auto_list_globally_disabled": get_settings().auto_list_globally_disabled,
    }


@app.put("/api/settings")
def put_user_settings(body: UserSettingsModel, user: User = Depends(current_user),
                      db: Session = Depends(get_db)) -> dict:
    engine.save_user_settings(db, user, body)
    return {"settings": body.model_dump()}


# --- connections -------------------------------------------------------------

def _cj_status(cred: Credential | None) -> dict:
    if not cred:
        return {"connected": False}
    meta = cred.meta or {}
    return {"connected": True, "key_hint": meta.get("key_hint"), "updated_at": _iso(cred.updated_at),
            "token_ok_at": meta.get("token_ok_at"), "last_error": meta.get("last_error")}


@app.get("/api/connections")
def connections(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return {"cj": _cj_status(engine.get_credential(db, user, "cj")),
            "ebay": ebay_account.connection_status(db, user),
            "encryption_configured": bool(get_settings().encryption_key)}


def _test_cj(db: Session, user: User) -> dict:
    cred = engine.get_credential(db, user, "cj")
    try:
        engine.make_cj_client(db, user).ensure_token()
        return {"ok": True, **_cj_status(cred)}
    except (CJError, ValueError) as e:
        cred.meta = {**(cred.meta or {}), "last_error": str(e)}
        db.commit()
        return {"ok": False, "error": str(e), **_cj_status(cred)}


@app.put("/api/connections/cj")
def put_cj(body: CJCredentialIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    key = body.api_key.strip()
    cred = engine.get_credential(db, user, "cj") or Credential(user_id=user.id, provider="cj")
    cred.secret_enc = encrypt_json({"api_key": key})
    cred.meta = {"key_hint": f"…{key[-4:]}", "saved_at": utcnow().isoformat()}
    db.add(cred)
    db.commit()
    return _test_cj(db, user)


@app.post("/api/connections/cj/test")
def test_cj(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if not engine.get_credential(db, user, "cj"):
        raise HTTPException(404, "no CJ API key saved")
    return _test_cj(db, user)


@app.delete("/api/connections/cj")
def delete_cj(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    cred = engine.get_credential(db, user, "cj")
    if cred:
        db.delete(cred)
        db.commit()
    return {"connected": False}


@app.get("/api/connections/ebay/start")
def ebay_start(sess: UserSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    if not ebay_account.app_configured():
        raise HTTPException(503, "eBay app keys (EBAY_APP_ID, EBAY_CERT_ID, EBAY_RUNAME) aren't configured")
    sess.oauth_state = secrets.token_urlsafe(24)
    db.commit()
    return {"url": ebay_account.consent_url(sess.oauth_state)}


@app.get("/api/ebay/callback")
def ebay_callback(request: Request, code: str | None = None, state: str | None = None,
                  db: Session = Depends(get_db)) -> RedirectResponse:
    base = get_settings().app_base_url
    try:
        sess = current_session(request, db)
    except HTTPException:
        return RedirectResponse(f"{base}/login?next=/connections")
    if not code or not state or not sess.oauth_state or not hmac.compare_digest(state, sess.oauth_state):
        return RedirectResponse(f"{base}/connections?ebay=error")
    sess.oauth_state = None
    db.commit()
    try:
        ebay_account.exchange_code_and_store(db, db.get(User, sess.user_id), code)
    except Exception:
        traceback.print_exc()
        return RedirectResponse(f"{base}/connections?ebay=error")
    return RedirectResponse(f"{base}/connections?ebay=connected")


@app.delete("/api/connections/ebay")
def ebay_disconnect(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    ebay_account.disconnect(db, user)
    return {"connected": False}


@app.get("/api/ebay/policies")
def ebay_policies(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    try:
        return ebay_account.get_policies(db, user)
    except (ebay_account.EbayNotConnected, ebay_account.ListingError) as e:
        raise HTTPException(400, str(e)) from None


# --- dashboard / runs / candidates ------------------------------------------

@app.get("/api/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    counts = dict(db.execute(select(Candidate.status, func.count(Candidate.id))
                             .where(Candidate.user_id == user.id).group_by(Candidate.status)).all())
    last = db.scalar(select(Run).where(Run.user_id == user.id).order_by(Run.id.desc()))
    s = engine.load_user_settings(db, user)
    next_slot = engine.last_slot(datetime.now(timezone.utc), s.schedule_hour_utc) + timedelta(days=1)
    return {
        "counts": counts,
        "active_run": run_out(engine.active_run(db, user)),
        "last_run": run_out(last),
        "schedule": {"enabled": s.schedule_enabled, "hour_utc": s.schedule_hour_utc,
                     "next_slot_at": _iso(next_slot) if s.schedule_enabled else None},
        "auto_list": {"enabled": s.auto_list_enabled, "max_per_run": s.auto_list_max_per_run},
        "connections": {"cj": bool(engine.get_credential(db, user, "cj")),
                        "ebay": bool(engine.get_credential(db, user, "ebay"))},
    }


@app.get("/api/runs")
def list_runs(limit: int = Query(20, le=100), user: User = Depends(current_user),
              db: Session = Depends(get_db)) -> list[dict]:
    runs = db.scalars(select(Run).where(Run.user_id == user.id).order_by(Run.id.desc()).limit(limit))
    return [run_out(r) for r in runs]


@app.post("/api/runs")
def start_run(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = engine.start_run(db, user, "manual")
    return run_out(engine.advance_run(db, run))


@app.post("/api/runs/{run_id}/continue")
def continue_run(run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(404, "run not found")
    if run.status == "running":
        engine.advance_run(db, run)
    return run_out(run)


CANDIDATE_VIEWS = {
    "review": ["passed"],
    "listed": ["listed", "listing_queued"],
    "failed": ["list_failed"],
    "rejected": ["rejected", "no_match"],
    "pending": ["discovered"],
    "dismissed": ["dismissed"],
}


@app.get("/api/candidates")
def list_candidates(view: str = "review", limit: int = Query(100, le=500), offset: int = 0,
                    user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    q = select(Candidate).where(Candidate.user_id == user.id)
    if view != "all":
        if view not in CANDIDATE_VIEWS:
            raise HTTPException(400, f"view must be one of {['all', *CANDIDATE_VIEWS]}")
        q = q.where(Candidate.status.in_(CANDIDATE_VIEWS[view]))
    order = Candidate.profit.desc() if view == "review" else Candidate.updated_at.desc()
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(order, Candidate.id.desc()).limit(limit).offset(offset))
    return {"total": total, "items": [candidate_out(c) for c in rows]}


def _owned_candidate(db: Session, user: User, cid: int) -> Candidate:
    cand = db.get(Candidate, cid)
    if not cand or cand.user_id != user.id:
        raise HTTPException(404, "candidate not found")
    return cand


@app.post("/api/candidates/{cid}/list")
def list_one(cid: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    cand = _owned_candidate(db, user, cid)
    if cand.status not in ("passed", "list_failed", "rejected"):
        raise HTTPException(409, f"can't list a candidate in status '{cand.status}'")
    if not cand.cj_pid or cand.cj_cost is None:
        raise HTTPException(409, "no CJ match to list from")
    engine.list_candidate(db, user, cand, engine.load_user_settings(db, user))
    return candidate_out(cand)


@app.post("/api/candidates/{cid}/dismiss")
def dismiss(cid: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    cand = _owned_candidate(db, user, cid)
    if cand.status in ("listed", "listing_queued"):
        raise HTTPException(409, "already listed")
    cand.details = {**(cand.details or {}), "status_before_dismiss": cand.status}
    cand.status = "dismissed"
    db.commit()
    return candidate_out(cand)


@app.post("/api/candidates/{cid}/restore")
def restore(cid: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    cand = _owned_candidate(db, user, cid)
    if cand.status != "dismissed":
        raise HTTPException(409, "not dismissed")
    cand.status = (cand.details or {}).get("status_before_dismiss") or ("passed" if cand.passes else "rejected")
    db.commit()
    return candidate_out(cand)


# --- cron --------------------------------------------------------------------

def _check_cron_auth(request: Request) -> None:
    secret = get_settings().cron_secret
    if not secret:
        raise HTTPException(503, "CRON_SECRET is not configured")
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied, f"Bearer {secret}"):
        raise HTTPException(401, "bad cron credentials")


@app.api_route("/api/cron/tick", methods=["GET", "POST"])
def tick(request: Request, db: Session = Depends(get_db)) -> dict:
    _check_cron_auth(request)
    return engine.cron_tick(db)
