"""Application-owned sessions, durable work queue, and append-only event archive."""

import json
import time
import uuid
from contextlib import contextmanager, suppress
from contextvars import ContextVar

from src import db
from src.config import settings

current_run: ContextVar[str | None] = ContextVar("current_run", default=None)


def _now() -> int:
    return time.time_ns() // 1_000_000


def _session(conn, chat_id: str) -> str:
    row = conn.execute(
        "SELECT id FROM chat_sessions WHERE chat_id=? AND closed_at IS NULL", (chat_id,)
    ).fetchone()
    if row:
        return row[0]
    session_id = str(uuid.uuid4())
    conn.execute("INSERT INTO chat_sessions VALUES (?, ?, ?, NULL)", (session_id, chat_id, _now()))
    return session_id


def active_session(chat_id: str) -> str:
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _session(conn, chat_id)


def reset_session(chat_id: str) -> str:
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE chat_sessions SET closed_at=? WHERE chat_id=? AND closed_at IS NULL",
            (_now(), chat_id),
        )
        return _session(conn, chat_id)


def _safe(value):
    """Remove known credentials from every archived payload, including error strings."""
    if isinstance(value, dict):
        return {
            k: "[REDACTED]"
            if any(
                s in k.lower()
                for s in (
                    "access_token",
                    "refresh_token",
                    "api_key",
                    "authorization",
                    "client_secret",
                    "webhook_secret",
                    "bot_token",
                    "service_account_json",
                    "cookie",
                )
            )
            else _safe(v)
            for k, v in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_safe(v) for v in value]
    if isinstance(value, str):
        for name in (
            "anthropic_api_key",
            "telegram_bot_token",
            "telegram_webhook_secret",
            "garmin_webhook_secret",
            "garmin_client_secret",
            "google_health_client_secret",
            "intervals_icu_api_key",
            "google_service_account_json_b64",
        ):
            secret = getattr(settings, name, None)
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if hasattr(value, "model_dump"):
        return _safe(value.model_dump(mode="json"))
    return value


def _event(conn, run_id: str, kind: str, payload, created_at: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO run_events(run_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        (run_id, kind, json.dumps(_safe(payload), default=str), created_at or _now()),
    )
    return cur.lastrowid


def event(kind: str, payload, run_id: str | None = None) -> int | None:
    target = run_id or current_run.get()
    if target is None:
        return None
    with db.get_conn() as conn:
        return _event(conn, target, kind, payload)


@contextmanager
def bind(run_id: str):
    token = current_run.set(run_id)
    try:
        yield
    finally:
        current_run.reset(token)


@contextmanager
def execution(kind: str, payload: dict):
    """Trace standalone scripts too; a worker-owned run keeps its existing identity."""
    if current_run.get():
        yield current_run.get()
        return
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM agent_runs WHERE status='running'").fetchone():
            raise RuntimeError("another run is active; use the bot queue for concurrent requests")
        run_id = str(uuid.uuid4())
        chat_id = str(settings.telegram_chat_id)
        conn.execute(
            "INSERT INTO agent_runs(id,session_id,chat_id,kind,status,input_json,created_at) "
            "VALUES (?,?,?,?,'running',?,?)",
            (run_id, _session(conn, chat_id), chat_id, kind, json.dumps(_safe(payload)), _now()),
        )
        _event(conn, run_id, "input.received", payload)
        _event(conn, run_id, "run.started", {})
    with bind(run_id):
        try:
            yield run_id
        except Exception as exc:
            event("run.error", {"error_type": type(exc).__name__})
            finish(run_id, "failed")
            raise
        else:
            finish(run_id, "completed")


def enqueue(
    chat_id: str,
    kind: str,
    payload: dict,
    source_key: str | None = None,
    brief_date: str | None = None,
) -> tuple[str, bool]:
    """Commit accepted input and dedup marker atomically before acknowledging it."""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if source_key:
            existing = conn.execute(
                "SELECT id FROM agent_runs WHERE source_key=?", (source_key,)
            ).fetchone()
            if existing:
                return existing[0], False
        if (
            brief_date
            and conn.execute(
                "SELECT 1 FROM brief_log WHERE calendar_date=?", (brief_date,)
            ).fetchone()
        ):
            return "", False
        run_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO agent_runs(id,session_id,chat_id,kind,source_key,status,input_json,"
            "created_at) VALUES (?,?,?,?,?,'queued',?,?)",
            (
                run_id,
                _session(conn, chat_id),
                chat_id,
                kind,
                source_key,
                json.dumps(_safe(payload)),
                _now(),
            ),
        )
        _event(conn, run_id, "input.received", payload)
        if kind == "chat":
            _event(
                conn, run_id, "conversation.message", {"role": "user", "content": payload["text"]}
            )
        if brief_date:
            conn.execute(
                "INSERT INTO brief_log(calendar_date,summary_id,sent_at) VALUES (?,?,?)",
                (brief_date, payload.get("summaryId", ""), int(time.time())),
            )
        return run_id, True


def get_run(run_id: str) -> dict:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT rowid AS sequence, * FROM agent_runs WHERE id=?", (run_id,)
        ).fetchone()
    if not row:
        raise ValueError("unknown run")
    return dict(row)


