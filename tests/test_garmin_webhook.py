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
    monkeypatch.setattr(main, "_run_brief_safely", dispatched.append)
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
