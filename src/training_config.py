"""Load the runner's private profile and expose deterministic training helpers."""
import base64
import binascii
import tomllib
from datetime import date, timedelta

from src.config import CONFIG_DIR, REPO_ROOT, settings

EXAMPLE_PROFILE_PATH = CONFIG_DIR / "training_profile.example.toml"


def _profile_bytes() -> tuple[bytes, str]:
    encoded = settings.training_profile_toml_b64
    if encoded:
        try:
            return base64.b64decode(encoded, validate=True), "TRAINING_PROFILE_TOML_B64"
        except (binascii.Error, ValueError) as exc:
            raise ValueError("TRAINING_PROFILE_TOML_B64 is not valid base64") from exc

    path = settings.training_profile_path
    if not path.is_absolute():
        path = REPO_ROOT / path
    if path.exists():
        return path.read_bytes(), str(path)
    return EXAMPLE_PROFILE_PATH.read_bytes(), str(EXAMPLE_PROFILE_PATH)


def _load_profile() -> tuple[dict, str]:
    raw, source = _profile_bytes()
    try:
        profile = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid training profile from {source}: {exc}") from exc

    training = profile.get("training", {})
    roles = profile.get("weekly_roles", {})
    role_detail = profile.get("role_detail", {})
    prompts = profile.get("prompts", {})
    required_days = {str(day) for day in range(7)}
    if set(roles) != required_days:
        raise ValueError(f"{source}: weekly_roles must define string keys 0 through 6")
    if not isinstance(training.get("current_phase"), str):
        raise ValueError(f"{source}: training.current_phase must be a string")
    if not isinstance(role_detail, dict) or not role_detail:
        raise ValueError(f"{source}: role_detail must not be empty")
    for key in ("training_principles", "project_context"):
        if not isinstance(prompts.get(key), str):
            raise ValueError(f"{source}: prompts.{key} must be a string")
    return profile, source


PROFILE, PROFILE_SOURCE = _load_profile()
_TRAINING = PROFILE["training"]
_PROMPTS = PROFILE["prompts"]

CURRENT_PHASE: str = _TRAINING["current_phase"]
_RACE_DATE_RAW = _TRAINING.get("race_date")
RACE_DATE: date | None = date.fromisoformat(_RACE_DATE_RAW) if _RACE_DATE_RAW else None
WEEKLY_ROLES: dict[int, str] = {
    int(day): role for day, role in PROFILE["weekly_roles"].items()
}
ROLE_DETAIL: dict[str, str] = dict(PROFILE["role_detail"])


def training_principles() -> str:
    return _PROMPTS["training_principles"].strip()


def project_context() -> str:
    return _PROMPTS["project_context"].strip()


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
    if RACE_DATE is None:
        return None
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
