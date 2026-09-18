import json

import pytest
from anthropic.types import Message

from src import archive, context_builder, conversation, worker
from src.clients import telegram


def start(text="hello", chat="123"):
    run_id, _ = archive.enqueue(chat, "chat", {"text": text, "sender_id": int(chat)})
    assert archive.claim_next()["id"] == run_id
    return run_id


def record(run_id, role, content):
    archive.event("conversation.message", {"role": role, "content": content}, run_id)


def history():
    run_id = start("Remember our track workout discussion?")
    record(
        run_id,
        "assistant",
        [
            {"type": "thinking", "thinking": "internalneedle", "signature": "opaque"},
            {"type": "text", "text": "internal evidence note"},
            {
                "type": "tool_use",
                "id": "lookup",
                "name": "search_conversation_history",
                "input": {"query": "queryneedle"},
            },
        ],
    )
    record(
        run_id,
        "user",
        [{"type": "tool_result", "tool_use_id": "lookup", "content": "resultneedle"}],
    )
    record(
        run_id,
        "assistant",
        [
            {"type": "thinking", "thinking": "internalneedle", "signature": "opaque" * 5000},
            {"type": "text", "text": "Earlier we discussed track workouts and pacing."},
        ],
    )
    archive.finish(run_id, "completed")
    return run_id


def test_search_and_read_only_actual_dialogue():
    old = history()
    active = start("track workouts")
    session = archive.get_run(active)["session_id"]
    record(
        active,
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "self",
                "name": "search",
                "input": {"query": "track workouts"},
            }
        ],
    )
    with archive.bind(active):
        matches = conversation._search_history({"query": "track workouts"})["matches"]
        assert matches and all(m["run_id"] == old for m in matches)
        for query in ("internalneedle", "queryneedle", "resultneedle", "opaque", "evidence"):
            assert not archive.search_history("123", session, query)
        full = conversation._read_history({"run_id": old})
    assert full["messages"] == [
        {"role": "user", "content": "Remember our track workout discussion?"},
        {"role": "assistant", "content": "Earlier we discussed track workouts and pacing."},
    ]
    assert len(json.dumps(full)) < 1000
    assert "opaque" in json.dumps(archive.events(old))
    assert not archive.search_history("999", session, "track")
    with pytest.raises(ValueError, match="outside"):
        context_builder.read_exchange("999", session, old)


def test_natural_recall_authorizes_previous_sessions_without_relaxing_default():
    old = history()
    archive.reset_session("123")
    current = start("I thought u said something about track workouts")
    with archive.bind(current):
        assert not conversation._search_history({"query": "track"})["matches"]
        matches = conversation._search_history(
            {"query": "track", "include_previous_sessions": True}
        )["matches"]
        assert any(m["run_id"] == old for m in matches)
    archive.finish(current, "completed")
    current = start("How was last night's sleep?")
    with archive.bind(current), pytest.raises(ValueError, match="explicit"):
        conversation._search_history({"query": "track", "include_previous_sessions": True})


def test_failed_partial_reply_not_presented_as_previous_advice():
    old = start("question")
    record(old, "assistant", [{"type": "text", "text": "unfinished advice"}])
    archive.finish(old, "failed")
    session = archive.get_run(old)["session_id"]
    assert not archive.search_history("123", session, "unfinished")
    assert context_builder.read_exchange("123", session, old)["messages"] == [
        {"role": "user", "content": "question"}
    ]
    assert "unfinished advice" in json.dumps(archive.events(old))


@pytest.mark.parametrize(
    "content",
    [
        [],
        [{"type": "text", "text": "partial output"}],
        [{"type": "tool_use", "id": "do-not-execute", "name": "update_schedule", "input": {}}],
    ],
)
def test_refusal_is_terminal_clear_and_never_retried(monkeypatch, content):
    calls = []
    sent = []

    def create(**kwargs):
        calls.append(kwargs)
        return Message.model_validate(
            dict(
                id="test",
                type="message",
                role="assistant",
                model="test",
                content=content,
                stop_reason="refusal",
                usage={"input_tokens": 10, "output_tokens": 5},
            )
        )

    monkeypatch.setattr(conversation._client.messages, "create", create)
    monkeypatch.setattr(telegram, "send_message", lambda text, **kw: sent.append(text))
    run_id, _ = archive.enqueue("123", "chat", {"text": "hello", "sender_id": 123})
    worker.process_pending()
    events = archive.events(run_id)
    assert len(calls) == 1
    assert "model declined" in sent[-1] and run_id in sent[-1]
    assert "partial output" not in sent[-1]
    assert any(e["kind"] == "chat.refused" for e in events)
    assert not any(e["kind"] in {"run.error", "tool.started", "chat.finished"} for e in events)
    assert archive.get_run(run_id)["status"] == "completed"
    raw = next(e["payload"]["response"]["content"] for e in events if e["kind"] == "model.response")
    assert len(raw) == len(content)
    assert all(
        all(saved[k] == v for k, v in original.items())
        for saved, original in zip(raw, content, strict=True)
    )
    replay = context_builder.exchange(archive.get_run(run_id))
    assert "do-not-execute" not in json.dumps(replay)
    assert "partial output" not in json.dumps(replay)
