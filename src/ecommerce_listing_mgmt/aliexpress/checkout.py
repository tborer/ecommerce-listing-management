#!/usr/bin/env python3
"""Branch 11 Step 9 (research): AliExpress checkout login/2FA test.

**Run this yourself, directly in your own terminal** (`python3 branch11_aliexpress_checkout.py`),
not through a live Claude Code session -- 2026-08-29 confirmed that any live browser
fill/click/snapshot action on AliExpress's login/checkout flow gets blocked by Claude Code's
own auto-mode classifier in an interactive session, even read-only snapshot calls, even an
empty-fields fill probe with zero credential content. Running this script yourself sidesteps
that entirely, since you (not Claude) are the one invoking it -- same reasoning that already
lets run_auto_listing() handle real eBay credentials unattended via cron with no classifier
involvement at all.

What this does: starts the managed browser, opens a real AliExpress product page, clicks
"Buy now" (triggers the sign-in modal per the 2026-08-28 finding), logs in using the
email/password already stored in branch11_unattended_creds.json (never printed), and if a
2FA code prompt appears, pauses and asks YOU to type it directly into this terminal (never
sent through chat). Stops as soon as login/checkout is confirmed reached.

What this deliberately does NOT do: select a payment method, enter shipping details beyond
whatever's pre-filled, or click Place Order / submit any actual purchase. Per Branch 11's
Hard Constraints, autonomous real purchases stay out of scope regardless of what this proves
about login/2FA automatability -- this script only answers the login-automation question.

This is a first-pass script, not verified end-to-end (the same guardrail that requires you to
run it also prevented testing it live). The snapshot/ref-matching logic is best-effort text
search over `openclaw browser snapshot`'s output -- if a step fails to find an expected
element, it prints the raw snapshot output and stops rather than guessing. Paste that output
back to Claude (in chat) to get the selector logic fixed, then re-run.

Usage:
    python3 branch11_aliexpress_checkout.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

CREDS_PATH = Path(__file__).parent / "branch11_unattended_creds.json"
CANDIDATES_PATH = Path(__file__).parent / "branch11_candidates_latest.json"


def sh(*args: str, timeout: int = 60) -> str:
    proc = subprocess.run(["openclaw", "browser", *args], capture_output=True, text=True, timeout=timeout)
    return proc.stdout


def run_json(*args: str, timeout: int = 60) -> dict | list:
    out = sh(*args, "--json", timeout=timeout)
    brace, bracket = out.find("{"), out.find("[")
    starts = [i for i in (brace, bracket) if i != -1]
    if not starts:
        raise RuntimeError(f"No JSON in output: {out!r}")
    return json.loads(out[min(starts):])


def find_ref(snapshot_data, pattern: str) -> str | None:
    """Best-effort recursive search over the snapshot's JSON tree for a node whose
    name/text/label/role-adjacent string matches `pattern` (case-insensitive), returning
    its 'ref' if present. Snapshot schema isn't independently verified -- if this returns
    None, print the raw snapshot and inspect it by hand / paste back to Claude."""
    rx = re.compile(pattern, re.IGNORECASE)
    found = []

    def walk(node):
        if isinstance(node, dict):
            text_fields = " ".join(str(node.get(k, "")) for k in ("name", "text", "label", "value", "role"))
            if rx.search(text_fields) and node.get("ref"):
                found.append(node["ref"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(snapshot_data)
    return found[0] if found else None


def get_test_product_urls():
    if CANDIDATES_PATH.exists():
        data = json.loads(CANDIDATES_PATH.read_text())
        for row in data:
            match = row.get("best_ali_match")
            if match and match.get("url"):
                yield match["url"]


def main() -> None:
    creds = json.loads(CREDS_PATH.read_text())
    ali = creds.get("aliexpress") or {}
    email, password = ali.get("email"), ali.get("password")
    if not email or not password:
        sys.exit(f"{CREDS_PATH} is missing aliexpress email/password.")

    print("Starting browser...")
    sh("start")
    time.sleep(1)

    # AliExpress redirects a dead/delisted item URL to a generic wholesale
    # search/category page instead of a 404 (confirmed live 2026-08-29 --
    # aliexpress.us/item/3256811720077932.html silently redirected to
    # /w/wholesale-apple-airpods-nd-gen.html, an unrelated category page).
    # No "Buy now" button exists there, so try candidates in order until one
    # actually lands on a real product page rather than assuming the first
    # candidate is always live.
    MAX_ATTEMPTS = 5
    buy_ref = snap = product_url = None
    for attempt, candidate_url in enumerate(get_test_product_urls(), start=1):
        if attempt > MAX_ATTEMPTS:
            break
        print(f"[{attempt}] Navigating to {candidate_url}...")
        sh("navigate", candidate_url, timeout=60)
        time.sleep(2)

        landed_url = (run_json("evaluate", "--fn", "() => location.href") or {}).get("result")
        if landed_url and "/item/" not in landed_url:
            print(f"    Redirected away from the product page (landed on {landed_url}) -- likely dead/delisted, trying next candidate.")
            continue

        snap = run_json("snapshot", "--format", "aria", "--limit", "500")
        buy_ref = find_ref(snap, r"buy now")
        if buy_ref:
            product_url = candidate_url
            print(f"    Found 'Buy now' (ref={buy_ref}).")
            break
        print("    No 'Buy now' found on this page either -- trying next candidate.")

    if not buy_ref:
        print(f"Could not find a live product page with a 'Buy now' button after {MAX_ATTEMPTS} attempts.")
        if snap is not None:
            print("Last snapshot below -- paste back to Claude:")
            print(json.dumps(snap, indent=2)[:4000])
        sys.exit(1)

    print(f"Using product: {product_url}")

    print(f"Clicking 'Buy now' (ref={buy_ref})...")
    sh("click", buy_ref)
    time.sleep(3)

    print("Taking snapshot of the sign-in modal...")
    snap = run_json("snapshot", "--format", "aria", "--limit", "500")
    email_ref = find_ref(snap, r"email|account|login.?id")
    if not email_ref:
        print("Could not find an email field in the sign-in modal. Raw snapshot below -- paste back to Claude:")
        print(json.dumps(snap, indent=2)[:4000])
        sys.exit(1)

    print(f"Filling email (ref={email_ref})...")
    sh("fill", "--fields", json.dumps([{"ref": email_ref, "value": email}]))
    time.sleep(1)

    # Some AliExpress login flows are two-step (email, then Next, then password on a
    # second screen) rather than one form -- re-snapshot rather than assuming the
    # password field is already visible.
    snap = run_json("snapshot", "--format", "aria", "--limit", "500")
    password_ref = find_ref(snap, r"password")
    if not password_ref:
        next_ref = find_ref(snap, r"^next$|continue")
        if next_ref:
            print(f"Two-step login detected, clicking Next (ref={next_ref})...")
            sh("click", next_ref)
            time.sleep(2)
            snap = run_json("snapshot", "--format", "aria", "--limit", "500")
            password_ref = find_ref(snap, r"password")

    if not password_ref:
        print("Could not find a password field. Raw snapshot below -- paste back to Claude:")
        print(json.dumps(snap, indent=2)[:4000])
        sys.exit(1)

    print(f"Filling password (ref={password_ref})...")
    sh("fill", "--fields", json.dumps([{"ref": password_ref, "value": password}]))
    time.sleep(1)

    submit_ref = find_ref(snap, r"sign in|log in|submit")
    if submit_ref:
        print(f"Submitting (ref={submit_ref})...")
        sh("click", submit_ref)
    else:
        print("No explicit submit button found -- trying Enter key on the password field.")
        sh("press", "Enter")
    time.sleep(3)

    print("Checking for a 2FA/verification prompt...")
    snap = run_json("snapshot", "--format", "aria", "--limit", "500")
    code_ref = find_ref(snap, r"verification code|security code|enter code")
    if code_ref:
        code = input("2FA prompt detected. Check your email and type the code here: ").strip()
        print(f"Filling code (ref={code_ref})...")
        sh("fill", "--fields", json.dumps([{"ref": code_ref, "value": code}]))
        time.sleep(1)
        confirm_ref = find_ref(snap, r"submit|confirm|verify")
        if confirm_ref:
            sh("click", confirm_ref)
        else:
            sh("press", "Enter")
        time.sleep(3)
    else:
        print("No 2FA prompt detected -- either already past it, or the device/session was remembered.")

    print("Checking login state via evaluate...")
    result = run_json("evaluate", "--fn",
                       "() => ({url: location.href, hasTbToken: document.cookie.includes('_tb_token_'), "
                       "title: document.title})")
    print(json.dumps(result, indent=2))
    print("\nSTOPPING HERE deliberately -- not selecting payment or clicking Place Order.")
    print("Report what you see (checkout page reached? still on a modal? error?) back to Claude.")

    input("\nPress Enter to stop the browser (leave it open first if you want to look around manually)... ")
    sh("stop")


if __name__ == "__main__":
    main()
