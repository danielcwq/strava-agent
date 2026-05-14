"""/last — show the most recently delivered morning brief."""
import time

from src import db
from src.pipeline import format_for_telegram
from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec


def _ago(seconds_ago: int) -> str:
    if seconds_ago < 60:
        return f"{seconds_ago}s ago"
    if seconds_ago < 3600:
        return f"{seconds_ago // 60}m ago"
    if seconds_ago < 86400:
        return f"{seconds_ago // 3600}h ago"
    return f"{seconds_ago // 86400}d ago"


def handle(ctx: CommandContext) -> CommandResult:
    last = db.get_last_brief()
    if not last:
        return CommandResult(text="no brief has been delivered yet")

    age = _ago(int(time.time()) - last["sent_at"])
    header = f"_{last['calendar_date']} · {age}_"

    if last["brief"]:
        body = format_for_telegram(last["brief"])
        return CommandResult(text=f"{header}\n\n{body}", parse_mode="Markdown")

    # Brief was sent before the brief_json column existed (or pre-migration).
    return CommandResult(
        text=f"{header}\n\n(brief content not stored for this entry — pre-v0.1)",
        parse_mode="Markdown",
    )


SPEC = CommandSpec(name="last", help="show the most recent morning brief", handler=handle)
