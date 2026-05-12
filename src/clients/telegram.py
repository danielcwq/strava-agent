"""Telegram Bot API client."""
import httpx

from src.config import settings

API_BASE = "https://api.telegram.org"


def send_message(text: str, parse_mode: str | None = "Markdown") -> None:
    """Send a message to the configured chat. Raises on non-2xx."""
    url = f"{API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    payload: dict = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    r = httpx.post(url, json=payload, timeout=10)
    r.raise_for_status()
