#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp",
#     "httpx2",
# ]
# ///
"""Branch 11 Item 22: daily DSers-based price-check, no LLM in the loop.

Runs once/day at 1 PM Central (its own dedicated cron, separate from the
8 AM/5 PM/~12 PM Branch 11 jobs -- same "one script, one concern, one cron"
pattern as everything else in this pipeline).

**Price source is DSers, not a fresh AliExpress scrape** (Travis's explicit
2026-09-01 decision): DSers is what actually gets used for fulfillment, so
its current cost is the number that matters. Uses the exact same
`dsers_my_products(store_id, keyword=<ebay_title>)` + exact-title-match
safety check that `branch11_dsers_mapping.py` (Item 37) already validated
live -- only checks SKUs already `dsers_mapped: true` in
`branch11_auto_listed.json`, since only those have a real DSers link to
query. Standalone DSers MCP session (`branch11_dsers_mcp_client.py`), zero
LLM cost, same as every other daily pipeline step.

**Baseline, not the ledger's `ali_price` field**: `branch11_auto_listed.json`'s
own `ali_price` field has a pre-existing bug (silently `None` on many real
entries -- fixed 2026-09-02 in `run_auto_listing()`, but old entries can't be
retroactively recovered). This script keeps its own baseline instead, in
`branch11_price_check.json`: the first-ever-observed DSers cost for a SKU,
established on that SKU's first check and never overwritten after.

**Flag condition** (Travis's explicit call, 2026-09-01): flag a SKU only
when the CURRENT DSers cost would make it fail the SAME target margin its
baseline cost used to pass -- not on every minor cent-level move (DSers
costs drift from currency/rounding noise even when nothing meaningful
changed). When flagged, `suggested_ebay_price()` (branch11_pipeline.py)
gives a real, actionable "raise it to at least $X" number -- Travis decides
whether/how to actually change the live listing; Hard Constraints forbid
this script (or any part of this pipeline) from auto-editing a real
listing's price.

**Advertising section**: real add automation as of 2026-09-02 (Travis's
explicit direction, PRODUCTION-READINESS.md Item 41's read-only half done
earlier the same day, write half MVP'd this same session). Each run, the top
`ADVERTISING_CANDIDATES_TOP_N` highest-current-margin tracked items that
AREN'T already in an active eBay ad campaign get **actually added** to the
account's one existing "General" campaign via the real Marketing API
(`createAdByListingId`, needs the `sell.marketing` write scope Travis
re-consented to live 2026-09-02). MVP, Travis's explicit framing: one flat
`AD_BID_PERCENTAGE_DEFAULT` bid rate (matches what's already live on the
account's other ads in that campaign), add-only -- no removal or per-item
rate logic yet, that's future work. Fails closed on the add step itself
(never guesses which campaign to use -- refuses if there isn't exactly one
RUNNING campaign, or if its funding model isn't COST_PER_SALE, since
`createAdByListingId` only supports CPS), but fails soft on the read/status
check (an error there just means nothing gets added this run, with a note
in the email, rather than crashing the whole price-check). Confirmed live
2026-09-02 this isn't hypothetical: 3 of 18 real listings were already
enrolled in a real "General" COST_PER_SALE campaign (likely eBay's own
default auto-enrollment, not something built by this pipeline) before any
of this pipeline's own adds.

Usage:
    uv run branch11_price_check.py [--dry-run] [--fee-pct 0.17] [--margin-pct 0.16]
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

from ecommerce_listing_mgmt.dsers.mcp_client import dsers_session, extract_items, is_error
from ecommerce_listing_mgmt.pipeline import compute_profit, suggested_ebay_price
from ecommerce_listing_mgmt.reporting.email_report import _money, send_email
from ecommerce_listing_mgmt.ebay.listing import _call, _error_detail

DEFAULT_STORE_ID = "2093790408921645056"  # the DSers-bridge WooCommerce store, per branch11-listing-rules.md
OUT_DIR = Path(__file__).parent
LEDGER_PATH = OUT_DIR / "branch11_auto_listed.json"
PRICE_CHECK_PATH = OUT_DIR / "branch11_price_check.json"
ADVERTISING_LEDGER_PATH = OUT_DIR / "branch11_advertising.json"
FEE_PCT_DEFAULT = 0.17
MARGIN_PCT_DEFAULT = 0.16
REPORT_TO = "tray14@hotmail.com"
ADVERTISING_CANDIDATES_TOP_N = 5
# MVP flat bid rate (Travis's explicit 2026-09-02 direction: "basic rate...
# add more logic later") -- matches the 10.0% already live on this account's
# 3 pre-existing ads in the same campaign, confirmed via a real getAd read
# 2026-09-02, not an arbitrary guess.
AD_BID_PERCENTAGE_DEFAULT = "10.0"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


async def check_price(session, store_id: str, sku: str, entry: dict, price_check_ledger: dict,
                       fee_pct: float, margin_pct: float) -> dict:
    """One SKU's price check. Mutates `price_check_ledger[sku]` in place on
    success (matches this codebase's established fail-closed pattern: an
    ambiguous/not-found/error result is recorded and skipped, never guessed)."""
    title = entry["ebay_title"]
    try:
        result = await session.call_tool("dsers_my_products", {"store_id": store_id, "keyword": title})
    except Exception as e:  # noqa: BLE001
        return {"status": "ERROR", "sku": sku, "ebay_title": title, "reason": str(e)}
    if is_error(result):
        return {"status": "ERROR", "sku": sku, "ebay_title": title, "reason": str(result)}

    products = extract_items(result)
    if not products:
        return {"status": "NOT_FOUND", "sku": sku, "ebay_title": title}

    # Same exact-match safety check as branch11_dsers_mapping.py (Item 37) --
    # keyword search can return partial/substring matches, never guess among them.
    exact = [p for p in products if p.get("title", "").strip().lower() == title.strip().lower()]
    if len(exact) != 1:
        return {"status": "AMBIGUOUS", "sku": sku, "ebay_title": title,
                "reason": f"{len(products)} result(s), {len(exact)} exact match(es)"}

    cost_range = exact[0].get("cost") or {}
    current_cost = cost_range.get("min")
    # A $0 (or missing) cost is never trustworthy real pricing -- confirmed
    # live 2026-09-02 that DSers itself sometimes reports cost: {min: 0, max: 0}
    # for a genuinely real, non-free product (a Makita power tool), almost
    # certainly an incomplete supplier-cost sync on DSers's own side rather
    # than an actual free item. Treated the same as missing data -- never
    # trusted as a real baseline/current cost, never let it pollute the
    # advertising-candidates ranking with a false 100% margin.
    if current_cost is None or current_cost <= 0:
        return {"status": "NO_COST_DATA", "sku": sku, "ebay_title": title}

    ebay_price = entry["ebay_price"]
    ali_shipping = entry.get("ali_shipping_cost") or 0.0

    prior = price_check_ledger.get(sku)
    is_new_baseline = prior is None
    baseline_cost = prior["baseline_cost"] if prior else current_cost
    baseline_established_at = prior["baseline_established_at"] if prior else date.today().isoformat()

    baseline_profit = compute_profit(ebay_price, baseline_cost, fee_pct, margin_pct, ali_shipping)
    current_profit = compute_profit(ebay_price, current_cost, fee_pct, margin_pct, ali_shipping)
    flagged = bool(baseline_profit["passes"] and not current_profit["passes"])
    suggested = (suggested_ebay_price(current_cost, fee_pct, margin_pct, ali_shipping)
                 if flagged else None)

    price_check_ledger[sku] = {
        "baseline_cost": baseline_cost,
        "baseline_established_at": baseline_established_at,
        "last_checked_cost": current_cost,
        "last_checked_at": date.today().isoformat(),
        "last_flagged_price_change": flagged,
    }

    return {
        "status": "OK", "sku": sku, "ebay_title": title, "ebay_price": ebay_price,
        "listing_id": entry.get("listing_id"),
        "baseline_cost": baseline_cost, "current_cost": current_cost,
        "baseline_profit": baseline_profit, "current_profit": current_profit,
        "flagged": flagged, "suggested_ebay_price": suggested,
        "is_new_baseline": is_new_baseline,
    }


# All Marketing API calls use credential_mode="local" (the same unattended
# local-credential path auto-listing uses -- refresh_access_token_unattended()
# via _call() in branch11_ebay_listing.py) since this runs from an unattended
# cron with no BW_SESSION available.

def _list_campaigns(env: str = "production") -> list[dict]:
    """All real ad campaigns on the account, paginated (offset+limit) rather
    than assuming today's single-campaign account holds forever."""
    campaigns: list[dict] = []
    offset = 0
    while True:
        status, body = _call(env, "GET", f"/sell/marketing/v1/ad_campaign?limit=100&offset={offset}",
                              credential_mode="local")
        if status != 200:
            raise RuntimeError(f"listing ad campaigns failed: {_error_detail(status, body)}")
        page = (body or {}).get("campaigns", [])
        campaigns.extend(page)
        offset += len(page)
        if not page or offset >= (body or {}).get("total", 0):
            break
    return campaigns


