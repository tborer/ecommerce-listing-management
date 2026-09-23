#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp",
#     "httpx2",
#     "pytesseract",
#     "Pillow",
# ]
# ///
"""Branch 11 Items 28/31: small-batch daily enrichment, no LLM in the loop.

For a handful of already-listed items per day (never reprocessing the same
one twice), pulls DSers's own `image_urls` + variant `options` for the real
sourced product (via `dsers_product_import` -- no separate
`dsers_product_preview` call needed, confirmed live 2026-08-31 that import's
own response already carries both), then extracts any text embedded in
those images via local Tesseract OCR (`pytesseract`, English + Simplified
Chinese -- AliExpress description images commonly mix both).

**Why DSers's images, not AliExpress's own product page**: the page's real
"extended description" content lives in an iframe
(`iframe.extend--iframe--to7hHIV`) that consistently returns empty under
browser automation (confirmed live 2026-08-30, root cause never found) --
DSers's `image_urls` sidesteps that unsolved problem entirely, and needs no
LLM since `branch11_dsers_mcp_client.py` (built 2026-08-30) already gives
standalone DSers MCP access.

**Never touches the live eBay (or WooCommerce) listing.** Hard Constraints
forbid editing/pricing a real eBay listing "after the fact" autonomously --
this only writes to `branch11_enrichment.json`, for Travis's own reference
or future use (e.g. once Item 25 phase 3's real Variations API support
exists and needs the captured `options` data). Applying enriched text back
to a live listing is a separate, explicit, not-yet-approved step.

**Cost/rate-limit shape, same lesson as the rest of this pipeline**
(see memory incident-2026-08-30-branch11-rate-limit): a small, fixed,
non-LLM daily batch (default 4 items), its own dedicated ~12 PM cron
job -- deliberately NOT run at discovery volume (hundreds/day) or folded
into the 30-min production-readiness backlog cron.

**DSers import-list drafts are deleted after extraction** (`dsers_product_delete`,
Travis's 2026-08-31 decision) -- the draft was only ever a means to get
`image_urls`/`options`, not something Travis needs sitting in his DSers
dashboard. Best-effort: the real enrichment data is already persisted
before cleanup runs, so a delete failure is logged on the ledger entry
(`cleanup_status`) rather than treated as that item's failure.

Usage:
    uv run branch11_enrichment.py [--batch-size 4] [--ledger-file branch11_auto_listed.json]
        [--enrichment-file branch11_enrichment.json]
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from datetime import date
from pathlib import Path

from ecommerce_listing_mgmt.dsers.mcp_client import dsers_session, extract_json, is_error

OUT_DIR = Path(__file__).parent
DEFAULT_LEDGER_PATH = OUT_DIR / "branch11_auto_listed.json"
DEFAULT_ENRICHMENT_PATH = OUT_DIR / "branch11_enrichment.json"
DEFAULT_BATCH_SIZE = 4
MAX_IMAGES_PER_ITEM = 8  # bounds per-item cost/time regardless of how many real images exist
MIN_OCR_TEXT_LEN = 8  # filters out single-character/logo-artifact OCR noise (e.g. "MG")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


def _select_batch(ledger: dict, enrichment: dict, batch_size: int) -> list[str]:
    """Oldest-listed-first, so the full history gets walked incrementally
    over many days rather than always re-checking the newest items. Skips
    anything already enriched or missing an AliExpress source URL (older
    ledger entries predating the 2026-08-28 ali_url fix, or anything listed
    outside the auto-listing path)."""
    eligible = [
        sku for sku, entry in ledger.items()
        if sku not in enrichment and entry.get("ali_url")
    ]
    eligible.sort(key=lambda sku: ledger[sku].get("listed_at") or "")
    return eligible[:batch_size]


async def _ocr_images(client, image_urls: list[str]) -> str:
    """Downloads up to MAX_IMAGES_PER_ITEM images and OCRs each with local
    Tesseract (English + Simplified Chinese). Returns the non-empty,
    non-noise text blocks joined together -- never raises on a single
    image's failure (a broken/unreachable image URL shouldn't lose the
    other images' real text)."""
    import pytesseract
    from PIL import Image

    blocks: list[str] = []
    for url in image_urls[:MAX_IMAGES_PER_ITEM]:
        try:
            resp = await client.get(url, timeout=20.0)
            img = Image.open(io.BytesIO(resp.content))
            text = pytesseract.image_to_string(img, lang="eng+chi_sim").strip()
            if len(text) >= MIN_OCR_TEXT_LEN:
                blocks.append(text)
        except Exception as e:  # noqa: BLE001 -- one bad image must not lose the rest
            print(f"[enrichment]   image OCR failed ({url[-40:]}): {e}", file=sys.stderr)
    return "\n---\n".join(blocks)