def events(run_id: str) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM run_events WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def claim_next() -> dict | None:
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Only one active execution on this single-user service, including /reset and briefs.
        if conn.execute("SELECT 1 FROM agent_runs WHERE status='running'").fetchone():
            return None
        row = conn.execute(
            "SELECT rowid AS sequence, * FROM agent_runs WHERE status='queued' "
            "ORDER BY rowid LIMIT 1"
        ).fetchone()
        if not row:
            return None
        run = dict(row)
        # A /reset queued before this request may have started a new session.
        run["session_id"] = _session(conn, run["chat_id"])
        conn.execute(
            "UPDATE agent_runs SET status='running',session_id=? WHERE id=?",
            (run["session_id"], run["id"]),
        )
        _event(conn, run["id"], "run.started", {})
        return run


def finish(run_id: str, status: str) -> None:
    if status not in {"completed", "failed", "interrupted"}:
        raise ValueError("invalid terminal status")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE agent_runs SET status=?,finished_at=? WHERE id=?", (status, _now(), run_id)
        )
        _event(conn, run_id, "run." + status, {})


def recover_interrupted() -> int:
    """Called once at process startup; never blindly replay interrupted side effects."""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT id FROM agent_runs WHERE status='running'").fetchall()
        for row in rows:
            _event(
                conn,
                row[0],
                "run.interrupted",
                {
                    "reason": "process restarted; an unfinished delivery may have been accepted",
                    "retry": "manual review required",
                },
            )
        conn.execute(
            "UPDATE agent_runs SET status='interrupted',finished_at=? WHERE status='running'",
            (_now(),),
        )
        return len(rows)


def migrate_legacy() -> None:
    """Import once, retaining the old table and labeling its incomplete tool history."""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM archive_metadata WHERE key='legacy_imported'").fetchone():
            return
        rows = conn.execute("SELECT * FROM conversation_turns ORDER BY chat_id,turn_seq").fetchall()
        runs = {}
        for row in rows:
            chat_id = row["chat_id"]
            if row["role"] == "user" or chat_id not in runs:
                run_id = str(uuid.uuid4())
                runs[chat_id] = run_id
                conn.execute(
                    "INSERT INTO agent_runs(id,session_id,chat_id,kind,status,input_json,"
                    "created_at,"
                    "finished_at,legacy_incomplete) "
                    "VALUES (?,?,?,'legacy','completed','{}',?,?,1)",
                    (
                        run_id,
                        _session(conn, chat_id),
                        chat_id,
                        row["created_at"] * 1000,
                        row["created_at"] * 1000,
                    ),
                )
            content = row["content"]
            if row["role"] == "assistant":
                with suppress(json.JSONDecodeError):
                    content = json.loads(content)
            _event(
                conn,
                runs[chat_id],
                "conversation.message",
                {"role": row["role"], "content": content},
                created_at=row["created_at"] * 1000,
            )
        conn.execute("INSERT INTO archive_metadata VALUES ('legacy_imported','1')")


def search_history(
    chat_id: str,
    session_id: str,
    query: str,
    *,
    include_previous_sessions: bool = False,
    limit: int = 10,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    from datetime import date

    params: list = [chat_id]
    filters = ["r.chat_id=?", "e.kind='conversation.message'"]
    if not include_previous_sessions:
        filters.append("r.session_id=?")
        params.append(session_id)
    for word in query.split()[:12]:
        filters.append("instr(lower(e.payload),lower(?))>0")
        params.append(word)
    for value, op in ((since, ">="), (until, "<=")):
        if value:
            date.fromisoformat(value)
            filters.append(f"date(r.created_at/1000,'unixepoch') {op} ?")
            params.append(value)
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT e.id,e.run_id,r.session_id,r.created_at,e.payload FROM run_events e "
            "JOIN agent_runs r ON r.id=e.run_id WHERE "
            + " AND ".join(filters)
            + " ORDER BY e.id DESC LIMIT ?",
            (*params, max(1, min(limit, 30))),
        ).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
