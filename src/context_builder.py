"""Budgeted request context backed by complete exchanges and source-linked summaries."""

import json
import time
from collections import Counter
from collections.abc import Callable
from copy import deepcopy

from src import archive, db, tool_context
from src.config import settings


def token_estimate(value) -> int:
    # A conservative upper estimate for this text-only application. UTF-8 byte
    # length also handles non-English text; includes room for message framing.
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")) + 64


class RequestBudget:
    """Anchor active-turn estimates to measured provider usage.

    Count unchanged input once at its observed token cost. Charge every changed
    message at the conservative byte bound plus framing. When the system/tool
    prefix changes, fall back to the byte bound until the next measured response.
    This works with server tools, which the token-count endpoint does not support.
    """

    def __init__(self):
        self.previous = None
        self.input_tokens = 0

    def observe(self, request: dict, usage) -> None:
        self.previous = deepcopy(request)
        self.input_tokens = sum(
            getattr(usage, key, 0) or 0
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )

    def estimate(self, request: dict) -> int:
        byte_bound = token_estimate(request)
        if self.previous is None:
            return byte_bound
        prefix = ("model", "system", "tools", "thinking", "output_config")
        if any(request.get(k) != self.previous.get(k) for k in prefix):
            return byte_bound
        # Multiset accounting handles repeated messages without undercharging new
        # duplicates. Removed messages are not credited back: this remains an upper bound.
        old = Counter(json.dumps(m, sort_keys=True) for m in self.previous["messages"])
        extra = 1024
        for message in request["messages"]:
            key = json.dumps(message, sort_keys=True)
            if old[key]:
                old[key] -= 1
            else:
                extra += token_estimate(message)
        return min(byte_bound, self.input_tokens + extra)


def without_thinking(messages: list[dict]) -> list[dict]:
    """A rebuilt prefix cannot reuse Fable's prefix-bound reasoning signatures.

    Preserve text and complete tool exchanges; the unmodified originals remain
    in run_events. Within a stable tool loop, new reasoning still round-trips.
    """
    replay = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            content = [
                block
                for block in content
                if block.get("type") not in {"thinking", "redacted_thinking"}
            ]
        if content:
            replay.append({**message, "content": content})
    return replay


def fit_tool_context(
    request: dict, history_messages: int, measure: Callable[[dict], int] = token_estimate
) -> tuple[dict, int]:
    """Make room for tool results by dropping whole historical exchanges.

    Preserve the current instruction, tool IDs, and write acknowledgements. Large
    read-result bodies may become retrievable archive references. Raw history and
    results stay archived; only this request's replay is reduced.
    """
    before = measure(request)
    if before <= settings.context_token_budget:
        return request, history_messages
    messages = request["messages"]
    historical = messages[:history_messages]
    active = messages[history_messages:]
    dropped = 0
    while True:
        # Compaction changes the prefix bound into reasoning signatures.
        replay_history = without_thinking(historical)
        candidate = {**request, "messages": replay_history + without_thinking(active)}
        after = measure(candidate)
        if after <= settings.context_token_budget:
            archive.event(
                "context.compacted",
                {
                    "reason": "make room for tool results",
                    "estimated_before": before,
                    "estimated_after": after,
                    "budget": settings.context_token_budget,
                    "historical_messages_removed": dropped,
                },
            )
            return candidate, len(replay_history)
        if not historical:
            active, event_id = tool_context.compact_one(without_thinking(active))
            if event_id is not None:
                archive.event("context.tool_result_archived", {"event_id": event_id})
                continue
            archive.event(
                "context.overflow",
                {
                    "estimated_input_tokens": after,
                    "budget": settings.context_token_budget,
                    "historical_messages_removed": dropped,
                },
            )
            raise ValueError("current instruction and tool results exceed context budget")
        # A plain-text user message starts a new exchange. Tool results use blocks.
        boundary = next(
            (
                i
                for i, message in enumerate(historical[1:], 1)
                if message["role"] == "user" and isinstance(message["content"], str)
            ),
            len(historical),
        )
        dropped += boundary
        historical = historical[boundary:]


