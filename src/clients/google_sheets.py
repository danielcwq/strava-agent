"""Google Sheets client — reads the past-runs sheet via service account."""
import base64
import json
from functools import cache

import gspread
from google.oauth2.service_account import Credentials

from src.config import settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


@cache
def _client() -> gspread.Client:
    info = json.loads(base64.b64decode(settings.google_service_account_json_b64))
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_recent_workouts(n: int = 14) -> list[dict]:
    """Return the most recent N workout rows as dicts (column header -> cell value).

    Sheet is newest-first (most recent rows at the top).
    Columns with empty header cells (blank separator columns) are dropped.
    """
    sh = _client().open_by_key(settings.google_sheet_id)
    ws = sh.worksheet(settings.google_sheet_tab)
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    headers = rows[0]
    keep = [(i, h) for i, h in enumerate(headers) if h]
    out: list[dict] = []
    for row in rows[1 : 1 + n]:
        out.append({h: (row[i] if i < len(row) else "") for i, h in keep})
    return out
