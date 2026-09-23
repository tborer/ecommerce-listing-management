#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp",
#     "httpx2",
# ]
# ///
"""Branch 11 step 2 replacement: call the DSers MCP's dsers_find_product for
every discovered eBay item, with NO LLM in the loop.

Why this exists: the original step 2 ran as a `claude -p` call because
dsers_find_product only existed inside an MCP session, and at the time that
meant "inside an LLM session". That's not actually true -- the official
DSers MCP server (https://mcp.dsers.com/dropshipping/mcp) is a standard
OAuth 2.1 + PKCE + Dynamic Client Registration remote MCP server, and the
official `mcp` Python SDK can drive that OAuth flow and call tools directly,
with zero token/judgment involved. See STATUS.md / memory
incident-2026-08-30-branch11-rate-limit for why this swap happened: an
uncapped discovery-stage pool expansion (2026-08-29) turned step 2's
per-item claude -p tool-call volume into a session that burned the entire
5-hour Claude rate-limit window and silently killed the whole daily run
(steps 3-5 never ran, no report, no error email either).

Connection/OAuth plumbing lives in branch11_dsers_mcp_client.py (factored
out 2026-08-30 when branch11_dsers_mapping.py, Step 5's equivalent rewrite,
needed the exact same DSers MCP connection logic) -- see that module's
docstring for the one-time browser-approval setup, shared by every script
using it.

Output format is unchanged from the old claude -p step, so branch11_pipeline.py
--stage finish (step 3) needs no changes: a JSON object keyed by sku
("branch11-" + the eBay item's id), each value
{"ebay_item": <original item>, "dsers_items": <raw dsers_find_product items array, or [] if none/errored>}.

**Early-stop, added 2026-09-01 (PRODUCTION-READINESS.md Item 27).** Original
proposal (2026-08-30) envisioned a single interleaved discover+search+judge
loop -- re-scoped smaller after checking real current volume (25 items on a
representative real day, 2026-09-01) now that the tile cap (Item 39-era) and
the exclusion list (Item 39) already do most of the volume-control work this
item was originally meant to address. Discovery stays a separate, already-
cheap, already-capped full pass, untouched. This script alone now judges
each item inline (reusing `_build_pipeline_result()`/`build_dsers_ali_match()`
from `branch11_pipeline.py` -- the *exact* same functions `--stage finish`'s
Pass 1 already uses, so early-stop's eligibility check can never drift from
what Pass 1 would decide later) and stops calling `dsers_find_product` once
enough HIGH-verdict, profit-passing candidates have been found to fill the
replenishment pool (`auto_list_top_n + replenish_buffer`, default 10 --
Item 36). `--stage finish` needs no changes at all: it just receives a
smaller `dsers_search_<date>.json` on an early-stop day, identical in shape
either way. Disable with `--no-early-stop` if this ever needs bypassing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ecommerce_listing_mgmt.dsers.mcp_client import dsers_session, extract_items, is_error

WRITE_EVERY = 10  # flush partial results to disk this often so a crash mid-run doesn't lose everything


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


async def run(ebay_only_path: Path, out_path: Path, item_limit: int | None, per_item_limit: int, supplier: str,
               sleep_s: float, early_stop: bool, pool_size: int, fee_pct: float, margin_pct: float) -> None:
    from branch11_pipeline import EbayCandidate, _build_pipeline_result, build_dsers_ali_match

    items = json.loads(ebay_only_path.read_text())
    if item_limit is not None:
        items = items[:item_limit]
    total = len(items)
    results: dict[str, dict] = {}
    processed = 0
    matched = 0
    eligible = 0

    async with dsers_session("Branch 11 DSers Search (standalone)") as session:
        for item in items:
            sku = f"branch11-{item['id']}"
            dsers_items: list[dict] = []
            try:
                result = await session.call_tool(
                    "dsers_find_product",
                    {"keyword": item["title"], "limit": per_item_limit, "supplier": supplier},
                )
                if not is_error(result):
                    dsers_items = extract_items(result)
            except Exception as exc:  # noqa: BLE001 -- match old step 2's "treat as zero results, keep going" contract
                print(f"[dsers-search] {sku}: error, treating as zero results ({exc})", file=sys.stderr)

            results[sku] = {"ebay_item": item, "dsers_items": dsers_items}
            processed += 1
            if dsers_items:
                matched += 1

            if early_stop:
                ebay_candidate = EbayCandidate(**item)
                match = build_dsers_ali_match(ebay_candidate.title, ebay_candidate.priceNum, dsers_items, verify=False)
                pr = _build_pipeline_result(ebay_candidate, match, fee_pct, margin_pct)
                if match.verdict == "HIGH" and pr.profit_check and pr.profit_check.get("passes"):
                    eligible += 1

            if processed % WRITE_EVERY == 0 or processed == total:
                _write(out_path, results)
                print(f"[dsers-search] {processed}/{total} processed, {matched} matched so far"
                      + (f", {eligible} eligible for the replenishment pool (target {pool_size})" if early_stop else ""))

            if early_stop and eligible >= pool_size:
                _write(out_path, results)
                print(f"[dsers-search] early-stop: {eligible} eligible candidate(s) found (pool_size={pool_size}) "
                      f"after {processed}/{total} item(s) -- stopping early, skipping the remaining {total - processed}")
                break

            await asyncio.sleep(sleep_s)

    _write(out_path, results)
    print(f"[dsers-search] done: {processed} eBay items processed, {matched} got at least one DSers result")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ebay-only-file", required=True, type=Path, help="Input JSON array from --stage discover")
    ap.add_argument("--out-file", required=True, type=Path, help="Output path (same shape the old claude -p step wrote)")
    ap.add_argument("--limit", type=int, default=None, help="Cap number of eBay items processed (mainly for testing)")
    ap.add_argument("--per-item-limit", type=int, default=10, help="dsers_find_product's own limit= per item")
    ap.add_argument("--supplier", default="aliexpress", help="dsers_find_product's supplier= (default matches the old step 2)")
    ap.add_argument("--sleep", type=float, default=0.4, help="Delay between dsers_find_product calls, seconds")
    ap.add_argument("--no-early-stop", action="store_true", help="Disable early-stop, process every discovered item regardless of eligible count (old behavior)")
    ap.add_argument("--early-stop-pool-size", type=int, default=None,
                     help="Stop once this many HIGH-verdict, profit-passing candidates are found (default: auto_list_top_n + replenish_buffer from branch11_pipeline.py, currently 10)")
    ap.add_argument("--fee-pct", type=float, default=0.17, help="Must match branch11_pipeline.py's --fee-pct for early-stop's inline judging to agree with Pass 1")
    ap.add_argument("--margin-pct", type=float, default=0.16, help="Must match branch11_pipeline.py's --margin-pct for early-stop's inline judging to agree with Pass 1")
    args = ap.parse_args()

    pool_size = args.early_stop_pool_size
    if pool_size is None:
        from branch11_pipeline import AUTO_LIST_REPLENISH_BUFFER_DEFAULT, AUTO_LIST_TOP_N_DEFAULT
        pool_size = AUTO_LIST_TOP_N_DEFAULT + AUTO_LIST_REPLENISH_BUFFER_DEFAULT

    asyncio.run(run(args.ebay_only_file, args.out_file, args.limit, args.per_item_limit, args.supplier, args.sleep,
                     not args.no_early_stop, pool_size, args.fee_pct, args.margin_pct))


if __name__ == "__main__":
    main()
