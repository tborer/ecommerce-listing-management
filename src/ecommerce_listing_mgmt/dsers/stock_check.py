#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp",
#     "httpx2",
# ]
# ///
"""Branch 11 Item 42, step 1: DSers supplier out-of-stock detection, no LLM
in the loop.

Why this exists: if a DSers-mapped item's AliExpress supplier goes out of
stock and nothing changes, the eBay listing stays live and sellable -- a
customer could order something Branch 11 can no longer source, risking a
failed/refunded order, negative feedback, and account-health exposure. This
script only detects and reports; it never remaps or touches a live eBay
listing (see Hard Constraints in PRODUCTION-READINESS.md).

**Detection mechanism, confirmed live 2026-09-01**: `dsers_my_products`
takes a `supplier_status` filter (multi-select: `out_of_stock` = supplier
SKU has 0 inventory, `product_out_of_stock` = supplier marked the whole
product unavailable, `not_found` = the supplier listing is gone entirely --
`sku_changed`/`cost_changed` deliberately excluded here, those are Item 22's
territory (price/cost drift), not a fulfillment-blocking signal). This is a
genuine server-side filter, not a guess: verified live against the real
DSers-bridge store and it correctly surfaced one real, currently-affected
live listing (`branch11-395766041202`, "Makita XRJ01Z-R 18V LXT Compact
Recipro Saw", flagged `product_out_of_stock`) on the very first real query.
Note the per-item response body has no visible stock field of its own (its
top-level `status` is `"OnSelling"` regardless) -- the filter is the only
way to see this, so this script always queries with the filter rather than
inspecting individual product records.

One call covers every product in our DSers-bridge store (the filter is
store_id-scoped, and that store only ever contains Branch 11's own mirrored
products) -- cheap, no need to loop per-SKU like `branch11_price_check.py`/
`branch11_dsers_mapping.py` do for their own (different, keyword-search-only)
lookups.

**Deliberately NOT built here** (per PRODUCTION-READINESS.md Item 42's
scope): searching for a replacement supplier (`dsers_find_product` +
`dsers_sku_remap`) and removing/flagging the eBay listing if none is found.
Both are separate, larger pieces of work -- remap because it needs the same
same-item verification discipline Items 19/37 already established (don't
trust the first search result as a genuine match), and listing removal
because it's explicitly Hard-Constraint-adjacent (autonomously ending a live
listing is not the narrow existing auto-list carve-out) and still
`NEEDS DECISION` in the doc. This script's only job is to make the problem
visible reliably, the same incremental-build pattern Item 30 already used.

Ledger (`branch11_stock_check.json`) tracks `first_detected_at` per SKU so a
still-broken item doesn't get treated as brand-new every day, and
`resolved_at` once a previously-flagged SKU stops appearing in the filtered
results (e.g. Travis manually remapped it, or the supplier restocked).

Usage:
    uv run branch11_stock_check.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from ecommerce_listing_mgmt.dsers.mcp_client import dsers_session, extract_items, is_error
from ecommerce_listing_mgmt.reporting.email_report import send_email

DEFAULT_STORE_ID = "2093790408921645056"  # the DSers-bridge WooCommerce store, per branch11-listing-rules.md
OUT_DIR = Path(__file__).parent
LEDGER_PATH = OUT_DIR / "branch11_auto_listed.json"
STOCK_CHECK_PATH = OUT_DIR / "branch11_stock_check.json"
REPORT_TO = "tray14@hotmail.com"

# Fulfillment-blocking statuses only -- cost_changed/sku_changed are Item 22's
# territory (price drift), not "we can no longer source this at all."
BLOCKING_STATUSES = ["out_of_stock", "product_out_of_stock", "not_found"]


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


async def fetch_blocked_dsers_ids(session, store_id: str) -> set[str]:
    """Walks every page of dsers_my_products filtered to the fulfillment-
    blocking supplier_status values, returns the set of dsers_product_id
    values affected. Paginates via `next_cursor` since a real day could have
    more than one page's worth (page_size capped at 100 by the tool)."""
    blocked: set[str] = set()
    cursor = None
    while True:
        args = {"store_id": store_id, "supplier_status": BLOCKING_STATUSES, "page_size": 100}
        if cursor:
            args["cursor"] = cursor
        result = await session.call_tool("dsers_my_products", args)
        if is_error(result):
            raise RuntimeError(f"dsers_my_products (supplier_status filter) returned an error: {result}")
        items = extract_items(result)
        for it in items:
            pid = it.get("dsers_product_id")
            if pid:
                blocked.add(str(pid))
        structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None) or {}
        cursor = structured.get("next_cursor") if isinstance(structured, dict) else None
        if not cursor or not items:
            break
    return blocked


