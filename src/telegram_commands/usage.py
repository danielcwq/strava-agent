"""/usage — today's and this week's Telegram-chat API token spend."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src import db
from src.config import settings
from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec

# Approximate Claude Sonnet pricing, USD per million tokens. Update if it shifts.
_USD_PER_MTOK_IN = 3.0
_USD_PER_MTOK_OUT = 15.0


def _cost(input_tok: int, output_tok: int) -> float:
    return input_tok / 1e6 * _USD_PER_MTOK_IN + output_tok / 1e6 * _USD_PER_MTOK_OUT


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

    lines = [
        "*API usage* — Telegram chat only (excludes the morning brief)",
        "",
        "*Today*",
        _cap_line("Input:", t["input_tok"], settings.daily_input_token_cap),
        _cap_line("Output:", t["output_tok"], settings.daily_output_token_cap),
        f"{t['requests']} messages · ~${_cost(t['input_tok'], t['output_tok']):.2f}",
        "",
        f"*Last 7 days* ({week['active_days']} active)",
        f"Input {week['input_tok']:,} · Output {week['output_tok']:,}",
        f"{week['requests']} messages · ~${_cost(week['input_tok'], week['output_tok']):.2f}",
    ]
    return CommandResult(text="\n".join(lines), parse_mode="Markdown")


SPEC = CommandSpec(
    name="usage",
    help="today's and this week's chat API token spend",
    handler=handle,
)
