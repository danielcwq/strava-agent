"""/runs [n] — show your last N runs in one line each. Default N=7."""
from src.clients import google_sheets
from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec


def _parse_n(args: list[str], default: int = 7, cap: int = 14) -> int:
    if not args:
        return default
    try:
        n = int(args[0])
    except ValueError:
        return default
    return max(1, min(n, cap))


def handle(ctx: CommandContext) -> CommandResult:
    n = _parse_n(ctx.args)
    rows = google_sheets.get_recent_workouts(n=n)
    if not rows:
        return CommandResult(text="no runs found in the sheet")

    lines = [f"*Last {len(rows)} runs*", ""]
    for r in rows:
        title = (r.get("Workout Title") or "").strip() or "(untitled)"
        date = (r.get("Workout Date / Time") or "")[:10]
        dist = r.get("Total Distance") or ""
        time_ = r.get("Moving Time") or ""
        hr = r.get("Average HR") or ""
        lines.append(f"`{date}` · {title} · {dist}m · {time_}s · HR {hr}")
    return CommandResult(text="\n".join(lines), parse_mode="Markdown")


SPEC = CommandSpec(name="runs", help="last N runs (default 7)", handler=handle)
