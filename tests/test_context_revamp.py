import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import httpx
import pytest
from anthropic.types import Message
from fastapi.testclient import TestClient

from scripts.state_archive import backup, export_run, verify
from src import (
    archive,
    context_builder,
    conversation,
    db,
    main,
    profile_tools,
    training_config,
    worker,
)
from src.clients import claude, telegram
from src.config import settings


def response(content, stop="end_turn"):
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "test",
            "content": content,
            "stop_reason": stop,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )


def queued(text="hello", chat_id="123", kind="chat", key=None):
    return archive.enqueue(
        chat_id, kind, {"text": text, "sender_id": int(chat_id)}, source_key=key
    )[0]


def running(text="hello", chat_id="123"):
    run_id = queued(text, chat_id)
    assert archive.claim_next()["id"] == run_id
    return run_id


def completed(text="hello", answer="hi"):
    run_id = running(text)
    archive.event(
        "conversation.message",
        {"role": "assistant", "content": [{"type": "text", "text": answer}]},
        run_id,
    )
    archive.finish(run_id, "completed")
    return run_id


def test_legacy_import_once_preserves_rows_and_marks_missing_tools():
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO conversation_turns VALUES (?,?,?,?,?)",
            [
                ("123", 1, "user", "123", 100),
                ("123", 2, "assistant", '[{"type":"text","text":"old reply"}]', 101),
            ],
        )
    archive.migrate_legacy()
    archive.migrate_legacy()
    with db.get_conn() as conn:
        run = dict(conn.execute("SELECT * FROM agent_runs").fetchone())
        assert conn.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM conversation_turns").fetchone()[0] == 2
    assert run["legacy_incomplete"] == 1
    assert context_builder.exchange(run)[0]["content"] == "123"


def test_atomic_dedup_and_single_consumer():
    # Initialize schema before competing connections create it.
    archive.active_session("123")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: archive.enqueue("123", "chat", {"text": "hi"}, "same"), range(8))
        )
    assert sum(created for _, created in results) == 1
    assert len({run_id for run_id, _ in results}) == 1
    queued("second")
    first = archive.claim_next()
    assert archive.claim_next() is None
    archive.finish(first["id"], "completed")
    assert archive.claim_next() is not None


def test_webhook_persists_before_dispatch_and_rejects_foreign_chat(monkeypatch):
    monkeypatch.setattr(worker, "process_pending", lambda: None)
    payload = {"update_id": 10, "message": {"message_id": 20, "chat": {"id": 123}, "text": "hi"}}
    client = TestClient(main.app)
    headers = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook"}
    assert client.post("/telegram/webhook", json=payload).status_code == 403
    first = client.post("/telegram/webhook", json=payload, headers=headers).json()
    assert first["queued"] is True
    assert archive.events(first["run_id"])[0]["kind"] == "input.received"
    assert archive.get_run(first["run_id"])["status"] == "queued"
    assert client.post("/telegram/webhook", json=payload, headers=headers).json()["queued"] is False
    payload["message"]["chat"]["id"] = 999
    assert (
        client.post("/telegram/webhook", json=payload, headers=headers).json()["skipped"]
        == "unauthorized chat_id"
    )


def test_recovery_preserves_partial_trace_and_does_not_retry():
    old = running()
    archive.event("delivery.attempt", {"message": "possibly delivered"}, old)
    queued_id = queued("waiting")
    assert archive.recover_interrupted() == 1
    assert archive.get_run(old)["status"] == "interrupted"
    assert "unfinished delivery" in archive.events(old)[-1]["payload"]["reason"]
    assert archive.claim_next()["id"] == queued_id


