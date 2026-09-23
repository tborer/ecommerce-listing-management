#!/bin/bash
# Branch 11 Item 42, step 1: daily DSers supplier out-of-stock detection
# email. See branch11_stock_check.py's own docstring for the full design
# (dsers_my_products' supplier_status filter, detect-and-report only --
# never auto-remaps or touches a live eBay listing).
#
# Deliberately its own dedicated cron -- same "one script, one concern, one
# cron" pattern as every other daily-cadence script in this pipeline.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/openclaw-troubleshooting"

uv run branch11_stock_check.py
