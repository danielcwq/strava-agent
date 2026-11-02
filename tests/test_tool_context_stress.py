"""Stress the actual tool loop and data retrieval, not just the formatter helpers."""

import copy
import json

import pytest
from anthropic.types import Message

from src import archive, context_builder, conversation, tool_context, training_config
from src.config import settings


def running(text="Assess my recent training", chat_id="123"):
    run_id, _ = archive.enqueue(chat_id, "chat", {"text": text, "sender_id": int(chat_id)})
    assert archive.claim_next()["id"] == run_id
    return run_id


def response(blocks, stop="tool_use"):
    return Message.model_validate(
        {
            "id": "test",
            "type": "message",
            "role": "assistant",
            "model": "test",
            "content": blocks,
            "stop_reason": stop,
            "usage": {"input_tokens": 100, "output_tokens": 30},
        }
    )


def saved_result(run_id, value, name="get_garmin_summary", tool_id="read"):
    return archive.event("tool.finished", {"id": tool_id, "name": name, "result": value}, run_id)


@pytest.mark.parametrize("records", [0, 1, 50, 500])
def test_large_arrays_can_be_read_completely_without_loss(records):
    run = running()
    data = {
        "workouts": [
            {"date": f"sample-{i}", "distance": i, "note": "🏃 & <pace>" * 8}
            for i in range(records)
        ]
    }
    event_id = saved_result(run, data)
    with archive.bind(run):
        view = tool_context.for_prompt(event_id, "get_garmin_summary", data)
        assert tool_context._size(view) <= tool_context.MAX_RESULT_BYTES
        all_rows, offset = [], 0
        while offset is not None:
            page = tool_context.read(event_id, "/workouts", offset, 50)
            assert tool_context._size(page) <= tool_context.MAX_RESULT_BYTES
            all_rows.extend(entry["value"] for entry in page["items"])
            offset = page["next_offset"]
        assert all_rows == data["workouts"]
    assert archive.events(run)[-1]["payload"]["result"] == data


def test_large_nested_unicode_strings_and_pointer_escapes_round_trip():
    run = running()
    text = "旅🏃\n<&>" * 20000
    data = {"a/b~c": {"notes": text, "distance": 21.1}}
    event_id = saved_result(run, data)
    path = "/a~1b~0c/notes"
    with archive.bind(run):
        offset, chunks = 0, []
        while offset is not None:
            page = tool_context.read(event_id, path, offset)
            assert tool_context._size(page) <= tool_context.MAX_RESULT_BYTES
            chunks.append(page["text"])
            offset = page["next_offset"]
        assert "".join(chunks) == text
        assert tool_context.read(event_id, "/a~1b~0c/distance")["value"] == 21.1


@pytest.mark.parametrize(
    "args",
    [
        {"offset": -1},
        {"limit": 0},
        {"limit": 51},
        {"offset": True},
        {"path": "/missing"},
        {"path": "/values/-1"},
        {"path": "/values/4"},
    ],
)
def test_invalid_page_requests_are_rejected(args):
    run = running()
    event_id = saved_result(run, {"values": [1, 2]})
    with archive.bind(run), pytest.raises(ValueError):
        tool_context.read(event_id, **args)


def test_archived_tool_reads_enforce_owner_and_reset_boundaries():
    first = running()
    event_id = saved_result(first, {"secret_data": "only this owner"})
    archive.finish(first, "completed")
    other = running(chat_id="456")
    with archive.bind(other), pytest.raises(ValueError, match="scope"):
        tool_context.read(event_id, previous_sessions=True)
    archive.finish(other, "completed")
    archive.reset_session("123")
    new = running("Show my current profile")
    with archive.bind(new):
        with pytest.raises(ValueError, match="scope"):
            tool_context.read(event_id)
        with pytest.raises(ValueError, match="explicit"):
            conversation._read_tool_result(
                {"event_id": event_id, "include_previous_sessions": True}
            )
    archive.finish(new, "completed")
    explicit = running("Read our previous conversation about this result")
    with archive.bind(explicit):
        page = conversation._read_tool_result(
            {"event_id": event_id, "include_previous_sessions": True}
        )
        assert page["items"][0]["value"] == "only this owner"