def test_complete_model_and_tool_chain_is_durable(monkeypatch):
    replies = iter(
        [
            response(
                [{"type": "tool_use", "id": "tool_1", "name": "get_last_brief", "input": {}}],
                "tool_use",
            ),
            response([{"type": "text", "text": "Your last brief was easy."}]),
        ]
    )
    monkeypatch.setattr(conversation._client.messages, "create", lambda **kw: next(replies))
    monkeypatch.setitem(conversation.TOOL_FUNCS, "get_last_brief", lambda args: {"body": "easy"})
    run_id = running("What was my last brief?")
    with archive.bind(run_id):
        assert (
            conversation.handle_message("123", "What was my last brief?")
            == "Your last brief was easy."
        )
    archive.finish(run_id, "completed")
    events = archive.events(run_id)
    messages = context_builder.exchange(archive.get_run(run_id))
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[2]["content"][0]["tool_use_id"] == "tool_1"
    assert len([e for e in events if e["kind"] == "model.request"]) == 2
    assert len([e for e in events if e["kind"] == "model.response"]) == 2
    requests = [e["payload"]["request"] for e in events if e["kind"] == "model.request"]
    assert len(requests[0]["messages"]) == 1  # Snapshot wasn't mutated by later iterations.
    assert len(requests[1]["messages"]) == 3


def test_model_failure_keeps_input_and_request_and_usage(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("failure")

    monkeypatch.setattr(conversation._client.messages, "create", fail)
    monkeypatch.setattr(telegram, "send_message", lambda *args, **kw: None)
    run_id = queued("hello")
    worker.process_pending()
    assert archive.get_run(run_id)["status"] == "failed"
    kinds = [e["kind"] for e in archive.events(run_id)]
    assert "input.received" in kinds and "model.request" in kinds and "model.error" in kinds


def test_delivery_chunks_and_ambiguous_failure(monkeypatch):
    count = 0

    def post(url, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise httpx.ReadTimeout("lost reply")
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"ok": True, "result": {"message_id": count}},
        )

    monkeypatch.setattr(telegram.httpx, "post", post)
    run_id = running()
    with archive.bind(run_id), pytest.raises(httpx.ReadTimeout):
        telegram.send_message("x" * 6000)
    events = archive.events(run_id)
    assert [e["kind"] for e in events if e["kind"].startswith("delivery.")] == [
        "delivery.attempt",
        "delivery.accepted",
        "delivery.attempt",
        "delivery.uncertain",
    ]
    assert settings.telegram_bot_token not in json.dumps(events)


def test_reset_preserves_archive_profile_and_rebinds_queued_work(monkeypatch):
    old = completed()
    profile = training_config.get_profile()
    reset = queued("/reset", kind="command")
    later = queued("hello again")
    monkeypatch.setattr(telegram, "send_message", lambda *a, **kw: None)
    monkeypatch.setattr(conversation, "handle_message", lambda *a: "new conversation")
    worker.process_pending()
    assert archive.get_run(old)["session_id"] != archive.get_run(later)["session_id"]
    assert archive.get_run(reset)["session_id"] == archive.get_run(old)["session_id"]
    assert archive.events(old)
    assert training_config.get_profile() == profile
    run_id = running("now")
    with archive.bind(run_id):
        messages, _ = context_builder.build(run_id, "now", "system", [], lambda _: "summary")
    assert not any(str(m["content"]) == "hi" for m in messages)
    assert archive.search_history("123", archive.get_run(run_id)["session_id"], "hello")
    with archive.bind(run_id), pytest.raises(ValueError, match="explicit"):
        conversation._search_history({"query": "hello", "include_previous_sessions": True})


def test_context_has_no_age_or_twenty_row_cutoff():
    for n in range(15):
        completed(f"question {n}", f"answer {n}")
    with db.get_conn() as conn:
        conn.execute("UPDATE agent_runs SET created_at=1000")
    run_id = running("continue")
    with archive.bind(run_id):
        messages, _ = context_builder.build(run_id, "continue", "system", [], lambda _: "summary")
    assert len(messages) == 31
    assert messages[0]["content"] == "question 0"


