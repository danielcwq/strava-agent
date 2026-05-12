"""intervals.icu API client.

Auth: HTTP Basic, username 'API_KEY', password = the API key.
Athlete ID format: 'i12345' (include leading 'i').
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from src.config import settings

BASE_URL = "https://intervals.icu/api/v1"


def _auth() -> tuple[str, str]:
    return ("API_KEY", settings.intervals_icu_api_key)


def _today_local() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


def get_wellness(days_back: int = 14) -> list[dict]:
    """Return wellness entries (CTL/ATL/TSB, sleep, HRV, etc.) for the trailing window.

    Newest entry is typically last in the list; check `entry["id"]` (the date as
    YYYY-MM-DD string) to find today's row.
    """
    today = _today_local()
    oldest = today - timedelta(days=days_back)
    url = f"{BASE_URL}/athlete/{settings.intervals_icu_athlete_id}/wellness"
    r = httpx.get(
        url,
        params={"oldest": oldest.isoformat(), "newest": today.isoformat()},
        auth=_auth(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def get_planned_today() -> list[dict]:
    """Return today's planned workout events from the calendar."""
    today = _today_local().isoformat()
    url = f"{BASE_URL}/athlete/{settings.intervals_icu_athlete_id}/events"
    r = httpx.get(
        url,
        params={"oldest": today, "newest": today, "category": "WORKOUT"},
        auth=_auth(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()
