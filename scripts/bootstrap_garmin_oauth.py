"""One-shot OAuth 2.0 PKCE flow to seed Garmin tokens.

Flow:
  1. Generate a PKCE code_verifier and SHA-256 code_challenge.
  2. Open Garmin's authorization URL in your browser.
  3. Listen on the redirect_uri (default http://localhost:8080/callback) for
     Garmin's redirect carrying the authorization code.
  4. POST the code + code_verifier to Garmin's token endpoint; receive
     access_token + refresh_token.
  5. Fetch the persistent user_id (used to match incoming push notifications).
  6. Write all four to data/state.db.

Run once locally:
    uv run scripts/bootstrap_garmin_oauth.py
"""
import base64
import hashlib
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
from src.clients.garmin import fetch_user_id_from_api  # noqa: E402
from src.config import settings  # noqa: E402

AUTH_URL = "https://connect.garmin.com/oauth2Confirm"
TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"


def _pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge). Verifier is 43-128 chars per spec."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    received_code: str | None = None
    received_state: str | None = None

    def do_GET(self):  # noqa: N802 (stdlib API)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != urllib.parse.urlparse(settings.garmin_redirect_uri).path:
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.received_code = (params.get("code") or [None])[0]
        _CallbackHandler.received_state = (params.get("state") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;padding:2rem'>"
            b"<h2>Authorized.</h2><p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, format, *args):  # silence default access logs
        return


def _wait_for_callback(timeout_seconds: int = 300) -> str:
    parsed = urllib.parse.urlparse(settings.garmin_redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80
    with socketserver.TCPServer((host, port), _CallbackHandler) as server:
        server.timeout = 1
        deadline = time.time() + timeout_seconds
        while time.time() < deadline and _CallbackHandler.received_code is None:
            server.handle_request()
    if _CallbackHandler.received_code is None:
        raise RuntimeError(f"Timed out waiting for Garmin redirect after {timeout_seconds}s")
    return _CallbackHandler.received_code


def _exchange_code(code: str, code_verifier: str) -> dict:
    r = httpx.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "client_id": settings.garmin_client_id,
            "client_secret": settings.garmin_client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": settings.garmin_redirect_uri,
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Token exchange failed: HTTP {r.status_code}\n{r.text}")
    return r.json()


def main() -> int:
    if not settings.garmin_client_id or not settings.garmin_client_secret:
        print("ERROR: GARMIN_CLIENT_ID and GARMIN_CLIENT_SECRET must be set in .env")
        print("       See SETUP.md section 5.")
        return 1

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": settings.garmin_client_id,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": settings.garmin_redirect_uri,
        "state": state,
    })

    print("=" * 64)
    print(" Garmin OAuth 2.0 PKCE bootstrap")
    print("=" * 64)
    print(f"\nRedirect URI:  {settings.garmin_redirect_uri}")
    print("(must match the value configured on your Garmin app in the developer portal)")
    print(f"\nOpening Garmin authorization URL in your browser:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print("Waiting for Garmin to redirect back (up to 5 minutes)...")
    code = _wait_for_callback()

    if _CallbackHandler.received_state != state:
        print(
            f"WARNING: state mismatch — expected {state!r}, "
            f"got {_CallbackHandler.received_state!r}. Aborting."
        )
        return 1

    print("Received authorization code. Exchanging for tokens...")
    tokens = _exchange_code(code, verifier)
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
    expires_in = int(tokens["expires_in"])

    print("Fetching your Garmin user_id...")
    user_id = fetch_user_id_from_api(access_token)

    db.save_tokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=int(time.time()) + expires_in,
        user_id=user_id,
    )

    print("\nTokens written to data/state.db")
    print(f"  user_id:             {user_id}")
    print(f"  access_token TTL:    {expires_in}s (~{expires_in // 3600}h)")
    print(f"  refresh_token TTL:   ~{tokens.get('refresh_token_expires_in', 7776000) // 86400}d")
    print(
        "\nNext: after deploying to Fly, upload data/state.db to /data/state.db "
        "on the Fly volume (see SETUP.md step 7.5)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