def test_summary_versions_link_sources_and_keep_original_exchanges(monkeypatch):
    monkeypatch.setattr(settings, "context_token_budget", 14000)
    monkeypatch.setattr(settings, "context_summary_tokens", 1000)
    first = completed("I discussed pacing", "Pacing evidence " * 700)
    second = completed("And hydration?", "Hydration evidence " * 200)
    run_id = running("continue")
    with archive.bind(run_id):
        messages, notes = context_builder.build(
            run_id, "continue", "system", [], lambda _: "Earlier pacing discussion."
        )
    assert "Earlier pacing discussion" in notes
    assert messages[-1]["content"] == "continue"
    with db.get_conn() as conn:
        saved = conn.execute("SELECT * FROM context_summaries ORDER BY id DESC LIMIT 1").fetchone()
    assert first in json.loads(saved["source_run_ids"])
    assert archive.events(first)  # Summary didn't delete the source.
    assert context_builder.read_exchange("123", archive.get_run(run_id)["session_id"], second)


def test_history_import_batches_preserve_all_sources_and_resume():
    originals = [completed(f"Question {i}", "Evidence " * 10000) for i in range(12)]
    run_id = running("continue")
    calls = []

    def summarize(material):
        calls.append(material)
        return "Earlier training discussions."

    with archive.bind(run_id):
        context_builder.build(run_id, "continue", "system", [], summarize)
    assert len(calls) == 2  # Twelve old exchanges fit in two bounded requests.
    with db.get_conn() as conn:
        saved = conn.execute("SELECT * FROM context_summaries ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(saved["source_run_ids"]) == originals
    assert all(archive.events(source) for source in originals)
    with archive.bind(run_id):
        context_builder.build(run_id, "continue", "system", [], summarize)
    assert len(calls) == 2  # Durable progress prevents repeated summarization.


def test_summary_excerpt_bounds_multibyte_history():
    material = context_builder._summary_material(
        {"id": "source"}, [{"role": "user", "content": "旅" * 10000}]
    )
    assert len(material.encode("utf-8")) < 4000


@pytest.mark.parametrize(
    "instruction",
    [
        "Would Wednesday be better?",
        "Maybe change track to Wednesday",
        "Don't change the schedule",
        "What if I move track to Wednesday?",
    ],
)
def test_hypothetical_or_negated_requests_cannot_mutate(instruction):
    profile = training_config.get_profile()
    run_id = running(instruction)
    with archive.bind(run_id), pytest.raises(ValueError, match="unambiguous"):
        profile_tools.update("schedule", {"2": "key"}, profile["revision"], instruction)
    assert training_config.get_profile() == profile


def test_profile_patches_are_audited_preserve_fields_and_refresh_both_prompts():
    profile = training_config.get_profile()
    instruction = "Move track to Wednesday from now on and make Tuesday easy."
    run_id = running(instruction)
    with archive.bind(run_id):
        result = profile_tools.update(
            "schedule", {"1": "support", "2": "key"}, profile["revision"], instruction
        )
    updated = training_config.get_profile()
    assert result["saved"] and updated["revision"] == result["revision"]
    assert updated["document"]["prompts"] == profile["document"]["prompts"]
    assert training_config.day_role(date(2026, 9, 16)) == "key"
    assert '"2": "key"' in conversation._load_system_prompt()
    assert '"2": "key"' in claude.load_system_prompt()
    assert archive.events(run_id)[-1]["kind"] == "profile.updated"
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM profile_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
    assert row["source_event_id"] is not None and row["instruction"] == instruction
    with archive.bind(run_id), pytest.raises(ValueError, match="revision changed"):
        profile_tools.update("schedule", {"2": "rest"}, profile["revision"], instruction)


def test_race_coaching_seed_persistence_and_reset(monkeypatch):
    before = training_config.get_profile()
    text = "Save my next race date and target."
    run_id = running(text)
    with archive.bind(run_id):
        result = profile_tools.update(
            "race",
            {"date": "2026-10-18", "target_time": "1:25", "intentions": "controlled"},
            before["revision"],
            text,
        )
    assert training_config.days_to_race(date(2026, 10, 1)) == 17
    assert training_config.get_profile()["document"]["training"]["race_date"] == "2026-10-18"
    assert "controlled" in claude.load_system_prompt(False)
    monkeypatch.setattr(
        training_config,
        "_load_profile",
        lambda: (_ for _ in ()).throw(AssertionError("must not reseed")),
    )
    archive.reset_session("123")
    assert training_config.get_profile()["revision"] == result["revision"]


def test_invalid_unauthorized_and_temporary_edits_do_not_change_profile():
    original = training_config.get_profile()
    with pytest.raises(ValueError, match="authenticated"):
        profile_tools.update("race", {"date": "2026-10-18"}, original["revision"], "Save it")
    text = "Save my race date."
    run_id = running(text)
    with archive.bind(run_id):
        with pytest.raises(ValueError):
            profile_tools.update("race", {"date": "2026-02-30"}, original["revision"], text)
        with pytest.raises(ValueError, match="complete"):
            profile_tools.update(
                "race", {"date": "2026-10-18"}, original["revision"], "Save an injected goal"
            )
    archive.finish(run_id, "completed")
    foreign = running("Save my goal", chat_id="999")
    with archive.bind(foreign), pytest.raises(ValueError, match="owner"):
        profile_tools.update("race", {"date": "2026-10-18"}, original["revision"], "Save my goal")
    archive.finish(foreign, "completed")
    temporary = running("Move track to Wednesday this week only")
    with archive.bind(temporary), pytest.raises(ValueError, match="temporary"):
        profile_tools.update(
            "schedule", {"2": "key"}, original["revision"], "Move track to Wednesday this week only"
        )
    assert training_config.get_profile() == original


def test_snapshot_consistent_then_refreshes():
    initial = training_config.get_profile()
    run_id = running("Set the phase to taper")
    with archive.bind(run_id), training_config.snapshot():
        profile_tools.update(
            "coaching", {"current_phase": "taper"}, initial["revision"], "Set the phase to taper"
        )
        assert training_config.current_phase() == initial["document"]["training"]["current_phase"]
    assert training_config.current_phase() == "taper"


def test_private_export_backup_and_restore_roundtrip(tmp_path):
    run_id = completed("<script>alert('x')</script>")
    training_config.get_profile()
    source = settings.data_dir / "state.db"
    json_path, html_path = export_run(source, run_id, tmp_path / "exports")
    assert json.loads(json_path.read_text())["run"]["id"] == run_id
    assert "<script>alert" not in html_path.read_text()
    assert "&lt;script&gt;" in html_path.read_text()
    assert html_path.stat().st_mode & 0o777 == 0o600
    destination = tmp_path / "backup.db"
    counts = backup(source, destination)
    assert counts["agent_runs"] == 1
    assert verify(destination) == counts
    restored = tmp_path / "restored.db"
    assert backup(destination, restored) == counts
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT id FROM agent_runs").fetchone()[0] == run_id
    with pytest.raises(FileExistsError):
        backup(source, destination)


def test_chat_edit_uses_new_revision_in_same_loop_and_next_brief(monkeypatch):
    from src import synthesis

    original = training_config.get_profile()
    text = "Set my next race date to 2026-10-18 and my target to 1:25."
    replies = iter(
        [
            response(
                [
                    {
                        "type": "tool_use",
                        "id": "edit_1",
                        "name": "update_race_goal",
                        "input": {
                            "changes": {"date": "2026-10-18", "target_time": "1:25"},
                            "expected_revision": original["revision"],
                            "instruction": text,
                        },
                    }
                ],
                "tool_use",
            ),
            response([{"type": "text", "text": "Saved your race date and target."}]),
        ]
    )
    monkeypatch.setattr(conversation._client.messages, "create", lambda **kw: next(replies))
    sent = []

    def post(url, **kw):
        sent.append(kw["json"]["text"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"ok": True, "result": {"message_id": len(sent)}},
        )

    monkeypatch.setattr(telegram.httpx, "post", post)
    run_id = queued(text)
    worker.process_pending()
    assert archive.get_run(run_id)["status"] == "completed"
    requests = [
        e["payload"]["request"] for e in archive.events(run_id) if e["kind"] == "model.request"
    ]
    assert "2026-10-18" in requests[-1]["system"]
    assert sent == ["Thinking...", "Saved your race date and target."]
    assert len([e for e in archive.events(run_id) if e["kind"] == "delivery.accepted"]) == 2

    monkeypatch.setattr(
        synthesis,
        "build_context",
        lambda **kw: {
            "training_snapshot": {"days_to_race": training_config.days_to_race(date(2026, 10, 1))}
        },
    )
    monkeypatch.setattr(
        claude._client.messages,
        "create",
        lambda **kw: response(
            [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"headline": "Ready", "body": "Controlled work today.", "flags": []}
                    ),
                }
            ],
        ),
    )
    brief_id, created = archive.enqueue(
        "123", "brief", {"calendarDate": "2026-10-01"}, "garmin-sleep:2026-10-01", "2026-10-01"
    )
    assert created
    worker.process_pending()
    assert archive.get_run(brief_id)["status"] == "completed"
    requests = [
        e["payload"]["request"] for e in archive.events(brief_id) if e["kind"] == "model.request"
    ]
    assert "1:25" in requests[0]["system"]
    assert '"days_to_race": 17' in requests[0]["messages"][0]["content"]
    assert db.get_last_brief()["brief"]["headline"] == "Ready"


