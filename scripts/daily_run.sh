#!/bin/bash
# Branch 11 daily automation, five steps: (1) bare-Python eBay Deals
# discovery only (2026-08-29 DSers-search swap for Steps 2-4 -- see
# branch11-listing-rules.md's AliExpress-search section and STATUS.md same
# date for the full validation/reasoning), (2) branch11_dsers_search.py --
# a standalone Python MCP client (2026-08-30, see memory
# incident-2026-08-30-branch11-rate-limit) that searches the DSers MCP's
# AliExpress pool for each discovered eBay item with NO LLM involved --
# this used to be a `claude -p` call (on the mistaken assumption that
# dsers_find_product could only be called from inside an LLM session), but
# an uncapped discovery-pool expansion turned that into 339 items in one
# claude -p session, which burned the whole 5-hour Claude rate-limit window
# and silently killed steps 3-5 (no candidates, no report email, not even
# an error email, since sending one needed a claude -p call too). The
# standalone script talks OAuth 2.1+PKCE directly to
# https://mcp.dsers.com/dropshipping/mcp -- zero token cost, no rate-limit
# exposure regardless of how large discovery gets. (3) a second bare-Python
# pass that judges those DSers results, verifies the winning match's
# description via one browser visit, profit-calcs, auto-lists the top N
# (also mirroring each into the DSers-bridge WooCommerce store), (4)
# branch11_email_report.py -- a standalone script (2026-08-30) that composes
# and sends the report email via plain SMTP (a Gmail App Password), (5)
# branch11_dsers_mapping.py (2026-08-30, same rewrite) -- shares
# branch11_dsers_search.py's DSers OAuth session (factored out into
# branch11_dsers_mcp_client.py) to call dsers_my_products/dsers_sku_remap
# directly for the DSers supplier-mapping automation, completing the mapping
# for any mirrored SKU Travis has since manually imported into DSers -- see
# that step's own comment below for why the import click itself can't be
# automated too.
#
# 2026-08-30: steps 4 and 5 were originally left on `claude -p`, on the
# assumption that email composition and confidence-gated apply/skip
# decisions needed real judgment. On closer inspection both are fixed,
# deterministic branching over fields the pipeline already computes (no
# natural-language reasoning actually happens) -- same class of fix as
# step 2's rewrite, so both were converted the same way. **As of this run,
# none of the five daily steps require an LLM at all** -- zero ongoing
# token cost, zero rate-limit exposure for the whole daily pipeline. Step
# 5's dsers_sku_remap apply/low-confidence branches are ported faithfully
# from the old prompt's exact logic but not yet exercised live end-to-end
# (nothing has hit DSers's "found, ready to remap" case yet as of this
# rewrite) -- worth watching the first real run that does.
#
# Deliberately separate steps, not one script/prompt -- see
# feedback_notification_architecture memory / the node4/desktop
# troubleshooting cron loop for why: bundling everything into one task has
# failed before, and keeping each step's failure mode isolated (one step's
# crash shouldn't silently skip the ones after it, per the 2026-08-30
# rate-limit incident) is more valuable now than when some steps were LLM
# calls anyway.
#
# The old single-pass `python3 branch11_pipeline.py` (no --stage flag,
# browser-based AliExpress search+matching) still exists unchanged and can
# be run directly as a fallback if this staged flow needs reverting --
# nothing about it was removed, see `main()`'s `--stage full` (the default).
#
# Credential access (2026-08-27, Travis's decision): auto-listing uses a
# dedicated local credential file (branch11_unattended_creds.json), not
# Bitwarden/BW_SESSION -- see run_auto_listing()'s docstring in
# branch11_pipeline.py and branch11-listing-rules.md's "Autonomous production
# publishing" section for the full reasoning and what it's scoped to.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/openclaw-troubleshooting"

TODAY=$(date +%F)
EBAY_ONLY_PATH="$HOME/openclaw-troubleshooting/branch11_ebay_only_${TODAY}.json"
DSERS_SEARCH_PATH="$HOME/openclaw-troubleshooting/branch11_dsers_search_${TODAY}.json"

# Daily good-item target (added 2026-09-02): the single place to change
# "how many good candidates does a day need." Was previously only settable
# by editing AUTO_LIST_TOP_N_DEFAULT/AUTO_LIST_REPLENISH_BUFFER_DEFAULT
# inside branch11_pipeline.py -- those constants still exist and still
# apply to any ad-hoc/manual run of the pipeline scripts, but the daily
# cron now always passes explicit values here so this file is the one
# place to look/change it without touching source. TARGET_GOOD_ITEMS is
# how many HIGH-verdict, profit-passing candidates get auto-listed
# (--auto-list-top-n); REPLENISH_BUFFER is how many extra candidates
# beyond that get verified+attempted as fallback if some of the top ones
# fail (--auto-list-replenish-buffer, PRODUCTION-READINESS.md Item 36).
# Step 2's early-stop (Item 27) already stops searching once
# TARGET_GOOD_ITEMS + REPLENISH_BUFFER eligible candidates are found among
# today's discovered items -- i.e. "work through today's 2 rotated URLs in
# batches until enough good items are found, then stop."
TARGET_GOOD_ITEMS="${BRANCH11_TARGET_GOOD_ITEMS:-5}"
REPLENISH_BUFFER="${BRANCH11_REPLENISH_BUFFER:-5}"

