"""FastAPI receiver for Garmin Health API Push notifications.

Endpoints:
    GET  /health        — liveness check for Fly
    POST /garmin/push   — Garmin sleep summary push; triggers morning brief

The push handler must ack 200 within 30 seconds (Garmin counts >30s or non-2xx
as failed and retries with backoff). Work is dispatched to a background task so
the response is immediate.
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

    sleeps = payload.get("sleeps") or []
    other_keys = [k for k in payload.keys() if k != "sleeps"]
    if other_keys:
        # Garmin may push other summary types (dailies, userMetrics, etc.) if
        # we subscribe to them. We accept the request but don't act on them in v0.
        logger.info("ignoring non-sleep summary types in push: %s", other_keys)

    accepted = 0
    for entry in sleeps:
        if _should_handle(entry):
            calendar_date = entry["calendarDate"]
            summary_id = entry.get("summaryId", "")
            # Mark sent BEFORE dispatching so Garmin retries don't double-fire.
            # Trade-off: if the background task fails, the brief is lost for the day.
            db.mark_brief_sent(calendar_date, summary_id)
            background_tasks.add_task(_run_brief_safely, entry)
            accepted += 1

    return {"accepted_sleeps": accepted, "received_sleeps": len(sleeps)}


def _should_handle(entry: dict) -> bool:
    calendar_date = entry.get("calendarDate")
    if not calendar_date:
        logger.warning("sleep entry missing calendarDate, skipping")
        return False

    tokens = db.get_tokens()
    if tokens and entry.get("userId") and entry["userId"] != tokens["user_id"]:
        logger.warning(
            "push userId %s does not match our user_id %s; skipping",
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