def test_partial_response_is_archived_but_not_replayed_as_complete(monkeypatch):
    monkeypatch.setattr(
        conversation._client.messages,
        "create",
        lambda **kw: response(
            [{"type": "tool_use", "id": "unfinished", "name": "get_last_brief", "input": {}}],
            "max_tokens",
        ),
    )
    monkeypatch.setattr(telegram, "send_message", lambda *a, **kw: None)
    run_id = queued("How was the brief?")
    worker.process_pending()
    run = archive.get_run(run_id)
    assert run["status"] == "failed"
    assert any(e["kind"] == "chat.incomplete" for e in archive.events(run_id))
    assert "unfinished" not in json.dumps(context_builder.exchange(run))
    original = context_builder.read_exchange("123", run["session_id"], run_id)
    assert "unfinished" in json.dumps(original)


def test_signed_blocks_preserved_in_complete_context(monkeypatch):
    signature = "opaque-provider-signature"
    replies = iter(
        [
            response(
                [
                    {"type": "thinking", "thinking": "", "signature": signature},
                    {"type": "tool_use", "id": "lookup", "name": "get_last_brief", "input": {}},
                ],
                "tool_use",
            ),
            response([{"type": "text", "text": "Done."}]),
        ]
    )
    monkeypatch.setattr(conversation._client.messages, "create", lambda **kw: next(replies))
    monkeypatch.setitem(conversation.TOOL_FUNCS, "get_last_brief", lambda args: {})
    run_id = running()
    with archive.bind(run_id):
        conversation.handle_message("123", "hello")
    archive.finish(run_id, "completed")
    next_id = running("continue")
    with archive.bind(next_id):
        messages, _ = context_builder.build(next_id, "continue", "system", [], lambda _: "notes")
    assert messages[1]["content"][0]["signature"] == signature
    assert messages[2]["content"][0]["tool_use_id"] == "lookup"