def _list_ads(campaign_id: str, env: str = "production") -> list[dict]:
    """All real ads in one campaign, paginated."""
    ads: list[dict] = []
    offset = 0
    while True:
        status, body = _call(env, "GET", f"/sell/marketing/v1/ad_campaign/{campaign_id}/ad?limit=100&offset={offset}",
                              credential_mode="local")
        if status != 200:
            raise RuntimeError(f"listing ads for campaign {campaign_id} failed: {_error_detail(status, body)}")
        page = (body or {}).get("ads", [])
        ads.extend(page)
        offset += len(page)
        if not page or offset >= (body or {}).get("total", 0):
            break
    return ads


def _get_advertised_listing_ids(campaigns: list[dict], env: str = "production") -> set[str]:
    """Every listingId currently enrolled in any of `campaigns` (any status)
    -- used to exclude already-advertised items from being re-suggested/
    re-added. Added 2026-09-02 alongside the sell.marketing.readonly scope
    grant."""
    listing_ids: set[str] = set()
    for c in campaigns:
        for ad in _list_ads(c["campaignId"], env):
            if ad.get("listingId"):
                listing_ids.add(str(ad["listingId"]))
    return listing_ids


def _select_ad_campaign(campaigns: list[dict]) -> dict:
    """MVP only ever adds to an existing campaign, never creates one,
    per Travis's explicit 2026-09-02 ask ("add them to that existing
    campaign"). Fails closed (raises) rather than guessing if there isn't
    exactly one usable campaign -- same ambiguity-handling pattern as this
    codebase's DSers exact-title-match (Item 37) -- and refuses a non-CPS
    campaign since createAdByListingId only supports the Cost-Per-Sale
    funding model (the CPC equivalent is a different call, not built here)."""
    running = [c for c in campaigns if c.get("campaignStatus") == "RUNNING"]
    if len(running) != 1:
        raise RuntimeError(f"expected exactly 1 RUNNING ad campaign, found {len(running)} -- not guessing which to use")
    campaign = running[0]
    funding_model = (campaign.get("fundingStrategy") or {}).get("fundingModel")
    if funding_model != "COST_PER_SALE":
        raise RuntimeError(f"campaign {campaign['campaignId']} funding model is {funding_model!r}, not "
                            f"COST_PER_SALE -- createAdByListingId only supports CPS, refusing to guess the CPC path")
    return campaign


