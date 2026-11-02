"""Bounded local snapshots and readiness checks for the writable state volume."""

import logging
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import date

from src import db
from src.config import settings

logger = logging.getLogger(__name__)
MIB = 1024 * 1024


def _snapshots():
    result = []
    for path in (settings.data_dir / "backups").glob("state-*.db"):
        if path.is_symlink() or not path.is_file():
            continue
        match = re.fullmatch(r"state-(\d{4}-\d{2}-\d{2})\.db", path.name)
        if not match:
            continue
        try:
            date.fromisoformat(match[1])
        except ValueError:
            continue
        result.append(path)
    return sorted(result)


def _prune(*, count: int, byte_limit: int, required_free: int):
    files = _snapshots()
    total = sum(p.stat().st_size for p in files)
    while len(files) > 1 and (
        len(files) > count
        or total > byte_limit
        or shutil.disk_usage(settings.data_dir).free < required_free
    ):
        oldest = files.pop(0)
        size = oldest.stat().st_size
        oldest.unlink()
        total -= size
        logger.info("removed expired daily snapshot %s bytes=%s", oldest.name, size)


def daily_backup(day: str) -> None:
    date.fromisoformat(day)
    destination = settings.data_dir / "backups" / f"state-{day}.db"
    reserve = settings.storage_min_free_mb * MIB
    limit = settings.backup_max_mb * MIB
    # Include the WAL as a conservative allowance for committed growth.
    needed = sum(
        p.stat().st_size
        for p in (settings.data_dir / "state.db", settings.data_dir / "state.db-wal")
        if p.exists()
    )
    required_free = reserve + needed
    if destination.exists():
        _prune(count=settings.backup_keep_count, byte_limit=limit, required_free=reserve)
        return
    # Prune before allocating another full copy; retain the newest existing
    # snapshot until a new integrity-checked snapshot has been published.
    _prune(
        count=max(1, settings.backup_keep_count - 1),
        byte_limit=max(0, limit - needed),
        required_free=required_free,
    )
    if shutil.disk_usage(settings.data_dir).free < required_free:
        raise OSError("insufficient free storage for a snapshot and database write reserve")
    db.backup_database(destination)
    _prune(count=settings.backup_keep_count, byte_limit=limit, required_free=reserve)


def ready() -> bool:
    try:
        if shutil.disk_usage(settings.data_dir).free < settings.storage_min_free_mb * MIB:
            return False
        # Exercise a real SQLite write without keeping probe data or schema changes.
        uri = (settings.data_dir / "state.db").resolve().as_uri() + "?mode=rw"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO archive_metadata VALUES (?,?)", ("_health_probe", "ok")
            )
            conn.rollback()
        return True
    except (OSError, sqlite3.Error):
        return False
