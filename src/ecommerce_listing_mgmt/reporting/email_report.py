#!/usr/bin/env python3
"""Branch 11 Step 4 replacement: build and send the daily report email via
plain SMTP (a Gmail App Password), no LLM involved.

Why: the old Step 4 was a `claude -p` call, but on inspection its entire
"judgment" is fixed, deterministic branching over fields the pipeline
already computed (listing_status prefix, profit_check.passes, verdict
grouping) -- no natural-language reasoning actually happens. Rewriting it as
plain Python removes an LLM/token dependency and rate-limit exposure from a
purely mechanical step, same motivation as the 2026-08-30 Step 2 rewrite
(see memory incident-2026-08-30-branch11-rate-limit and
branch11_dsers_search.py).

Reads a branch11_candidates_<date>.json file (this run's finished pipeline
output -- same shape whether produced by `--stage finish` or `--stage
full`) and sends ONE plain-text email to REPORT_TO via smtp.gmail.com,
authenticated with an App Password stored in branch11_unattended_creds.json
under a "gmail" key (mode 600 -- same file already used for the eBay/
AliExpress unattended credentials, see branch11-listing-rules.md's
Credential Handling section). App Passwords require 2FA already enabled on
the Google account; generate one at myaccount.google.com -> Security -> App
Passwords, then run this script's --setup-creds flag once to store it.

Mirrors the old `claude -p` Step 4 prompt's exact section structure and
field selection (branch11_daily_run.sh, pre-2026-08-30) so the report
Travis reads doesn't change shape, only how it's produced. Grouping/sorting
is computed explicitly here (by verdict, then by total_profit descending)
rather than trusting the input array's existing order, which the old prompt
had to be told not to disturb -- this way it's correct regardless of how the
pipeline writes the file.
"""
from __future__ import annotations

import argparse
import getpass
import json
import smtplib
from datetime import date
from email.mime.text import MIMEText
from pathlib import Path

CREDS_PATH = Path(__file__).parent / "branch11_unattended_creds.json"
LEDGER_PATH = Path(__file__).parent / "branch11_auto_listed.json"
REPORT_TO = "tray14@hotmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

VERDICT_ORDER = ["HIGH", "MEDIUM", "NONE"]


def load_gmail_creds() -> tuple[str, str]:
    if not CREDS_PATH.exists():
        raise SystemExit(f"{CREDS_PATH} does not exist -- run with --setup-creds first.")
    creds = json.loads(CREDS_PATH.read_text())
    gmail = creds.get("gmail") or {}
    if not gmail.get("address") or not gmail.get("app_password"):
        raise SystemExit(f"{CREDS_PATH} has no gmail.address/gmail.app_password -- run with --setup-creds first.")
    return gmail["address"], gmail["app_password"]


def setup_creds() -> None:
    """One-time interactive step: prompt for the Gmail address + App
    Password and merge into the existing unattended-creds file (creating it
    fresh with mode 600 if it doesn't exist yet -- matches the file's
    existing ebay/aliexpress entries)."""
    address = input("Gmail address to send from (e.g. travis.borer@gmail.com): ").strip()
    app_password = getpass.getpass("Gmail App Password (16 chars, from myaccount.google.com > Security > App Passwords): ").strip()
    creds = json.loads(CREDS_PATH.read_text()) if CREDS_PATH.exists() else {}
    creds["gmail"] = {"address": address, "app_password": app_password}
    CREDS_PATH.write_text(json.dumps(creds, indent=2))
    CREDS_PATH.chmod(0o600)
    print(f"Saved gmail credentials to {CREDS_PATH} (mode 600).")


def _money(x) -> str:
    return f"${x:.2f}" if isinstance(x, (int, float)) else "n/a"


def _listing_id_from_status(status: str) -> str | None:
    """Extracts just the numeric listingId out of `listing_status`, e.g.
    "LISTED (listingId=800600000000)" -- or, since 2026-09-04, "LISTED
    (listingId=800600000000) -- LLM-ASSISTED (...)" -- stops at the FIRST
    ")" rather than stripping trailing ")" from the whole string, which
    used to silently swallow anything appended after the id (confirmed
    live: an LLM-assisted status's own trailing detail was getting pulled
    into the "New live listing" URL)."""
    if "listingId=" not in status:
        return None
    return status.split("listingId=")[-1].split(")")[0]


