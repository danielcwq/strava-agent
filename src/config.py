"""Environment-loaded configuration. Validates required vars at import time."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
CONFIG_DIR = REPO_ROOT / "config"


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

    # Google Health API OAuth 2.0 client. Tokens are seeded by
    # scripts/bootstrap_google_health_oauth.py and stored in SQLite.
    google_health_client_id: str | None = None
    google_health_client_secret: str | None = None
    google_health_redirect_uri: str = "http://localhost:8080/google-health/callback"

    # Garmin vars are optional during Phase A (test script runs without them).
    garmin_client_id: str | None = None
    garmin_client_secret: str | None = None
    garmin_redirect_uri: str = "http://localhost:8080/callback"
    # Required at runtime for /garmin/push/{secret}. If omitted, the endpoint
    # remains disabled instead of accepting unauthenticated health payloads.
    garmin_webhook_secret: str | None = None

    # Personal coaching context. Local development reads the ignored TOML file;
    # deployments can provide the same file as a base64-encoded secret.
    training_profile_path: Path = CONFIG_DIR / "training_profile.local.toml"
    training_profile_toml_b64: str | None = None

    # Where SQLite lives. Local default = ./data; on Fly, set DATA_DIR=/data via env
    # so it lands on the mounted volume.
    data_dir: Path = REPO_ROOT / "data"

    timezone: str = "America/Los_Angeles"
    log_level: str = "INFO"

    # Daily token-spend ceilings for the Telegram chat agent — a runaway-bug
    # backstop, not a hard budget. 0 = unlimited. Defaults sit far above any real
    # use; tune (or zero out) via DAILY_INPUT_TOKEN_CAP / DAILY_OUTPUT_TOKEN_CAP
    # env vars without a redeploy.
    daily_input_token_cap: int = 10_000_000
    daily_output_token_cap: int = 2_000_000


settings = Settings()
