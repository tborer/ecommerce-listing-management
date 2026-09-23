#!/bin/bash
# Branch 11 Step 5 frequent automation: complete DSers supplier-mapping for
# any auto-listed SKU that's become visible in DSers since the last check.
#
# Added 2026-08-31 (PRODUCTION-READINESS.md Item 30 follow-up, Travis's
# explicit request): previously this only ran once/day as part of
# branch11_daily_run.sh's step 5. Live testing that day showed a newly
# auto-listed item (branch11-203094541405) became mappable in DSers well
# before the next day's 8am run would have reached it -- whether because
# Travis re-clicked "Import Products from WooCommerce" or because DSers's
# WooCommerce plugin does some periodic sync of its own is still unconfirmed
# (see PRODUCTION-READINESS.md Item 30's open question), but either way,
# running this more often means a product gets its supplier mapping
# completed within ~30 minutes of becoming visible in DSers instead of
# waiting up to 24 hours for the next daily cron.
#
# Deliberately its OWN dedicated cron, not folded into the 30-min
# branch11-production-readiness backlog cron -- that cron is an LLM-driven
# (`claude -p`) session working through PRODUCTION-READINESS.md's backlog,
# not a place for routine deterministic pipeline steps to run inline (same
# reasoning already established for Item 28's daily enrichment cron). This
# script is bare Python + the DSers MCP, zero LLM cost, safe to run every 30
# minutes indefinitely -- same shape as branch11_orders_run.sh's read-only
# half, just without an email step (nothing here needs Travis's attention
# unless something errors, and this doesn't change what the existing 8am
# daily report already surfaces).
#
# Still runs harmlessly as a no-op when nothing new is mappable (matches
# branch11_dsers_mapping.py's own "0 newly mapped" normal-day behavior) --
# safe to schedule frequently without any special idle handling here.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/openclaw-troubleshooting"

uv run branch11_dsers_mapping.py --ledger-file branch11_auto_listed.json
