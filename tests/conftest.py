"""Safe import-time settings for tests and public CI environments."""

import os
import sys
from pathlib import Path

import pytest

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


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    from src.config import CONFIG_DIR, settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "training_profile_toml_b64", None)
    monkeypatch.setattr(
        settings, "training_profile_path", CONFIG_DIR / "training_profile.example.toml"
    )
    monkeypatch.setattr(settings, "telegram_chat_id", "123")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "test-webhook")
    monkeypatch.setattr(settings, "context_token_budget", 48000)
    monkeypatch.setattr(settings, "context_summary_tokens", 3000)
    monkeypatch.setattr(settings, "chat_max_tokens", 16000)
    monkeypatch.setattr(settings, "brief_max_tokens", 16000)