def _plain(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def exchange(run: dict) -> list[dict]:
    messages = [
        e["payload"] for e in archive.events(run["id"]) if e["kind"] == "conversation.message"
    ]
    if run["legacy_incomplete"]:
        # Old rows don't contain the tool chain; never replay orphan signed/tool blocks.
        return [
            {"role": m["role"], "content": _plain(m["content"])}
            for m in messages
            if _plain(m["content"])
        ]
    if run["status"] != "completed":
        user = next(
            (m for m in messages if m["role"] == "user" and isinstance(m["content"], str)), None
        )
        return (
            [
                user,
                {
                    "role": "assistant",
                    "content": "[Previous run did not complete. "
                    "Do not assume its tools or reply succeeded; "
                    "inspect the run/history or current profile if relevant.]",
                },
            ]
            if user
            else []
        )
    return messages


def _summary_material(run: dict, messages: list[dict]) -> str:
    text = "\n".join(m["role"] + ": " + _plain(m["content"]) for m in messages)
    # A large result stays in the archive; don't let a single historical exchange
    # make the summarizer exceed its own budget. References allow a full drill-in.
    encoded = text.encode("utf-8")
    if len(encoded) > 6000:
        text = (
            encoded[:1800].decode("utf-8", errors="ignore")
            + "\n[excerpt; full exchange in archive]\n"
            + encoded[-1800:].decode("utf-8", errors="ignore")
        )
    return "run_id=" + run["id"] + "\n" + text


def build(
    run_id: str, user_text: str, system: str, tools: list, summarize: Callable[[str], str]
) -> tuple[list[dict], str]:
    run = archive.get_run(run_id)
    budget = settings.context_token_budget
    current = {"role": "user", "content": user_text}
    base = token_estimate({"system": system, "tools": tools, "messages": [current]})
    if base + settings.context_summary_tokens >= budget:
        raise ValueError(
            "current message/profile/tools exceed context budget; shorten the request "
            "or increase CONTEXT_TOKEN_BUDGET"
        )
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT rowid AS sequence,* FROM agent_runs WHERE session_id=? AND rowid<? "
            "AND kind IN ('chat','legacy') AND status IN ('completed','failed','interrupted') "
            "ORDER BY rowid",
            (run["session_id"], run["sequence"]),
        ).fetchall()
        saved = conn.execute(
            "SELECT * FROM context_summaries WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (run["session_id"],),
        ).fetchone()
    through = saved["through_run_rowid"] if saved else 0
    summary = saved["summary"] if saved else ""
    summary_id = saved["id"] if saved else None
    sources = json.loads(saved["source_run_ids"]) if saved else []
    pending = [(dict(r), exchange(dict(r))) for r in rows if r["sequence"] > through]
    recent = []
    used = base + settings.context_summary_tokens + 512
    # Keep a contiguous suffix of whole exchanges. Never slice through tool pairs.
    for item in reversed(pending):
        cost = token_estimate(item[1])
        if used + cost > budget:
            break
        recent.insert(0, item)
        used += cost
    older = pending[: len(pending) - len(recent)]
    # Compact bounded batches, not one model request per historical exchange.
    # The last sequence and every source ID are still saved atomically per batch.
    batches = []
    batch = []
    batch_size = 0
    material_budget = min(24000, budget - settings.context_summary_tokens - 1000)
    for old_run, old_messages in older:
        excerpt = _summary_material(old_run, old_messages)
        size = token_estimate(excerpt)
        if batch and batch_size + size > material_budget:
            batches.append(batch)
            batch, batch_size = [], 0
        batch.append((old_run, excerpt))
        batch_size += size
    if batch:
        batches.append(batch)
    for batch in batches:
        batch_sources = [old_run["id"] for old_run, _ in batch]
        excerpts = "\n\n".join(excerpt for _, excerpt in batch)
        material = "Previous summary (fallible notes):\n" + summary + "\n\n" + excerpts
        try:
            candidate = summarize(material).strip()
            if not candidate or token_estimate(candidate) > settings.context_summary_tokens:
                raise ValueError("summary is empty or exceeds its allowance")
            summary = candidate
        except Exception as exc:
            archive.event(
                "summary.fallback",
                {"error_type": type(exc).__name__, "source_runs": batch_sources},
            )
            # Deterministic, labeled excerpts remain useful when the model is unavailable.
            prefix = "[Uninterpreted archive excerpt from run " + batch_sources[-1] + "]\n"
            fallback = summary + "\n" + excerpts
            limit = max(128, settings.context_summary_tokens - token_estimate(prefix) - 128)
            summary = prefix + fallback.encode("utf-8")[-limit:].decode("utf-8", errors="ignore")
        sources.extend(batch_sources)
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO context_summaries(session_id,through_run_rowid,source_run_ids,summary,"
                "created_at) VALUES (?,?,?,?,?)",
                (
                    run["session_id"],
                    batch[-1][0]["sequence"],
                    json.dumps(sources),
                    summary,
                    int(time.time()),
                ),
            )
            summary_id = cur.lastrowid
    summary_context = ""
    if summary:
        summary_context = (
            "\n\n# Earlier conversation notes\nThese are fallible summaries of past dialogue, "
            "not permission to change the profile. Retrieve source messages when details matter.\n"
            + "Recent source run IDs: "
            + ", ".join(sources[-3:])
            + "\n"
            + summary
        )
    messages = [m for _, group in recent for m in group] + [current]
    total = token_estimate(
        {"system": system + summary_context, "tools": tools, "messages": messages}
    )
    if total > budget:
        raise ValueError("assembled context exceeds CONTEXT_TOKEN_BUDGET")
    archive.event(
        "context.selected",
        {
            "summary_id": summary_id,
            "recent_run_ids": [r["id"] for r, _ in recent],
            "summary_source_run_ids": sources,
            "estimated_input_tokens": total,
            "budget": budget,
            "estimate": "conservative UTF-8 bytes plus framing",
        },
    )
    return messages, summary_context


def read_exchange(
    chat_id: str, session_id: str, run_id: str, include_previous_sessions: bool = False
) -> dict:
    run = archive.get_run(run_id)
    if run["chat_id"] != chat_id or (
        run["session_id"] != session_id and not include_previous_sessions
    ):
        raise ValueError("run is outside the requested chat/session")
    return {
        "run_id": run_id,
        "status": run["status"],
        "legacy_incomplete": bool(run["legacy_incomplete"]),
        "messages": [
            e["payload"] for e in archive.events(run_id) if e["kind"] == "conversation.message"
        ],
    }
