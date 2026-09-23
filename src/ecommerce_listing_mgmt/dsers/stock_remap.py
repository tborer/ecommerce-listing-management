#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp",
#     "httpx2",
# ]
# ///
"""Branch 11 Item 42, step 2: for any SKU `branch11_stock_check.py` still
has flagged unresolved (its AliExpress supplier is out of stock/unavailable/
gone), search for a real replacement supplier and remap to it via DSers if a
genuine same-item match is found -- no LLM in the loop.

Why this exists: detection alone (step 1, `branch11_stock_check.py`) only
makes the problem visible; it never fixes anything. A DSers-mapped item
whose supplier goes out of stock stays unsourceable until someone finds and
links a new supplier. This script is that someone, using the exact same
same-item verification discipline (`judge_match()`/`build_dsers_ali_match()`
-- construction-material mismatch, brand-authenticity mismatch, accessory/
light/wired-wireless checks, all of it) already trusted elsewhere in this
pipeline to decide HIGH-verdict matches, including the one already-approved
carve-out that auto-publishes brand-new eBay listings (`AUTO_LIST_TOP_N`).
Remapping a DSers supplier for an *already-existing, already-approved*
listing is lower-stakes than that: it never touches eBay at all (no
create/edit/price on a live listing), never spends real money, and uses the
exact `dsers_sku_remap` preview/apply + confidence-threshold pattern Item 37
already established as safe to run unattended.

**Scope discipline, per PRODUCTION-READINESS.md Item 42**: only ever calls
`dsers_sku_remap`. Never touches eBay. If no acceptable replacement is
found, the SKU is left exactly as `branch11_stock_check.py` already flagged
it -- this script does NOT end/remove the eBay listing (Item 42 step 3,
still `NEEDS DECISION`, explicitly out of scope here).

**Acceptance bar for an autonomous remap** (deliberately at least as strict
as the auto-list carve-out, arguably stricter):
  1. A live `dsers_find_product` search for the eBay item's own title.
  2. The currently-broken supplier's own AliExpress id is excluded from
     candidates (comparing the numeric id embedded in `ali_url`/`import_url`)
     -- otherwise a stale/cached DSers search could "remap" right back to the
     same broken supplier.
  3. `build_dsers_ali_match(..., verify=True)` (the exact function/rubric
     `--stage finish` uses, including the one real browser product-page
     visit for MEDIUM/HIGH candidates) must return verdict `HIGH` --
     `MEDIUM` is never auto-acted on, matching the Hard Constraint text for
     the eBay auto-list carve-out ("does not extend to MEDIUM-verdict
     matches") even though this specific action isn't an eBay write.
  4. The replacement must still clear the *original* target margin at the
     existing eBay price (`compute_profit()`, same fee/margin defaults as
     `branch11_price_check.py`) -- a technically-same-item replacement that
     would make the listing unprofitable is not auto-remapped; recorded
     instead as `UNPROFITABLE` for Travis's manual call.
  5. `dsers_sku_remap(mode='preview')`'s own diff confidences must all clear
     `CONFIDENCE_THRESHOLD` (70, same bar as Item 37) before `mode='apply'`
     is ever called. Never skips the preview step.
On success, mirrors the new `ali_url`/`ali_id`/`ali_price`/`ali_shipping_cost`
back onto `branch11_auto_listed.json` (the same fields Item 30's WooCommerce
order-push and Item 22's price-check both read) so downstream steps see the
new supplier, not the dead one. Does NOT touch `branch11_stock_check.json`'s
`resolved_at` itself -- that ledger's job is to report DSers's own
ground-truth `supplier_status`, and the next `branch11_stock_check.py` run
will observe (and record) resolution for real once DSers's own data reflects
it, rather than this script asserting success that DSers hasn't confirmed.

New ledger `branch11_stock_remap.json` (per-SKU list of attempts: date,
result, candidate title/url, verdict, match_score, confidences) -- a record
of what was tried and why, whether or not it succeeded.

Usage:
    uv run branch11_stock_remap.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

from ecommerce_listing_mgmt.dsers.mcp_client import dsers_session, extract_items, extract_json, is_error
from ecommerce_listing_mgmt.pipeline import browser_start, browser_stop, build_dsers_ali_match, compute_profit
from ecommerce_listing_mgmt.reporting.email_report import send_email

DEFAULT_STORE_ID = "2093790408921645056"  # the DSers-bridge WooCommerce store, per branch11-listing-rules.md
OUT_DIR = Path(__file__).parent
LEDGER_PATH = OUT_DIR / "branch11_auto_listed.json"
STOCK_CHECK_PATH = OUT_DIR / "branch11_stock_check.json"
REMAP_PATH = OUT_DIR / "branch11_stock_remap.json"
REPORT_TO = "tray14@hotmail.com"
CONFIDENCE_THRESHOLD = 70  # same bar as branch11_dsers_mapping.py (Item 37)
FEE_PCT_DEFAULT = 0.17
MARGIN_PCT_DEFAULT = 0.16
_ALI_ID_RE = re.compile(r"/item/(\d+)\.html")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _ali_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _ALI_ID_RE.search(url)
    return m.group(1) if m else None


async def attempt_remap(session, store_id: str, sku: str, entry: dict, fee_pct: float, margin_pct: float) -> dict:
    """One SKU's remap attempt. Returns a result dict, never raises -- any
    failure is recorded and treated as 'no replacement found this time',
    matching this codebase's established fail-closed pattern."""
    title = entry["ebay_title"]
    ebay_price = entry["ebay_price"]
    broken_ali_id = entry.get("ali_id") or _ali_id_from_url(entry.get("ali_url"))

    try:
        result = await session.call_tool("dsers_find_product", {"keyword": title, "limit": 10, "supplier": "aliexpress"})
    except Exception as exc:  # noqa: BLE001
        return {"result": "ERROR", "reason": f"dsers_find_product error: {exc}"}
    if is_error(result):
        return {"result": "ERROR", "reason": f"dsers_find_product returned an error: {result}"}

    candidates = [c for c in extract_items(result) if _ali_id_from_url(c.get("import_url")) != broken_ali_id]
    if not candidates:
        return {"result": "NO_REPLACEMENT_FOUND", "reason": "dsers_find_product returned no candidates (excluding the broken supplier)"}

    browser_start()
    try:
        match = build_dsers_ali_match(title, ebay_price, candidates, verify=True)
    finally:
        browser_stop()

    if match.verdict != "HIGH":
        return {"result": "NO_REPLACEMENT_FOUND", "reason": f"best candidate verdict {match.verdict} (need HIGH)",
                "candidate_title": match.title, "candidate_url": match.url, "match_score": match.match_score}

    cheapest = min(float(p.replace("$", "")) for p in match.prices) if match.prices else None
    if cheapest is None:
        return {"result": "NO_REPLACEMENT_FOUND", "reason": "HIGH verdict but no usable price on the candidate",
                "candidate_title": match.title, "candidate_url": match.url}

    ali_shipping = match.shipping_cost if match.shipping_cost is not None else 0.0
    profit = compute_profit(ebay_price, cheapest, fee_pct, margin_pct, ali_shipping)
    if not profit["passes"]:
        return {"result": "UNPROFITABLE", "reason": f"replacement clears verification but fails the original target margin "
                                                      f"(would-be profit {profit['total_profit']:.2f})",
                "candidate_title": match.title, "candidate_url": match.url, "match_score": match.match_score}

    dsers_product_id = entry.get("dsers_product_id")
    if not dsers_product_id:
        return {"result": "ERROR", "reason": "ledger entry has no dsers_product_id -- cannot call dsers_sku_remap"}

    try:
        preview = await session.call_tool(
            "dsers_sku_remap",
            {"dsers_product_id": str(dsers_product_id), "store_id": store_id, "new_supplier_url": match.url, "mode": "preview"},
        )
        preview_body = extract_json(preview) or {}
    except Exception as exc:  # noqa: BLE001
        return {"result": "ERROR", "reason": f"dsers_sku_remap preview error: {exc}"}

    diffs = preview_body.get("diffs") or []
    confidences = [d.get("confidence", 0) for d in diffs]
    if not diffs or any(c < CONFIDENCE_THRESHOLD for c in confidences):
        return {"result": "LOW_CONFIDENCE", "reason": f"dsers_sku_remap preview confidence(s) {confidences} below {CONFIDENCE_THRESHOLD}",
                "candidate_title": match.title, "candidate_url": match.url, "match_score": match.match_score, "confidences": confidences}

    return {
        "result": "REMAP_READY", "candidate_title": match.title, "candidate_url": match.url,
        "candidate_ali_id": match.id, "candidate_price": cheapest, "candidate_shipping": ali_shipping,
        "match_score": match.match_score, "confidences": confidences, "profit": profit,
    }


