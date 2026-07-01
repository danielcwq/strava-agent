"""Google Health API client for Fitbit / Google Health data.

Tokens are seeded by scripts/bootstrap_google_health_oauth.py and stored in
SQLite. The client refreshes access tokens on demand.
"""
import time
from typing import Any

import httpx

from src import db
from src.config import settings

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://health.googleapis.com/v4"

REFRESH_LEEWAY_SECONDS = 300


class GoogleHealthAuthError(RuntimeError):
    pass


class GoogleHealthTokenExpired(GoogleHealthAuthError):
    pass


class GoogleHealthApiError(RuntimeError):
    pass


def _token_expired_message() -> str:
    return (
        "Google Health refresh token expired or was revoked; rerun "
        "scripts/bootstrap_google_health_oauth.py and upload data/state.db to Fly"
    )


def _post_refresh(refresh_token: str) -> dict:
    if not settings.google_health_client_id or not settings.google_health_client_secret:
        raise GoogleHealthAuthError("GOOGLE_HEALTH_CLIENT_ID/SECRET not set in env")

    response = httpx.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "client_id": settings.google_health_client_id,
            "client_secret": settings.google_health_client_secret,
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    if response.status_code != 200:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if body.get("error") == "invalid_grant":
            raise GoogleHealthTokenExpired(_token_expired_message())
        error = body.get("error") or f"HTTP {response.status_code}"
        raise GoogleHealthAuthError(f"Google Health token refresh failed: {error}")
    return response.json()


def get_access_token() -> str:
    """Return a valid Google Health API access token, refreshing if needed."""
    tokens = db.get_google_health_tokens()
    if not tokens:
        raise GoogleHealthAuthError(
            "no Google Health tokens in db - run scripts/bootstrap_google_health_oauth.py first"
        )

    now = int(time.time())
    if tokens["expires_at"] - now > REFRESH_LEEWAY_SECONDS:
        return tokens["access_token"]

    refresh_token_expires_at = tokens.get("refresh_token_expires_at")
    if refresh_token_expires_at is not None and int(refresh_token_expires_at) <= now:
        raise GoogleHealthTokenExpired(_token_expired_message())

    fresh = _post_refresh(tokens["refresh_token"])
    if fresh.get("refresh_token_expires_in") is not None:
        refresh_token_expires_at = now + int(fresh["refresh_token_expires_in"])

    db.save_google_health_tokens(
        access_token=fresh["access_token"],
        refresh_token=fresh.get("refresh_token", tokens["refresh_token"]),
        expires_at=now + int(fresh["expires_in"]),
        scopes=fresh.get("scope", tokens.get("scopes")),
        token_type=fresh.get("token_type", tokens.get("token_type") or "Bearer"),
        refresh_token_expires_at=refresh_token_expires_at,
    )
    return fresh["access_token"]


def request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict:
    """Make an authenticated Google Health API request."""
    response = httpx.request(
        method,
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {get_access_token()}"},
        params=params,
        json=json_body,
        timeout=20,
    )
    if response.status_code >= 400:
        raise GoogleHealthApiError(
            f"{method} {path} failed: {response.status_code} {response.text}"
        )
    if not response.content:
        return {}
    return response.json()


def list_data_points(
    data_type: str,
    *,
    filter_expr: str | None = None,
    page_size: int = 25,
) -> list[dict]:
    """Return one page of raw data points for a Google Health data type."""
    params: dict[str, Any] = {"pageSize": page_size}
    if filter_expr:
        params["filter"] = filter_expr

    body = request(
        "GET",
        f"/users/me/dataTypes/{data_type}/dataPoints",
        params=params,
    )
    return body.get("dataPoints", [])
