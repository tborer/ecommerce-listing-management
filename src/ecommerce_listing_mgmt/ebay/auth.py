#!/usr/bin/env python3
"""eBay OAuth client for Branch 11 -- sandbox/production environment switching.

Design principle: sandbox and production credentials are never interchangeable
(eBay enforces this server-side -- a token minted in one environment simply
fails against the other's API, per developer.ebay.com's own docs). This module
makes the *current* environment explicit everywhere rather than implicit, so a
mistake shows up as a loud, obvious error instead of silently hitting the wrong
API. There is deliberately NO default environment -- every entry point requires
an explicit choice.

Credentials live in Bitwarden, not in this repo or any config file, following
the same write-only handling used for the AliExpress/eBay login items:
  - Item "eBay Sandbox API"    -- fields: app_id, cert_id, dev_id, runame, refresh_token
  - Item "eBay Production API" -- same fields

`refresh_token` starts empty on both items until Travis completes the one-time
OAuth consent per environment (see build_consent_url() / exchange_code()).
Ask Travis before using either item, every time, per his standing instruction
(see branch11-listing-rules.md Credential Handling) -- this module does not
enforce that itself, the calling code/session must.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ENVIRONMENTS = ("sandbox", "production")

_ENDPOINTS = {
    "sandbox": {
        "auth_base": "https://auth.sandbox.ebay.com/oauth2/authorize",
        "token_url": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "api_base": "https://api.sandbox.ebay.com",
        "bw_item": "eBay Sandbox API",
    },
    "production": {
        "auth_base": "https://auth.ebay.com/oauth2/authorize",
        "token_url": "https://api.ebay.com/identity/v1/oauth2/token",
        "api_base": "https://api.ebay.com",
        "bw_item": "eBay Production API",
    },
}

# Least-privilege for what's actually being built right now (2026-08-26):
# Steps 5/6 read shipping/account policies, Step 8 reads orders -- all
# read-only. Expand deliberately via a SEPARATE re-consent (not by just
# editing this list) when a write-capable step is actually being built:
#   Step 7  (create listings)      -> add sell.inventory (full, not .readonly)
#   Step 10 (write tracking info)  -> add sell.fulfillment (full, not .readonly)
# Re-consenting to add scope is a real, visible checkpoint for Travis to
# consciously approve broader access -- don't request ahead of need.
DEFAULT_SCOPES = [
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
]


def _require_env(env: str) -> dict:
    if env not in ENVIRONMENTS:
        raise ValueError(f"env must be one of {ENVIRONMENTS}, got {env!r} -- no default, be explicit")
    return _ENDPOINTS[env]


@dataclass
class EbayAppCredentials:
    env: str
    app_id: str
    cert_id: str
    dev_id: str
    runame: str
    refresh_token: str | None
    granted_scopes: list[str] | None


# Local, non-Bitwarden credential file for the unattended daily auto-listing
# path ONLY (branch11_pipeline.py's run_auto_listing()) -- Travis's explicit
# 2026-08-27 decision (see branch11-listing-rules.md Credential Handling).
# The unattended 8 AM cron has no live session to `bw unlock`, so it can't
# use the Bitwarden path below at all. Deliberately narrow: production eBay
# only, not a general secrets store -- Travis's own instruction was to keep
# this file limited to just eBay + (future) AliExpress creds needed for THIS
# automation, kept separate from all other password/credential management.
# Mode 600, travis-only -- still a real, standing secret on disk, a
# materially different risk than the interactive Bitwarden-only pattern used
# everywhere else in this project; accepted deliberately, not a default.
LOCAL_CREDS_PATH = Path(__file__).parent / "branch11_unattended_creds.json"


def load_local_credentials(env: str) -> EbayAppCredentials:
    """Loads eBay credentials for the unattended path from LOCAL_CREDS_PATH
    instead of Bitwarden -- see refresh_access_token_unattended() for why
    this exists. Originally production-only ("sandbox/interactive testing
    has no unattended need") -- extended 2026-09-04 to also support
    'sandbox' (PRODUCTION-READINESS.md Item 25 Phase 3 sandbox mechanism
    testing needed to run unattended, e.g. from an isolated cron/agent
    session with no live BW_SESSION). Sandbox carries zero real
    money/inventory risk, unlike production's standing write-capable
    secret -- extending this already-accepted pattern to a strictly
    lower-stakes environment doesn't change the real risk posture Travis
    already accepted for production. Bootstrapped the same one-time way:
    copying the current Bitwarden 'eBay Sandbox API' item's fields into
    ebay.sandbox (Travis explicitly approved this specific bootstrap)."""
    if env not in ("production", "sandbox"):
        raise ValueError(f"load_local_credentials only supports 'production'/'sandbox', got {env!r}")
    if not LOCAL_CREDS_PATH.exists():
        raise RuntimeError(f"{LOCAL_CREDS_PATH} does not exist -- run the one-time bootstrap step first "
                            f"(copy the current Bitwarden 'eBay {env.capitalize()} API' item's fields into it)")
    data = json.loads(LOCAL_CREDS_PATH.read_text())
    fields = data.get("ebay", {}).get(env)
    if not fields:
        raise RuntimeError(f"{LOCAL_CREDS_PATH} has no ebay.{env} entry")
    missing = [k for k in ("app_id", "cert_id", "dev_id", "runame", "refresh_token") if not fields.get(k)]
    if missing:
        raise RuntimeError(f"{LOCAL_CREDS_PATH}'s ebay.{env} entry is missing field(s) {missing}")
    granted = fields.get("granted_scopes")
    return EbayAppCredentials(
        env=env, app_id=fields["app_id"], cert_id=fields["cert_id"],
        dev_id=fields["dev_id"], runame=fields["runame"],
        refresh_token=fields["refresh_token"],
        granted_scopes=granted.split() if isinstance(granted, str) else (granted or None),
    )


_UNATTENDED_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def refresh_access_token_unattended(env: str) -> dict:
    """Non-interactive token refresh for the unattended auto-listing path
    ONLY -- sources credentials from LOCAL_CREDS_PATH, not Bitwarden. Every
    other function in this module (build_consent_url/exchange_code/
    refresh_access_token) stays Bitwarden-backed and interactive, unchanged --
    this is a deliberately separate, parallel path, not a replacement, so
    it's always obvious from the call site which credential source is in use.
    Does not write anything back (a plain refresh doesn't rotate the refresh
    token) -- LOCAL_CREDS_PATH is only ever written by the one-time bootstrap
    step, never by this function.

    Caches the token in-process per env for the rest of its real lifetime
    minus a 60s safety margin (2026-08-30, Travis's decision, see memory
    incident-2026-08-30-branch11-rate-limit): branch11_pipeline.py's
    annotate_category_analysis() calls this once per profit-passing
    candidate -- 100+ some days post the 16%-margin change -- and every call
    was doing a real token-endpoint POST with no reuse at all. The cache is
    a plain module-level dict, so it only ever lives for one process's
    lifetime -- a fresh cron run still refreshes once at the start, same as
    before this change.
    """
    cached = _UNATTENDED_TOKEN_CACHE.get(env)
    if cached and cached[1] > time.time():
        return {"access_token": cached[0]}
    creds = load_local_credentials(env)
    endpoints = _ENDPOINTS[env]
    effective_scopes = creds.granted_scopes or DEFAULT_SCOPES
    print(f"[branch11_ebay_auth] Refreshing access token against {env.upper()} (unattended/local-file credential path)")
    result = _post_token_request(endpoints, creds, {
        "grant_type": "refresh_token",
        "refresh_token": creds.refresh_token,
        "scope": " ".join(effective_scopes),
    })
    expires_in = result.get("expires_in") or 1800  # conservative fallback if eBay ever omits it
    _UNATTENDED_TOKEN_CACHE[env] = (result["access_token"], time.time() + max(expires_in - 60, 60))
    return result


# Application (client-credentials) token for the public-data Buy APIs --
# Browse, Deal, Taxonomy (2026-09-23, web-app-plan.md §4a step 1: moving
# eBay discovery off page scraping). Different animal from every user token
# above: minted from app_id/cert_id alone, no seller consent, no refresh
# token involved, and the only scope is the base public-data one -- it can
# read public listings/deals/categories and nothing else (no account,
# inventory, or order access). So it's safe to use from the unattended path.
APPLICATION_SCOPE = "https://api.ebay.com/oauth/api_scope"

_APPLICATION_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def load_env_credentials(env: str) -> EbayAppCredentials:
    """App keys from environment variables -- the web app API's source
    (serverless: no Bitwarden, no local file). EBAY_APP_ID / EBAY_CERT_ID /
    EBAY_DEV_ID / EBAY_RUNAME. There is no refresh token here: in the web
    app each user's own refresh token lives encrypted in the database."""
    _require_env(env)
    fields = {k: os.environ.get(f"EBAY_{k.upper()}", "") for k in ("app_id", "cert_id", "dev_id", "runame")}
    missing = [f"EBAY_{k.upper()}" for k in ("app_id", "cert_id") if not fields[k]]
    if missing:
        raise RuntimeError(f"missing environment variable(s) {missing}")
    return EbayAppCredentials(env=env, refresh_token=None, granted_scopes=None, **fields)


def get_application_token(env: str, credential_mode: str = "local") -> str:
    """Client-credentials access token for `env`, cached in-process for its
    lifetime minus 60s (same caching reasoning as
    refresh_access_token_unattended()). `credential_mode` picks where the
    app_id/cert_id come from: "local" (branch11_unattended_creds.json) or
    "bitwarden" -- only the app keys are read either way."""
    endpoints = _require_env(env)
    cached = _APPLICATION_TOKEN_CACHE.get(env)
    if cached and cached[1] > time.time():
        return cached[0]
    if credential_mode == "env":
        creds = load_env_credentials(env)
    elif credential_mode == "local":
        creds = load_local_credentials(env)
    else:
        creds = load_credentials(env)
    result = _post_token_request(endpoints, creds, {
        "grant_type": "client_credentials",
        "scope": APPLICATION_SCOPE,
    })
    expires_in = result.get("expires_in") or 1800
    _APPLICATION_TOKEN_CACHE[env] = (result["access_token"], time.time() + max(expires_in - 60, 60))
    return result["access_token"]


def _post_token_request(endpoints: dict, creds: EbayAppCredentials, extra_params: dict) -> dict:
    basic_auth = base64.b64encode(f"{creds.app_id}:{creds.cert_id}".encode()).decode()
    data = urllib.parse.urlencode(extra_params).encode()
    req = urllib.request.Request(
        endpoints["token_url"], data=data, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic_auth}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _bw_get_item(item_name: str) -> dict:
    """Fetch a Bitwarden item by name. Requires BW_SESSION already exported
    in this process's environment -- does not manage unlocking itself."""
    proc = subprocess.run(
        ["bw", "get", "item", item_name], capture_output=True, text=True, check=True
    )
    return json.loads(proc.stdout)


def load_credentials(env: str) -> EbayAppCredentials:
    """Load the App ID/Cert ID/Dev ID/RuName/refresh_token/granted_scopes for
    `env` from Bitwarden. Raises clearly if the vault item or a required field
    is missing, rather than silently proceeding with partial credentials.
    """
    endpoints = _require_env(env)
    item = _bw_get_item(endpoints["bw_item"])
    fields = {f["name"]: f.get("value") for f in item.get("fields", [])}
    missing = [k for k in ("app_id", "cert_id", "dev_id", "runame") if not fields.get(k)]
    if missing:
        raise RuntimeError(
            f"Bitwarden item {endpoints['bw_item']!r} is missing field(s) {missing} "
            f"-- expected custom fields app_id, cert_id, dev_id, runame, refresh_token, granted_scopes"
        )
    granted = fields.get("granted_scopes")
    return EbayAppCredentials(
        env=env, app_id=fields["app_id"], cert_id=fields["cert_id"],
        dev_id=fields["dev_id"], runame=fields["runame"],
        refresh_token=fields.get("refresh_token") or None,
        granted_scopes=granted.split() if granted else None,
    )


def _save_tokens(env: str, refresh_token: str, scopes: list[str]) -> None:
    """Write refresh_token + granted_scopes back to the environment's Bitwarden
    item, in one edit. Centralized here so every call site stays consistent --
    previously this was hand-written inline each time, error-prone (it once
    saved a refresh_token without recording which scopes it was actually
    granted with, causing refresh_access_token to guess wrong -- see STATUS.md
    2026-08-26 for the bug this caused).
    """
    endpoints = _ENDPOINTS[env]
    item = _bw_get_item(endpoints["bw_item"])
    fields = item.get("fields", [])
    updates = {"refresh_token": refresh_token, "granted_scopes": " ".join(scopes)}
    for name, value in updates.items():
        for f in fields:
            if f.get("name") == name:
                f["value"] = value
                break
        else:
            fields.append({"name": name, "value": value, "type": 0})
    item["fields"] = fields
    encoded = subprocess.run(
        ["bw", "encode"], input=json.dumps(item), capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["bw", "edit", "item", item["id"], encoded], capture_output=True, text=True, check=True)
    print(f"[branch11_ebay_auth] Saved refresh_token + granted_scopes to "
          f"'{endpoints['bw_item']}' ({len(scopes)} scope(s))")


def build_consent_url(env: str, scopes: list[str] | None = None, state: str | None = None) -> str:
    """Build the URL Travis visits himself, in his own browser, to grant consent.

    This is the one-time human step -- never automate visiting this URL. Pass
    the SAME `scopes` list to exchange_code() afterward so the granted scope
    gets recorded correctly (see _save_tokens's docstring for why this matters).
    """
    creds = load_credentials(env)
    endpoints = _ENDPOINTS[env]
    params = {
        "client_id": creds.app_id,
        "redirect_uri": creds.runame,
        "response_type": "code",
        "scope": " ".join(scopes or DEFAULT_SCOPES),
    }
    if state:
        params["state"] = state
    print(f"[branch11_ebay_auth] Building {env.upper()} consent URL -- "
          f"Travis must open this in his own browser and consent:")
    return f"{endpoints['auth_base']}?{urllib.parse.urlencode(params)}"


def exchange_code(env: str, authorization_code: str, scopes: list[str] | None = None) -> dict:
    """Exchange a one-time authorization code (from the consent redirect) for
    an access token + refresh token, and save both the refresh token AND the
    granted scopes to Bitwarden. `scopes` must match what was actually passed
    to build_consent_url() for this same consent -- defaults to DEFAULT_SCOPES,
    same as build_consent_url's own default, so they stay in sync if neither
    is overridden."""
    creds = load_credentials(env)
    endpoints = _ENDPOINTS[env]
    used_scopes = scopes or DEFAULT_SCOPES
    print(f"[branch11_ebay_auth] Exchanging code for tokens against {env.upper()} "
          f"({endpoints['token_url']})")
    result = _token_request(env, {
        "grant_type": "authorization_code",
        "code": authorization_code,
        "redirect_uri": creds.runame,
    })
    if "refresh_token" in result:
        _save_tokens(env, result["refresh_token"], used_scopes)
    return result


def refresh_access_token(env: str, scopes: list[str] | None = None) -> dict:
    """Use the stored refresh token to mint a new ~2h access token. This is
    the normal path for every real API call -- authorization_code (above) only
    happens once per environment, at initial consent.

    Defaults to whatever scope was actually granted at consent time (stored
    alongside the refresh token) -- NOT to DEFAULT_SCOPES, which may differ
    from what this particular refresh token was actually issued for (a 400
    from eBay is the symptom if these mismatch; see STATUS.md 2026-08-26).
    """
    creds = load_credentials(env)
    if not creds.refresh_token:
        raise RuntimeError(
            f"No refresh_token stored for {env} yet -- run the one-time consent "
            f"flow first (build_consent_url + exchange_code)."
        )
    effective_scopes = scopes or creds.granted_scopes or DEFAULT_SCOPES
    print(f"[branch11_ebay_auth] Refreshing access token against {env.upper()}")
    return _token_request(env, {
        "grant_type": "refresh_token",
        "refresh_token": creds.refresh_token,
        "scope": " ".join(effective_scopes),
    })


def _token_request(env: str, extra_params: dict) -> dict:
    creds = load_credentials(env)
    return _post_token_request(_ENDPOINTS[env], creds, extra_params)


def api_base(env: str) -> str:
    """The REST API base URL for `env` -- use this, never hardcode api.ebay.com,
    so a call can't silently target the wrong environment."""
    return _require_env(env)["api_base"]


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2 or sys.argv[1] not in ("consent-url", "status"):
        print("Usage:")
        print("  python3 branch11_ebay_auth.py consent-url <sandbox|production>")
        print("  python3 branch11_ebay_auth.py status <sandbox|production>")
        sys.exit(1)

    cmd, env = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None
    if not env or env not in ENVIRONMENTS:
        print(f"Second argument must be one of {ENVIRONMENTS}")
        sys.exit(1)

    if cmd == "consent-url":
        print(build_consent_url(env))
    elif cmd == "status":
        try:
            creds = load_credentials(env)
            print(f"{env}: app_id={creds.app_id[:8]}... "
                  f"refresh_token={'set' if creds.refresh_token else 'NOT SET'} "
                  f"granted_scopes={creds.granted_scopes or 'NOT SET'}")
        except Exception as e:
            print(f"{env}: {e}")
