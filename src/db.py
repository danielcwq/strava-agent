"""SQLite state: Garmin OAuth tokens and morning-brief dedup."""
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from src.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS garmin_tokens (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    INTEGER NOT NULL,
    user_id       TEXT NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS brief_log (
    calendar_date TEXT PRIMARY KEY,
    summary_id    TEXT NOT NULL,
    sent_at       INTEGER NOT NULL
);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.data_dir / "state.db")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_tokens() -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM garmin_tokens WHERE id = 1").fetchone()
        return dict(row) if row else None


def save_tokens(
    *,
    access_token: str,
    refresh_token: str,
    expires_at: int,
    user_id: str,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO garmin_tokens (id, access_token, refresh_token, expires_at, user_id, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                access_token  = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at    = excluded.expires_at,
                user_id       = excluded.user_id,
                updated_at    = excluded.updated_at
            """,
            (access_token, refresh_token, expires_at, user_id, int(time.time())),
        )


def is_brief_sent(calendar_date: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM brief_log WHERE calendar_date = ?", (calendar_date,)
        ).fetchone()
    return row is not None


def mark_brief_sent(calendar_date: str, summary_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO brief_log (calendar_date, summary_id, sent_at) VALUES (?, ?, ?)",
            (calendar_date, summary_id, int(time.time())),
        )
