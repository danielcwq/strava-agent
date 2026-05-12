"""Garmin Health API client with OAuth 2.0 PKCE token refresh.

Access tokens last 24h; refresh tokens last ~90d. Garmin issues a new refresh
token on every refresh, so we persist both each time.
"""
import time

import httpx

from src import db
from src.config import settings

TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
API_BASE = "https://apis.garmin.com/wellness-api/rest"

# Refresh 10 minutes before expiry per Garmin's recommendation.
REFRESH_LEEWAY_SECONDS = 600


class GarminAuthError(RuntimeError):
    pass


def _post_refresh(refresh_token: str) -> dict:
    if not settings.garmin_client_id or not settings.garmin_client_secret:
        raise GarminAuthError("GARMIN_CLIENT_ID/SECRET not set in env")
    r = httpx.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "client_id": settings.garmin_client_id,
            "client_secret": settings.garmin_client_secret,
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise GarminAuthError(f"refresh failed: {r.status_code} {r.text}")
    return r.json()


def get_access_token() -> str:
    """Return a valid access token, refreshing and persisting if near expiry."""
    tokens = db.get_tokens()
    if not tokens:
        raise GarminAuthError(
            "no Garmin tokens in db — run scripts/bootstrap_garmin_oauth.py first"
        )

    now = int(time.time())
    if tokens["expires_at"] - now > REFRESH_LEEWAY_SECONDS:
        return tokens["access_token"]

    fresh = _post_refresh(tokens["refresh_token"])
    db.save_tokens(
        access_token=fresh["access_token"],
        refresh_token=fresh["refresh_token"],
        expires_at=now + int(fresh["expires_in"]),
        user_id=tokens["user_id"],
    )
    return fresh["access_token"]


def get_user_id() -> str:
    tokens = db.get_tokens()
    if not tokens:
        raise GarminAuthError("no Garmin tokens in db")
    return tokens["user_id"]


def fetch_user_id_from_api(access_token: str) -> str:
    """Used during bootstrap before tokens have been persisted."""
    r = httpx.get(
        f"{API_BASE}/user/id",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["userId"]
