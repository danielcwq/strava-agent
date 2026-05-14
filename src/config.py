"""Environment-loaded configuration. Validates required vars at import time."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str
    telegram_chat_id: str
    # Optional: only required if the inbound /telegram/webhook is enabled.
    telegram_webhook_secret: str | None = None

    anthropic_api_key: str

    intervals_icu_api_key: str
    intervals_icu_athlete_id: str

    google_sheet_id: str
    google_sheet_tab: str = "Sheet1"
    google_service_account_json_b64: str

    # Garmin vars are optional during Phase A (test script runs without them).
    garmin_client_id: str | None = None
    garmin_client_secret: str | None = None
    garmin_redirect_uri: str = "http://localhost:8080/callback"

    # Where SQLite lives. Local default = ./data; on Fly, set DATA_DIR=/data via env
    # so it lands on the mounted volume.
    data_dir: Path = REPO_ROOT / "data"

    timezone: str = "America/Los_Angeles"
    log_level: str = "INFO"


settings = Settings()