def _profit_line(pc: dict) -> str:
    return (f"  Profit breakdown: fee {_money(pc.get('ebay_fee'))}, target margin {_money(pc.get('target_margin'))}, "
            f"budget {_money(pc.get('budget_for_supply_and_shipping'))}, ali price {_money(pc.get('ali_price'))}, "
            f"ali shipping {_money(pc.get('ali_shipping_assumed'))}, headroom {_money(pc.get('remaining_after_supply_and_shipping'))}, "
            f"TOTAL PROFIT {_money(pc.get('total_profit'))}")


def _category_lines(ca: dict | None) -> list[str]:
    if not ca:
        return []
    lines = [f"  eBay category: '{ca.get('category_name')}' (match {ca.get('match_score')})"]
    if ca.get("needs_unresolvable_variation"):
        aspects = ", ".join(ca.get("unresolvable_variation_aspects") or [])
        lines.append(f"  NOT auto-listed: needs a product-variation value ({aspects}) that can't be safely auto-filled "
                      f"(no eBay Variations/multi-SKU support yet). Can still be approved manually by SKU with a specific value.")
    return lines


def _auto_listed_block(item: dict) -> str:
    ebay, ali = item["ebay_item"], item.get("best_ali_match") or {}
    status = item.get("listing_status", "")
    lines = [f"* {item['sku']} -- {ebay['title']}",
             f"  Original eBay item: {ebay['url']}"]
    listing_id = _listing_id_from_status(status)
    if listing_id:
        lines.append(f"  New live listing: https://www.ebay.com/itm/{listing_id}")
    if ali.get("url"):
        lines.append(f"  AliExpress source: {ali['url']}")
    if "LLM-ASSISTED" in status:
        # 2026-09-04: node5 (local LLM) resolved a required variant aspect
        # that every deterministic tier failed on -- surfaced distinctly,
        # not blended silently into an ordinary auto-listed item, so
        # Travis can spot-check these even though the mechanism is
        # autonomous (his explicit decision) -- see branch11_llm_assist.py.
        detail = status.split("LLM-ASSISTED", 1)[-1].strip()
        lines.append(f"  LLM-ASSISTED LISTING -- node5 resolved: {detail}")
    if item.get("profit_check"):
        # Fixed 2026-09-01 (Travis flagged this live): this used to only
        # print "eBay price $X, total profit $Y" with no visible fee/
        # AliExpress-price breakdown -- total_profit itself was already
        # correctly computed as ebay_price - ebay_fee - ali_price -
        # ali_shipping (compute_profit() in branch11_pipeline.py, confirmed
        # against real ledger data), but with no breakdown shown, there was
        # no way to see that from the email, which read as if the fee
        # wasn't being subtracted at all. Now shows the same full breakdown
        # _passed_block() already does, via the shared _profit_line() helper,
        # so the fee subtraction is visible and verifiable here too.
        lines.append(f"  eBay price {_money(item['profit_check'].get('ebay_price'))}")
        lines.append(_profit_line(item["profit_check"]))
    return "\n".join(lines)


def _auto_list_failed_block(item: dict) -> str:
    ebay, ali = item["ebay_item"], item.get("best_ali_match") or {}
    reason = item.get("listing_status", "").split("AUTO_LIST_FAILED:", 1)[-1].strip()
    lines = [f"* {item['sku']} -- {ebay['title']}",
             f"  Original eBay item: {ebay['url']}"]
    if ali.get("url"):
        lines.append(f"  AliExpress source: {ali['url']}")
    lines.append(f"  Failure reason: {reason}")
    return "\n".join(lines)


def _phase3_opportunity_block(item: dict) -> str:
    """Item 25 Phase 3: a genuine, fully-resolved multi-variant fix was
    found (branch11_variation_check.py, real DSers options data mapped to
    real eBay-enum values -- see that module's own docstring). Shows
    everything Travis needs to approve or reject in the reply -- reusing
    this data directly avoids re-deriving it if he says yes."""
    ebay, ali = item["ebay_item"], item.get("best_ali_match") or {}
    plan = item.get("phase3_variant_plan") or {}
    lines = [f"* {item['sku']} -- {ebay['title']}",
             f"  Original eBay item: {ebay['url']}"]
    if ali.get("url"):
        lines.append(f"  AliExpress source: {ali['url']}")
    lines.append(f"  Variation aspect: {plan.get('aspect')}")
    for dsers_value, ebay_value in (plan.get("mapping") or {}).items():
        lines.append(f"    - eBay value \"{ebay_value}\" (DSers: \"{dsers_value}\")")
    if item.get("profit_check"):
        lines.append(f"  eBay price {_money(item['profit_check'].get('ebay_price'))}")
        lines.append(_profit_line(item["profit_check"]))
    lines.append("  NOT auto-listed -- reply/tell Claude to list this SKU to publish the real multi-variant listing.")
    return "\n".join(lines)