async def run(store_id: str, fee_pct: float, margin_pct: float, dry_run: bool) -> None:
    ledger = _load_json(LEDGER_PATH)
    stock_check = _load_json(STOCK_CHECK_PATH)
    remap_ledger = _load_json(REMAP_PATH)

    targets = [sku for sku, s in stock_check.items() if s.get("resolved_at") is None and sku in ledger]
    print(f"[stock-remap] {len(targets)} SKU(s) still unresolved per branch11_stock_check.json.")

    if not targets:
        print("[stock-remap] nothing to do.")
        return

    report_lines: list[str] = []
    today = date.today().isoformat()

    async with dsers_session("Branch 11 Stock Remap (standalone)") as session:
        for sku in targets:
            entry = ledger[sku]
            r = await attempt_remap(session, store_id, sku, entry, fee_pct, margin_pct)
            r["attempted_at"] = today
            remap_ledger.setdefault(sku, []).append(r)

            if r["result"] == "REMAP_READY":
                if dry_run:
                    print(f"[stock-remap] {sku}: would remap to {r['candidate_title']!r} "
                          f"(confidences {r['confidences']}) -- --dry-run, not applying")
                    report_lines.append(f"* {sku} -- {entry['ebay_title']} -- WOULD REMAP (dry-run) to "
                                         f"{r['candidate_title']} (${r['candidate_price']})")
                else:
                    try:
                        apply_result = await session.call_tool(
                            "dsers_sku_remap",
                            {"dsers_product_id": str(entry["dsers_product_id"]), "store_id": store_id,
                             "new_supplier_url": r["candidate_url"], "mode": "apply"},
                        )
                    except Exception as exc:  # noqa: BLE001
                        r["result"] = "ERROR"
                        r["reason"] = f"dsers_sku_remap apply error: {exc}"
                        print(f"[stock-remap] {sku}: apply errored -- {exc}")
                        continue
                    if is_error(apply_result):
                        r["result"] = "ERROR"
                        r["reason"] = f"dsers_sku_remap apply returned an error: {apply_result}"
                        print(f"[stock-remap] {sku}: apply returned an error")
                        continue

                    old_ali_id = entry.get("ali_id")
                    entry["ali_url"] = r["candidate_url"]
                    entry["ali_id"] = r["candidate_ali_id"]
                    entry["ali_price"] = r["candidate_price"]
                    entry["ali_shipping_cost"] = r["candidate_shipping"]
                    entry["dsers_mapping_confidence"] = min(r["confidences"]) if r["confidences"] else None
                    entry["dsers_mapped_at"] = today
                    entry.setdefault("remap_history", []).append(
                        {"remapped_at": today, "from_ali_id": old_ali_id, "to_ali_id": r["candidate_ali_id"],
                         "reason": "out_of_stock (Item 42)"}
                    )
                    r["result"] = "REMAPPED"
                    print(f"[stock-remap] {sku}: REMAPPED to {r['candidate_title']!r} (confidences {r['confidences']})")
                    report_lines.append(f"* {sku} -- {entry['ebay_title']} -- REMAPPED to "
                                         f"{r['candidate_title']} (${r['candidate_price']}), confidence {r['confidences']}")
            else:
                print(f"[stock-remap] {sku}: {r['result']} -- {r.get('reason')}")
                report_lines.append(f"* {sku} -- {entry['ebay_title']} -- {r['result']}: {r.get('reason')}")

    if not dry_run:
        _save_json(REMAP_PATH, remap_ledger)
        _save_json(LEDGER_PATH, ledger)

    if not report_lines:
        return
    subject = f"Branch 11 Stock Remap{' (DRY RUN -- nothing applied)' if dry_run else ''} - {today}"
    body = ("Attempted supplier-remap for out-of-stock SKUs flagged by the stock-check email. "
            "Only real, verified (HIGH-verdict, still-profitable, confidence-gated) replacements are "
            "ever applied -- this never touches eBay, only which AliExpress supplier DSers uses.\n\n"
            + "\n".join(report_lines))
    # Email goes out even in --dry-run: this is a real, actionable report either
    # way (a --dry-run cron is running unattended -- stdout alone would be lost),
    # only the actual DSers write and ledger persistence are gated on dry_run.
    send_email(subject, body)
    print(f"[stock-remap] Sent '{subject}' to {REPORT_TO}.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store-id", default=DEFAULT_STORE_ID)
    ap.add_argument("--fee-pct", type=float, default=FEE_PCT_DEFAULT)
    ap.add_argument("--margin-pct", type=float, default=MARGIN_PCT_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="Preview only -- never calls mode='apply', never writes any ledger, never emails.")
    args = ap.parse_args()
    asyncio.run(run(args.store_id, args.fee_pct, args.margin_pct, args.dry_run))


if __name__ == "__main__":
    main()
