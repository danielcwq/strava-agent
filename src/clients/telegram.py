"""Telegram Bot API client."""

import httpx

from src import archive
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


def send_message(
    text: str, parse_mode: str | None = "Markdown", *, category: str = "reply"
) -> None:
    """Send a message to the configured chat, splitting if it exceeds Telegram's per-message limit.

    Raises on non-2xx for any chunk.
    """
    if archive.current_run.get() is None:
        with archive.execution("delivery.manual", {"text": text, "category": category}):
            return send_message(text, parse_mode, category=category)
    url = f"{API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    for index, chunk in enumerate(_split_for_telegram(text)):
        payload: dict = {
            "chat_id": settings.telegram_chat_id,
            "text": chunk,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        attempt = archive.event(
            "delivery.attempt", {"chunk_index": index, "category": category, "message": payload}
        )
        try:
            r = httpx.post(url, json=payload, timeout=10)
            r.raise_for_status()
            result = r.json()
            if not result.get("ok"):
                archive.event(
                    "delivery.rejected",
                    {"attempt_id": attempt, "error_code": result.get("error_code")},
                )
                raise RuntimeError("Telegram rejected message")
        except httpx.HTTPStatusError as exc:
            archive.event(
                "delivery.rejected",
                {"attempt_id": attempt, "status_code": exc.response.status_code},
            )
            raise
        except (httpx.RequestError, ValueError) as exc:
            archive.event(
                "delivery.uncertain", {"attempt_id": attempt, "error_type": type(exc).__name__}
            )
            raise
        archive.event(
            "delivery.accepted",
            {
                "attempt_id": attempt,
                "message_id": result.get("result", {}).get("message_id"),
                "category": category,
                "chunk_index": index,
            },
        )
