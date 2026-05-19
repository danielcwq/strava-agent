"""Durable training config — the stable layer the agent reasons against.

Encodes day-of-week *roles* (the defaults the runner usually follows) and current
phase metadata. Deliberately small: dynamic state — recent sessions, readiness,
accumulated load — is computed from data, never stored here.

Day roles are *defaults*, not a fixed plan. The runner trains by feel; readiness
and life legitimately shift a day. The prompt treats the role as a default to
reason from, not a rule to enforce.

Keep this in sync with prompts/training_principles.md — that file is the prose
version Claude reads; this file is what the code computes against.
"""
from datetime import date, timedelta

# --- Phase metadata --------------------------------------------------------
CURRENT_PHASE = "half-marathon-specific build"
RACE_DATE = date(2026, 7, 26)   # SF 2nd Half / City Half

# --- Weekly rhythm ---------------------------------------------------------
# Day roles, keyed by date.weekday() (Mon=0 … Sun=6). Defaults the runner usually
# follows, not a fixed plan.
#   key      — the week's quality session (usually CCSF track)
#   long     — long run, often with steady/progression elements
#   support  — easy running or off; absorbs load between hard days
#   rest     — off
WEEKLY_ROLES: dict[int, str] = {
    0: "support",   # Mon — protects Tuesday
    1: "key",       # Tue — quality, usually CCSF track
    2: "support",   # Wed
    3: "support",   # Thu
    4: "support",   # Fri
    5: "long",      # Sat
    6: "rest",      # Sun
}

ROLE_DETAIL: dict[str, str] = {
    "key": "key quality day (usually CCSF track)",
    "long": "long run day",
    "support": "easy / support day",
    "rest": "rest day",
}


def day_role(d: date) -> str:
    """Return the default training role for a calendar date."""
    return WEEKLY_ROLES[d.weekday()]


def next_key_day(d: date) -> tuple[date, int]:
    """Return the next 'key' day on or after `d`, and how many days away it is.

    If `d` itself is a key day, returns (d, 0).
    """
    for offset in range(7):
        cand = d + timedelta(days=offset)
        if day_role(cand) == "key":
            return cand, offset
    return d, 0   # no key day configured — degrade gracefully


def days_to_race(d: date) -> int | None:
    """Whole days from `d` to race day; None once the race is in the past."""
    delta = (RACE_DATE - d).days
    return delta if delta >= 0 else None


def describe_today(d: date) -> str:
    """One-line, human-readable summary of today's role — for prompt injection."""
    role = day_role(d)
    parts = [f"{d:%A} {d.isoformat()} — usual role: {ROLE_DETAIL.get(role, role)}."]
    next_key, days_away = next_key_day(d)
    if role in ("support", "rest") and days_away == 1:
        parts.append("Sits the day before the key session — its job is to protect it.")
    elif role != "key" and days_away > 0:
        parts.append(f"Next key day: {next_key:%A} ({days_away}d away).")
    if (dtr := days_to_race(d)) is not None:
        parts.append(f"{dtr}d to race.")
    return " ".join(parts)
