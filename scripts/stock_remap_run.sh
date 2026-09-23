#!/bin/bash
# Branch 11 Item 42, step 2: attempts a verified DSers supplier remap for
# any SKU branch11_stock_check.py still has flagged unresolved. See
# branch11_stock_remap.py's own docstring for the full acceptance bar
# (HIGH verdict via the same rubric that gates auto-listing, still passes
# the original target margin, dsers_sku_remap preview confidence >= 70).
#
# Deliberately runs with --dry-run for now (PRODUCTION-READINESS.md Item 42):
# a real live test against the one real broken SKU as of 2026-09-01 found a
# HIGH-verdict, confidence-100, still-profitable candidate that is NOT the
# same exact model number as the eBay listing's advertised title (a
# different Makita reciprocating saw model, same brand/voltage/category).
# The existing HIGH-verdict rubric was built and trusted for fresh
# listing-time matching, where any sufficiently-close AliExpress source is
# fine since the eBay listing is built FROM it -- reusing that same bar to
# swap the supplier behind an EXISTING, already-advertised listing is a real
# escalation (a customer could pay for one model and receive a differently-
# labeled one), and hasn't been explicitly blessed at that stakes level.
# Runs propose-only (dry-run, emails what it WOULD do) until Travis decides
# whether the bar needs to be stricter for remap specifically. Remove
# --dry-run below only after that decision is made.
#
# Deliberately its own dedicated cron -- same "one script, one concern, one
# cron" pattern as every other daily-cadence script in this pipeline.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/openclaw-troubleshooting"

uv run branch11_stock_remap.py --dry-run