def test_compaction_preserves_mutation_acknowledgement_and_original_instruction():
    run = running("Update my coaching instructions: prioritize consistency.")
    acknowledgement = {"saved": True, "revision": 9, "changes": {"training_principles": "x" * 5000}}
    saved_result(run, acknowledgement, "update_coaching_instructions", "write")
    event_id = saved_result(run, {"records": ["evidence" * 2000]}, tool_id="read")
    messages = [
        {"role": "user", "content": "Update my coaching instructions: prioritize consistency."},
        {"role": "assistant", "content": [{"type": "text", "text": "Evidence: distance 21.1 km"}]},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "write",
                    "content": json.dumps(acknowledgement),
                },
                {"type": "tool_result", "tool_use_id": "read", "content": "x" * 10000},
            ],
        },
    ]
    original = copy.deepcopy(messages)
    with archive.bind(run):
        compacted, source = tool_context.compact_one(messages)
        assert source == event_id
        assert compacted[0] == messages[0]
        assert compacted[1] == messages[1]
        assert compacted[-1]["content"][0] == messages[-1]["content"][0]
        assert json.loads(compacted[-1]["content"][1]["content"])["archived_result"] == event_id
    assert messages == original


@pytest.mark.parametrize("rounds,per_round", [(3, 3), (8, 3), (15, 1)])
def test_long_analysis_chains_finish_with_traceable_evidence(monkeypatch, rounds, per_round):
    # Match a substantial real profile plus historical notes, not an empty prompt.
    monkeypatch.setattr(conversation, "_load_system_prompt", lambda: "Coaching context. " * 900)
    monkeypatch.setattr(
        conversation, "_summarize_history", lambda material: "Prior evidence is archived."
    )
    rows = [
        {
            "date": f"2026-09-{i + 1:02}",
            "distance_km": 10 + i / 10,
            "moving_seconds": 3000,
            "notes": "ordinary training observation " * 4,
        }
        for i in range(14)
    ]
    monkeypatch.setitem(conversation.TOOL_FUNCS, "get_recent_runs", lambda args: {"runs": rows})
    replies = []
    for index in range(rounds):
        replies.append(
            response(
                [
                    {"type": "thinking", "thinking": "", "signature": f"signed-{index}"},
                    {
                        "type": "text",
                        "text": f"Evidence round {index}: runs include a 10 km session in 3000s.",
                    },
                    *[
                        {
                            "type": "tool_use",
                            "id": f"read-{index}-{j}",
                            "name": "get_recent_runs",
                            "input": {"n": 14},
                        }
                        for j in range(per_round)
                    ],
                ]
            )
        )
    replies.append(
        response(
            [{"type": "text", "text": "Assessment based on inspected training evidence."}],
            "end_turn",
        )
    )
    responses = iter(replies)

    def measured_reply(**kw):
        reply = next(responses)
        reply.usage.input_tokens = context_builder.token_estimate(kw) // 2
        return reply

    monkeypatch.setattr(conversation._client.messages, "create", measured_reply)
    run = running()
    before = training_config.get_profile()
    with archive.bind(run):
        assert conversation.handle_message("123", "Assess my recent training").startswith(
            "Assessment"
        )
    events = archive.events(run)
    requests = [e["payload"]["request"] for e in events if e["kind"] == "model.request"]
    assert len(requests) == rounds + 1
    assert all(
        e["payload"]["estimated_input_tokens"] <= settings.context_token_budget
        for e in events
        if e["kind"] == "context.request_budget"
    )
    assert len([e for e in events if e["kind"] == "tool.finished"]) == rounds * per_round
    assert training_config.get_profile() == before
    for request in requests:
        outstanding = []
        for message in request["messages"]:
            if isinstance(message["content"], str):
                continue
            for block in message["content"]:
                if block["type"] == "tool_use":
                    outstanding.append(block["id"])
                if block["type"] == "tool_result":
                    assert block["tool_use_id"] in outstanding
                    outstanding.remove(block["tool_use_id"])
        assert not outstanding
    if rounds * per_round >= 24:
        assert any(e["kind"] == "context.tool_result_archived" for e in events)
        assert f"Evidence round {rounds - 1}" in json.dumps(requests[-1])


