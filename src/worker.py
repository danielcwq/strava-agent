"""Single-consumer durable queue for the personal bot (one app process / volume)."""

import json
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from src import archive, db
from src.clients import telegram
from src.config import settings

logger = logging.getLogger(__name__)
_consumer = threading.Lock()


def process_pending() -> None:
    if not _consumer.acquire(blocking=False):
        return
    try:
        while run := archive.claim_next():
            with archive.bind(run["id"]):
                try:
                    failed = _execute(run)
                except Exception as exc:
                    logger.error("run %s failed: %s", run["id"], type(exc).__name__)
                    archive.event(
                        "run.error", {"error_type": type(exc).__name__, "message": str(exc)}
                    )
                    # A send attempt could already have succeeded despite a lost response.
                    # Never automatically repeat a potentially delivered reply.
                    delivery_attempted = any(
                        e["kind"] == "delivery.attempt"
                        and e["payload"].get("category") != "loading"
                        for e in archive.events(run["id"])
                    )
                    if not delivery_attempted:
                        try:
                            changes = [
                                e["payload"]
                                for e in archive.events(run["id"])
                                if e["kind"] == "profile.updated"
                            ]
                            saved_notice = (
                                f" Profile revision {changes[-1]['revision']} was saved; "
                                "use /context to review it."
                                if changes
                                else ""
                            )
                            telegram.send_message(
                                f"I couldn't complete that request ({type(exc).__name__}). "
                                f"The trace is saved: {run['id']}.{saved_notice}",
                                parse_mode=None,
                            )
                        except Exception:
                            logger.error("failed to deliver error notice for %s", run["id"])
                    archive.finish(run["id"], "failed")
                else:
                    archive.finish(run["id"], "failed" if failed else "completed")
    finally:
        _consumer.release()


def _execute(run: dict) -> bool:
    from src.conversation import handle_message
    from src.pipeline import morning_brief
    from src.telegram_commands import dispatch

    payload = json.loads(run["input_json"])
    if run["kind"] == "brief":
        morning_brief(sleep_summary=payload)
        return False
    if run["kind"] == "command":
        archive.event("command.started", {"text": payload["text"]})
        result = dispatch(
            payload["text"], chat_id=run["chat_id"], message_id=payload.get("message_id", 0)
        )
        if result is None:
            raise ValueError("empty slash command")
        archive.event("command.finished", {"text": result.text, "failed": result.failed})
        telegram.send_message(result.text, parse_mode=result.parse_mode)
        return result.failed
    if run["kind"] != "chat":
        raise ValueError("unsupported queued run kind")
    try:
        telegram.send_message("Thinking...", parse_mode=None, category="loading")
    except Exception:
        archive.event("loading.failed", {})
    reply = handle_message(run["chat_id"], payload["text"])
    telegram.send_message(reply, parse_mode="Markdown")
    return False


def serve(stop: threading.Event) -> None:
    backed_up_day = None
    while not stop.is_set():
        try:
            today = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
            if today != backed_up_day:
                destination = settings.data_dir / "backups" / f"state-{today}.db"
                try:
                    if not destination.exists():
                        db.backup_database(destination)
                except Exception:
                    logger.exception("daily backup failed; queue processing will continue")
                backed_up_day = today
            process_pending()
        except Exception:
            logger.exception("queue processing failed")
        stop.wait(1)
