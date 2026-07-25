"""Natural-language conversation handler with Claude + tool use.

Routes inbound Telegram messages that aren't slash commands through Claude.
Claude has access to tools that fetch the user's training data on demand.

The loop:
    1. Load chat history from SQLite.
    2. Append the new user message.
    3. Call Claude with tools.
    4. If Claude returns tool_use blocks, execute each tool, append results, loop.
    5. If Claude returns a final text response, save and return it.

Hard limits:
    - Up to MAX_TOOL_ITERATIONS turns per user message (prevents infinite loops).
    - Records token usage in api_usage_daily; soft-caps the day's spend.
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from anthropic import Anthropic

from src import db, training_config
from src.clients import google_sheets, intervals_icu
from src.config import PROMPTS_DIR, settings
from src.synthesis import _extract_laps, _is_quality_session, _parse_workout_date

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-7"
# Per-response output ceiling (API-required). 16k keeps us in safe non-streaming
# territory; the SDK refuses larger non-streaming requests as a timeout guard.
MAX_TOKENS = 16000
# Runaway-bug backstop on the tool-use loop, not a budget. Claude self-terminates
# via stop_reason="end_turn"; this only fires on genuine infinite loops.
MAX_TOOL_ITERATIONS = 30

_client = Anthropic(api_key=settings.anthropic_api_key)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "get_activity_detail",
        "description": (
            "Get ALL workouts logged on a specific date from the Strava sheet, as a list. "
            "Many runners log a single training session as several activities (warmup, "
            "intervals, cooldown) — each becomes its own row. Each activity in the list "
            "includes title, distance, time, HR, and lap-level data when it was a quality "
            "session. If the user asks about a structured workout, look through the list "
            "for the one that has `laps` populated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format (user's local timezone).",
                },
            },
            "required": ["date"],
        },
    },
    {
        "name": "get_recent_runs",
        "description": (
            "Get a summary of the last N runs. Each run includes title, date, distance, "
            "moving time, and average HR. Use this for 'how was my week', 'what have i "
            "been doing', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "How many recent runs to return (1-14). Default 7.",
                },
            },
        },
    },
    {
        "name": "get_wellness_window",
        "description": (
            "Get HRV / RHR / sleep / CTL / ATL / TSB for the trailing N days from "
            "intervals.icu. Use for recovery questions, trend analysis, 'am i fresh', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_back": {
                    "type": "integer",
                    "description": "Trailing window length in days (1-30). Default 14.",
                },
            },
        },
    },
    {
        "name": "get_last_brief",
        "description": (
            "Get the most recently delivered morning brief (headline, body, flags). "
            "Use when the user asks 'what did the brief say' or references "
            "'this morning's brief'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_garmin_summary",
        "description": (
            "Get a stored Garmin push payload for a specific summary type and date. "
            "Summary types: 'sleeps', 'dailies' (RHR + Body Battery), 'userMetrics' (VO2 max), "
            "'hrv', 'stressDetails'. Use when answering questions about specific overnight "
            "metrics that aren't in the wellness window."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary_type": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["summary_type", "date"],
        },
    },
    {
        "name": "search_workouts",
        "description": (
            "Search the runner's FULL workout history from the Strava sheet — not just "
            "recent runs. Use it to find past sessions by title keyword, type, or time "
            "window: e.g. 'all the track sessions this block', 'when did I last do a "
            "tempo', 'find my 3x2k'. Returns newest-first summaries (date, title, "
            "distance, moving time, HR, is_quality); for lap-level detail on a specific "
            "result, follow up with get_activity_detail for that date. The runner logs "
            "warmup / intervals / cooldown as separate rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keyword(s) matched case-insensitively against the workout "
                        "title; every word must appear. Optional."
                    ),
                },
                "family": {
                    "type": "string",
                    "enum": ["quality", "easy", "all"],
                    "description": (
                        "Session-type filter. 'quality' = interval / tempo / track-type "
                        "work (heuristic); 'easy' = everything else. Default 'all'."
                    ),
                },
                "days_back": {
                    "type": "integer",
                    "description": "How many days back to scan. Default 120.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (1-50). Default 20.",
                },
            },
        },
    },
]


# Anthropic's server-side web search tool — Claude runs it itself (no _tool_*
# function), we just declare it. web_search_20260209 also auto-enables code
# execution for "dynamic filtering": Claude prunes search results before they
# hit the context window — leaner token use, and free when paired with web
# search. Lets the agent research training-science questions against live
# sources, with citations.
WEB_SEARCH_TOOL: dict = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 5,
}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _today_iso() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).date().isoformat()


def _tool_get_activity_detail(date_str: str) -> dict:
    tz = ZoneInfo(settings.timezone)
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return {"error": f"invalid date '{date_str}', expected YYYY-MM-DD"}

    # Scan deep — search_workouts can surface old sessions, and a drill-in here
    # must reach them, not just the last ~30 rows.
    rows = google_sheets.get_recent_workouts(n=5000)
    matches: list[dict[str, Any]] = []
    for w in rows:
        parsed = _parse_workout_date(w.get("Workout Date / Time") or "", tz)
        if parsed != target:
            continue
        record: dict[str, Any] = {
            "title": w.get("Workout Title"),
            "moving_time": w.get("Moving Time"),
            "elapsed_time": w.get("Elapsed Time"),
            "distance": w.get("Total Distance"),
            "avg_hr": w.get("Average HR"),
            "started_at": w.get("Workout Date / Time"),
        }
        if _is_quality_session(w):
            laps = _extract_laps(w)
            if laps:
                record["laps"] = laps
        matches.append(record)

    if not matches:
        return {"error": f"no workouts found for {date_str}"}
    return {"date": date_str, "count": len(matches), "activities": matches}


def _tool_get_recent_runs(n: int = 7) -> dict:
    n = max(1, min(14, int(n)))
    rows = google_sheets.get_recent_workouts(n=n)
    return {
        "runs": [
            {
                "date": (w.get("Workout Date / Time") or "")[:10],
                "title": w.get("Workout Title"),
                "distance": w.get("Total Distance"),
                "moving_time": w.get("Moving Time"),
                "avg_hr": w.get("Average HR"),
            }
            for w in rows
        ]
    }


def _tool_get_wellness_window(days_back: int = 14) -> dict:
    days_back = max(1, min(30, int(days_back)))
    rows = intervals_icu.get_wellness(days_back=days_back)
    slim = []
    for r in rows:
        slim.append({
            "date": r.get("id"),
            "hrv": r.get("hrv"),
            "rhr": r.get("restingHR"),
            "sleep_secs": r.get("sleepSecs"),
            "sleep_score": r.get("sleepScore"),
            "ctl": r.get("ctl"),
            "atl": r.get("atl"),
            "tsb": (r.get("ctl") - r.get("atl"))
            if (r.get("ctl") is not None and r.get("atl") is not None)
            else None,
        })
    return {"window_days": days_back, "wellness": slim}


def _tool_get_last_brief() -> dict:
    last = db.get_last_brief()
    return last or {"error": "no brief delivered yet"}


def _tool_get_garmin_summary(summary_type: str, date_str: str) -> dict:
    data = db.get_garmin_summary(summary_type, date_str)
    if data is None:
        return {"error": f"no {summary_type} stored for {date_str}"}
    return data


def _tool_search_workouts(
    query: str | None = None,
    family: str | None = None,
    days_back: int = 120,
    limit: int = 20,
) -> dict:
    """Search the full workout history by title keyword(s), family, and time window.

    Scans the whole sheet — not just the recent rows the other tools see — so the
    agent can answer 'what have I done across this block', not only 'last week'.
    Returns newest-first summaries; `is_quality` flags interval/tempo/track work.
    The runner logs warmup/intervals/cooldown as separate rows, so `quality`
    naturally isolates the work rows while `easy`/`all` include every row.
    """
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    days_back = max(1, min(3650, int(days_back)))
    limit = max(1, min(50, int(limit)))
    cutoff = today - timedelta(days=days_back)

    terms = (query or "").lower().split()
    family = (family or "all").lower()

    rows = google_sheets.get_recent_workouts(n=5000)  # whole sheet; the client slices
    matches: list[dict[str, Any]] = []
    for w in rows:
        d = _parse_workout_date(w.get("Workout Date / Time") or "", tz)
        if d is None or not (cutoff <= d <= today):
            continue
        title = w.get("Workout Title") or ""
        if terms and not all(t in title.lower() for t in terms):
            continue
        quality = _is_quality_session(w)
        if family == "quality" and not quality:
            continue
        if family == "easy" and quality:
            continue
        matches.append({
            "date": d.isoformat(),
            "title": title,
            "distance": w.get("Total Distance"),
            "moving_time": w.get("Moving Time"),
            "avg_hr": w.get("Average HR"),
            "is_quality": quality,
        })

    matches.sort(key=lambda m: m["date"], reverse=True)
    return {
        "query": query,
        "family": family,
        "days_back": days_back,
        "total_matches": len(matches),
        "workouts": matches[:limit],
    }


TOOL_FUNCS = {
    "get_activity_detail": lambda args: _tool_get_activity_detail(args["date"]),
    "get_recent_runs": lambda args: _tool_get_recent_runs(args.get("n", 7)),
    "get_wellness_window": lambda args: _tool_get_wellness_window(args.get("days_back", 14)),
    "get_last_brief": lambda args: _tool_get_last_brief(),
    "get_garmin_summary": lambda args: _tool_get_garmin_summary(args["summary_type"], args["date"]),
    "search_workouts": lambda args: _tool_search_workouts(
        query=args.get("query"),
        family=args.get("family"),
        days_back=args.get("days_back") or 120,
        limit=args.get("limit") or 20,
    ),
}


def _execute_tool(name: str, args: dict) -> Any:
    fn = TOOL_FUNCS.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return fn(args or {})
    except Exception as e:
        logger.exception("tool %s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Conversation handler
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    """Chat-mode system prompt, assembled fresh each call: base prompt + training
    principles + project context, plus a dynamic one-liner naming today's date
    and default day-role so the agent doesn't infer the weekly rhythm itself."""
    parts = [
        (PROMPTS_DIR / "conversation_system.md").read_text(),
        training_config.training_principles(),
        training_config.project_context(),
    ]
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    parts.append("# Today\n\n" + training_config.describe_today(today))
    return "\n\n---\n\n".join(part for part in parts if part)