# Step 1: eBay-only discovery (bare Python, zero LLM cost). Scraping
# mechanism (rotation, price filter, keyword-source pages) unchanged;
# 2026-09-02 added scroll-to-load-more inside _scrape_tiles_for_url() so a
# lazy-loading Deals page actually yields tiles up to MAX_TILES_PER_URL
# (raised 50->100 same day) instead of just whatever rendered before any
# scrolling -- see branch11_pipeline.py's MAX_TILES_PER_URL/
# _scroll_to_load_more() comments for the live numbers that motivated this.
python3 branch11_pipeline.py --stage discover

# Step 2 (rewritten 2026-08-30, see memory
# incident-2026-08-30-branch11-rate-limit): search the DSers MCP for each
# discovered eBay item via a standalone OAuth 2.1+PKCE MCP client, no LLM
# involved -- zero token cost, and volume can't blow a Claude rate-limit
# budget no matter how big discovery's pool gets. Same no-judgment
# pass-through contract as before (errors -> empty results, keep going;
# no filtering/ranking, that's still step 3's job) and the same output
# shape, so step 3 needs no changes. One-time setup: the very first run
# needs a human to approve DSers access in a browser (see the script's own
# docstring for the localhost callback / SSH-tunnel details); after that,
# the cached refresh token (branch11_dsers_mcp_tokens.json) keeps this
# fully unattended. --early-stop-pool-size now passed explicitly so it
# tracks TARGET_GOOD_ITEMS/REPLENISH_BUFFER above instead of the constants
# baked into branch11_pipeline.py.
uv run branch11_dsers_search.py --ebay-only-file "${EBAY_ONLY_PATH}" --out-file "${DSERS_SEARCH_PATH}" \
    --early-stop-pool-size "$((TARGET_GOOD_ITEMS + REPLENISH_BUFFER))"

# Step 3: judge the DSers search results (deterministic Python, reusing the
# existing judge_match()/profit-calc/category-analysis/auto-listing code
# unchanged from the old single-pass path -- only the AliExpress-matching
# INPUT source changed, see build_dsers_ali_match() in branch11_pipeline.py).
python3 branch11_pipeline.py --stage finish --dsers-search-file "${DSERS_SEARCH_PATH}" \
    --auto-list-top-n "${TARGET_GOOD_ITEMS}" --auto-list-replenish-buffer "${REPLENISH_BUFFER}"

JSON_PATH="$HOME/openclaw-troubleshooting/branch11_candidates_${TODAY}.json"

# Step 4 (rewritten 2026-08-30, see branch11-listing-rules.md): compose and
# send the daily report email via plain SMTP (branch11_email_report.py, a
# Gmail App Password stored in branch11_unattended_creds.json), no LLM
# involved -- on inspection this step's whole "judgment" was fixed
# deterministic branching over already-computed fields (listing_status
# prefix, profit_check.passes, verdict grouping), same class of fix as the
# 2026-08-30 Step 2 rewrite. One-time setup: run
# `python3 branch11_email_report.py --setup-creds` once to store the App
# Password.
python3 branch11_email_report.py --candidates-file "${JSON_PATH}"

# Step 5 (added 2026-08-29, DSers-mapping automation, "item 1"; rewritten
# 2026-08-30 to drop the LLM -- see branch11_dsers_mapping.py's own
# docstring): complete the DSers product-to-AliExpress mapping for any SKU
# whose WooCommerce mirror (created by run_auto_listing()/
# _mirror_to_woocommerce() in the pipeline step above) has since been
# manually imported into DSers by Travis via his periodic "Import Products
# from WooCommerce" dashboard click -- confirmed 2026-08-29 that step itself
# has no API/MCP equivalent (a fresh WooCommerce product does not auto-sync
# into DSers -- live-tested, see STATUS.md), so it stays the one manual
# touch in this flow. Everything else here is autonomous, no chat approval,
# per Travis's 2026-08-29 decision -- mapping only links data for a future
# manual supplier order, it doesn't move money or create/change any live
# listing, so it's lower-stakes than auto-listing itself already is.
# branch11_dsers_mapping.py reuses branch11_dsers_mcp_client.py's same
# cached DSers OAuth session as step 2 -- no separate auth, no LLM.
LEDGER_PATH="$HOME/openclaw-troubleshooting/branch11_auto_listed.json"

uv run branch11_dsers_mapping.py --ledger-file "${LEDGER_PATH}"
