"""/laps [YYYY-MM-DD] — lap breakdown for a run on the given date.

Without an argument, shows laps for the most recent run.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.clients import google_sheets
from src.config import settings
from src.synthesis import _extract_laps, _parse_workout_date
from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec


def _resolve_target_date(args: list[str], tz: ZoneInfo) -> date | None:
    if not args:
        return None  # caller uses "most recent" semantics
    try:
        return datetime.strptime(args[0], "%Y-%m-%d").date()
    except ValueError:
        return None


def handle(ctx: CommandContext) -> CommandResult:
    tz = ZoneInfo(settings.timezone)
    target = _resolve_target_date(ctx.args, tz)
    if ctx.args and target is None:
        return CommandResult(text=f"can't parse date `{ctx.args[0]}`. expected YYYY-MM-DD")

    workouts = google_sheets.get_recent_workouts(n=14)
    if not workouts:
        return CommandResult(text="no runs in the sheet")

    if target is None:
        w = workouts[0]
    else:
        w = next(
            (
                r for r in workouts
                if _parse_workout_date(r.get("Workout Date / Time") or "", tz) == target
            ),
            None,
        )
    if w is None:
        return CommandResult(text=f"no run found for {target.isoformat()}")

    title = (w.get("Workout Title") or "(untitled)").strip()
    date_s = (w.get("Workout Date / Time") or "")[:10]
    laps = _extract_laps(w)
    if not laps:
        return CommandResult(text=f"{date_s} · {title}\nno lap data")

    lines = [f"*{date_s} · {title}*", ""]
    lines.append("`#  Time   Speed  Dist    HR`")
    for lap in laps:
        n = f"{lap['n']:>2}"
        t = f"{(lap.get('time') or ''):>6}"
        sp = f"{(lap.get('speed') or ''):>5}"
        d = f"{(lap.get('distance') or ''):>6}"
        hr = f"{(lap.get('avg_hr') or ''):>4}"
        lines.append(f"`{n} {t} {sp} {d} {hr}`")
    return CommandResult(text="\n".join(lines), parse_mode="Markdown")


SPEC = CommandSpec(name="laps", help="lap breakdown for a date (YYYY-MM-DD) or last run", handler=handle)