def build_report(newly_flagged: list[dict], still_flagged: list[dict], resolved: list[dict]) -> tuple[str, str] | None:
    if not newly_flagged and not still_flagged and not resolved:
        return None
    today = date.today().isoformat()
    lines = [
        f"DSers supplier stock check, {today}. {len(newly_flagged)} newly flagged, "
        f"{len(still_flagged)} still unresolved from a prior check, {len(resolved)} resolved since last check.",
        "",
        "Nothing was changed automatically -- this pipeline never auto-remaps a supplier or "
        "removes/edits a live eBay listing without your say-so. Review each item below and "
        "either remap it to a new supplier in DSers yourself, or end the eBay listing if no "
        "replacement is available.",
        "",
    ]

    def _fmt(entry: dict) -> list[str]:
        out = [f"* {entry['sku']} -- {entry['ebay_title']}"]
        if entry.get("listing_id"):
            out.append(f"  Live listing: https://www.ebay.com/itm/{entry['listing_id']}")
        out.append(f"  eBay price: ${entry.get('ebay_price', '?')}  |  DSers supplier status: {entry['dsers_statuses']}")
        out.append(f"  First detected: {entry['first_detected_at']}")
        return out

    if newly_flagged:
        lines.append(f"=== NEWLY FLAGGED ({len(newly_flagged)}) ===")
        for e in newly_flagged:
            lines.extend(_fmt(e))
            lines.append("")
    if still_flagged:
        lines.append(f"=== STILL UNRESOLVED ({len(still_flagged)}) ===")
        for e in still_flagged:
            lines.extend(_fmt(e))
            lines.append("")
    if resolved:
        lines.append(f"=== RESOLVED SINCE LAST CHECK ({len(resolved)}) ===")
        for sku in resolved:
            lines.append(f"* {sku}")
        lines.append("")

    subject = f"Branch 11 Stock Check - {len(newly_flagged) + len(still_flagged)} item(s) need attention - {today}"
    return subject, "\n".join(lines)


async def run(store_id: str, dry_run: bool) -> None:
    ledger = _load_json(LEDGER_PATH)
    stock_check = _load_json(STOCK_CHECK_PATH)
    mapped = {sku: e for sku, e in ledger.items() if e.get("dsers_mapped") and e.get("dsers_product_id")}
    print(f"[stock-check] {len(mapped)}/{len(ledger)} ledger entries are dsers_mapped with a dsers_product_id.")

    async with dsers_session("Branch 11 Stock Check (standalone)") as session:
        blocked_ids = await fetch_blocked_dsers_ids(session, store_id)
    print(f"[stock-check] {len(blocked_ids)} product(s) in the DSers bridge store currently flagged {BLOCKING_STATUSES}.")

    today = date.today().isoformat()
    newly_flagged, still_flagged = [], []
    now_flagged_skus = set()

    for sku, entry in mapped.items():
        if str(entry["dsers_product_id"]) not in blocked_ids:
            continue
        now_flagged_skus.add(sku)
        prior = stock_check.get(sku)
        # A prior entry that was previously marked resolved counts as a fresh
        # occurrence (e.g. it broke, got fixed, then broke again) rather than
        # "still unresolved" -- re-flag it as new, not a continuation.
        is_new = prior is None or prior.get("resolved_at") is not None
        first_detected_at = today if is_new else prior["first_detected_at"]
        record = {
            "sku": sku, "ebay_title": entry["ebay_title"], "listing_id": entry.get("listing_id"),
            "ebay_price": entry.get("ebay_price"), "dsers_statuses": BLOCKING_STATUSES,
            "first_detected_at": first_detected_at,
        }
        stock_check[sku] = {"first_detected_at": first_detected_at, "last_checked_at": today, "resolved_at": None}
        (newly_flagged if is_new else still_flagged).append(record)

    resolved = []
    for sku, prior in stock_check.items():
        if sku not in now_flagged_skus and prior.get("resolved_at") is None:
            prior["resolved_at"] = today
            resolved.append(sku)

    if not dry_run:
        _save_json(STOCK_CHECK_PATH, stock_check)

    report = build_report(newly_flagged, still_flagged, resolved)
    if report is None:
        print("[stock-check] nothing to report -- no flagged/resolved items.")
        return
    subject, body = report
    if dry_run:
        print(f"\nSubject: {subject}\n")
        print(body)
        return
    send_email(subject, body)
    print(f"[stock-check] Sent '{subject}' to {REPORT_TO}.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store-id", default=DEFAULT_STORE_ID)
    ap.add_argument("--dry-run", action="store_true", help="Print the email instead of sending it, and don't write the ledger.")
    args = ap.parse_args()
    asyncio.run(run(args.store_id, args.dry_run))


if __name__ == "__main__":
    main()