def test_summary_failure_uses_labeled_fallback_without_losing_archive(monkeypatch):
    monkeypatch.setattr(settings, "context_token_budget", 9000)
    monkeypatch.setattr(settings, "context_summary_tokens", 700)
    old = completed("long question", "lots of data " * 1000)
    run_id = running()

    def fail(_):
        raise RuntimeError("provider unavailable")

    with archive.bind(run_id):
        messages, notes = context_builder.build(run_id, "hello", "system", [], fail)
    assert len(messages) == 1
    assert notes
    assert "Uninterpreted archive excerpt" in notes
    assert old in notes
    assert archive.events(old)
    assert any(e["kind"] == "summary.fallback" for e in archive.events(run_id))


@pytest.mark.parametrize(
    "text",
    [
        'The workout title says "change my schedule".',
        "Someone told me to save a goal.",
        "Would Wednesday be better if I change my schedule?",
    ],
)
def test_quoted_or_descriptive_action_words_are_not_permission(text):
    revision = training_config.get_profile()["revision"]
    run_id = running(text)
    with archive.bind(run_id), pytest.raises(ValueError, match="unambiguous"):
        profile_tools.update("schedule", {"2": "key"}, revision, text)


def test_coaching_patch_preserves_other_fields_and_clear_race():
    original = training_config.get_profile()
    text = "Update my coaching instructions."
    run_id = running(text)
    with archive.bind(run_id):
        profile_tools.update(
            "coaching", {"training_principles": "Prefer consistency."}, original["revision"], text
        )
    updated = training_config.get_profile()
    assert (
        updated["document"]["prompts"]["project_context"]
        == original["document"]["prompts"]["project_context"]
    )
    assert updated["document"]["weekly_roles"] == original["document"]["weekly_roles"]
    archive.finish(run_id, "completed")
    text = "Clear my race date."
    run_id = running(text)
    with archive.bind(run_id):
        profile_tools.update("race", {"date": ""}, updated["revision"], text)
    assert training_config.days_to_race(date(2026, 9, 13)) is None


