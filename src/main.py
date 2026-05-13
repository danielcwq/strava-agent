"""FastAPI receiver for Garmin Health API Push notifications.

Endpoints:
    GET  /health        — liveness check for Fly
    POST /garmin/push   — Garmin push notifications; triggers morning brief on sleeps

Garmin sends push payloads keyed by summary type (sleeps, dailies, userMetrics,
hrv, stressDetails, etc.). We:
  - Store every summary type in SQLite so later briefs can read recent values.
  - Trigger the morning brief only on `sleeps` (one trigger per calendarDate).

The handler must ack 200 within 30s; brief generation runs in a background task.
"""
import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from src import db
from src.config import settings
from src.pipeline import morning_brief

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="morning-brief-agent")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/garmin/push")
async def garmin_push(request: Request, background_tasks: BackgroundTasks) -> dict:
    try:
        payload = await request.json()
    except Exception as e:
        logger.warning("invalid JSON on /garmin/push: %s", e)
        raise HTTPException(status_code=400, detail="invalid JSON")

    received: dict[str, int] = {}
    stored = 0
    brief_dispatched = 0

    for summary_type, entries in payload.items():
        if not isinstance(entries, list):
            logger.info("ignoring non-list payload key: %s", summary_type)
            continue
        received[summary_type] = len(entries)
        for entry in entries:
            calendar_date = entry.get("calendarDate")
            if not calendar_date:
                logger.warning("%s entry missing calendarDate, skipping", summary_type)
                continue

            db.store_garmin_summary(
                summary_type=summary_type,
                calendar_date=calendar_date,
                payload=entry,
                summary_id=entry.get("summaryId"),
            )
            stored += 1

            if summary_type == "sleeps" and _should_trigger_brief(entry):
                db.mark_brief_sent(calendar_date, entry.get("summaryId", ""))
                background_tasks.add_task(_run_brief_safely, entry)
                brief_dispatched += 1

    return {
        "received": received,
        "stored": stored,
        "brief_dispatched": brief_dispatched,
    }


def _should_trigger_brief(entry: dict) -> bool:
    """Brief fires once per (userId, calendarDate). Updates and replays are ignored."""
    calendar_date = entry["calendarDate"]

    tokens = db.get_tokens()
    if tokens and entry.get("userId") and entry["userId"] != tokens["user_id"]:
        logger.warning(
            "sleep push userId %s does not match stored user_id %s; skipping brief",
            entry.get("userId"),
            tokens["user_id"],
        )
        return False

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
