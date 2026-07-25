"""/wellness — today's HRV / RHR / sleep numbers and deltas vs trailing baseline."""

from src.clients import intervals_icu
from src.synthesis import _readiness_deltas, _today_local, _today_wellness
from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec


def _fmt_delta(delta: float | None, unit: str, signed: bool = True) -> str:
    if delta is None:
        return "—"
    sign = "+" if delta > 0 else ""
    return f"{sign}{delta}{unit}" if signed else f"{delta}{unit}"


def handle(ctx: CommandContext) -> CommandResult:
    today_iso = _today_local().isoformat()
    wellness = intervals_icu.get_wellness(days_back=14)
    today_w = _today_wellness(wellness, today_iso)
    deltas = _readiness_deltas(today_w, wellness)

    if not today_w:
        return CommandResult(text="no wellness data for today yet")

    hrv = today_w.get("hrv")
    rhr = today_w.get("restingHR")
    sleep_secs = today_w.get("sleepSecs")
    sleep_score = today_w.get("sleepScore")
    ctl = today_w.get("ctl")
    atl = today_w.get("atl")
    tsb = (ctl - atl) if (ctl is not None and atl is not None) else None

    def _hours_min(s):
        if not s:
            return "—"
        return f"{int(s) // 3600}h {(int(s) % 3600) // 60}m"

    lines = [f"*Wellness · {today_iso}*", ""]
    lines.append(
        f"HRV: {hrv or '—'}ms  ({_fmt_delta(deltas['hrv_delta_sigma'], 'σ')})"
    )
    lines.append(
        f"RHR: {rhr or '—'}bpm  ({_fmt_delta(deltas['rhr_delta_bpm'], 'bpm')})"
    )
    lines.append(
        f"Sleep: {_hours_min(sleep_secs)}"
        + (f" ({sleep_score})" if sleep_score else "")
        + f"  ({_fmt_delta(deltas['sleep_delta_pct'], '%')})"
    )
    lines.append("")
    lines.append(
        f"CTL {ctl:.1f} · ATL {atl:.1f} · TSB {tsb:+.1f}"
        if (ctl is not None and atl is not None and tsb is not None)
        else "CTL/ATL/TSB: —"
    )
    return CommandResult(text="\n".join(lines), parse_mode="Markdown")


SPEC = CommandSpec(name="wellness", help="today's HRV / RHR / sleep + deltas", handler=handle)