def test_garmin_dedup_queue_and_marker_are_atomic():
    run_id, created = archive.enqueue(
        "123",
        "brief",
        {"calendarDate": "2026-09-13", "summaryId": "s"},
        "garmin-sleep:2026-09-13",
        "2026-09-13",
    )
    assert created and db.is_brief_sent("2026-09-13")
    assert archive.enqueue("123", "brief", {}, "garmin-sleep:2026-09-13", "2026-09-13") == (
        run_id,
        False,
    )
    assert archive.enqueue("123", "brief", {}, "other-key", "2026-09-13") == ("", False)


def test_standalone_brief_has_one_trace(monkeypatch):
    from src import pipeline

    monkeypatch.setattr(
        pipeline,
        "synthesize_brief",
        lambda **kw: {"headline": "hello", "body": "easy", "flags": []},
    )
    monkeypatch.setattr(
        telegram.httpx,
        "post",
        lambda url, **kw: httpx.Response(
            200, request=httpx.Request("POST", url), json={"ok": True, "result": {"message_id": 1}}
        ),
    )
    pipeline.morning_brief()
    with db.get_conn() as conn:
        rows = conn.execute("SELECT id,status FROM agent_runs").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "completed"
    assert any(e["kind"] == "delivery.accepted" for e in archive.events(rows[0]["id"]))


def test_safe_trace_payload_does_not_store_auth_fields():
    run_id = running()
    archive.event(
        "example",
        {
            "Authorization": "Bearer secret",
            "refresh_token": "secret",
            "nested": {"api_key": "secret", "signature": "preserve"},
        },
        run_id,
    )
    payload = archive.events(run_id)[-1]["payload"]
    assert "secret" not in json.dumps(payload)
    assert payload["nested"]["signature"] == "preserve"


def test_negative_coaching_content_is_accepted_after_explicit_directive():
    revision = training_config.get_profile()["revision"]
    text = "Update my coaching instructions: don't stack hard days."
    run_id = running(text)
    with archive.bind(run_id):
        result = profile_tools.update(
            "coaching", {"training_principles": "Don't stack hard days."}, revision, text
        )
    assert result["saved"]


