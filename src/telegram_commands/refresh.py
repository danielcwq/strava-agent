"""/refresh — re-synthesize the morning brief on demand using the latest sleep payload.

Bypasses dedup (no calendar-date guard). Uses the most recent sleep summary stored
from Garmin pushes; falls back to no-sleep mode if none exists yet.
"""
from contextlib import suppress

from src import db
from src.pipeline import format_for_telegram
from src.synthesis import synthesize_brief
from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec


def handle(ctx: CommandContext) -> CommandResult:
    sleep = db.get_latest_garmin_summary("sleeps")
    brief = synthesize_brief(sleep_summary=sleep)
    # Persist this as the "last brief" so /last reflects the refresh
    calendar_date = (sleep or {}).get("calendarDate")
    if calendar_date:
        with suppress(Exception):
            db.record_brief_content(calendar_date, brief)
    return CommandResult(text=format_for_telegram(brief), parse_mode="Markdown")


SPEC = CommandSpec(
    name="refresh",
    help="re-run the morning brief synthesis (ignores dedup)",
    handler=handle,
)
