#!/bin/bash
# Branch 11 Item 22: daily DSers-based price-check email, 1 PM Central.
# See branch11_price_check.py's own docstring for the full design (DSers
# cost as source of truth, baseline-vs-current flagging, suggested-price
# calculation). Also runs the Item 41 advertising MVP each day: adds up to
# ADVERTISING_CANDIDATES_TOP_N of today's highest-margin not-yet-advertised
# items to the account's existing eBay ad campaign (real write, added
# 2026-09-02, Travis-directed).
#
# Deliberately its own dedicated cron -- same "one script, one concern, one
# cron" pattern as every other daily-cadence script in this pipeline.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/openclaw-troubleshooting"

uv run branch11_price_check.py
