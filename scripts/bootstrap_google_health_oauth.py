"""One-shot OAuth 2.0 flow to seed Google Health API tokens.

Flow:
  1. Build a Google OAuth consent URL for the Google Health read scopes.
  2. Open the URL in your browser.
  3. Listen on the redirect_uri (default http://localhost:8080/google-health/callback)
     for Google's redirect carrying the authorization code.
  4. Exchange the code for access_token + refresh_token.
  5. Write the tokens to data/state.db.

Run once locally:
    uv run scripts/bootstrap_google_health_oauth.py
"""
import http.server
import secrets
import socketserver
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db  # noqa: E402
from src.config import settings  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
]


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    received_code: str | None = None
    received_state: str | None = None
    received_error: str | None = None

    def do_GET(self):  # noqa: N802 (stdlib API)
        parsed = urllib.parse.urlparse(self.path)
        expected_path = urllib.parse.urlparse(settings.google_health_redirect_uri).path
        if parsed.path != expected_path:
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.received_code = (params.get("code") or [None])[0]
        _CallbackHandler.received_state = (params.get("state") or [None])[0]
        _CallbackHandler.received_error = (params.get("error") or [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

        if _CallbackHandler.received_error:
            body = (
                "<html><body style='font-family:sans-serif;padding:2rem'>"
                "<h2>Authorization failed.</h2>"
                f"<p>Error: {_CallbackHandler.received_error}</p>"
                "<p>You can close this tab and return to the terminal.</p>"
                "</body></html>"
            )
        else:
            body = (
                "<html><body style='font-family:sans-serif;padding:2rem'>"
                "<h2>Authorized.</h2>"
                "<p>You can close this tab and return to the terminal.</p>"
                "</body></html>"
            )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):  # silence default access logs
        return


def _wait_for_callback(timeout_seconds: int = 300) -> str:
    _CallbackHandler.received_code = None
    _CallbackHandler.received_state = None
    _CallbackHandler.received_error = None

    parsed = urllib.parse.urlparse(settings.google_health_redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80

    with socketserver.TCPServer((host, port), _CallbackHandler) as server:
        server.timeout = 1
        deadline = time.time() + timeout_seconds
        while (
            time.time() < deadline
            and _CallbackHandler.received_code is None
            and _CallbackHandler.received_error is None
        ):
            server.handle_request()

    if _CallbackHandler.received_error:
        raise RuntimeError(f"Google authorization failed: {_CallbackHandler.received_error}")
    if _CallbackHandler.received_code is None:
        raise RuntimeError(f"Timed out waiting for Google redirect after {timeout_seconds}s")
    return _CallbackHandler.received_code


def _exchange_code(code: str) -> dict:
    response = httpx.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "client_id": settings.google_health_client_id,
            "client_secret": settings.google_health_client_secret,
            "code": code,
            "redirect_uri": settings.google_health_redirect_uri,
        },
        timeout=15,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Token exchange failed: HTTP {response.status_code}\n{response.text}")
    return response.json()


def main() -> int:
    if not settings.google_health_client_id or not settings.google_health_client_secret:
        print("ERROR: GOOGLE_HEALTH_CLIENT_ID and GOOGLE_HEALTH_CLIENT_SECRET must be set in .env")
        return 1

    state = secrets.token_urlsafe(24)
    scope = " ".join(SCOPES)
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": settings.google_health_client_id,
            "redirect_uri": settings.google_health_redirect_uri,
            "scope": scope,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )

    print("=" * 64)
    print(" Google Health API OAuth bootstrap")
    print("=" * 64)
    print(f"\nRedirect URI:  {settings.google_health_redirect_uri}")
    print("(must be listed on the OAuth client in Google Cloud)")
    print("\nRequested scopes:\n  " + "\n  ".join(SCOPES))
    print(f"\nOpening Google authorization URL in your browser:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print("Waiting for Google to redirect back (up to 5 minutes)...")
    code = _wait_for_callback()

    if _CallbackHandler.received_state != state:
        print(
            f"WARNING: state mismatch - expected {state!r}, "
            f"got {_CallbackHandler.received_state!r}. Aborting."
        )
        return 1

    print("Received authorization code. Exchanging for tokens...")
    tokens = _exchange_code(code)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("ERROR: Google did not return a refresh_token.")
        print("       Revoke the app permission from your Google Account, then re-run this script.")
        print("       The script already requests access_type=offline and prompt=consent.")
        return 1

    now = int(time.time())
    expires_in = int(tokens["expires_in"])
    refresh_token_expires_in = tokens.get("refresh_token_expires_in")
    refresh_token_expires_at = (
        now + int(refresh_token_expires_in) if refresh_token_expires_in is not None else None
    )

    db.save_google_health_tokens(
        access_token=tokens["access_token"],
        refresh_token=refresh_token,
        expires_at=now + expires_in,
        scopes=tokens.get("scope", scope),
        token_type=tokens.get("token_type", "Bearer"),
        refresh_token_expires_at=refresh_token_expires_at,
    )

    print("\nGoogle Health tokens written to data/state.db")
    print(f"  access_token TTL:          {expires_in}s (~{expires_in // 60}m)")
    if refresh_token_expires_in is not None:
        days = int(refresh_token_expires_in) // 86400
        print(f"  refresh_token TTL:         {days}d")
        print("  note: Testing-mode OAuth refresh tokens are time-limited.")
    else:
        print("  refresh_token TTL:         not reported")
    print("\nNext: run scripts/smoke_google_health.py, then deploy secrets/code and")
    print("      upload data/state.db to /data/state.db on the Fly volume.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
