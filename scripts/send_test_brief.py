"""Fire the morning brief end-to-end without needing a Garmin webhook.

Usage: uv run scripts/send_test_brief.py
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline import morning_brief  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    print("Running morning brief without Garmin sleep data (Phase A test)...")
    print("This calls intervals.icu, Google Sheets, Claude, then sends Telegram.\n")
    try:
        brief = morning_brief(sleep_summary=None)
    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        return 1
    print("\n--- Brief delivered ---")
    print(f"Headline: {brief.get('headline')}")
    print(f"Body:     {brief.get('body')}")
    print(f"Flags:    {brief.get('flags')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