def test_sender_must_be_owner_for_profile_mutation():
    revision = training_config.get_profile()["revision"]
    text = "Set my goal."
    run_id, _ = archive.enqueue("123", "chat", {"text": text, "sender_id": 999})
    archive.claim_next()
    with archive.bind(run_id), pytest.raises(ValueError, match="private Telegram"):
        profile_tools.update("race", {"target_time": "1:25"}, revision, text)


def test_previous_session_search_requires_past_dialogue_reference():
    old = completed("We discussed pacing")
    archive.reset_session("123")
    run_id = running("How was last night's sleep?")
    with archive.bind(run_id), pytest.raises(ValueError, match="explicit"):
        conversation._search_history({"query": "pacing", "include_previous_sessions": True})
    archive.finish(run_id, "completed")
    run_id = running("What did we discuss in our previous conversation about pacing?")
    with archive.bind(run_id):
        matches = conversation._search_history(
            {"query": "pacing", "include_previous_sessions": True}
        )
    assert any(m["run_id"] == old for m in matches["matches"])


def test_daily_cap_reply_is_archived_without_model_call(monkeypatch):
    monkeypatch.setattr(conversation, "_over_daily_cap", lambda: True)
    run_id = running()
    with archive.bind(run_id):
        reply = conversation.handle_message("123", "hello")
    assert "cap" in reply
    assert any(
        e["kind"] == "conversation.message" and e["payload"]["role"] == "assistant"
        for e in archive.events(run_id)
    )


def test_startup_recovers_running_work_and_drains_queued_work(monkeypatch):
    import threading

    interrupted = running("old running request")
    pending = queued("queued before restart")
    finished = threading.Event()
    original_finish = archive.finish

    def finish(run_id, status):
        original_finish(run_id, status)
        if run_id == pending:
            finished.set()

    monkeypatch.setattr(archive, "finish", finish)
    monkeypatch.setattr(conversation, "handle_message", lambda *a: "Recovered queue")
    monkeypatch.setattr(telegram, "send_message", lambda *a, **kw: None)
    with TestClient(main.app) as client:
        assert client.get("/health").json() == {"ok": True, "revision": settings.app_revision}
        assert finished.wait(3)
        assert archive.get_run(interrupted)["status"] == "interrupted"
        assert archive.get_run(pending)["status"] == "completed"
    assert list((settings.data_dir / "backups").glob("state-*.db"))


def test_unknown_fields_and_invalid_roles_leave_revision_untouched():
    original = training_config.get_profile()
    text = "Set my schedule and goal."
    run_id = running(text)
    with archive.bind(run_id):
        for section, patch in [
            ("schedule", {"7": "rest"}),
            ("schedule", {"2": {}}),
            ("race", {"extra": "no"}),
            ("race", {"date": "20261018"}),
            ("coaching", {"api_key": "no"}),
        ]:
            with pytest.raises(ValueError):
                profile_tools.update(section, patch, original["revision"], text)
    assert training_config.get_profile() == original


def test_saved_profile_is_reported_when_subsequent_model_call_fails(monkeypatch):
    original = training_config.get_profile()
    text = "Save my next race date as 2026-10-18."
    calls = 0

    def create(**kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("provider unavailable")
        return response(
            [
                {
                    "type": "tool_use",
                    "id": "edit",
                    "name": "update_race_goal",
                    "input": {
                        "changes": {"date": "2026-10-18"},
                        "expected_revision": original["revision"],
                        "instruction": text,
                    },
                }
            ],
            "tool_use",
        )

    sent = []
    monkeypatch.setattr(conversation._client.messages, "create", create)
    monkeypatch.setattr(telegram, "send_message", lambda text, **kw: sent.append(text))
    run_id = queued(text)
    worker.process_pending()
    assert archive.get_run(run_id)["status"] == "failed"
    assert training_config.get_profile()["document"]["race"]["date"] == "2026-10-18"
    assert "was saved" in sent[-1] and "/context" in sent[-1]