def _passed_block(item: dict) -> str:
    ebay, ali, pc = item["ebay_item"], item.get("best_ali_match") or {}, item.get("profit_check") or {}
    lines = [f"* SKU: {item['sku']}",
             f"  {ebay['title']}",
             f"  eBay item URL: {ebay['url']}",
             f"  eBay price: {_money(ebay.get('priceNum'))}",
             f"  AliExpress match: {ali.get('title', 'n/a')}",
             f"  AliExpress item URL: {ali.get('url', 'n/a')}",
             f"  AliExpress price(s): {', '.join(ali.get('prices') or []) or 'n/a'}",
             f"  Verdict: {ali.get('verdict', 'n/a')} (score {ali.get('match_score', 'n/a')})",
             _profit_line(pc),
             f"  listing_status: {item.get('listing_status', 'n/a')}"]
    lines.extend(_category_lines(item.get("category_analysis")))
    mismatch_flags = [r for r in (ali.get("reasons") or [])
                       if any(k in r.lower() for k in
                              ("accessory-only", "voltage", "model", "wired", "wireless", "size", "tier", "implausible"))]
    for flag in mismatch_flags:
        lines.append(f"  ! {flag}")
    return "\n".join(lines)


def _not_met_block(item: dict) -> str:
    ebay, ali, pc = item["ebay_item"], item.get("best_ali_match"), item.get("profit_check")
    lines = [f"* {ebay['title']} -- {_money(ebay.get('priceNum'))}",
             f"  eBay item URL: {ebay['url']}"]
    if ali:
        lines.append(f"  Best AliExpress match: {ali.get('title', 'n/a')} ({ali.get('url', 'n/a')})")
        lines.append(f"  Verdict: {ali.get('verdict', 'n/a')} (score {ali.get('match_score', 'n/a')})")
        if ali.get("reasons"):
            lines.append(f"  Reasons: {'; '.join(ali['reasons'])}")
    else:
        lines.append("  No AliExpress match found.")
    if pc and not pc.get("passes"):
        headroom = pc.get("remaining_after_supply_and_shipping")
        if isinstance(headroom, (int, float)) and headroom < 0:
            lines.append(f"  Did not pass: profit shortfall of {_money(-headroom)}")
        else:
            lines.append("  Did not pass: profit check failed")
    elif not pc:
        lines.append("  Did not pass: no profit data (no match found)")
    return "\n".join(lines)


def _pending_dsers_import_lines(ledger: dict) -> list[str]:
    """Next-steps item 29 (2026-08-30): the WooCommerce-mirror -> DSers-mapping
    chain has one unavoidable manual step (Travis's periodic "Import Products
    from WooCommerce" DSers-dashboard click -- no API/MCP equivalent exists,
    confirmed 2026-08-29), and nothing previously surfaced *how many* SKUs
    were sitting mirrored-but-unmapped waiting on it. A SKU could in
    principle get a real order before DSers ever knows how to source it.
    This makes that backlog visible every day instead of silently invisible."""
    pending = [(sku, e) for sku, e in ledger.items()
               if e.get("woo_product_id") is not None and not e.get("dsers_mapped", False)]
    if not pending:
        return []
    lines = [f"=== PENDING DSERS IMPORT -- needs your WooCommerce-import click ({len(pending)}) ===",
             "(Mirrored to the WooCommerce bridge store, but DSers doesn't know about them yet -- "
             "import them in the DSers dashboard so a real order can actually be sourced.)"]
    for sku, entry in sorted(pending, key=lambda kv: kv[1].get("listed_at", ""), reverse=True):
        lines.append(f"* {sku} -- {entry.get('ebay_title', 'n/a')} (listed {entry.get('listed_at', 'n/a')}, "
                     f"woo_product_id={entry.get('woo_product_id')})")
    lines.append("")
    return lines


