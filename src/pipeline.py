"""Morning brief orchestrator: gather data, synthesize, deliver."""
import logging

from src.clients import telegram
from src.synthesis import synthesize_brief

logger = logging.getLogger(__name__)


def format_for_telegram(brief: dict) -> str:
    headline = brief.get("headline", "Morning brief")
    body = brief.get("body", "")
    flags = brief.get("flags") or []
    parts = [f"*{headline}*", "", body]
    if flags:
        parts.append("")
        parts.append("_" + " · ".join(flags) + "_")
    return "\n".join(parts)


def morning_brief(sleep_summary: dict | None = None) -> dict:
    """Run the full pipeline end-to-end. Returns the parsed brief.

    sleep_summary may be None during Phase A testing — the brief is then built
    from intervals.icu + Sheets only, with `sleep_last_night: null` in the prompt.
    """
    logger.info("morning_brief.start has_sleep=%s", sleep_summary is not None)
    brief = synthesize_brief(sleep_summary=sleep_summary)
    telegram.send_message(format_for_telegram(brief))
    logger.info("morning_brief.delivered flags=%s", brief.get("flags"))
    return brief
