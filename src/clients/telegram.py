"""Telegram Bot API client."""
import httpx

from src.config import settings

API_BASE = "https://api.telegram.org"

# Telegram's per-message hard limit is 4096 chars. We split a touch under that
# to leave room for Markdown parsing quirks.
TELEGRAM_MAX_LEN = 4000


def _split_for_telegram(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Split text into Telegram-sized chunks, preferring paragraph/sentence boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        window = remaining[:max_len]
        # Prefer a paragraph break, then a line break, then a sentence end, then a space.
        split_at = window.rfind("\n\n")
        if split_at < max_len // 2:
            split_at = window.rfind("\n")
        if split_at < max_len // 2:
            for sep in (". ", "? ", "! "):
                idx = window.rfind(sep)
                if idx >= max_len // 2:
                    split_at = idx + len(sep) - 1
                    break
        if split_at < max_len // 2:
            split_at = window.rfind(" ")
        if split_at <= 0:
            split_at = max_len - 1
        chunks.append(remaining[: split_at + 1].rstrip())
        remaining = remaining[split_at + 1 :].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def send_message(text: str, parse_mode: str | None = "Markdown") -> None:
    """Send a message to the configured chat, splitting if it exceeds Telegram's per-message limit.

    Raises on non-2xx for any chunk.
    """
    url = f"{API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    for chunk in _split_for_telegram(text):
        payload: dict = {
            "chat_id": settings.telegram_chat_id,
            "text": chunk,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        r = httpx.post(url, json=payload, timeout=10)
        r.raise_for_status()
