from fastapi.testclient import TestClient

from src import main

_SECRET = "a" * 64
_USER_ID = "authorized-user"


def _sleep_entry(**overrides) -> dict:
    entry = {
        "userId": _USER_ID,
        "calendarDate": "2026-07-25",
        "summaryId": "sleep-1",
    }
    entry.update(overrides)
    return entry


def _configure_webhook(monkeypatch):
    stored: list[tuple[str, str, dict, str | None]] = []
    marked: list[tuple[str, str]] = []
    dispatched: list[dict] = []

    monkeypatch.setattr(main.settings, "garmin_webhook_secret", _SECRET)
    monkeypatch.setattr(main, "_today_local", lambda: "2026-07-25")
    monkeypatch.setattr(main.db, "get_tokens", lambda: {"user_id": _USER_ID})
    monkeypatch.setattr(main.db, "is_brief_sent", lambda _date: False)
    monkeypatch.setattr(
        main.db,
        "store_garmin_summary",
        lambda summary_type, calendar_date, payload, summary_id=None: stored.append(
            (summary_type, calendar_date, payload, summary_id)
        ),
    )
    monkeypatch.setattr(
        main.db,
        "mark_brief_sent",
        lambda calendar_date, summary_id: marked.append((calendar_date, summary_id)),
    )

    def enqueue(chat_id, kind, payload, source_key=None, brief_date=None):
        marked.append((brief_date, payload.get("summaryId", "")))
        dispatched.append(payload)
        return "run-1", True

    monkeypatch.setattr(main.archive, "enqueue", enqueue)
    monkeypatch.setattr(main.worker, "process_pending", lambda: None)
    return stored, marked, dispatched


def test_rejects_wrong_webhook_secret_before_reading_account(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "garmin_webhook_secret", _SECRET)
    monkeypatch.setattr(
        main.db,
        "get_tokens",
        lambda: (_ for _ in ()).throw(AssertionError("must not read account")),
    )

    response = TestClient(main.app).post(
        "/garmin/push/wrong-secret",
        json={"sleeps": [_sleep_entry()]},
    )

    assert response.status_code == 404


def test_rejects_missing_or_wrong_garmin_user_before_storage(monkeypatch) -> None:
    stored, _, _ = _configure_webhook(monkeypatch)
    client = TestClient(main.app)

    missing = client.post(
        f"/garmin/push/{_SECRET}",
        json={"sleeps": [_sleep_entry(userId=None)]},
    )
    wrong = client.post(
        f"/garmin/push/{_SECRET}",
        json={"sleeps": [_sleep_entry(userId="someone-else")]},
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert stored == []


def test_validates_complete_batch_before_any_storage(monkeypatch) -> None:
    stored, _, _ = _configure_webhook(monkeypatch)

    response = TestClient(main.app).post(
        f"/garmin/push/{_SECRET}",
        json={
            "dailies": [_sleep_entry(summaryId="daily-1")],
            "sleeps": [_sleep_entry(calendarDate="not-a-date")],
        },
    )

    assert response.status_code == 400
    assert stored == []


def test_authorized_sleep_is_stored_and_dispatched(monkeypatch) -> None:
    stored, marked, dispatched = _configure_webhook(monkeypatch)
    entry = _sleep_entry()

    response = TestClient(main.app).post(
        f"/garmin/push/{_SECRET}",
        json={"sleeps": [entry]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "received": {"sleeps": 1},
        "stored": 1,
        "brief_dispatched": 1,
    }
    assert stored == [("sleeps", "2026-07-25", entry, "sleep-1")]
    assert marked == [("2026-07-25", "sleep-1")]
    assert dispatched == [entry]


def test_webhook_is_disabled_without_configured_secret(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "garmin_webhook_secret", None)

    response = TestClient(main.app).post(
        f"/garmin/push/{_SECRET}",
        json={"sleeps": [_sleep_entry()]},
    )

    assert response.status_code == 503


def test_replayed_sleep_batch_uses_today_after_all_records_are_stored(monkeypatch):
    for reverse_order in (False, True):
        stored, marked, dispatched = _configure_webhook(monkeypatch)
        yesterday = _sleep_entry(calendarDate="2026-07-24", summaryId="old")
        today = _sleep_entry(summaryId="current")
        entries = [yesterday, today] if not reverse_order else [today, yesterday]
        original = main.archive.enqueue

        def enqueue(*args, stored=stored, original=original, **kwargs):
            assert len(stored) == 3  # both sleep records and the readiness metrics
            return original(*args, **kwargs)

        monkeypatch.setattr(main.archive, "enqueue", enqueue)
        response = TestClient(main.app).post(
            f"/garmin/push/{_SECRET}",
            json={"sleeps": entries, "hrv": [_sleep_entry(summaryId="hrv")]},
        )
        assert response.status_code == 200
        assert response.json()["stored"] == 3
        assert response.json()["brief_dispatched"] == 1
        assert dispatched == [today]
        assert marked == [("2026-07-25", "current")]


def test_historical_only_batch_is_saved_without_sending_stale_briefs(monkeypatch):
    stored, marked, dispatched = _configure_webhook(monkeypatch)
    response = TestClient(main.app).post(
        f"/garmin/push/{_SECRET}",
        json={
            "sleeps": [
                _sleep_entry(calendarDate="2026-07-23"),
                _sleep_entry(calendarDate="2026-07-24"),
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["stored"] == 2
    assert response.json()["brief_dispatched"] == 0
    assert len(stored) == 2 and not marked and not dispatched


def test_same_day_correction_uses_last_stored_record(monkeypatch):
    stored, _, dispatched = _configure_webhook(monkeypatch)
    old = _sleep_entry(summaryId="preliminary")
    corrected = _sleep_entry(summaryId="corrected")
    response = TestClient(main.app).post(
        f"/garmin/push/{_SECRET}", json={"sleeps": [old, corrected]}
    )
    assert response.status_code == 200
    assert stored[-1][2] == corrected
    assert dispatched == [corrected]


def test_current_day_dedup_survives_replayed_batches(monkeypatch):
    stored, marked, dispatched = _configure_webhook(monkeypatch)
    monkeypatch.setattr(main.db, "is_brief_sent", lambda day: day == "2026-07-25")
    response = TestClient(main.app).post(
        f"/garmin/push/{_SECRET}",
        json={"sleeps": [_sleep_entry(calendarDate="2026-07-24"), _sleep_entry()]},
    )
    assert response.status_code == 200 and len(stored) == 2
    assert response.json()["brief_dispatched"] == 0
    assert not marked and not dispatched
