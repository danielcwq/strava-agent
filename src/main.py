"""FastAPI receiver for Garmin Health API Push notifications.

Endpoints:
    GET  /health        — liveness check for Fly
    POST /garmin/push/{secret}
                        — authenticated Garmin push; triggers a brief on sleeps

Garmin sends push payloads keyed by summary type (sleeps, dailies, userMetrics,
hrv, stressDetails, etc.). We:
  - Store every summary type in SQLite so later briefs can read recent values.
  - Trigger the morning brief only on `sleeps` (one trigger per calendarDate).

The handler must ack 200 within 30s; brief generation runs in a background task.
"""
import json
import logging
import random
import re
import secrets
from datetime import date

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from src import db
from src.clients import telegram
from src.config import settings
from src.conversation import handle_message as handle_chat_message
from src.pipeline import morning_brief
from src.telegram_commands import dispatch as dispatch_command

# Short status messages the bot fires immediately on inbound non-slash text, so the
# user sees activity within ~200ms instead of waiting 5–30s for Claude to respond.
_LOADING_PHRASES = (
    "Thinking...",
    "Pondering...",
    "Loading...",
    "On it...",
    "Just a sec...",
    "Looking it up...",
    "Crunching numbers...",
    "Cogitating...",
    "Hmm...",
    "Reading your data...",
)

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="morning-brief-agent")

_MAX_GARMIN_BODY_BYTES = 1_000_000
_SUMMARY_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/garmin/push/{webhook_secret}")
async def garmin_push(
    webhook_secret: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Accept a single runner's Garmin payload only after complete validation."""
    expected_secret = settings.garmin_webhook_secret
    if not expected_secret:
        logger.error("Garmin webhook is disabled: GARMIN_WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=503, detail="webhook not configured")
    if not secrets.compare_digest(webhook_secret, expected_secret):
        raise HTTPException(status_code=404, detail="not found")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_GARMIN_BODY_BYTES:
                raise HTTPException(status_code=413, detail="payload too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid content-length") from None

    raw_body = await request.body()
    if len(raw_body) > _MAX_GARMIN_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON") from None

    validated = _validate_garmin_payload(payload)

    received: dict[str, int] = {}
    stored = 0
    brief_dispatched = 0

    for summary_type, entries in validated.items():
        received[summary_type] = len(entries)
        for entry in entries:
            calendar_date = entry["calendarDate"]

            db.store_garmin_summary(
                summary_type=summary_type,
                calendar_date=calendar_date,
                payload=entry,
                summary_id=entry.get("summaryId"),
            )
            stored += 1

            if summary_type == "sleeps" and _should_trigger_brief(entry):
                db.mark_brief_sent(calendar_date, entry.get("summaryId") or "")
                background_tasks.add_task(_run_brief_safely, entry)
                brief_dispatched += 1

    return {
        "received": received,
        "stored": stored,
        "brief_dispatched": brief_dispatched,
    }


def _validate_garmin_payload(payload: object) -> dict[str, list[dict]]:
    """Validate the complete batch before any health data is persisted."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    tokens = db.get_tokens()
    if not tokens:
        raise HTTPException(status_code=503, detail="Garmin account not bootstrapped")
    expected_user_id = str(tokens["user_id"])

    validated: dict[str, list[dict]] = {}
    for summary_type, entries in payload.items():
        if not isinstance(summary_type, str) or not _SUMMARY_TYPE_RE.fullmatch(summary_type):
            raise HTTPException(status_code=400, detail="invalid summary type")
        if not isinstance(entries, list):
            raise HTTPException(status_code=400, detail=f"{summary_type} must be a list")

        valid_entries: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise HTTPException(status_code=400, detail=f"invalid {summary_type} entry")

            user_id = entry.get("userId")
            if not isinstance(user_id, str) or not secrets.compare_digest(
                user_id, expected_user_id
            ):
                logger.warning("rejected Garmin payload for an unauthorized or missing user")
                raise HTTPException(status_code=403, detail="unauthorized Garmin user")

            calendar_date = entry.get("calendarDate")
            if not isinstance(calendar_date, str):
                raise HTTPException(status_code=400, detail="missing calendarDate")
            try:
                date.fromisoformat(calendar_date)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid calendarDate") from None
            valid_entries.append(entry)
        validated[summary_type] = valid_entries
    return validated


def _should_trigger_brief(entry: dict) -> bool:
    """Brief fires once per calendar date. Updates and replays are ignored."""
    calendar_date = entry["calendarDate"]

    if db.is_brief_sent(calendar_date):
        logger.info("brief already sent for %s; ignoring update", calendar_date)
        return False

    return True


def _run_brief_safely(sleep_entry: dict) -> None:
    try:
        morning_brief(sleep_summary=sleep_entry)
    except Exception:
        logger.exception(
            "morning_brief failed for sleep summaryId=%s",
            sleep_entry.get("summaryId"),
        )


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    """Inbound Telegram webhook. Phase 5.0: echo-only.

    Authenticates via Telegram's secret_token mechanism (set during setWebhook).
    Drops messages from any chat_id other than the configured TELEGRAM_CHAT_ID,
    so even if a stranger finds the bot, they can't make it respond.
    """
    if not settings.telegram_webhook_secret:
        raise HTTPException(status_code=503, detail="webhook not configured")
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        logger.warning("/telegram/webhook bad secret token; rejected")
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json") from None

    message = update.get("message")
    if not message:
        # We only handle text messages right now. Edits, callbacks, etc. ignored.
        return {"ok": True, "skipped": "no message"}

    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id != str(settings.telegram_chat_id):
        logger.warning("inbound from unauthorized chat_id=%s; ignoring", chat_id)
        return {"ok": True, "skipped": "unauthorized chat_id"}

    text = (message.get("text") or "").strip()
    if not text:
        return {"ok": True, "skipped": "empty text"}

    result = dispatch_command(text, chat_id=chat_id, message_id=message.get("message_id", 0))
    if result is None:
        # Non-slash chat: send a loading message immediately so the user sees activity,
        # then dispatch the (slow) Claude call as a background task so the webhook
        # returns 200 fast (avoids Telegram's webhook timeout).
        try:
            telegram.send_message(random.choice(_LOADING_PHRASES), parse_mode=None)
        except Exception:
            logger.exception("failed to send loading message; proceeding")
        background_tasks.add_task(_run_chat_safely, chat_id, text)
        return {"ok": True, "replied": True, "via": "chat-scheduled"}

    telegram.send_message(result.text, parse_mode=result.parse_mode)
    return {"ok": True, "replied": True, "via": "command"}


def _run_chat_safely(chat_id: str, user_text: str) -> None:
    try:
        reply = handle_chat_message(chat_id, user_text)
    except Exception as e:
        logger.exception("conversation handler failed for chat=%s", chat_id)
        reply = f"error: {type(e).__name__}: {e}"
    try:
        telegram.send_message(reply, parse_mode="Markdown")
    except Exception:
        logger.exception("failed to send chat reply")
