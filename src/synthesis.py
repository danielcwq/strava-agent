"""Build the prompt context from all data sources, call Claude, return the brief."""
import statistics
from contextlib import suppress
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src import db, training_config
from src.clients import claude, google_health, google_sheets, intervals_icu
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
    return len(speeds) >= 4 and (max(speeds) - min(speeds)) > 0.5


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


def _safe_float(raw) -> float | None:
    """Parse a sheet cell to float, tolerating blanks and junk."""
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


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
        with suppress(TypeError, ValueError):
            out["rhr_delta_bpm"] = round(float(rhr_today) - statistics.mean(rhr_series), 1)

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


GOOGLE_HEALTH_DATA_TYPES = (
    ("sleep", 5),
    ("weight", 1),
    ("body-fat", 1),
    ("daily-heart-rate-variability", 3),
    ("daily-resting-heart-rate", 3),
    ("daily-oxygen-saturation", 3),
    ("daily-respiratory-rate", 3),
    ("steps", 3),
    ("active-zone-minutes", 3),
    ("exercise", 3),
)


def _int_or_none(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _float_or_none(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _first_present(d: dict | None, keys: tuple[str, ...]) -> Any:
    if not d:
        return None
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return value
    return None


def _civil_date(civil_datetime: dict | None) -> str | None:
    if not civil_datetime:
        return None
    date_part = civil_datetime.get("date") or {}
    year = date_part.get("year")
    month = date_part.get("month")
    day = date_part.get("day")
    if not (year and month and day):
        return None
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _civil_time(civil_datetime: dict | None) -> str | None:
    if not civil_datetime:
        return None
    time_part = civil_datetime.get("time") or {}
    hours = int(time_part.get("hours") or 0)
    minutes = int(time_part.get("minutes") or 0)
    seconds = int(time_part.get("seconds") or 0)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _local_datetime_parts(raw: str | None, tz: ZoneInfo) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    with suppress(ValueError):
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(tz)
        return dt.date().isoformat(), dt.time().isoformat(timespec="seconds")
    return None, None


def _duration_to_minutes(duration: str | None) -> int | None:
    if not duration or not duration.endswith("s"):
        return None
    seconds = _float_or_none(duration[:-1])
    if seconds is None:
        return None
    return round(seconds / 60)


def _compact_google_health_value(value):
    """Keep Google Health records prompt-sized without losing the source shape."""
    if isinstance(value, dict):
        return {k: _compact_google_health_value(v) for k, v in value.items()}
    if isinstance(value, list):
        limit = 8
        out = [_compact_google_health_value(v) for v in value[:limit]]
        if len(value) > limit:
            out.append({"truncated_items": len(value) - limit})
        return out
    return value


def _slim_google_health_record(record: dict) -> dict:
    out = {}
    for key, value in record.items():
        if key == "name":
            continue
        out[key] = _compact_google_health_value(value)
    return out


def _google_health_sleep_filter(today: date) -> str:
    tomorrow = (today + timedelta(days=1)).isoformat()
    return (
        f'sleep.interval.civil_end_time >= "{today.isoformat()}" '
        f'AND sleep.interval.civil_end_time < "{tomorrow}"'
    )


def _normalize_google_health_sleep(record: dict | None) -> dict | None:
    sleep = (record or {}).get("sleep")
    if not sleep:
        return None

    interval = sleep.get("interval") or {}
    summary = sleep.get("summary") or {}
    metadata = sleep.get("metadata") or {}

    stage_minutes = {}
    for stage in summary.get("stagesSummary") or []:
        stage_type = stage.get("type")
        minutes = _int_or_none(stage.get("minutes"))
        if stage_type and minutes is not None:
            stage_minutes[stage_type] = minutes

    start_civil = interval.get("civilStartTime")
    end_civil = interval.get("civilEndTime")
    tz = ZoneInfo(settings.timezone)
    start_date_fallback, start_time_fallback = _local_datetime_parts(
        interval.get("startTime"),
        tz,
    )
    end_date_fallback, end_time_fallback = _local_datetime_parts(
        interval.get("endTime"),
        tz,
    )
    minutes_asleep = _int_or_none(summary.get("minutesAsleep"))
    minutes_in_period = _int_or_none(summary.get("minutesInSleepPeriod"))
    if minutes_in_period is None:
        minutes_in_period = _duration_to_minutes(sleep.get("duration"))

    return {
        "source": "Google Health API (Fitbit)",
        "end_date": _civil_date(end_civil) or end_date_fallback,
        "start_date": _civil_date(start_civil) or start_date_fallback,
        "start_time": _civil_time(start_civil) or start_time_fallback,
        "end_time": _civil_time(end_civil) or end_time_fallback,
        "start_time_utc": interval.get("startTime"),
        "end_time_utc": interval.get("endTime"),
        "minutes_asleep": minutes_asleep,
        "minutes_in_sleep_period": minutes_in_period,
        "minutes_awake": _int_or_none(summary.get("minutesAwake")),
        "minutes_after_wake_up": _int_or_none(summary.get("minutesAfterWakeUp")),
        "minutes_to_fall_asleep": _int_or_none(summary.get("minutesToFallAsleep")),
        "stage_minutes": stage_minutes,
        "sleep_type": sleep.get("type"),
        "processed": metadata.get("processed"),
        "nap": metadata.get("nap"),
        "manually_edited": metadata.get("manuallyEdited"),
        "stages_status": metadata.get("stagesStatus"),
        "update_time": sleep.get("updateTime"),
    }


def _best_google_health_sleep(records: list[dict], today: date) -> dict | None:
    normalized = [_normalize_google_health_sleep(record) for record in records]
    sleeps = [record for record in normalized if record]
    if not sleeps:
        return None

    today_iso = today.isoformat()
    candidates = [record for record in sleeps if record.get("end_date") == today_iso]
    if not candidates:
        candidates = sleeps

    def _sort_key(record: dict) -> tuple[int, int]:
        return (
            1 if record.get("nap") else 0,
            -(record.get("minutes_asleep") or record.get("minutes_in_sleep_period") or 0),
        )

    return sorted(candidates, key=_sort_key)[0]


def _google_health_context(today: date) -> dict:
    """Fetch a compact Google Health snapshot for the brief.

    This is a corroborating source, so failures are captured in-context instead
    of failing the whole morning brief.
    """
    if not db.get_google_health_tokens():
        return {
            "available": False,
            "reason": "tokens_missing",
            "source": "Google Health API",
        }

    try:
        google_health.get_access_token()
    except google_health.GoogleHealthTokenExpired:
        return {
            "available": False,
            "reason": "oauth_refresh_token_expired",
            "source": "Google Health API",
        }
    except google_health.GoogleHealthAuthError:
        return {
            "available": False,
            "reason": "oauth_unavailable",
            "source": "Google Health API",
        }

    records: dict[str, list[dict]] = {}
    errors: dict[str, dict] = {}
    for data_type, page_size in GOOGLE_HEALTH_DATA_TYPES:
        try:
            filter_expr = _google_health_sleep_filter(today) if data_type == "sleep" else None
            points = google_health.list_data_points(
                data_type,
                filter_expr=filter_expr,
                page_size=page_size,
            )
        except google_health.GoogleHealthTokenExpired:
            return {
                "available": False,
                "reason": "oauth_refresh_token_expired",
                "source": "Google Health API",
            }
        except google_health.GoogleHealthAuthError:
            return {
                "available": False,
                "reason": "oauth_unavailable",
                "source": "Google Health API",
            }
        except Exception as exc:
            errors[data_type] = {
                "type": type(exc).__name__,
                "message": "request_failed",
            }
            continue
        records[data_type] = [_slim_google_health_record(point) for point in points]

    return {
        "available": True,
        "source": "Google Health API (Fitbit)",
        "note": (
            "secondary source; data appears only after Fitbit syncs to "
            "Google Health"
        ),
        "sleep_last_night": _best_google_health_sleep(records.get("sleep", []), today),
        "latest_records": records,
        "errors": errors,
    }


def _normalize_intervals_sleep(today_wellness: dict | None) -> dict | None:
    if not today_wellness:
        return None
    sleep_secs = _int_or_none(today_wellness.get("sleepSecs"))
    score = _int_or_none(today_wellness.get("sleepScore"))
    return {
        "source": "intervals.icu",
        "date": today_wellness.get("id"),
        "minutes_asleep": round(sleep_secs / 60) if sleep_secs is not None else None,
        "score": score,
    }


def _normalize_garmin_sleep(sleep_summary: dict | None) -> dict | None:
    if not sleep_summary:
        return None

    sleep_seconds = _int_or_none(
        _first_present(
            sleep_summary,
            (
                "totalSleepTimeInSeconds",
                "sleepTimeSeconds",
                "sleepDurationInSeconds",
                "durationInSeconds",
            ),
        )
    )
    score = _int_or_none(
        _first_present(
            sleep_summary,
            ("overallSleepScore", "sleepScore", "sleepScoreFeedback"),
        )
    )
    return {
        "source": "Garmin webhook",
        "calendarDate": sleep_summary.get("calendarDate"),
        "minutes_asleep": round(sleep_seconds / 60) if sleep_seconds is not None else None,
        "score": score,
        "raw_keys_available": sorted(sleep_summary.keys()),
    }


def _sleep_source_comparison(
    *,
    sleep_summary: dict | None,
    today_wellness: dict | None,
    google_health_ctx: dict,
) -> dict:
    intervals_sleep = _normalize_intervals_sleep(today_wellness)
    garmin_sleep = _normalize_garmin_sleep(sleep_summary)
    google_sleep = google_health_ctx.get("sleep_last_night")

    primary = intervals_sleep or garmin_sleep
    comparison = {
        "guidance": (
            "Use Garmin/intervals sleep as the stat-line source when fresh. "
            "Use Fitbit/Google Health to corroborate or flag disagreement, not "
            "to override silently."
        ),
        "primary": primary,
        "garmin_webhook": garmin_sleep,
        "intervals_icu": intervals_sleep,
        "google_health": google_sleep,
        "conflict": False,
        "difference_minutes": None,
        "note": None,
    }

    primary_minutes = (primary or {}).get("minutes_asleep")
    google_minutes = (google_sleep or {}).get("minutes_asleep")
    if primary_minutes is None or google_minutes is None:
        return comparison

    diff = int(google_minutes) - int(primary_minutes)
    threshold = max(45, int(primary_minutes * 0.2))
    comparison["difference_minutes"] = diff
    if abs(diff) >= threshold:
        comparison["conflict"] = True
        comparison["note"] = (
            "Sleep sources materially disagree; mention the disagreement in "
            "flags and avoid overstating certainty."
        )
    return comparison


def _seconds_to_minutes(s) -> int | None:
    if s in (None, "", 0):
        return None
    try:
        return round(int(s) / 60)
    except (TypeError, ValueError):
        return None


def _calendar_volume(workouts: list[dict], today: date, tz: ZoneInfo) -> dict:
    """Sum distance into trailing *calendar-day* windows.

    Replaces the old _aggregate_volume, which summed the last 7 *rows* and
    mislabelled it `last_7d`. The runner logs warmup / intervals / cooldown as
    separate rows, so a row count is not a day count. `Total Distance` on the
    sheet is in metres. `days_run` counts distinct calendar dates with at least
    one logged activity.
    """
    windows = (7, 14, 28)
    km = {w: 0.0 for w in windows}
    dates: dict[int, set[date]] = {w: set() for w in windows}
    for w in workouts:
        d = _parse_workout_date(w.get("Workout Date / Time") or "", tz)
        if d is None:
            continue
        age = (today - d).days
        if age < 0:
            continue
        dist_m = _safe_float(w.get("Total Distance"))
        for win in windows:
            if age < win:
                if dist_m is not None:
                    km[win] += dist_m / 1000.0
                dates[win].add(d)
    return {
        "km_last_7d": round(km[7], 1),
        "km_last_14d": round(km[14], 1),
        "km_last_28d": round(km[28], 1),
        "days_run_last_7d": len(dates[7]),
        "days_run_last_14d": len(dates[14]),
        "note": "trailing calendar-day windows; distance summed from the sheet (m to km)",
    }


def _quality_recency(workouts: list[dict], today: date, tz: ZoneInfo) -> dict:
    """Recency of quality work, measured by distinct calendar date.

    A 'quality day' is any date with >=1 logged activity that looks like
    intervals / tempo / track (see _is_quality_session). Counting dates, not
    rows, is robust to one session being logged as several activities. This is a
    heuristic — it can tag a session family, not judge how well it was executed.
    """
    quality_dates: set[date] = set()
    for w in workouts:
        d = _parse_workout_date(w.get("Workout Date / Time") or "", tz)
        if d is None or (today - d).days < 0:
            continue
        if _is_quality_session(w):
            quality_dates.add(d)
    note = "heuristic — title / lap-pattern detection of interval/tempo/track work"
    if not quality_dates:
        return {
            "days_since_last_quality": None,
            "quality_days_last_7d": 0,
            "quality_days_last_14d": 0,
            "note": note,
        }
    last = max(quality_dates)
    return {
        "days_since_last_quality": (today - last).days,
        "last_quality_date": last.isoformat(),
        "quality_days_last_7d": sum((today - d).days < 7 for d in quality_dates),
        "quality_days_last_14d": sum((today - d).days < 14 for d in quality_dates),
        "note": note,
    }


def _build_training_snapshot(today: date, workouts: list[dict], tz: ZoneInfo) -> dict:
    """The computed day-role / load context the agent decides against.

    Deterministic and factual: it states the day's *role* and recent *load*; it
    does not prescribe a workout. The decision logic lives in the prompt and in
    prompts/training_principles.md.
    """
    role = training_config.day_role(today)
    next_key, days_away = training_config.next_key_day(today)
    return {
        "weekday": today.strftime("%A"),
        "today_role": role,
        "today_role_detail": training_config.ROLE_DETAIL.get(role, role),
        "next_key_day": {"weekday": next_key.strftime("%A"), "days_away": days_away},
        "today_protects_next_key": role in ("support", "rest") and days_away == 1,
        "phase": training_config.CURRENT_PHASE,
        "days_to_race": training_config.days_to_race(today),
        "volume": _calendar_volume(workouts, today, tz),
        "quality": _quality_recency(workouts, today, tz),
    }


def build_context(sleep_summary: dict | None = None) -> dict:
    """Gather all data sources into one dict for the Claude prompt."""
    tz = ZoneInfo(settings.timezone)
    now_local = datetime.now(tz)
    today = now_local.date()
    today_iso = today.isoformat()
    yesterday_iso = (today - timedelta(days=1)).isoformat()
    wellness = intervals_icu.get_wellness(days_back=14)
    # Wide fetch: the 28-day calendar-volume window needs many rows, since the
    # runner logs each session as several activities. Same API cost — the client
    # pulls the whole sheet and slices. Only `recent` is shown in the prompt.
    all_workouts = google_sheets.get_recent_workouts(n=120)
    recent = all_workouts[:14]

    today_w = _today_wellness(wellness, today_iso)
    dailies_yesterday = db.get_garmin_summary("dailies", yesterday_iso)
    user_metrics_latest = db.get_latest_garmin_summary("userMetrics")
    google_health_ctx = _google_health_context(today)
    source_comparison = _sleep_source_comparison(
        sleep_summary=sleep_summary,
        today_wellness=today_w,
        google_health_ctx=google_health_ctx,
    )

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
            "google_health_available": google_health_ctx.get("available") is True,
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
        "google_health": google_health_ctx,
        "source_comparison": {
            "sleep": source_comparison,
        },
        "training_snapshot": _build_training_snapshot(today, all_workouts, tz),
        "most_recent_run": _most_recent_run(recent, today, tz),
        "recent_runs": _summarize_workouts(recent),
    }


def synthesize_brief(sleep_summary: dict | None = None) -> dict:
    """Build context, call Claude, return parsed {headline, body, flags}.

    Project context (race goal, training philosophy) is included only on Mondays
    in the user's local timezone — Monday's brief anchors the training week
    without spamming race reminders on every other day.
    """
    context = build_context(sleep_summary=sleep_summary)
    is_monday = _today_local().weekday() == 0
    return claude.synthesize(context, include_project_context=is_monday)