def test_huge_parallel_fetches_are_bounded_before_second_model_call(monkeypatch):
    huge = {"samples": [{"t": i, "hr": i % 90 + 90} for i in range(30000)], "averageHR": 145}
    monkeypatch.setitem(conversation.TOOL_FUNCS, "get_garmin_summary", lambda args: huge)
    replies = iter(
        [
            response(
                [
                    {
                        "type": "tool_use",
                        "id": f"fetch-{i}",
                        "name": "get_garmin_summary",
                        "input": {"summary_type": "dailies", "date": "2026-09-12"},
                    }
                    for i in range(3)
                ]
            ),
            response(
                [
                    {
                        "type": "text",
                        "text": "Average HR is 145; sample-level analysis needs paging.",
                    }
                ],
                "end_turn",
            ),
        ]
    )
    monkeypatch.setattr(conversation._client.messages, "create", lambda **kw: next(replies))
    run = running()
    with archive.bind(run):
        conversation.handle_message("123", "Check the stored daily summaries")
    events = archive.events(run)
    assert all(e["payload"]["result"] == huge for e in events if e["kind"] == "tool.finished")
    request = [e["payload"]["request"] for e in events if e["kind"] == "model.request"][-1]
    assert context_builder.token_estimate(request) <= settings.context_token_budget
    assert "read_tool_result" in json.dumps(request)


def test_provider_usage_avoids_rejecting_bytes_as_tokens():
    from types import SimpleNamespace

    request = {
        "model": "test",
        "system": "system",
        "tools": [],
        "messages": [{"role": "user", "content": "old data " * 3500}],
    }
    budget = context_builder.RequestBudget()
    budget.observe(request, SimpleNamespace(input_tokens=8000))
    # Mutate the live messages list, as the conversation loop does after sampling.
    request["messages"].append({"role": "assistant", "content": "new data " * 3000})
    assert context_builder.token_estimate(request) > settings.context_token_budget
    assert budget.estimate(request) < settings.context_token_budget
    assert len(budget.previous["messages"]) == 1


def test_token_budget_counts_cached_input_and_charges_duplicates():
    from types import SimpleNamespace

    message = {"role": "user", "content": "data " * 2000}
    request = {"model": "test", "system": "profile", "messages": [message]}
    budget = context_builder.RequestBudget()
    budget.observe(
        request,
        SimpleNamespace(
            input_tokens=200, cache_read_input_tokens=2000, cache_creation_input_tokens=3000
        ),
    )
    assert budget.input_tokens == 5200
    doubled = {**request, "messages": [message, message]}
    assert budget.estimate(doubled) >= 5200 + context_builder.token_estimate(message)
    # Profile/tool/schema changes must invalidate the measured-prefix estimate.
    changed = {**doubled, "system": "a changed profile " * 2000}
    assert budget.estimate(changed) == context_builder.token_estimate(changed)


@pytest.mark.parametrize("seed", range(10))
def test_usage_anchor_stays_conservative_under_mixed_message_edits(seed):
    import random
    from types import SimpleNamespace

    randomizer = random.Random(seed)
    old = [
        {"role": "user", "content": str(i) + " data" * randomizer.randint(30, 3000)}
        for i in range(8)
    ]
    request = {"model": "test", "system": "profile", "messages": old}
    budget = context_builder.RequestBudget()
    # Deterministic stand-in tokenizer: ~2 serialized bytes per input token.
    budget.observe(
        request, SimpleNamespace(input_tokens=context_builder.token_estimate(request) // 2)
    )
    new = old[randomizer.randint(0, 4) :] + [
        {"role": "user", "content": "🏃" * randomizer.randint(30, 1500)}
    ]
    candidate = {**request, "messages": new}
    assert budget.estimate(candidate) >= context_builder.token_estimate(candidate) // 2