def _effective_bid_percentage(campaign: dict, requested_bid_percentage: str) -> str | None:
    """eBay rejects an explicit bidPercentage on createAdByListingId when the
    campaign's adRateStrategy is DYNAMIC -- confirmed live 2026-09-02
    (errorId 35010, "The bidPercentage should not be provided when selected
    adRateStrategy is DYNAMIC for the campaign") against this account's real
    campaign. The rate is eBay-managed within the campaign's own configured
    cap in that case (`dynamicAdRatePreferences[].adRateCapPercent`), not
    settable per-ad. Returns None (omit the field entirely) for a DYNAMIC
    campaign, otherwise the requested flat rate."""
    strategy = (campaign.get("fundingStrategy") or {}).get("adRateStrategy")
    return None if strategy == "DYNAMIC" else requested_bid_percentage


def _add_to_campaign(campaign_id: str, listing_id: str, bid_percentage: str | None, env: str = "production") -> dict:
    """One real, mutating call: enrolls `listing_id` in `campaign_id`, at
    `bid_percentage` if given (omitted entirely for a DYNAMIC-rate campaign,
    see _effective_bid_percentage()). Raises with eBay's real error detail on
    failure -- caller is responsible for catching per-candidate so one
    failure doesn't stop the rest (same pattern as every other per-item
    write in this pipeline)."""
    body = {"listingId": listing_id}
    if bid_percentage is not None:
        body["bidPercentage"] = bid_percentage
    status, resp_body = _call(env, "POST", f"/sell/marketing/v1/ad_campaign/{campaign_id}/ad",
                               body=body, credential_mode="local")
    if status not in (200, 201, 204):
        raise RuntimeError(_error_detail(status, resp_body))
    return resp_body or {}


