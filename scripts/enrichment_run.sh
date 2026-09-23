#!/bin/bash
# Branch 11 Items 28/31: daily description/image-text enrichment, small
# batch (4 items/day default), non-LLM. See branch11_enrichment.py's own
# docstring for the full design (DSers image_urls + local Tesseract OCR,
# no AliExpress-iframe dependency, no LLM needed).
#
# Deliberately its own dedicated ~12 PM cron, separate from both the 8 AM/
# 5 PM Branch 11 jobs and the 30-min production-readiness backlog cron --
# same reasoning as every other daily-cadence script in this pipeline
# (Travis's 2026-08-30 decision for this exact item).
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/openclaw-troubleshooting"

uv run branch11_enrichment.py
