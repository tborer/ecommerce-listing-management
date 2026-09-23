#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp",
#     "httpx2",
# ]
# ///
"""Branch 11 step 5 replacement: complete DSers supplier-mapping for any
auto-listed SKU Travis has since manually imported into DSers, with NO LLM
in the loop.

Why this exists: the original step 5 ran as a `claude -p` call under the
assumption that its confidence-gated apply/skip decision needed real
judgment. On inspection (2026-08-30) it's the same class of fixed,
deterministic branching as step 2 turned out to be -- a missing-field-as-
false check, a hardcoded 70-confidence threshold, and error-record-don't-
retry -- so it was rewritten the same way: a standalone script calling the
DSers MCP's dsers_my_products/dsers_sku_remap tools directly via OAuth
2.1+PKCE, no LLM session involved. Connection/OAuth plumbing (including the
cached token file, already approved once for branch11_dsers_search.py) lives
in branch11_dsers_mcp_client.py -- see that module's docstring for the
one-time setup if the token cache is ever missing.

What this replaces, exactly (branch11_daily_run.sh's old Step 5 prompt):
for every ledger entry (branch11_auto_listed.json) where dsers_mapped is
false or missing, AND woo_product_id is present and not null, AND ali_url
is not null:
  1. dsers_my_products(store_id, keyword=<ebay_title>) -- has Travis
     manually imported this product into DSers yet? (there's no API for
     that import click itself, still a human touch -- see the docstring
     note below.) total==0 is expected/normal, not an error: skip
     silently. **Fixed 2026-08-31 (PRODUCTION-READINESS.md Item 37):**
     originally searched by our internal SKU (e.g. "branch11-123..."),
     but DSers's keyword search matches on product *title*, not our SKU
     -- confirmed live that searching by SKU always returns zero results
     even for products that are genuinely present, making this step
     silently useless since it first shipped 2026-08-30. Now searches by
     `ebay_title` (present on every ledger entry) instead, which is what
     WooCommerce mirrors as the DSers product's title. Since keyword
     search can return partial/substring matches, also requires an exact
     case-insensitive title match among the results before treating
     anything as found -- zero or multiple exact matches is recorded as
     `dsers_mapping_error` (ambiguous), never guessed via `results[0]`.
     Validated live 2026-08-31 against one real item (branch11-388183263465,
     "Wallet Tracker...") before being folded into this script: found it,
     previewed at confidence 75, applied, confirmed via a follow-up
     dsers_my_products call that `supplier` flipped to `aliexpress` with
     the correct cost/URL.
  2. If found (dsers_product_id), dsers_sku_remap(..., mode='preview')
     against this entry's ali_url.
  3. If every diff's confidence >= CONFIDENCE_THRESHOLD: re-call with
     mode='apply'. On success, set dsers_mapped=true + record the id/
     confidence/date.
  4. If any diff is below threshold: leave dsers_mapped false, record
     dsers_mapping_low_confidence for Travis's manual review instead.
  5. On any dsers_sku_remap error: leave dsers_mapped false, record
     dsers_mapping_error, do not retry.
Writes the ledger back preserving every other field/entry untouched.

The one manual step this can never remove: a fresh WooCommerce product does
not auto-sync into DSers (confirmed live 2026-08-29, no API/MCP equivalent
found) -- Travis's periodic "Import Products from WooCommerce" dashboard
click is what makes dsers_my_products ever find a given product at all.
Until that click has happened for a SKU, this script correctly finds
total==0 and skips it -- that's expected, not a failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from ecommerce_listing_mgmt.dsers.mcp_client import dsers_session, extract_items, extract_json, is_error

DEFAULT_STORE_ID = "2093790408921645056"  # the DSers-bridge WooCommerce store, per branch11-listing-rules.md
CONFIDENCE_THRESHOLD = 70


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _eligible(entry: dict) -> bool:
    return (
        not entry.get("dsers_mapped", False)
        and entry.get("woo_product_id") is not None
        and entry.get("ali_url") is not None
        and entry.get("ebay_title") is not None
    )


async def run(ledger_path: Path, store_id: str, confidence_threshold: int, dry_run: bool) -> None:
    ledger: dict[str, dict] = json.loads(ledger_path.read_text())
    eligible_skus = [sku for sku, entry in ledger.items() if _eligible(entry)]

    print(f"[dsers-mapping] {len(eligible_skus)}/{len(ledger)} ledger entries eligible "
          f"(dsers_mapped false/missing, woo_product_id set, ali_url set).")

    newly_mapped = pending_import = low_confidence = errored = 0

    async with dsers_session("Branch 11 DSers Mapping (standalone)") as session:
        for sku in eligible_skus:
            entry = ledger[sku]
            title = entry["ebay_title"]
            try:
                result = await session.call_tool("dsers_my_products", {"store_id": store_id, "keyword": title})
            except Exception as exc:  # noqa: BLE001 -- record and move on, per the old step's "do not retry" contract
                entry["dsers_mapping_error"] = f"dsers_my_products error: {exc}"
                errored += 1
                print(f"[dsers-mapping] {sku}: dsers_my_products errored -- {exc}")
                continue

            if is_error(result):
                entry["dsers_mapping_error"] = f"dsers_my_products returned an error result: {result}"
                errored += 1
                print(f"[dsers-mapping] {sku}: dsers_my_products returned an error")
                continue

            products = extract_items(result)
            if not products:
                pending_import += 1
                print(f"[dsers-mapping] {sku}: not found in DSers yet (pending Travis's WooCommerce-import click) -- skipping")
                continue

            # Keyword search can return partial/substring matches (confirmed live,
            # e.g. "Hamster Cage" matched on a substring) -- require an exact
            # case-insensitive title match before treating anything as found,
            # never blindly take products[0].
            exact = [p for p in products if p.get("title", "").strip().lower() == title.strip().lower()]
            if len(exact) != 1:
                entry["dsers_mapping_error"] = (
                    f"keyword search for title {title!r} returned {len(products)} result(s) but "
                    f"{len(exact)} exact title match(es) -- ambiguous, needs manual review"
                )
                errored += 1
                print(f"[dsers-mapping] {sku}: {len(products)} result(s), {len(exact)} exact match(es) -- ambiguous, recording error")
                continue

            dsers_product_id = exact[0].get("dsers_product_id")
            if not dsers_product_id:
                entry["dsers_mapping_error"] = f"dsers_my_products found a match but no dsers_product_id field: {exact[0]}"
                errored += 1
                print(f"[dsers-mapping] {sku}: matched but no dsers_product_id -- recording error")
                continue

            try:
                preview = await session.call_tool(
                    "dsers_sku_remap",
                    {"dsers_product_id": str(dsers_product_id), "store_id": store_id,
                     "new_supplier_url": entry["ali_url"], "mode": "preview"},
                )
                preview_body = extract_json(preview) or {}
            except Exception as exc:  # noqa: BLE001
                entry["dsers_mapping_error"] = f"dsers_sku_remap preview error: {exc}"
                errored += 1
                print(f"[dsers-mapping] {sku}: dsers_sku_remap preview errored -- {exc}")
                continue

            diffs = preview_body.get("diffs") or []
            confidences = [d.get("confidence", 0) for d in diffs]
            if not diffs or any(c < confidence_threshold for c in confidences):
                entry["dsers_mapping_low_confidence"] = {
                    "confidences": confidences,
                    "note": f"needs Travis's manual review in the DSers dashboard (threshold {confidence_threshold})",
                }
                low_confidence += 1
                print(f"[dsers-mapping] {sku}: confidence(s) {confidences} below threshold {confidence_threshold} -- not applying")
                continue

            if dry_run:
                print(f"[dsers-mapping] {sku}: would apply (confidence(s) {confidences}) -- --dry-run, not writing")
                continue

            try:
                apply_result = await session.call_tool(
                    "dsers_sku_remap",
                    {"dsers_product_id": str(dsers_product_id), "store_id": store_id,
                     "new_supplier_url": entry["ali_url"], "mode": "apply"},
                )
                apply_body = extract_json(apply_result) or {}
            except Exception as exc:  # noqa: BLE001
                entry["dsers_mapping_error"] = f"dsers_sku_remap apply error: {exc}"
                errored += 1
                print(f"[dsers-mapping] {sku}: dsers_sku_remap apply errored -- {exc}")
                continue

            if is_error(apply_result):
                entry["dsers_mapping_error"] = f"dsers_sku_remap apply returned an error result: {apply_body}"
                errored += 1
                print(f"[dsers-mapping] {sku}: dsers_sku_remap apply returned an error")
                continue

            entry["dsers_mapped"] = True
            entry["dsers_product_id"] = dsers_product_id
            entry["dsers_mapping_confidence"] = min(confidences) if confidences else None
            entry["dsers_mapped_at"] = date.today().isoformat()
            entry.pop("dsers_mapping_low_confidence", None)
            entry.pop("dsers_mapping_error", None)
            newly_mapped += 1
            print(f"[dsers-mapping] {sku}: mapped (dsers_product_id={dsers_product_id}, confidence {confidences})")

    if not dry_run:
        _write(ledger_path, ledger)

    print(f"[dsers-mapping] done: {newly_mapped} newly mapped, {pending_import} still pending WooCommerce import, "
          f"{low_confidence} low-confidence (manual review needed), {errored} errored.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger-file", type=Path,
                     default=Path(__file__).resolve().parent / "branch11_auto_listed.json",
                     help="Path to branch11_auto_listed.json")
    ap.add_argument("--store-id", default=DEFAULT_STORE_ID, help="DSers store_id for the WooCommerce bridge store")
    ap.add_argument("--confidence-threshold", type=int, default=CONFIDENCE_THRESHOLD,
                     help="Minimum per-diff confidence to auto-apply a mapping (default 70)")
    ap.add_argument("--dry-run", action="store_true", help="Preview only -- never calls mode='apply', never writes the ledger")
    args = ap.parse_args()

    asyncio.run(run(args.ledger_file, args.store_id, args.confidence_threshold, args.dry_run))


if __name__ == "__main__":
    main()
