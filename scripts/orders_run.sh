#!/bin/bash
# Branch 11 Step 8 daily automation: pull pending eBay orders, prep AliExpress
# sourcing info per line item, then a separate fresh `claude -p` call reads
# the resulting JSON and emails Travis a status report -- same two-step
# pattern as branch11_daily_run.sh (bare script, zero LLM cost, then a
# single-purpose email step) and for the same reason, see
# feedback_notification_architecture: bundling the email into the main task's
# own prompt has failed before.
#
# Read-only against eBay (sell.fulfillment.readonly, GET /sell/fulfillment/v1/order
# only) -- this never places an AliExpress order, never touches eBay order/
# tracking state, and never messages a customer. Steps 9-11 stay fully manual
# per Hard Constraints.
#
# **Added 2026-08-31 (PRODUCTION-READINESS.md Item 30)**: branch11_orders.py
# now also mirrors each fully-sourced order into the DSers-bridge WooCommerce
# store (status "pending", no payment implied) so it shows up in DSers ready
# for Travis's own one-click "Place Order" approval + DSers's native
# tracking-auto-sync, instead of only ever existing inside this email. Each
# order in the JSON now carries a `woo_push` field the email prompt below
# reports on.
#
# Credential access: production eBay, credential_mode=local -- the same
# standing local-file token run_auto_listing() uses (branch11_unattended_creds.json),
# not Bitwarden/BW_SESSION. Verified working against real production data
# 2026-08-29 before this was wired into cron.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/openclaw-troubleshooting"

python3 branch11_orders.py production --credential-mode local

TODAY=$(date +%F)
JSON_PATH="$HOME/openclaw-troubleshooting/branch11_orders_pending_${TODAY}.json"

claude -p --permission-mode acceptEdits --allowedTools "Read,mcp__claude_ai_Gmail__send_message" --output-format json "Read the file ${JSON_PATH} (a JSON array of pending eBay orders for the Branch 11 dropship pipeline, each with ship_to address, per-line-item AliExpress sourcing info, and a woo_push status). Compose and send ONE email to tray14@hotmail.com via the Gmail MCP tool (mcp__claude_ai_Gmail__send_message).

Subject: Branch 11 Order Report - ${TODAY}

Important framing, state this plainly near the top of the email: this system does NOT automatically place AliExpress orders or spend any money yet (that step is still manual, by design -- see Hard Constraints). 'Pushed to WooCommerce/DSers' below means the order is now staged and visible in Travis's DSers dashboard, ready for him to do DSers's own one-click 'Place Order' approval (which still requires him to complete payment on AliExpress's checkout) -- it does NOT mean anything was ordered or paid for. Never imply an AliExpress purchase happened.

Body structure:
1. One-line summary at the top: total pending orders, total line items, how many line items have sourcing status FOUND vs NEEDS_MANUAL_SOURCING_LOOKUP, and how many orders were newly pushed to WooCommerce/DSers this run (woo_push.status == PUSHED) vs already pushed on a prior run (ALREADY_PUSHED) vs still can't be pushed (SKIPPED_MISSING_SOURCING or PUSH_ERROR).
2. If the array is empty: just say clearly there are no pending orders today, nothing further needed.
3. Otherwise, one section per order, most recent creation_date first. For each order show: order_id, creation_date, full ship-to (full_name, address_line1/2, city, state, postal_code, country, phone), and its woo_push status --
   - if woo_push.status is PUSHED or ALREADY_PUSHED: 'ACTION: this order is staged in DSers (woo_order_id shown) -- go to DSers and use Place Order to complete it (you'll still confirm payment on AliExpress there).'
   - if woo_push.status is SKIPPED_MISSING_SOURCING or PUSH_ERROR: show the reason, and 'ACTION: not yet staged in DSers -- see line items below for what's missing.'
   Then list each line item: sku, title, quantity, line_item_cost + currency, and its sourcing status -- if FOUND, show ali_url/ali_price/ali_shipping_cost for reference; if NEEDS_MANUAL_SOURCING_LOOKUP, show the reason and 'ACTION: find this item on AliExpress yourself, no source on file, this is why the order couldn't be staged in DSers.'
4. Keep formatting clean and scannable -- this runs daily, so it needs to be quick to read, not verbose.

PLAIN TEXT ONLY, every time: pass the message via the `body` parameter of mcp__claude_ai_Gmail__send_message and leave `htmlBody` unset. Do NOT write any HTML markup (no <div>, <p>, <strong>, <hr>, etc.) anywhere in `body` -- Gmail sends `body` as literal plain text, so any HTML tags in it show up as raw text in the recipient's inbox, not formatted. Use blank lines, dashes, and plain "ACTION:" labels for structure instead, matching the style of Branch 11's other daily report emails.

Send the email now. Do not take any other action yourself (no ordering, no eBay/AliExpress/WooCommerce/DSers API calls, no other tool use) -- your only job is reading this one JSON file and reporting it accurately."
