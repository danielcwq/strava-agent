"""Build the prompt context from all data sources, call Claude, return the brief."""
import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src import db
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
    for w in wellness:
        if w.get("id") == today_iso:
            return w
    return wellness[-1]


_WORKOUT_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
)


def _parse_workout_date(s: str, tz: ZoneInfo) -> date | None:
    """Parse the Sheet's date column and return the date IN THE USER'S LOCAL TIMEZONE.

    A run timestamp like `2026-05-12T06:30:00Z` (which is May 11 ~11:30pm in Pacific)
    must report as May 11 locally, not May 12.
    """
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz).date()
    except ValueError:
        pass
    for fmt in _WORKOUT_DATE_FORMATS:
        try:
            naive = datetime.strptime(s, fmt)
            return naive.replace(tzinfo=tz).date()
        except ValueError:
            continue
    return None


def _most_recent_run(workouts: list[dict], today: date, tz: ZoneInfo) -> dict | None:
    """Sheet is newest-first, so workouts[0] is the most recent run.

    Returns a summary with `days_ago` (in local days) so Claude can decide if it's
    worth commenting on. Includes lap-level detail for quality sessions.
    """
    if not workouts:
        return None
    w = workouts[0]
    parsed = _parse_workout_date(w.get("Workout Date / Time") or "", tz)
    summary = {
        "title": w.get("Workout Title"),
        "date_raw": w.get("Workout Date / Time"),
        "date_local": parsed.isoformat() if parsed else None,
        "days_ago": (today - parsed).days if parsed else None,
        "moving_time": w.get("Moving Time"),
        "distance": w.get("Total Distance"),
        "avg_hr": w.get("Average HR"),
    }
    if _is_quality_session(w):
        laps = _extract_laps(w)
        if laps:
            summary["laps"] = laps
    return summary


def _readiness_deltas(today_wellness: dict | None, trailing: list[dict]) -> dict:
    """Compute today's HRV / RHR / sleep deltas against the trailing-window baseline.

    Excludes today from the baseline so the comparison is meaningful.
    Returns None for any metric where today's value or the baseline is missing.
    """
    out = {
        "hrv_delta_sigma": None,
        "rhr_delta_bpm": None,
        "sleep_delta_pct": None,
    }
    if not today_wellness:
        return out

    today_id = today_wellness.get("id")
    baseline = [w for w in trailing if w.get("id") != today_id]
    if not baseline:
        return out

    def _series(field: str) -> list[float]:
        out_: list[float] = []
        for w in baseline:
            v = w.get(field)
            if v in (None, "", 0):
                continue
            try:
                out_.append(float(v))
            except (TypeError, ValueError):
                continue
        return out_

    hrv_today = today_wellness.get("hrv")
    hrv_series = _series("hrv")
    if hrv_today not in (None, "", 0) and len(hrv_series) >= 3:
        try:
            mean = statistics.mean(hrv_series)
            sd = statistics.stdev(hrv_series)
            if sd > 0:
                out["hrv_delta_sigma"] = round((float(hrv_today) - mean) / sd, 2)
        except (statistics.StatisticsError, TypeError, ValueError):
            pass

    rhr_today = today_wellness.get("restingHR")
    rhr_series = _series("restingHR")
    if rhr_today not in (None, "", 0) and rhr_series:
        try:
            out["rhr_delta_bpm"] = round(float(rhr_today) - statistics.mean(rhr_series), 1)
        except (TypeError, ValueError):
            pass

    sleep_today = today_wellness.get("sleepSecs")
    sleep_series = _series("sleepSecs")
    if sleep_today not in (None, "", 0) and sleep_series:
        try:
            mean = statistics.mean(sleep_series)
            if mean > 0:
                out["sleep_delta_pct"] = round(((float(sleep_today) - mean) / mean) * 100, 1)
        except (TypeError, ValueError):
            pass

    return out


def _slim_dailies(d: dict | None) -> dict | None:
    """Pull just the fields useful for the brief out of the raw Garmin Dailies payload."""
    if not d:
        return None
    return {
        "calendarDate": d.get("calendarDate"),
        "restingHeartRate": d.get("restingHeartRateInBeatsPerMinute"),
        "averageHeartRate": d.get("averageHeartRateInBeatsPerMinute"),
        "steps": d.get("steps"),
        "distanceMeters": d.get("distanceInMeters"),
        "activeKilocalories": d.get("activeKilocalories"),
        "moderateMinutes": _seconds_to_minutes(d.get("moderateIntensityDurationInSeconds")),
        "vigorousMinutes": _seconds_to_minutes(d.get("vigorousIntensityDurationInSeconds")),
        "bodyBatteryCharged": d.get("bodyBatteryChargedValue"),
        "bodyBatteryDrained": d.get("bodyBatteryDrainedValue"),
    }


def _slim_user_metrics(d: dict | None) -> dict | None:
    if not d:
        return None
    return {
        "calendarDate": d.get("calendarDate"),
        "vo2Max": d.get("vo2Max"),
        "vo2MaxCycling": d.get("vo2MaxCycling"),
        "fitnessAge": d.get("fitnessAge"),
        "enhanced": d.get("enhanced"),
    }


def _seconds_to_minutes(s) -> int | None:
    if s in (None, "", 0):
        return None
    try:
        return round(int(s) / 60)
    except (TypeError, ValueError):
        return None


def build_context(sleep_summary: dict | None = None) -> dict:
    """Gather all data sources into one dict for the Claude prompt."""
    tz = ZoneInfo(settings.timezone)
    now_local = datetime.now(tz)
    today = now_local.date()
    today_iso = today.isoformat()
    yesterday_iso = (today - timedelta(days=1)).isoformat()
    wellness = intervals_icu.get_wellness(days_back=14)
    workouts = google_sheets.get_recent_workouts(n=14)

    today_w = _today_wellness(wellness, today_iso)
    dailies_yesterday = db.get_garmin_summary("dailies", yesterday_iso)
    user_metrics_latest = db.get_latest_garmin_summary("userMetrics")

    return {
        "time": {
            "now_local": now_local.isoformat(timespec="seconds"),
            "now_utc": datetime.now().astimezone().isoformat(timespec="seconds"),
            "today_local": today_iso,
            "yesterday_local": yesterday_iso,
            "timezone": settings.timezone,
            # Truthful if intervals.icu has today's wellness row already synced.
            "wellness_today_synced": (today_w or {}).get("id") == today_iso,
            # Truthful if Garmin Dailies for yesterday has arrived from the push.
            "garmin_dailies_yesterday_received": dailies_yesterday is not None,
        },
        "sleep_last_night": sleep_summary,
        "wellness": {
            "today": today_w,
            "trailing_14d": wellness,
            "deltas_vs_baseline": _readiness_deltas(today_w, wellness),
        },
        "garmin": {
            "dailies_yesterday": _slim_dailies(dailies_yesterday),
            "user_metrics_latest": _slim_user_metrics(user_metrics_latest),
        },
        "most_recent_run": _most_recent_run(workouts, today, tz),
        "recent_runs": _summarize_workouts(workouts),
        "volume_stats": _aggregate_volume(workouts),
    }


def synthesize_brief(sleep_summary: dict | None = None) -> dict:
    """Build context, call Claude, return parsed {headline, body, flags}."""
    context = build_context(sleep_summary=sleep_summary)
    return claude.synthesize(context)