async def run(ledger_path: Path, enrichment_path: Path, batch_size: int) -> None:
    ledger = _load_json(ledger_path)
    enrichment = _load_json(enrichment_path)
    batch = _select_batch(ledger, enrichment, batch_size)
    print(f"[enrichment] {len(batch)}/{batch_size} slot(s) filled this run "
          f"({len(ledger) - len(enrichment)} total eligible remaining after this run's picks).")

    if not batch:
        return

    import httpx2

    async with dsers_session("Branch 11 Enrichment (standalone)") as session:
        async with httpx2.AsyncClient() as http_client:
            for sku in batch:
                entry = ledger[sku]
                print(f"[enrichment] {sku} ({entry.get('ebay_title', '')[:50]})")
                try:
                    result = await session.call_tool("dsers_product_import", {"source_url": entry["ali_url"]})
                except Exception as e:  # noqa: BLE001
                    enrichment[sku] = {"status": "IMPORT_ERROR", "reason": str(e), "checked_at": date.today().isoformat()}
                    print(f"[enrichment]   import error: {e}")
                    continue

                if is_error(result):
                    enrichment[sku] = {"status": "IMPORT_ERROR", "reason": str(result), "checked_at": date.today().isoformat()}
                    print("[enrichment]   import returned an error")
                    continue

                body = extract_json(result) or {}
                image_urls = body.get("image_urls") or []
                options = body.get("options") or []

                ocr_text = await _ocr_images(http_client, image_urls) if image_urls else ""
                import_item_id = body.get("import_item_id")

                enrichment[sku] = {
                    "status": "DONE",
                    "enriched_at": date.today().isoformat(),
                    "dsers_import_item_id": import_item_id,
                    "image_count": len(image_urls),
                    "images_ocred": min(len(image_urls), MAX_IMAGES_PER_ITEM),
                    "ocr_text": ocr_text,
                    # Raw variant/option data, captured for future use (Item 25
                    # phase 3's real Variations API support) -- not acted on here.
                    "variant_options": options,
                }
                _save_json(enrichment_path, enrichment)  # persist immediately, matches this codebase's own pattern -- a mid-run crash must not lose an already-completed item's real work
                print(f"[enrichment]   {len(image_urls)} image(s), {len(ocr_text)} OCR char(s) captured, "
                      f"{len(options)} variant option group(s)")

                # Added 2026-08-31 (Travis's decision): clean up the DSers
                # import-list draft now that its data is safely captured --
                # the draft was only ever a means to get image_urls/options,
                # not something Travis needs sitting in his DSers dashboard.
                # Best-effort: the real enrichment data above is already
                # persisted by this point, so a delete failure here is
                # logged, not treated as this item's failure.
                if import_item_id:
                    try:
                        del_result = await session.call_tool(
                            "dsers_product_delete", {"import_item_id": import_item_id, "confirm": True})
                        if is_error(del_result):
                            enrichment[sku]["cleanup_status"] = f"DELETE_ERROR: {del_result}"
                            print(f"[enrichment]   cleanup FAILED (import draft left in DSers): {del_result}")
                        else:
                            enrichment[sku]["cleanup_status"] = "DELETED"
                    except Exception as e:  # noqa: BLE001
                        enrichment[sku]["cleanup_status"] = f"DELETE_ERROR: {e}"
                        print(f"[enrichment]   cleanup FAILED (import draft left in DSers): {e}")
                    _save_json(enrichment_path, enrichment)

    print(f"[enrichment] done: {len(batch)} item(s) processed this run.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger-file", type=Path, default=DEFAULT_LEDGER_PATH)
    ap.add_argument("--enrichment-file", type=Path, default=DEFAULT_ENRICHMENT_PATH)
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = ap.parse_args()
    asyncio.run(run(args.ledger_file, args.enrichment_file, args.batch_size))


if __name__ == "__main__":
    main()