async def add_to_advertising(ok_results: list[dict], campaigns: list[dict], advertised_ids: set[str],
                              advertising_ledger: dict, bid_percentage: str,
                              dry_run: bool) -> tuple[list[dict], list[dict], dict | None, str | None, str]:
    """Selects up to ADVERTISING_CANDIDATES_TOP_N of today's highest-current-
    margin tracked items not already in an active ad campaign, and actually
    adds them to the account's one existing campaign (MVP, Travis's explicit
    2026-09-02 direction -- flat bid rate where the campaign allows one,
    add-only). Mutates `advertising_ledger` in place on each real success.
    Returns (added, failed, campaign_used, error, rate_label) -- `error` is
    set (and nothing attempted) only if campaign selection itself fails
    (e.g. not exactly one RUNNING campaign); `rate_label` describes the rate
    actually used, for the email/ledger."""
    eligible = [r for r in ok_results if str(r.get("listing_id")) not in advertised_ids]
    candidates = sorted(eligible, key=_margin_pct, reverse=True)[:ADVERTISING_CANDIDATES_TOP_N]
    if not candidates:
        return [], [], None, None, bid_percentage

    try:
        campaign = _select_ad_campaign(campaigns)
    except RuntimeError as e:
        return [], [], None, str(e), bid_percentage

    effective_bid = _effective_bid_percentage(campaign, bid_percentage)
    rate_label = effective_bid if effective_bid is not None else "dynamic (eBay-managed, capped by the campaign's own settings)"

    added: list[dict] = []
    failed: list[dict] = []
    for r in candidates:
        sku, listing_id = r["sku"], str(r["listing_id"])
        if dry_run:
            added.append(r)
            continue
        try:
            result = _add_to_campaign(campaign["campaignId"], listing_id, effective_bid)
            advertising_ledger[sku] = {
                "listing_id": listing_id, "ebay_title": r["ebay_title"],
                "campaign_id": campaign["campaignId"], "bid_percentage": rate_label,
                "ad_id": result.get("adId"), "added_at": date.today().isoformat(),
                "margin_at_add_time": round(_margin_pct(r), 4),
            }
            added.append(r)
            print(f"[price-check] added {sku} (listing {listing_id}) to campaign {campaign['campaignId']} ({rate_label})")
        except Exception as e:  # noqa: BLE001 -- one candidate's failure must not stop the rest
            failed.append({**r, "reason": str(e)})
            print(f"[price-check] FAILED to add {sku} to advertising: {e}")
    return added, failed, campaign, None, rate_label


