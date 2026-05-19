"""SQLite state: Garmin OAuth tokens, morning-brief dedup, and Garmin summary store."""
import json
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

-- Stores the latest Garmin push payload per (summary_type, calendar_date).
-- Updates from Garmin for the same (type, date) overwrite — matches their
-- "updated summary records" semantics where the freshest version wins.
CREATE TABLE IF NOT EXISTS garmin_summaries (
    summary_type  TEXT NOT NULL,
    calendar_date TEXT NOT NULL,
    summary_id    TEXT,
    payload       TEXT NOT NULL,
    received_at   INTEGER NOT NULL,
    PRIMARY KEY (summary_type, calendar_date)
);

-- One row per turn in a Telegram chat. `content` is JSON: either a plain string
-- (for user turns) or a list of Anthropic content blocks (for assistant turns
-- that include tool_use / tool_result for multi-turn fidelity).
CREATE TABLE IF NOT EXISTS conversation_turns (
    chat_id     TEXT NOT NULL,
    turn_seq    INTEGER NOT NULL,
    role        TEXT NOT NULL,   -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (chat_id, turn_seq)
);

-- Daily token usage for the conversational tool-use loop, for soft cost capping.
CREATE TABLE IF NOT EXISTS api_usage_daily (
    day         TEXT PRIMARY KEY,    -- YYYY-MM-DD local
    input_tok   INTEGER NOT NULL DEFAULT 0,
    output_tok  INTEGER NOT NULL DEFAULT 0,
    requests    INTEGER NOT NULL DEFAULT 0
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply backward-compatible schema additions to pre-existing tables."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(brief_log)").fetchall()}
    if "brief_json" not in cols:
        conn.execute("ALTER TABLE brief_log ADD COLUMN brief_json TEXT")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.data_dir / "state.db")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
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


def record_brief_content(calendar_date: str, brief: dict) -> None:
    """Persist the rendered brief JSON after successful delivery, so /last can show it.

    Updates the existing brief_log row if present (normal path: mark_brief_sent ran first).
    Inserts a new row if missing (e.g. /refresh, which bypasses the dedup mark).
    """
    payload = json.dumps(brief)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE brief_log SET brief_json = ? WHERE calendar_date = ?",
            (payload, calendar_date),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO brief_log (calendar_date, summary_id, sent_at, brief_json) "
                "VALUES (?, ?, ?, ?)",
                (calendar_date, "manual", int(time.time()), payload),
            )


def get_last_brief() -> dict | None:
    """Return the most recently sent brief along with its metadata, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT calendar_date, summary_id, sent_at, brief_json FROM brief_log "
            "ORDER BY sent_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return {
        "calendar_date": row["calendar_date"],
        "summary_id": row["summary_id"],
        "sent_at": row["sent_at"],
        "brief": json.loads(row["brief_json"]) if row["brief_json"] else None,
    }


def store_garmin_summary(
    summary_type: str,
    calendar_date: str,
    payload: dict,
    summary_id: str | None = None,
) -> None:
    """Upsert a Garmin summary payload by (type, date)."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO garmin_summaries (summary_type, calendar_date, summary_id, payload, received_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(summary_type, calendar_date) DO UPDATE SET
                summary_id  = excluded.summary_id,
                payload     = excluded.payload,
                received_at = excluded.received_at
            """,
            (summary_type, calendar_date, summary_id, json.dumps(payload), int(time.time())),
        )


def get_garmin_summary(summary_type: str, calendar_date: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM garmin_summaries WHERE summary_type = ? AND calendar_date = ?",
            (summary_type, calendar_date),
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def get_latest_garmin_summary(summary_type: str) -> dict | None:
    """Return the most recent (by calendar_date) stored summary of the given type."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM garmin_summaries "
            "WHERE summary_type = ? ORDER BY calendar_date DESC LIMIT 1",
            (summary_type,),
        ).fetchone()
    return json.loads(row["payload"]) if row else None


# === Conversation history (Phase 5.2) ===

def append_conversation_turn(chat_id: str, role: str, content) -> None:
    """Append a new turn. `content` may be str or a JSON-serializable list of blocks."""
    payload = content if isinstance(content, str) else json.dumps(content, default=_block_default)
    with get_conn() as conn:
        next_seq = conn.execute(
            "SELECT COALESCE(MAX(turn_seq), 0) + 1 FROM conversation_turns WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO conversation_turns (chat_id, turn_seq, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, next_seq, role, payload, int(time.time())),
        )


def load_conversation_history(chat_id: str, max_turns: int = 20, max_age_seconds: int = 86400) -> list[dict]:
    """Return last N turns within the time window, oldest-first, in Anthropic message format."""
    cutoff = int(time.time()) - max_age_seconds
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversation_turns "
            "WHERE chat_id = ? AND created_at >= ? "
            "ORDER BY turn_seq DESC LIMIT ?",
            (chat_id, cutoff, max_turns),
        ).fetchall()
    messages: list[dict] = []
    for row in reversed(rows):
        content_raw = row["content"]
        # Assistant turns are JSON arrays of content blocks; user turns are plain strings.
        try:
            content = json.loads(content_raw)
        except json.JSONDecodeError:
            content = content_raw
        messages.append({"role": row["role"], "content": content})
    return messages


def reset_conversation(chat_id: str) -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM conversation_turns WHERE chat_id = ?", (chat_id,))
    return cur.rowcount


def record_api_usage(day: str, input_tokens: int, output_tokens: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO api_usage_daily (day, input_tok, output_tok, requests)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(day) DO UPDATE SET
                input_tok  = input_tok + excluded.input_tok,
                output_tok = output_tok + excluded.output_tok,
                requests   = requests + 1
            """,
            (day, input_tokens, output_tokens),
        )


def get_api_usage(day: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT input_tok, output_tok, requests FROM api_usage_daily WHERE day = ?",
            (day,),
        ).fetchone()
    if not row:
        return {"input_tok": 0, "output_tok": 0, "requests": 0}
    return dict(row)


def get_api_usage_since(start_day: str) -> dict:
    """Sum chat API usage from start_day (YYYY-MM-DD) through the latest row.

    `day` is an ISO date string, so a lexical >= comparison is chronological.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT input_tok, output_tok, requests FROM api_usage_daily WHERE day >= ?",
            (start_day,),
        ).fetchall()
    return {
        "input_tok": sum(r["input_tok"] for r in rows),
        "output_tok": sum(r["output_tok"] for r in rows),
        "requests": sum(r["requests"] for r in rows),
        "active_days": len(rows),
    }


def _block_default(o):
    """JSON encoder fallback for Anthropic SDK content block objects."""
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if hasattr(o, "__dict__"):
        return o.__dict__
    raise TypeError(f"not serializable: {type(o)}")
