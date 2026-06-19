"""Smoke-test Google Health API access without printing health values.

This checks which data types return at least one data point for the authorized
account. It prints counts and top-level response keys only.

Run locally:
    uv run scripts/smoke_google_health.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.clients import google_health  # noqa: E402

DATA_TYPES = [
    "sleep",
    "weight",
    "body-fat",
    "daily-heart-rate-variability",
    "daily-resting-heart-rate",
    "daily-oxygen-saturation",
    "daily-respiratory-rate",
    "steps",
    "active-zone-minutes",
    "exercise",
]


def _shape(records: list[dict]) -> list[str]:
    keys: set[str] = set()
    for record in records:
        keys.update(record.keys())
    return sorted(keys)


def main() -> int:
    print("Google Health API smoke test")
    print("(showing record counts and top-level keys only; no health values)\n")

    failures = 0
    for data_type in DATA_TYPES:
        try:
            records = google_health.list_data_points(data_type, page_size=3)
        except Exception as exc:
            failures += 1
            print(f"{data_type}: ERROR {type(exc).__name__}: {exc}")
            continue
        print(f"{data_type}: {len(records)} records; keys={_shape(records)}")

    if failures:
        print(f"\nCompleted with {failures} data type error(s).")
        return 1

    print("\nSmoke test completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
