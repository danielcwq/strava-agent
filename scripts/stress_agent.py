#!/usr/bin/env python3
"""Opt-in live-model stress suite against an isolated, private database copy.

Requires configured provider/data credentials. Makes real model and read-only data
API calls, but never sends Telegram messages or edits the source database. Reports
and traces contain private training data and must not be committed or published.
"""

import argparse
import json
import os
import shutil
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="consistent SQLite backup")
    parser.add_argument("--output", type=Path, required=True, help="NEW private output directory")
    parser.add_argument("--replay-trace", type=Path, help="use original failed message for case 1")
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--cases", nargs="+", help="optional subset of named scenarios")
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error("real API calls require --allow-model-calls")
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    shutil.copyfile(args.db, args.output / "state.db")
    os.chmod(args.output / "state.db", 0o600)

    from src import archive, conversation, training_config
    from src.config import settings

    settings.data_dir = args.output.resolve()
    initial = "Assess whether my next race goal is realistic based on recent training."
    if args.replay_trace:
        trace = json.loads(args.replay_trace.read_text())
        settings.telegram_chat_id = trace["run"]["chat_id"]
        initial = trace["run"]["input_json"]["text"]
    owner = str(settings.telegram_chat_id)
    baseline = training_config.get_profile()
    cases = [
        ("race_assessment", initial, "read"),
        (
            "evidence_followup",
            "Which specific sessions support that assessment? Give dates and "
            "distinguish warmups/cooldowns from quality work. Do not change my preferences.",
            "read",
        ),
        (
            "wellness_and_long_runs",
            "Compare my last 30 days of wellness with my recent long runs. "
            "What does the data actually support, and where is it missing? "
            "Do not change preferences.",
            "read",
        ),
        (
            "history_recall",
            "What did we decide in our earlier conversations about my race goal "
            "and my current life priorities? Do not change anything.",
            "read",
        ),
        (
            "coaching_edit",
            "Update my coaching instructions: when I ask for training advice, "
            "give one practical option and a brief rationale. Keep my race goal and weekly "
            "schedule unchanged.",
            "coaching",
        ),
        (
            "schedule_edit",
            "Set my recurring weekly schedule: Tuesday easy/support, Wednesday "
            "key quality, and Saturday long run. Keep my race goal and other days unchanged.",
            "schedule",
        ),
        (
            "saved_state_followup",
            "Read my saved profile and confirm my current weekly schedule, "
            "race goal, and the advice-style preference I just saved. Do not change anything.",
            "read",
        ),
    ]
    if args.cases:
        unknown = set(args.cases) - {case[0] for case in cases}
        if unknown:
            parser.error("unknown scenarios: " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case[0] in args.cases]
    report = []
    original_event = archive.event

    def event(kind, payload, run_id=None):
        result = original_event(kind, payload, run_id)
        if kind in {
            "model.request",
            "tool.started",
            "context.tool_result_archived",
            "profile.updated",
        }:
            print(
                json.dumps(
                    {"event": kind, "purpose": payload.get("purpose"), "tool": payload.get("name")}
                ),
                flush=True,
            )
        return result

    archive.event = event
    # Cache identical read-only data queries during this test session. Reads of
    # profile/history/archive are deliberately uncached so follow-ups test persistence.
    for name in (
        "get_recent_runs",
        "get_wellness_window",
        "get_activity_detail",
        "search_workouts",
    ):
        original = conversation.TOOL_FUNCS[name]
        cache = {}

        def cached(arguments, original=original, cache=cache):
            key = json.dumps(arguments, sort_keys=True)
            if key not in cache:
                cache[key] = original(arguments)
            return cache[key]

        conversation.TOOL_FUNCS[name] = cached

    for name, text, kind in cases:
        start = time.monotonic()
        before = training_config.get_profile()
        run_id, _ = archive.enqueue(
            owner, "chat", {"text": text, "sender_id": int(owner)}, source_key="stress:" + name
        )
        claimed = archive.claim_next()
        if claimed is None or claimed["id"] != run_id:
            raise RuntimeError("snapshot contains pending work; use a quiescent backup")
        print(json.dumps({"case": name, "status": "started", "run_id": run_id}), flush=True)
        result = {"case": name, "run_id": run_id}
        try:
            with archive.bind(run_id):
                reply = conversation.handle_message(owner, text)
            after = training_config.get_profile()
            assert reply.strip()
            if kind == "read":
                assert after == before, "read-only request mutated the profile"
            elif kind == "coaching":
                assert after["document"]["prompts"] != before["document"]["prompts"]
                assert after["document"]["race"] == before["document"]["race"]
                assert after["document"]["weekly_roles"] == before["document"]["weekly_roles"]
            else:
                roles = after["document"]["weekly_roles"]
                assert roles["1"] == "support" and roles["2"] == "key" and roles["5"] == "long"
                assert after["document"]["race"] == before["document"]["race"]
            archive.finish(run_id, "completed")
            result.update(status="passed", reply=reply)
        except Exception as exc:
            archive.finish(run_id, "failed")
            result.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        events = archive.events(run_id)
        result.update(
            seconds=round(time.monotonic() - start, 1),
            model_calls=sum(e["kind"] == "model.request" for e in events),
            tool_calls=sum(e["kind"] == "tool.started" for e in events),
            archived_results=sum(e["kind"] == "context.tool_result_archived" for e in events),
        )
        report.append(result)
        output = args.output / "report.json"
        output.write_text(
            json.dumps(
                {"baseline_revision": baseline["revision"], "cases": report},
                indent=2,
                ensure_ascii=False,
            )
        )
        output.chmod(0o600)
        print(
            json.dumps({k: v for k, v in result.items() if k not in {"reply", "error"}}), flush=True
        )
    if any(r["status"] != "passed" for r in report):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
