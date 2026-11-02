import random
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import db, main, maintenance, worker
from src.config import settings


def initialize():
    with db.get_conn() as conn:
        conn.execute("INSERT INTO archive_metadata VALUES ('preserved','context')")


def test_daily_backups_are_bounded_and_original_context_survives():
    initialize()
    for n in range(20):
        maintenance.daily_backup((date(2026, 10, 1) + timedelta(days=n)).isoformat())
    files = maintenance._snapshots()
    assert len(files) == settings.backup_keep_count
    assert files[0].name == "state-2026-10-14.db"
    assert files[-1].name == "state-2026-10-20.db"
    with db.get_conn() as conn:
        assert (
            conn.execute("SELECT value FROM archive_metadata WHERE key='preserved'").fetchone()[0]
            == "context"
        )
    maintenance.daily_backup("2026-10-20")
    assert maintenance._snapshots() == files


def test_prunes_before_allocating_when_volume_is_full(monkeypatch):
    initialize()
    root = settings.data_dir / "backups"
    for n in range(10):
        db.backup_database(root / f"state-2026-10-{n + 1:02}.db")
    files = maintenance._snapshots()
    initial_bytes = sum(f.stat().st_size for f in files)
    needed = (settings.data_dir / "state.db").stat().st_size
    reserve = settings.storage_min_free_mb * maintenance.MIB

    def usage(_):
        remaining = sum(f.stat().st_size for f in maintenance._snapshots())
        return SimpleNamespace(free=reserve + initial_bytes - remaining)

    monkeypatch.setattr(maintenance.shutil, "disk_usage", usage)
    original = db.backup_database
    calls = []

    def create(destination):
        calls.append(usage(None).free)
        assert usage(None).free >= reserve + needed
        original(destination)

    monkeypatch.setattr(db, "backup_database", create)
    maintenance.daily_backup("2026-10-21")
    assert len(calls) == 1
    assert len(maintenance._snapshots()) <= 7


def test_byte_limit_and_unrelated_files(monkeypatch):
    initialize()
    monkeypatch.setattr(settings, "backup_max_mb", 1)
    root = settings.data_dir / "backups"
    root.mkdir()
    unrelated = root / "manual-important.db"
    unrelated.write_text("keep")
    invalid = root / "state-2026-99-99.db"
    invalid.write_text("keep")
    outside = settings.data_dir / "manual-source.db"
    outside.write_text("keep")
    link = root / "state-2026-10-01.db"
    link.symlink_to(outside)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO archive_metadata VALUES ('large',?)",
            (random.Random(1).randbytes(800_000),),
        )
    for day in range(2, 6):
        maintenance.daily_backup(f"2026-10-{day:02}")
    assert len(maintenance._snapshots()) == 1
    assert sum(f.stat().st_size for f in maintenance._snapshots()) <= maintenance.MIB
    assert unrelated.read_text() == invalid.read_text() == outside.read_text() == "keep"
    assert link.is_symlink()


def test_skip_backup_without_consuming_database_reserve(monkeypatch):
    initialize()
    existing = settings.data_dir / "backups" / "state-2026-10-20.db"
    db.backup_database(existing)
    monkeypatch.setattr(maintenance.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(OSError, match="reserve"):
        maintenance.daily_backup("2026-10-21")
    assert maintenance._snapshots() == [existing]
    assert not list(existing.parent.glob("*.partial"))


def test_failed_snapshot_never_published_and_existing_snapshot_not_overwritten(monkeypatch):
    initialize()
    destination = settings.data_dir / "backups" / "state-2026-10-21.db"

    @contextmanager
    def broken_source():
        class Broken:
            def backup(self, _):
                raise OSError("disk full")

        yield Broken()

    monkeypatch.setattr(db, "get_conn", broken_source)
    with pytest.raises(OSError):
        db.backup_database(destination)
    assert not destination.exists()
    assert not list(destination.parent.iterdir())
    destination.write_text("keep")
    with pytest.raises(ValueError):
        db.backup_database(destination)
    assert destination.read_text() == "keep"


def test_readiness_detects_low_space_and_failed_writes(monkeypatch):
    initialize()
    client = TestClient(main.app)
    assert client.get("/health").status_code == 200
    with db.get_conn() as conn:
        assert not conn.execute(
            "SELECT 1 FROM archive_metadata WHERE key='_health_probe'"
        ).fetchone()
    original = shutil.disk_usage
    monkeypatch.setattr(maintenance.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    assert client.get("/health").status_code == 503
    monkeypatch.setattr(maintenance.shutil, "disk_usage", original)
    (settings.data_dir / "state.db").unlink()
    assert client.get("/health").status_code == 503
    assert not (settings.data_dir / "state.db").exists()


def test_queue_still_processed_when_backup_fails(monkeypatch):
    initialize()
    stop = threading.Event()
    calls = []

    def fail(_):
        raise OSError("disk full")

    def pending():
        calls.append("processed")
        stop.set()

    monkeypatch.setattr(maintenance, "daily_backup", fail)
    monkeypatch.setattr(worker, "process_pending", pending)
    worker.serve(stop)
    assert calls == ["processed"]


def test_health_recovers_after_database_lock_released():
    initialize()
    with sqlite3.connect(settings.data_dir / "state.db") as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert not maintenance.ready()
        conn.rollback()
    assert maintenance.ready()
