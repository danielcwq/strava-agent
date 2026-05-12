"""Build the prompt context from all data sources, call Claude, return the brief."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.clients import claude, google_sheets, intervals_icu
from src.config import settings

QUALITY_KEYWORDS = ("interval", "tempo", "track", "x800", "x400", "x600",
                    "repeat", "fartlek", "threshold", "vo2")


def _today_local() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


def _is_quality_session(workout: dict) -> bool:
    """Heuristic: title mentions a quality keyword, or has ≥4 laps with varied speed."""
    title = (workout.get("Workout Title") or "").lower()
    if any(kw in title for kw in QUALITY_KEYWORDS):
        return True
    speeds = []
    for i in range(1, 25):
        raw = workout.get(f"Lap {i} Speed")
        if raw in (None, "", 0):
            continue
        try:
            speeds.append(float(raw))
        except (TypeError, ValueError):
            continue
    if len(speeds) >= 4 and (max(speeds) - min(speeds)) > 0.5:
        return True
    return False


def _extract_laps(workout: dict) -> list[dict]:
    laps = []
    for i in range(1, 25):
        time = workout.get(f"Lap {i} Time")
        if not time:
            break
        laps.append({
            "n": i,
            "time": time,
            "speed": workout.get(f"Lap {i} Speed"),
            "distance": workout.get(f"Lap {i} Distance"),
            "avg_hr": workout.get(f"Lap {i} Avg HR"),
        })
    return laps


def _summarize_workouts(workouts: list[dict]) -> list[dict]:
    out = []
    for w in workouts:
        row = {
            "title": w.get("Workout Title"),
            "date": w.get("Workout Date / Time"),
            "moving_time": w.get("Moving Time"),
            "distance": w.get("Total Distance"),
            "avg_hr": w.get("Average HR"),
        }
        if _is_quality_session(w):
            laps = _extract_laps(w)
            if laps:
                row["laps"] = laps
        out.append(row)
    return out


def _aggregate_volume(workouts: list[dict]) -> dict:
    if not workouts:
        return {}
    # workouts is newest-first; take the 7 most recent.
    last_7 = workouts[:7]
    distances = []
    for w in last_7:
        raw = w.get("Total Distance")
        if raw in (None, ""):
            continue
        try:
            distances.append(float(raw))
        except (TypeError, ValueError):
            continue
    return {
        "last_7d_count": len(last_7),
        "last_7d_total_distance": round(sum(distances), 2) if distances else None,
    }


def _today_wellness(wellness: list[dict], today_iso: str) -> dict | None:
    if not wellness:
        return None
    # intervals.icu stores the date as the entry's "id" in YYYY-MM-DD form.
    for w in wellness:
        if w.get("id") == today_iso:
            return w
    return wellness[-1]


def build_context(sleep_summary: dict | None = None) -> dict:
    """Gather all data sources into one dict for the Claude prompt."""
    today_iso = _today_local().isoformat()
    wellness = intervals_icu.get_wellness(days_back=14)
    planned = intervals_icu.get_planned_today()
    workouts = google_sheets.get_recent_workouts(n=14)

    return {
        "date": today_iso,
        "timezone": settings.timezone,
        "sleep_last_night": sleep_summary,
        "training_load": {
            "today": _today_wellness(wellness, today_iso),
            "trailing_14d_wellness": wellness,
        },
        "planned_today": planned,
        "recent_runs": _summarize_workouts(workouts),
        "volume_stats": _aggregate_volume(workouts),
    }


def synthesize_brief(sleep_summary: dict | None = None) -> dict:
    """Build context, call Claude, return parsed {headline, body, flags}."""
    context = build_context(sleep_summary=sleep_summary)
    return claude.synthesize(context)
