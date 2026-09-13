import json
from contextlib import contextmanager

import pytest
from anthropic.types import Message
from pydantic import ValidationError

from src import archive, context_builder, conversation, model_calls, training_config
from src.clients import claude
from src.config import settings


def reply(content, stop="end_turn"):
    return Message.model_validate(
        dict(
            id="test",
            type="message",
            role="assistant",
            model="claude-fable-5-1",
            content=content,
            stop_reason=stop,
            usage=dict(input_tokens=10, output_tokens=5),
        )
    )


def test_native_brief_contract_and_configurable_allowance(monkeypatch):
    seen = {}
    brief = dict(headline="Easy day", body="Keep it comfortable.", flags=[])

    @contextmanager
    def stream(**kw):
        seen.update(kw)

        class Stream:
            def get_final_message(self):
                return reply([dict(type="text", text=json.dumps(brief))])

        yield Stream()

    monkeypatch.setattr(settings, "brief_max_tokens", 24576)
    monkeypatch.setattr(claude._client.messages, "stream", stream)
    assert claude.synthesize({"synthetic": True}) == brief
    assert seen["model"] == "claude-fable-5-1"
    assert seen["max_tokens"] == 24576
    assert "tool_choice" not in seen and "tools" not in seen
    schema = seen["output_config"]["format"]["schema"]
    assert set(schema["required"]) == {"headline", "body", "flags"}
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
def test_incomplete_brief_never_delivered(monkeypatch, stop):
    monkeypatch.setattr(claude._client.messages, "create", lambda **kw: reply([], stop))
    with pytest.raises(RuntimeError, match="incomplete or refused"):
        claude.synthesize({})


@pytest.mark.parametrize("text", ["{}", '{"headline":1,"body":"ok","flags":[]}'])
def test_invalid_brief_rejected(monkeypatch, text):
    monkeypatch.setattr(
        claude._client.messages, "create", lambda **kw: reply([dict(type="text", text=text)])
    )
    with pytest.raises(ValidationError):
        claude.synthesize({})


def test_reasoning_removed_from_replay_without_mutating_archive():
    messages = [
        dict(
            role="assistant",
            content=[
                dict(type="thinking", thinking="", signature="saved"),
                dict(type="redacted_thinking", data="opaque"),
                dict(type="text", text="Answer", citations=[]),
            ],
        )
    ]
    replay = context_builder.without_thinking(messages)
    assert len(replay[0]["content"]) == 1
    assert replay[0]["content"][0]["citations"] == []
    assert len(messages[0]["content"]) == 3


def test_profile_change_strips_bound_reasoning_but_keeps_trace(monkeypatch):
    profile = training_config.get_profile()
    instruction = "Set my next race date to 2026-10-18."
    answers = iter(
        [
            reply(
                [
                    dict(type="thinking", thinking="", signature="original-signed-block"),
                    dict(
                        type="tool_use",
                        id="edit",
                        name="update_race_goal",
                        input=dict(
                            changes={"date": "2026-10-18"},
                            expected_revision=profile["revision"],
                            instruction=instruction,
                        ),
                    ),
                ],
                "tool_use",
            ),
            reply([dict(type="text", text="Saved.")]),
        ]
    )
    monkeypatch.setattr(conversation._client.messages, "create", lambda **kw: next(answers))
    run_id, _ = archive.enqueue("123", "chat", dict(text=instruction, sender_id=123))
    archive.claim_next()
    with archive.bind(run_id):
        assert conversation.handle_message("123", instruction) == "Saved."
    events = archive.events(run_id)
    requests = [e["payload"]["request"] for e in events if e["kind"] == "model.request"]
    assert "original-signed-block" not in json.dumps(requests[-1])
    assert "original-signed-block" in json.dumps(events)
    assert "2026-10-18" in requests[-1]["system"]


def test_stream_failure_is_archived(monkeypatch):
    @contextmanager
    def broken(**kw):
        raise TimeoutError("connection lost")
        yield

    monkeypatch.setattr(claude._client.messages, "stream", broken)
    with archive.execution("test", {}) as run_id:
        with pytest.raises(TimeoutError):
            model_calls.create(claude._client, purpose="test", max_tokens=32768)
        assert archive.events(run_id)[-1]["kind"] == "model.error"