def _margin_pct(r: dict) -> float:
    profit = r["current_profit"]["total_profit"]
    price = r["ebay_price"]
    return (profit / price) if price else 0.0


def build_email_body(results: list[dict], advertised_ids: set[str] | None = None,
                      ad_check_error: str | None = None, added: list[dict] | None = None,
                      failed: list[dict] | None = None, add_error: str | None = None,
                      dry_run: bool = False, rate_label: str = AD_BID_PERCENTAGE_DEFAULT) -> tuple[str, str]:
    ok = [r for r in results if r["status"] == "OK"]
    flagged = [r for r in ok if r["flagged"]]
    problems = [r for r in results if r["status"] != "OK"]
    today = date.today().isoformat()

    lines = [f"Checked {len(results)} DSers-mapped item(s): {len(ok)} OK, {len(flagged)} flagged for a price "
             f"increase, {len(problems)} could not be checked.", ""]

    lines.append("=== PRICE INCREASE -- ACTION NEEDED "
                 f"({len(flagged)}) ===")
    lines.append("(Supplier cost has risen enough that the current eBay price no longer clears its own "
                 "original target margin. This does NOT change anything automatically -- review and update "
                 "the live eBay listing yourself if you agree.)")
    lines.append("")
    if flagged:
        for r in flagged:
            lines.append(f"* {r['sku']} -- {r['ebay_title']}")
            if r.get("listing_id"):
                lines.append(f"  Live listing: https://www.ebay.com/itm/{r['listing_id']}")
            lines.append(f"  Current eBay price: {_money(r['ebay_price'])}")
            lines.append(f"  DSers cost: {_money(r['baseline_cost'])} (baseline) -> {_money(r['current_cost'])} (now)")
            lines.append(f"  Profit at baseline cost: {_money(r['baseline_profit']['total_profit'])} (passed target margin)")
            lines.append(f"  Profit at current cost: {_money(r['current_profit']['total_profit'])} (now FAILS target margin)")
            lines.append(f"  Suggested new eBay price to restore the same target margin: {_money(r['suggested_ebay_price'])}")
            lines.append("")
    else:
        lines.append("(none)")
        lines.append("")

    advertised_ids = advertised_ids or set()
    added = added or []
    failed = failed or []
    eligible = [r for r in ok if str(r.get("listing_id")) not in advertised_ids]
    already_advertised_count = len(ok) - len(eligible)

    verb = "WOULD ADD" if dry_run else "ADDED"
    lines.append(f"=== ADVERTISING -- {verb} TO YOUR EXISTING EBAY AD CAMPAIGN (top {ADVERTISING_CANDIDATES_TOP_N}/day by margin) ===")
    lines.append(f"(MVP, per your direction: add-only, rate {rate_label} -- your campaign uses eBay's dynamic ad "
                 "rate, so eBay manages the actual bid within your campaign's own configured cap rather than a "
                 "value this pipeline sets. No removal or per-item rate logic yet.)")
    reason = ad_check_error or add_error
    if reason:
        lines.append(f"(Nothing added this run -- {reason})")
    lines.append("")
    if added:
        for r in added:
            lines.append(f"* {r['sku']} -- {r['ebay_title']} -- margin {_margin_pct(r):.0%} "
                         f"(profit {_money(r['current_profit']['total_profit'])} on {_money(r['ebay_price'])})")
        lines.append("")
    elif not reason:
        lines.append("(none eligible today)")
        lines.append("")
    if failed:
        lines.append(f"COULD NOT ADD ({len(failed)}):")
        for r in failed:
            lines.append(f"* {r['sku']} -- {r['ebay_title']} -- {r['reason']}")
        lines.append("")
    if already_advertised_count and not ad_check_error:
        lines.append(f"({already_advertised_count} other tracked item(s) skipped -- already in an active eBay ad campaign)")
        lines.append("")

    lines.append(f"=== ALL TRACKED ITEMS ({len(ok)}) ===")
    for r in sorted(ok, key=lambda r: r["sku"]):
        lines.append(f"* {r['sku']} -- {r['ebay_title'][:60]} -- cost {_money(r['current_cost'])}, "
                     f"margin {_margin_pct(r):.0%}, checked {today}"
                     + (" [NEW BASELINE]" if r["is_new_baseline"] else ""))
    lines.append("")

    if problems:
        lines.append(f"=== COULD NOT CHECK ({len(problems)}) ===")
        for r in problems:
            lines.append(f"* {r['sku']} -- {r['ebay_title']} -- {r['status']}"
                         + (f": {r['reason']}" if r.get("reason") else ""))

    subject = f"Branch 11 Price Check Report - {today}"
    return subject, "\n".join(lines)


