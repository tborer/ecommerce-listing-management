#!/usr/bin/env python3
"""eBay Marketplace Account Deletion (MAD) notification endpoint.

Required by eBay before production API keys activate (GDPR/CCPA-style
compliance: eBay must be able to tell every third-party app when a user
deletes their eBay account, so the app can delete any retained data about
that user). This doesn't do anything with the notifications beyond logging
them and alerting Travis -- there's no automated deletion logic here, because
nothing in Branch 11 currently retains long-term personal data about eBay
users other than what's already in normal order-processing flow (see
branch11-listing-rules.md Hard Constraints).

Runs as a systemd --user service, listening on 127.0.0.1 only. Tailscale
serve/funnel handles the public HTTPS termination and proxies to this port --
see branch11-listing-rules.md's "Marketplace Account Deletion webhook" section
for the full Tailscale setup and why port 8443 (not 443) was chosen.

Verification token and the exact registered endpoint URL live in a local
config file (`ebay_mad_webhook_config.json`, mode 600), NOT Bitwarden, despite
every other credential in this project going through Bitwarden -- deliberate
exception: this service must auto-start on boot via systemd with no human
present to run `bw unlock`, and this specific secret is low-sensitivity (a
shared verification string, not an account/financial credential -- worst case
if leaked, someone could send fake log entries, nothing more). A Bitwarden
copy ("eBay MAD Webhook" item) is kept too, as a human-readable backup/
reference, but the running service never depends on it. eBay's own form also
has a "notify if endpoint is down" email field as a built-in fallback, which
further reduces the risk of this design choice.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LOG_PATH = Path(__file__).parent / "ebay_deletion_notifications.jsonl"
CONFIG_PATH = Path(__file__).parent / "ebay_mad_webhook_config.json"
PORT = 8791


def _load_config() -> tuple[str, str]:
    """Load verification_token + endpoint_url from the local config file."""
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"{CONFIG_PATH} does not exist -- create it with "
            f'{{"verification_token": "...", "endpoint_url": "..."}}, mode 600'
        )
    config = json.loads(CONFIG_PATH.read_text())
    token, url = config.get("verification_token"), config.get("endpoint_url")
    if not token or not url:
        raise RuntimeError(f"{CONFIG_PATH} missing verification_token/endpoint_url")
    return token, url


def _log_event(kind: str, detail: dict) -> None:
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "kind": kind, **detail}
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[{entry['timestamp']}] {kind}: {json.dumps(detail)[:300]}", flush=True)


class Handler(BaseHTTPRequestHandler):
    verification_token: str = ""
    endpoint_url: str = ""

    def log_message(self, format, *args):
        pass  # suppress default stderr access logging; _log_event covers it

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        challenge_code = qs.get("challenge_code", [None])[0]

        if not challenge_code:
            _log_event("get_no_challenge", {"path": self.path})
            self.send_response(400)
            self.end_headers()
            return

        # eBay's exact spec: sha256(challengeCode + verificationToken + endpointURL), hex-encoded.
        digest = hashlib.sha256(
            (challenge_code + self.verification_token + self.endpoint_url).encode()
        ).hexdigest()
        body = json.dumps({"challengeResponse": digest}).encode()

        _log_event("challenge_verified", {"challenge_code": challenge_code})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw.decode(errors="replace")}

        _log_event("deletion_notification", {"payload": payload})

        # Acknowledge quickly -- eBay retries if it doesn't get a prompt 200.
        self.send_response(200)
        self.end_headers()


def main() -> None:
    token, url = _load_config()
    Handler.verification_token = token
    Handler.endpoint_url = url
    print(f"eBay MAD webhook listening on 127.0.0.1:{PORT}, endpoint_url={url}", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