def build_email_body(candidates: list[dict], ledger: dict | None = None) -> tuple[str, str]:
    auto_listed = [c for c in candidates if (c.get("listing_status") or "").startswith("LISTED")]
    auto_failed = [c for c in candidates if (c.get("listing_status") or "").startswith("AUTO_LIST_FAILED")]
    phase3_opportunities = [c for c in candidates if (c.get("listing_status") or "").startswith("PHASE3_OPPORTUNITY")]
    already_shown_skus = {c["sku"] for c in auto_listed + auto_failed + phase3_opportunities}

    passed = [c for c in candidates
              if c["sku"] not in already_shown_skus and c.get("profit_check") and c["profit_check"].get("passes")]
    not_met = [c for c in candidates if c["sku"] not in already_shown_skus and c not in passed]

    def by_verdict_then_profit(items: list[dict]) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {v: [] for v in VERDICT_ORDER}
        for c in items:
            verdict = (c.get("best_ali_match") or {}).get("verdict", "NONE")
            groups.setdefault(verdict, []).append(c)
        for v in groups:
            groups[v].sort(key=lambda c: (c.get("profit_check") or {}).get("total_profit", 0), reverse=True)
        return groups

    total = len(candidates)
    n_passed = len(passed) + len(auto_listed) + len(auto_failed)
    n_not_met = len(not_met)
    n_listed = len(auto_listed)

    lines = [f"Scanned {total} item(s): {n_passed} passed criteria, {n_not_met} did not meet criteria, "
             f"{n_listed} auto-listed this run"
             + (f", {len(phase3_opportunities)} Phase 3 opportunity(ies) awaiting your approval" if phase3_opportunities else "")
             + ".", ""]

    if ledger:
        pending_lines = _pending_dsers_import_lines(ledger)
        if pending_lines:
            lines.extend(pending_lines)

    lines.append("=== AUTO-LISTED THIS RUN -- now live on production ===")
    if auto_listed:
        for c in auto_listed:
            lines.append(_auto_listed_block(c))
            lines.append("")
    else:
        lines.append("(none)")
        lines.append("")
    if auto_failed:
        lines.append("-- Auto-list attempted, failed --")
        for c in auto_failed:
            lines.append(_auto_list_failed_block(c))
            lines.append("")

    if phase3_opportunities:
        lines.append("=== PHASE 3 OPPORTUNITY -- genuine multi-variant listing available, needs your approval ===")
        lines.append("(Item 25 Phase 3: a real, fully-resolved eBay Variations API fix was found -- "
                      "NOT auto-listed. Reply/tell Claude which SKU(s) to publish.)")
        lines.append("")
        for c in phase3_opportunities:
            lines.append(_phase3_opportunity_block(c))
            lines.append("")

    lines.append("=== PASSED CRITERIA -- candidates for listing ===")
    lines.append("(Proposals only, not auto-listed. Approve by replying with the SKU(s) to list.)")
    lines.append("")
    passed_groups = by_verdict_then_profit(passed)
    any_passed = False
    for verdict in VERDICT_ORDER:
        items = passed_groups[verdict]
        if not items:
            continue
        any_passed = True
        lines.append(f"--- {verdict} ({len(items)}) ---")
        for c in items:
            lines.append(_passed_block(c))
            lines.append("")
    if not any_passed:
        lines.append("(none)")
        lines.append("")

    lines.append("=== DID NOT MEET CRITERIA -- not listed ===")
    not_met_groups = by_verdict_then_profit(not_met)
    any_not_met = False
    for verdict in VERDICT_ORDER:
        items = not_met_groups[verdict]
        if not items:
            continue
        any_not_met = True
        lines.append(f"--- {verdict} ({len(items)}) ---")
        for c in items:
            lines.append(_not_met_block(c))
            lines.append("")
    if not any_not_met:
        lines.append("(none)")

    today = date.today().isoformat()
    subject = f"Branch 11 Daily Item Report - {today}"
    return subject, "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    from_address, app_password = load_gmail_creds()
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = REPORT_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(from_address, app_password)
        server.sendmail(from_address, [REPORT_TO], msg.as_string())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates-file", type=Path, help="Path to branch11_candidates_<date>.json")
    ap.add_argument("--setup-creds", action="store_true", help="Interactively store the Gmail App Password, then exit.")
    ap.add_argument("--dry-run", action="store_true", help="Print the email instead of sending it.")
    args = ap.parse_args()

    if args.setup_creds:
        setup_creds()
        return

    if not args.candidates_file:
        raise SystemExit("--candidates-file is required (unless using --setup-creds).")

    candidates = json.loads(args.candidates_file.read_text())
    ledger = json.loads(LEDGER_PATH.read_text()) if LEDGER_PATH.exists() else {}
    subject, body = build_email_body(candidates, ledger)

    if args.dry_run:
        print(f"Subject: {subject}\n")
        print(body)
        return

    send_email(subject, body)
    print(f"[email-report] Sent '{subject}' to {REPORT_TO} ({len(candidates)} candidates).")


if __name__ == "__main__":
    main()