def _over_daily_cap() -> bool:
    """True if today's chat token spend has reached either configured cap.

    The caps are a runaway-bug backstop, not a budget (see config.Settings). A
    cap of 0 means unlimited — that dimension is never enforced.
    """
    u = db.get_api_usage(_today_iso())
    in_cap = settings.daily_input_token_cap
    out_cap = settings.daily_output_token_cap
    return bool(
        (in_cap and u["input_tok"] >= in_cap)
        or (out_cap and u["output_tok"] >= out_cap)
    )


def handle_message(chat_id: str, user_text: str) -> str:
    """Run a Claude tool-use loop for a single user message. Returns the reply text."""
    if _over_daily_cap():
        return (
            "I've hit my daily API token cap. Try again tomorrow, or raise "
            "DAILY_INPUT_TOKEN_CAP / DAILY_OUTPUT_TOKEN_CAP (0 = unlimited)."
        )

    history = db.load_conversation_history(chat_id)
    messages: list[dict] = list(history) + [{"role": "user", "content": user_text}]

    total_input = 0
    total_output = 0
    final_text = ""

    for _iteration in range(MAX_TOOL_ITERATIONS):
        response = _client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=_load_system_prompt(),
            tools=TOOLS + [WEB_SEARCH_TOOL],
            messages=messages,
        )
        total_input += response.usage.input_tokens
        total_output += response.usage.output_tokens

        # Convert SDK content blocks to plain dicts for storage / serialization
        content_dicts = [_block_to_dict(b) for b in response.content]
        messages.append({"role": "assistant", "content": content_dicts})

        if response.stop_reason == "end_turn":
            final_text = "".join(b.text for b in response.content if b.type == "text").strip()
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        if response.stop_reason == "pause_turn":
            # A server-tool turn (web search / code execution) paused mid-flight.
            # The assistant content is already appended above — re-send so Claude
            # resumes.
            continue

        # max_tokens or other stop reasons — break with whatever text we have
        final_text = "".join(b.text for b in response.content if b.type == "text").strip()
        final_text = final_text or f"(stopped: {response.stop_reason})"
        break
    else:
        final_text = "(hit max tool iterations without final answer)"

    # Persist the new user turn + the full assistant reply chain
    db.append_conversation_turn(chat_id, "user", user_text)
    db.append_conversation_turn(chat_id, "assistant", content_dicts)
    db.record_api_usage(_today_iso(), total_input, total_output)

    return final_text or "(empty response)"


def _block_to_dict(b) -> dict:
    """Convert an Anthropic SDK content block into a plain JSON-serializable dict
    suitable for re-sending in a subsequent messages.create call.

    Server-tool blocks from web search and code execution (server_tool_use,
    web_search_tool_result, *_code_execution_tool_result) carry encrypted fields
    — encrypted_content on results, encrypted_index on citations — that MUST
    round-trip unchanged or citations break on later turns. Those go through
    model_dump() whole; text blocks keep their citations too.
    """
    if b.type == "text":
        out: dict = {"type": "text", "text": b.text}
        citations = getattr(b, "citations", None)
        if citations:
            out["citations"] = [
                c.model_dump() if hasattr(c, "model_dump") else c for c in citations
            ]
        return out
    if b.type == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if b.type == "tool_result":
        return {"type": "tool_result", "tool_use_id": b.tool_use_id, "content": b.content}
    # server-tool blocks (web search, code execution) and any other block: keep
    # the full SDK payload so encrypted_content / encrypted_index survive replay.
    if hasattr(b, "model_dump"):
        return b.model_dump()
    return {"type": "unknown"}
