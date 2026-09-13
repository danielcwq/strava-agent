#!/usr/bin/env python3
"""Inspect private traces and back up/restore SQLite without loading app credentials."""

import argparse
import html
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def connect(path: Path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def verify(path: Path) -> dict:
    """Open a restored copy, verify SQLite and event JSON, report counts only."""
    with connect(path) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("foreign key check failed")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "run_events" in tables:
            for row in conn.execute("SELECT payload FROM run_events"):
                json.loads(row[0])
        return {
            table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in ("agent_runs", "run_events", "profile_revisions", "conversation_turns")
            if table in tables
        }


def backup(source: Path, destination: Path) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with connect(source) as src, sqlite3.connect(destination) as dst:
            src.backup(dst)
        # Restore to a separate database and inspect that copy, not only the backup file.
        with tempfile.TemporaryDirectory() as directory:
            restored = Path(directory) / "restored.db"
            with connect(destination) as src, sqlite3.connect(restored) as dst:
                src.backup(dst)
            return verify(restored)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def export_run(source: Path, run_id: str, output_dir: Path) -> tuple[Path, Path]:
    with connect(source) as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("unknown run")
        run = dict(row)
        run["input_json"] = json.loads(run["input_json"])
        events = [
            {**dict(r), "payload": json.loads(r["payload"])}
            for r in conn.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY id", (run_id,))
        ]
    # Validate the identifier before it can become a filename.
    import uuid

    uuid.UUID(run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    json_path, html_path = output_dir / f"{run_id}.json", output_dir / f"{run_id}.html"
    document = {"run": run, "events": events}
    body = "".join(
        f"<details open><summary>{e['id']} · {html.escape(e['kind'])} · "
        f"{datetime.fromtimestamp(e['created_at'] / 1000, UTC).isoformat()}</summary>"
        f"<pre>{html.escape(json.dumps(e['payload'], indent=2, ensure_ascii=False))}</pre>"
        "</details>"
        for e in events
    )
    page = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Private bot trace</title><style>
body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 20px;color:#17202a}
details{border:1px solid #ccd3da;border-radius:8px;margin:12px 0;padding:14px}
summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}
</style><h1>Private bot trace</h1>"""
    page += "<pre>" + html.escape(json.dumps(run, indent=2, ensure_ascii=False)) + "</pre>"
    page += body + "</html>"
    for path, content in (
        (json_path, json.dumps(document, indent=2, ensure_ascii=False)),
        (html_path, page),
    ):
        with path.open("x", encoding="utf-8") as f:
            path.chmod(0o600)
            f.write(content)
    return json_path, html_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list recent runs without message contents")
    export = sub.add_parser("export", help="export one private JSON/HTML trace")
    export.add_argument("run_id")
    export.add_argument("--output-dir", type=Path, default=Path("private-artifacts"))
    for name in ("backup", "restore"):
        cmd = sub.add_parser(
            name, help="copy consistently to a NEW file and verify restored contents"
        )
        cmd.add_argument("destination", type=Path)
    sub.add_parser("verify", help="validate a restored database")
    args = parser.parse_args()
    if args.command == "list":
        with connect(args.db) as conn:
            rows = conn.execute(
                "SELECT id,kind,status,created_at,legacy_incomplete FROM agent_runs "
                "ORDER BY rowid DESC LIMIT 30"
            ).fetchall()
            print(json.dumps([dict(r) for r in rows], indent=2))
    elif args.command == "export":
        print("\n".join(map(str, export_run(args.db, args.run_id, args.output_dir))))
    elif args.command in {"backup", "restore"}:
        print(json.dumps(backup(args.db, args.destination), indent=2))
    else:
        print(json.dumps(verify(args.db), indent=2))


if __name__ == "__main__":
    main()
