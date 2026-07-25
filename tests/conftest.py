"""Safe import-time settings for tests and public CI environments."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REQUIRED_ENV = {
    "TELEGRAM_BOT_TOKEN": "test-token",
    "TELEGRAM_CHAT_ID": "test-chat",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "INTERVALS_ICU_API_KEY": "test-intervals-key",
    "INTERVALS_ICU_ATHLETE_ID": "test-athlete",
    "GOOGLE_SHEET_ID": "test-sheet",
    "GOOGLE_SERVICE_ACCOUNT_JSON_B64": "e30=",
}

for _name, _value in _REQUIRED_ENV.items():
    os.environ.setdefault(_name, _value)
