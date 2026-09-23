#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp",
#     "httpx2",
# ]
# ///
"""Shared DSers MCP OAuth 2.1 + PKCE client plumbing for Branch 11's
standalone (no-LLM) scripts.

Factored out 2026-08-30 from branch11_dsers_search.py when a second script
(branch11_dsers_mapping.py) needed the exact same connection logic --
same DSers MCP server (https://mcp.dsers.com/dropshipping/mcp), same cached
token file, same one-time-browser-approval flow. See
branch11_dsers_search.py's own docstring / memory
incident-2026-08-30-branch11-rate-limit for the full "why a standalone MCP
client instead of `claude -p`" reasoning -- this module only holds the
connection plumbing both scripts share, not any per-tool business logic.

One-time setup (only needed once total, shared by every script using this
module): the first run needs a human to approve DSers access in a browser
reaching http://localhost:<CALLBACK_PORT>/callback on this machine (SSH-
tunnel that port first if headless). After that, branch11_dsers_mcp_tokens.json
(mode 600) keeps every script using this module authenticated unattended.

Usage:
    from branch11_dsers_mcp_client import dsers_session, extract_items, is_error

    async with dsers_session() as session:
        result = await session.call_tool("dsers_find_product", {"keyword": "..."})
        if not is_error(result):
            items = extract_items(result)
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import smtplib
import socketserver
import sys
import threading
import time
import webbrowser
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken

DSERS_MCP_URL = "https://mcp.dsers.com/dropshipping/mcp"
DSERS_OAUTH_METADATA_URL = "https://mcp.dsers.com/.well-known/oauth-authorization-server"
SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_FILE = SCRIPT_DIR / "branch11_dsers_mcp_tokens.json"
# Cross-process mutex for the whole dsers_session() lifetime (2026-09-04, see
# incident-2026-09-04-branch11-dsers-token-race / PRODUCTION-READINESS.md):
# branch11-daily-item-research (8am), branch11-dsers-mapping (every ~30min),
# branch11-price-check (1pm), and branch11-enrichment (noon) are four
# independent cron processes that all share TOKEN_FILE, but each only had
# its OWN per-job flock (e.g. /tmp/branch11-dsers-mapping.lock) preventing
# self-overlap -- nothing prevented two DIFFERENT jobs from calling DSers's
# refresh endpoint at the same moment. FileTokenStorage's read/write lock
# only covers the brief disk I/O, not the "check expiry -> call DSers ->
# write result" round trip in between, so two concurrent refreshes could
# both succeed against DSers but only one's rotated token wins the disk
# write -- leaving a token on disk that DSers itself no longer honors.
# Confirmed as the real trigger: an off-schedule manual catch-up run of
# branch11_daily_run.sh wrote a fresh token at 2026-09-03T14:18:51, two
# seconds after branch11-dsers-mapping's regular tick at 14:18:49 -- the
# very next access-token expiry (6h later, 20:18) found the persisted
# refresh_token already dead, and every attempt since failed the same way.
# Acquiring this lock for a session's full duration (not just the refresh)
# also incidentally serializes CALLBACK_PORT usage below.
OAUTH_LOCK_FILE = SCRIPT_DIR / "branch11_dsers_oauth.lock"
CALLBACK_PORT = 8734

# Alerting for a dead refresh_token (2026-09-04, same incident as the lock
# above): the OAUTH_LOCK_FILE fix closes the concurrent-refresh race that
# caused this specific incident, but DSers could still kill the token for
# an unrelated reason someday (manual revocation, a policy expiry, an
# incident on their side) -- and when that happens, the only signal today
# is silent cron failures piling up in `openclaw cron list` and repeated
# "still blocked" lines in PRODUCTION-READINESS.md. Nothing pings Travis
# directly, which is exactly why this specific incident sat broken a full
# day before he noticed listings had stopped. _send_auth_alert_email()
# below fires the moment any script falls through to the interactive-auth
# path, reusing the same Gmail creds branch11_email_report.py already uses.
ALERT_CREDS_PATH = SCRIPT_DIR / "branch11_unattended_creds.json"
ALERT_TO = "tray14@hotmail.com"
ALERT_SMTP_HOST = "smtp.gmail.com"
ALERT_SMTP_PORT = 587
# Dedup marker: records which dead token generation we've already alerted
# on, keyed by the file's own tokens_issued_at (present even for a dead
# token -- it's just the timestamp of whenever it was last successfully
# written). All four DSers-touching cron jobs share this file and OAUTH_LOCK_FILE
# already fully serializes them, so there's no concurrent-write race on this
# marker either -- only one process can ever be in the alert path at a time.
AUTH_ALERT_STATE_FILE = SCRIPT_DIR / "branch11_dsers_auth_alert_state.json"
CALLBACK_TIMEOUT_S = 600  # 10 min -- generous for a human to complete a one-time login
_REFRESH_RETRY_ATTEMPTS = 3  # total attempts (1 original + up to 2 retries) on a transient 5xx
_REFRESH_RETRY_DELAY_S = 3  # linear backoff base (3s, 6s) -- kept short, this runs inside a daily cron


class FileTokenStorage(TokenStorage):
    """Persists OAuth tokens + client registration across runs so only the
    very first run (across ALL scripts using this module) ever needs a
    human/browser.

    Every read re-reads the file fresh from disk (shared lock) and every
    write re-reads-under-an-exclusive-lock-then-merges, rather than trusting
    an in-memory snapshot taken at construction. Fixed 2026-09-03
    (PRODUCTION-READINESS.md Item 45): several independent scripts
    (dsers-mapping/enrichment/price-check/dsers-search/etc, each its own
    process, several on their own 30-min/daily cron schedules) share this
    one file, and DSers's refresh tokens are single-use/rotating. The old
    design read the file once at construction and wrote the whole in-memory
    dict back on every save -- if process A refreshed and rotated the
    refresh_token while process B was still alive holding its own
    now-stale copy, B's next save (for any reason, even an unrelated
    client_info write) clobbered A's freshly-rotated token with the old,
    already-consumed one. Confirmed live: DSers rejected a refresh with
    `invalid_grant`/"already rotated" after a day of heavy concurrent usage
    (several manual verification runs stacked on top of the regular
    crons) -- exactly the symptom this race would produce. This doesn't
    close every possible race (two processes refreshing at the exact same
    instant can still both reach DSers before either write lands), but it
    closes the much larger, easily-hit window where a plain stale in-memory
    write clobbers a concurrent process's good token."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with open(self.path, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                content = f.read()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return json.loads(content) if content else {}

    def _write(self, mutate) -> None:
        self.path.touch(exist_ok=True)
        with open(self.path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                content = f.read()
                data = json.loads(content) if content else {}
                mutate(data)
                f.seek(0)
                f.write(json.dumps(data, indent=2))
                f.truncate()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        os.chmod(self.path, 0o600)

    async def get_tokens(self) -> OAuthToken | None:
        data = self._read()
        raw = data.get("tokens")
        if not raw:
            return None
        token = OAuthToken.model_validate(raw)
        # Fixed 2026-08-31 (PRODUCTION-READINESS.md Item 34): correct
        # expires_in for real elapsed time since issuance, not the original
        # value from whenever it was first written to disk -- see
        # _RefreshAwareOAuthClientProvider below for why this matters.
        issued_at = data.get("tokens_issued_at")
        if issued_at is not None and token.expires_in is not None:
            remaining = token.expires_in - (time.time() - issued_at)
            token.expires_in = max(0, int(remaining))
        return token

    async def set_tokens(self, tokens: OAuthToken) -> None:
        def mutate(data: dict[str, Any]) -> None:
            data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
            data["tokens_issued_at"] = time.time()
        self._write(mutate)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        def mutate(data: dict[str, Any]) -> None:
            data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(mutate)


class _CallbackHandler(BaseHTTPRequestHandler):
    def __init__(self, request: Any, client_address: tuple[str, int], server: socketserver.BaseServer, callback_data: dict[str, Any]):
        self.callback_data = callback_data
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:  # noqa: N802 -- stdlib method name
        query = parse_qs(urlparse(self.path).query)
        if "code" in query:
            self.callback_data["code"] = query["code"][0]
            self.callback_data["state"] = query.get("state", [None])[0]
            self.callback_data["iss"] = query.get("iss", [None])[0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h3>Authorized.</h3><p>You can close this tab and go back to the terminal.</p></body></html>")
        elif "error" in query:
            self.callback_data["error"] = query["error"][0]
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(f"<html><body><h3>Authorization failed: {query['error'][0]}</h3></body></html>".encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- stdlib signature
        pass


class CallbackServer:
    def __init__(self, port: int):
        self.port = port
        self.server: HTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.callback_data: dict[str, Any] = {"code": None, "state": None, "iss": None, "error": None}

    def start(self) -> None:
        callback_data = self.callback_data

        class Handler(_CallbackHandler):
            def __init__(self, request: Any, client_address: tuple[str, int], server: socketserver.BaseServer):
                super().__init__(request, client_address, server, callback_data)

        self.server = HTTPServer(("localhost", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=1)

    def wait_for_callback(self, timeout: int) -> str:
        start = time.time()
        while time.time() - start < timeout:
            if self.callback_data["code"]:
                return self.callback_data["code"]
            if self.callback_data["error"]:
                raise RuntimeError(f"DSers authorization failed: {self.callback_data['error']}")
            time.sleep(0.1)
        raise TimeoutError(f"Timed out after {timeout}s waiting for the DSers OAuth redirect.")


class _RefreshAwareOAuthClientProvider(OAuthClientProvider):
    """Works around a gap in the MCP SDK's OAuthClientProvider (confirmed
    2026-08-31 against the vendored `mcp` package, see PRODUCTION-READINESS.md
    Item 34): `_initialize()` loads persisted tokens from storage but never
    calls `context.update_token_expiry()` afterward, so `is_token_valid()`
    treats any file-loaded token as valid forever (its `token_expiry_time`
    stays None). That skips the preemptive refresh-token-grant path entirely
    -- expiry is only discovered via a real 401 from the server, and the
    401 handler goes straight into a *full interactive* authorization-code
    flow (browser + localhost callback) instead of trying the refresh token
    first. Under a headless cron with no human present, that hangs/errors
    even though a perfectly good refresh_token is sitting right there.

    Re-registering the (already elapsed-time-corrected, see
    FileTokenStorage.get_tokens) expiry immediately after load makes
    is_token_valid() correctly report expired/near-expired on process
    start, so the SDK's own working preemptive-refresh branch fires and
    silently mints a fresh access token before any request ever goes out.

    Second gap found while testing the above (2026-08-31): the preemptive
    refresh path builds its token-endpoint URL via `_get_token_endpoint()`,
    which falls back to `urljoin(server_base_url, "/token")` whenever
    `context.oauth_metadata` hasn't been discovered yet -- true here, since
    metadata discovery normally only happens inside the interactive 401
    flow this override is specifically trying to avoid. That guess is wrong
    for DSers: confirmed live that DSers's real token endpoint is
    `https://mcp.dsers.com/oauth/token` (per its
    `/.well-known/oauth-authorization-server` document), not
    `https://mcp.dsers.com/token` -- the guessed URL 404s. So this override
    also eagerly fetches and installs the real metadata before returning."""

    async def _initialize(self) -> None:
        await super()._initialize()
        if self.context.current_tokens is not None:
            self.context.update_token_expiry(self.context.current_tokens)
        if self.context.oauth_metadata is None:
            try:
                async with httpx2.AsyncClient(timeout=15.0) as client:
                    response = await client.get(DSERS_OAUTH_METADATA_URL)
                    response.raise_for_status()
                    self.context.oauth_metadata = OAuthMetadata.model_validate_json(response.content)
            except Exception as exc:  # noqa: BLE001 -- best-effort prefetch; interactive flow's own
                # discovery (which tries multiple candidate URLs) still runs as a fallback if this
                # single direct fetch fails for any reason.
                print(f"(non-fatal: eager OAuth metadata prefetch failed: {exc})", file=sys.stderr)

    async def _handle_refresh_response(self, response: httpx2.Response) -> bool:
        """Retries a transient (5xx) token-refresh failure a few times before
        falling back to the base SDK's behavior. Confirmed live 2026-09-03
        (PRODUCTION-READINESS.md, an 8am cron run): DSers's token endpoint
        returned a bare 503 for one refresh attempt. The base class treats
        ANY non-200 refresh response as fatal -- it discards a perfectly
        good refresh_token and lets the next request go out unauthenticated,
        which draws a 401 and falls through to a full interactive
        authorization-code flow (browser + localhost callback). Under a
        headless cron nothing can complete that flow, so it just burns
        CALLBACK_TIMEOUT_S (600s) and fails the whole run -- for what was
        really just a momentary blip on DSers's side, not an actually-
        invalid token. Retrying the same refresh POST a few times first
        gives a transient failure a real chance to clear before paying that
        cost."""
        resp = response
        for attempt in range(1, _REFRESH_RETRY_ATTEMPTS):
            if resp.status_code < 500:
                break
            print(f"(token refresh got {resp.status_code}, retrying in {_REFRESH_RETRY_DELAY_S * attempt}s "
                  f"-- attempt {attempt + 1}/{_REFRESH_RETRY_ATTEMPTS})", file=sys.stderr)
            await asyncio.sleep(_REFRESH_RETRY_DELAY_S * attempt)
            refresh_request = await self._refresh_token()
            async with httpx2.AsyncClient(timeout=30.0) as client:
                resp = await client.send(refresh_request)
        return await super()._handle_refresh_response(resp)


def _dead_token_generation() -> str:
    """A cheap identifier for "which dead token are we currently failing
    against" -- the file's own tokens_issued_at if present, else a fixed
    sentinel for the very-first-ever-setup case (no token file at all)."""
    try:
        data = json.loads(TOKEN_FILE.read_text())
        return str(data.get("tokens_issued_at", "unknown"))
    except Exception:
        return "no-token-file"


def _send_auth_alert_email(authorization_url: str) -> None:
    """Best-effort, alerts-once-per-incident email to Travis the moment any
    script falls through to the interactive-auth path -- see the
    ALERT_*/AUTH_ALERT_STATE_FILE comment above for why this exists. Never
    raises: a failure here must never break the actual OAuth flow, same
    spirit as _initialize()'s own best-effort metadata prefetch above."""
    try:
        generation = _dead_token_generation()
        already_alerted = AUTH_ALERT_STATE_FILE.exists() and json.loads(AUTH_ALERT_STATE_FILE.read_text()).get("last_alerted_for") == generation
        if already_alerted:
            return
        if not ALERT_CREDS_PATH.exists():
            print(f"(non-fatal: can't send auth-dead alert email, {ALERT_CREDS_PATH} missing)", file=sys.stderr)
            return
        creds = json.loads(ALERT_CREDS_PATH.read_text())
        gmail = creds.get("gmail") or {}
        from_address, app_password = gmail.get("address"), gmail.get("app_password")
        if not from_address or not app_password:
            print("(non-fatal: can't send auth-dead alert email, no gmail creds on file)", file=sys.stderr)
            return

        body = (
            "Branch 11's DSers login has expired/been revoked -- every DSers-dependent "
            "script (daily item research, dsers-mapping, price-check, enrichment) is "
            "blocked until this is redone. Takes under a minute:\n\n"
            "1. On your OWN LAPTOP (not a terminal already SSH'd into node1 -- check the "
            "prompt shows your laptop's own hostname), open a terminal and run:\n"
            f"   ssh -L {CALLBACK_PORT}:localhost:{CALLBACK_PORT} travis@100.126.15.80\n"
            "   Leave that window open.\n\n"
            "2. On that SAME laptop, open this URL in a browser (single-use -- if it's "
            "gone stale by the time you get to it, ask Claude for a fresh one):\n"
            f"   {authorization_url}\n\n"
            "That's it -- the waiting script picks up the callback and mints a fresh "
            "token automatically, no further action needed."
        )
        msg = MIMEText(body, "plain")
        msg["Subject"] = "Branch 11: DSers login expired, needs your action"
        msg["From"] = from_address
        msg["To"] = ALERT_TO
        with smtplib.SMTP(ALERT_SMTP_HOST, ALERT_SMTP_PORT) as server:
            server.starttls()
            server.login(from_address, app_password)
            server.sendmail(from_address, [ALERT_TO], msg.as_string())

        AUTH_ALERT_STATE_FILE.write_text(json.dumps({"last_alerted_for": generation, "alerted_at": time.time()}))
        os.chmod(AUTH_ALERT_STATE_FILE, 0o600)
        print(f"(sent DSers auth-dead alert email to {ALERT_TO})", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 -- best-effort alert, must never break the real OAuth flow
        print(f"(non-fatal: failed to send auth-dead alert email: {exc})", file=sys.stderr)


async def _redirect_handler(authorization_url: str) -> None:
    print("\n=== DSers one-time authorization required ===", file=sys.stderr)
    print(f"Open this URL in a browser that can reach http://localhost:{CALLBACK_PORT}/callback on THIS machine:", file=sys.stderr)
    print(f"  {authorization_url}\n", file=sys.stderr)
    print(
        f"If this host is headless/remote, first run from your own machine:\n"
        f"  ssh -L {CALLBACK_PORT}:localhost:{CALLBACK_PORT} <this-host>\n"
        f"then open the URL above in your own local browser.\n",
        file=sys.stderr,
    )
    _send_auth_alert_email(authorization_url)
    try:
        webbrowser.open(authorization_url)
    except Exception:
        pass


def extract_items(result: Any) -> list[dict]:
    """Pulls a tool result's `items` array regardless of whether the server
    sent it as structuredContent or as a JSON text block -- both have been
    observed live across different DSers tools."""
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if isinstance(structured, dict) and isinstance(structured.get("items"), list):
        return structured["items"]
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                return parsed["items"]
    return []


def extract_json(result: Any) -> dict | None:
    """Like extract_items, but for tools whose payload isn't a simple
    {"items": [...]} shape (e.g. dsers_sku_remap's {diffs, summary, ...}) --
    returns the full parsed object instead of reaching into one key."""
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    return None


def is_error(result: Any) -> bool:
    val = getattr(result, "isError", None)
    if val is None:
        val = getattr(result, "is_error", False)
    return bool(val)


@contextlib.asynccontextmanager
async def dsers_session(client_name: str = "Branch 11 DSers (standalone)") -> AsyncIterator[ClientSession]:
    """Yields a ready, initialized ClientSession authenticated against the
    DSers MCP server -- handles OAuth (cached token reuse, or one-time
    browser approval on first-ever use) internally.

    Holds OAUTH_LOCK_FILE (see its own comment) for the entire session, so
    only one of the several independent branch11 cron processes can ever be
    touching DSers auth at a time -- released automatically (even on a
    crash/SIGKILL) since it's a plain flock tied to this process's own fd,
    not a lock-file-exists convention that could go stale."""
    lock_file = open(OAUTH_LOCK_FILE, "w")
    await asyncio.to_thread(fcntl.flock, lock_file, fcntl.LOCK_EX)
    try:
        async with _dsers_session_locked(client_name) as session:
            yield session
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


@contextlib.asynccontextmanager
async def _dsers_session_locked(client_name: str) -> AsyncIterator[ClientSession]:
    """The actual session setup, run only while dsers_session() holds
    OAUTH_LOCK_FILE -- split out so that lock's try/finally doesn't have to
    span this whole nested async-with chain by hand."""
    callback_server = CallbackServer(CALLBACK_PORT)
    callback_server.start()

    async def callback_handler() -> AuthorizationCodeResult:
        try:
            code = callback_server.wait_for_callback(CALLBACK_TIMEOUT_S)
            return AuthorizationCodeResult(code=code, state=callback_server.callback_data["state"], iss=callback_server.callback_data["iss"])
        finally:
            callback_server.stop()

    client_metadata = OAuthClientMetadata.model_validate(
        {
            "client_name": client_name,
            "redirect_uris": [f"http://localhost:{CALLBACK_PORT}/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }
    )

    oauth_auth = _RefreshAwareOAuthClientProvider(
        # NOTE: the MCP SDK's own example client strips a trailing "/mcp"
        # here, but DSers's server advertises its OAuth protected-resource
        # identifier as the exact full endpoint URL (confirmed live:
        # stripping it raises OAuthFlowError "Protected resource
        # https://mcp.dsers.com/dropshipping/mcp does not match expected
        # https://mcp.dsers.com/dropshipping") -- so pass the full URL
        # unmodified for this server.
        server_url=DSERS_MCP_URL,
        client_metadata=client_metadata,
        storage=FileTokenStorage(TOKEN_FILE),
        redirect_handler=_redirect_handler,
        callback_handler=callback_handler,
    )

    # Default httpx2 timeout is too tight for a remote MCP server doing a
    # real product search/mapping call per request -- gave a ReadTimeout on
    # the very first request when tested live against DSers (2026-08-30).
    async with httpx2.AsyncClient(auth=oauth_auth, follow_redirects=True, timeout=60.0) as http_client:
        async with streamable_http_client(url=DSERS_MCP_URL, http_client=http_client) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
