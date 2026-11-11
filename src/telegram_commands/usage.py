"""Telegram chat token usage."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src import db
from src.config import settings
from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec


def _cap_line(label: str, used: int, cap: int) -> str:
    """One 'used / cap (pct)' line, or 'used (no cap)' when cap is 0 (unlimited)."""
    if not cap:
        return f"{label} {used:,} (no cap)"
    return f"{label} {used:,} / {cap:,} ({used / cap * 100:.0f}%)"


def handle(ctx: CommandContext) -> CommandResult:
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    t = db.get_api_usage(today.isoformat())
    week = db.get_api_usage_since((today - timedelta(days=6)).isoformat())
    subscription = settings.model_backend == "claude_code"
    accounting = (
        "Shared Claude plan limits are shown in Claude Settings → Usage."
        if subscription
        else "Billing and credits are shown in Anthropic Console."
    )
    unit = "recorded model sessions" if subscription else "model requests"

    lines = [
        "*Chat token usage* (excludes the morning brief)",
        "",
        "*Today*",
        _cap_line("Input:", t["input_tok"], settings.daily_input_token_cap),
        _cap_line("Output:", t["output_tok"], settings.daily_output_token_cap),
        f"{t['requests']} {unit}",
        "",
        f"*Last 7 days* ({week['active_days']} active)",
        f"Input {week['input_tok']:,} · Output {week['output_tok']:,}",
        f"{week['requests']} {unit}",
        "",
        f"Includes context summaries. {accounting}",
    ]
    return CommandResult(text="\n".join(lines), parse_mode="Markdown")


SPEC = CommandSpec(
    name="usage",
    help="today's and this week's chat token usage",
    handler=handle,
)
