from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src import archive, db, pipeline, worker
from src.config import settings


def test_delayed_garmin_run_does_not_send_yesterdays_sleep(monkeypatch):
    yesterday = (datetime.now(ZoneInfo(settings.timezone)).date() - timedelta(days=1)).isoformat()
    rid, _ = archive.enqueue(
        "123", "brief", {"calendarDate": yesterday}, source_key="garmin-sleep:" + yesterday
    )

    def reject(**kwargs):
        raise AssertionError("stale brief must not be synthesized or sent")

    monkeypatch.setattr(pipeline, "morning_brief", reject)
    worker.process_pending()
    assert archive.get_run(rid)["status"] == "completed"
    assert any(e["kind"] == "brief.skipped" for e in archive.events(rid))
    assert not any(e["kind"] == "delivery.attempt" for e in archive.events(rid))


def test_worker_reads_latest_saved_correction_for_today(monkeypatch):
    today = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    initial = {"calendarDate": today, "summaryId": "preliminary"}
    corrected = {"calendarDate": today, "summaryId": "corrected"}
    rid, _ = archive.enqueue("123", "brief", initial, source_key="garmin-sleep:" + today)
    db.store_garmin_summary("sleeps", today, corrected)
    seen = []
    monkeypatch.setattr(pipeline, "morning_brief", lambda **kwargs: seen.append(kwargs))
    worker.process_pending()
    assert seen == [{"sleep_summary": corrected}]
    assert archive.get_run(rid)["status"] == "completed"


def test_manual_recovery_without_sleep_remains_available(monkeypatch):
    rid, _ = archive.enqueue("123", "brief", {}, source_key="manual-recovery")
    seen = []
    monkeypatch.setattr(pipeline, "morning_brief", lambda **kwargs: seen.append(kwargs))
    worker.process_pending()
    assert seen == [{"sleep_summary": {}}]
    assert archive.get_run(rid)["status"] == "completed"
