#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp",
#     "httpx2",
# ]
# ///
"""Item 25 Phase 3: checks whether a candidate that's otherwise blocked by
`needs_unresolvable_variation` has a genuine, fully-resolvable multi-variant
fix available via DSers's real per-variant `options` data.

Standalone module (owns its own asyncio bridge via `check_phase3_opportunity()`,
a plain sync function) so `branch11_pipeline.py`'s `--stage finish` --
deliberately kept synchronous/no-LLM, matching `branch11_daily_run.sh`'s own
"why separate steps, not one script" reasoning -- doesn't need to become
async itself just for this rare (a handful of real blocked candidates/day
at most) check. Only ever called for a candidate that's ALREADY about to be
skipped for `needs_unresolvable_variation`, so the extra DSers API call this
makes is cheap in aggregate, matching this project's frugal DSers-call-volume
design elsewhere.

Real finding this exists to act on (PRODUCTION-READINESS.md Item 25, real
data from `branch11_enrichment.json`): DSers's "options" field labels are
NOT reliably genuine values for the aspect they're labeled under -- a real
"Color" option group's values have included battery-pack configurations
like "20V 2PCS 8Ah-Charger", not colors at all. `resolve_variant_values()`
(`branch11_ebay_listing.py`) is the real evidence-based safeguard against
that; this module is just the DSers-data-fetching + full-candidate-
resolution wiring around it -- never lists anything itself, only reports
whether a genuine, fully-resolved opportunity exists."""
from __future__ import annotations

import argparse
import asyncio
import json

from ecommerce_listing_mgmt.dsers.mcp_client import dsers_session, extract_json, is_error
from ecommerce_listing_mgmt.ebay.listing import get_required_aspects, resolve_variant_values

# The only two aspects real data (Item 25's Phase 3 research) confirmed
# DSers's options data can genuinely back -- Type and dimension blocks are
# NOT included here on purpose: Type has no real multi-value option data
# behind it at all (confirmed live, 0/8 real candidates), and dimensions are
# a real physical measurement (Item 26's territory), not a variant choice.
PHASE3_ASPECTS = {"Color", "Number of Shelves"}


async def _fetch_dsers_options(ali_url: str) -> list[dict]:
    async with dsers_session("Branch 11 Variation Check (standalone)") as session:
        result = await session.call_tool("dsers_product_import", {"source_url": ali_url})
        if is_error(result):
            return []
        body = extract_json(result) or {}
        options = body.get("options") or []
        import_item_id = body.get("import_item_id")
        if import_item_id:
            # Best-effort cleanup, same pattern branch11_enrichment.py uses --
            # the real options data above is already captured by this point,
            # so a delete failure here doesn't affect this check's result.
            try:
                await session.call_tool("dsers_product_delete", {"import_item_id": import_item_id, "confirm": True})
            except Exception:  # noqa: BLE001
                pass
        return options


def check_phase3_opportunity(env: str, category_id: str, ali_url: str,
                              unresolvable_variation_aspects: list[str],
                              credential_mode: str = "bitwarden") -> dict | None:
    """Returns `{"aspect": name, "mapping": {dsers_value: ebay_value}}` only
    if EVERY aspect in `unresolvable_variation_aspects` can be fully
    resolved this way -- a partial fix (some aspects still unresolvable)
    doesn't unblock the listing at all, so isn't reported as an opportunity.
    Returns None (not a Phase 3 case) without calling DSers at all if any
    non-Phase-3 aspect (Type, a dimension, anything else) is also
    unresolvable, or if more than one Phase-3-eligible aspect is
    unresolvable at once (this build is single-pivot-aspect only -- see
    `create_inventory_item_group()`'s own comment -- so a genuine 2-
    dimensional variation matrix correctly stays unresolved rather than
    guessed at)."""
    if not unresolvable_variation_aspects or not set(unresolvable_variation_aspects) <= PHASE3_ASPECTS:
        return None
    if len(unresolvable_variation_aspects) != 1:
        return None
    aspect_name = unresolvable_variation_aspects[0]

    required = get_required_aspects(env, category_id, credential_mode=credential_mode)
    aspect = next((a for a in required if a["name"] == aspect_name), None)
    if not aspect:
        return None

    options = asyncio.run(_fetch_dsers_options(ali_url))
    option_group = next((o for o in options if o.get("name") == aspect_name), None)
    if not option_group or not option_group.get("values"):
        return None

    mapping = resolve_variant_values(aspect["values"], option_group["values"])
    if not mapping:
        return None
    return {"aspect": aspect_name, "mapping": mapping}


def main() -> None:
    """CLI entrypoint, invoked as a subprocess from `branch11_pipeline.py`
    (plain `python3`, no `mcp`/`httpx2` in that environment) via `uv run` --
    same bridge pattern already used for `branch11_dsers_search.py`. Prints
    one JSON line to stdout: `{"opportunity": null}` or
    `{"opportunity": {"aspect": ..., "mapping": {...}}}`. Never raises on a
    DSers-side error -- an unreachable/errored check is indistinguishable
    from "no opportunity" to the caller, which is the correct fail-closed
    behavior here (never guess an opportunity exists)."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", required=True)
    ap.add_argument("--category-id", required=True)
    ap.add_argument("--ali-url", required=True)
    ap.add_argument("--unresolvable-aspect", action="append", default=[],
                     help="Repeatable -- one per unresolvable variation aspect for this candidate.")
    ap.add_argument("--credential-mode", default="bitwarden")
    args = ap.parse_args()

    try:
        opportunity = check_phase3_opportunity(
            args.env, args.category_id, args.ali_url, args.unresolvable_aspect,
            credential_mode=args.credential_mode,
        )
    except Exception as exc:  # noqa: BLE001 -- fail-closed: any error means "no opportunity found," never guessed
        print(json.dumps({"opportunity": None, "error": str(exc)}))
        return
    print(json.dumps({"opportunity": opportunity}))


if __name__ == "__main__":
    main()