async def run(store_id: str, fee_pct: float, margin_pct: float, dry_run: bool) -> None:
    ledger = _load_json(LEDGER_PATH)
    price_check_ledger = _load_json(PRICE_CHECK_PATH)
    targets = [(sku, e) for sku, e in ledger.items() if e.get("dsers_mapped")]
    print(f"[price-check] {len(targets)} DSers-mapped ledger entries to check.")

    results: list[dict] = []
    async with dsers_session("Branch 11 Price Check (standalone)") as session:
        for sku, entry in targets:
            r = await check_price(session, store_id, sku, entry, price_check_ledger, fee_pct, margin_pct)
            results.append(r)
            _save_json(PRICE_CHECK_PATH, price_check_ledger)  # persist immediately, matches this codebase's own pattern
            if r["status"] == "OK":
                flag = " FLAGGED" if r["flagged"] else ""
                print(f"[price-check] {sku}: cost {r['baseline_cost']} -> {r['current_cost']}{flag}")
            else:
                print(f"[price-check] {sku}: {r['status']}" + (f" -- {r.get('reason')}" if r.get("reason") else ""))

    try:
        campaigns = _list_campaigns()
        advertised_ids = _get_advertised_listing_ids(campaigns)
        ad_check_error = None
        print(f"[price-check] {len(advertised_ids)} listing(s) currently in an active eBay ad campaign.")
    except Exception as e:  # noqa: BLE001 -- fails soft: nothing added this run, not a crash
        campaigns, advertised_ids, ad_check_error = [], set(), str(e)
        print(f"[price-check] could not check ad-campaign status, skipping advertising this run: {e}")

    advertising_ledger = _load_json(ADVERTISING_LEDGER_PATH)
    added: list[dict] = []
    failed: list[dict] = []
    add_error: str | None = None
    rate_label = AD_BID_PERCENTAGE_DEFAULT
    if ad_check_error is None:
        ok_results = [r for r in results if r["status"] == "OK"]
        added, failed, _campaign, add_error, rate_label = await add_to_advertising(
            ok_results, campaigns, advertised_ids, advertising_ledger, AD_BID_PERCENTAGE_DEFAULT, dry_run)
        if not dry_run and (added or failed):
            _save_json(ADVERTISING_LEDGER_PATH, advertising_ledger)

    subject, body = build_email_body(results, advertised_ids, ad_check_error, added, failed, add_error, dry_run, rate_label)
    if dry_run:
        print(f"\nSubject: {subject}\n")
        print(body)
        return

    send_email(subject, body)
    print(f"[price-check] Sent '{subject}' to {REPORT_TO}.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store-id", default=DEFAULT_STORE_ID)
    ap.add_argument("--fee-pct", type=float, default=FEE_PCT_DEFAULT)
    ap.add_argument("--margin-pct", type=float, default=MARGIN_PCT_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="Print the email instead of sending it.")
    args = ap.parse_args()
    asyncio.run(run(args.store_id, args.fee_pct, args.margin_pct, args.dry_run))


if __name__ == "__main__":
    main()
