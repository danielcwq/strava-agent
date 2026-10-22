from types import SimpleNamespace

import pytest

from src import context_builder, model_limits
from src.config import Settings, settings


def test_default_model_is_opus_and_context_is_automatic():
    assert Settings.model_fields["chat_model"].default == "claude-opus-5-5"
    assert Settings.model_fields["brief_model"].default == "claude-opus-5-5"
    assert Settings.model_fields["context_token_budget"].default is None


def test_budget_tracks_provider_capacity_and_is_cached(monkeypatch):
    model_limits.context_window.cache_clear()
    calls = []

    def retrieve(model):
        calls.append(model)
        return SimpleNamespace(max_input_tokens=1_000_000 if model == "large-test" else 200_000)

    monkeypatch.setattr(model_limits._client.models, "retrieve", retrieve)
    monkeypatch.setattr(settings, "context_token_budget", None)
    monkeypatch.setattr(settings, "chat_model", "large-test")
    monkeypatch.setattr(settings, "chat_max_tokens", 32768)
    assert model_limits.input_budget() == 1_000_000 - 32768 - 4096
    assert model_limits.input_budget() == 1_000_000 - 32768 - 4096
    monkeypatch.setattr(settings, "chat_model", "small-test")
    assert model_limits.input_budget() == 200_000 - 32768 - 4096
    assert calls == ["large-test", "small-test"]
    model_limits.context_window.cache_clear()


def test_optional_override_is_explicit():
    # The isolated test fixture supplies this override; production defaults to None.
    assert model_limits.input_budget() == 48000


@pytest.mark.parametrize("limit", [None, 0, -1])
def test_missing_provider_limit_has_no_silent_48k_fallback(monkeypatch, limit):
    model_limits.context_window.cache_clear()
    monkeypatch.setattr(
        model_limits._client.models, "retrieve", lambda _: SimpleNamespace(max_input_tokens=limit)
    )
    with pytest.raises(ValueError, match="provider did not report"):
        model_limits.context_window("missing-limit")
    model_limits.context_window.cache_clear()


def test_request_above_old_guard_keeps_active_tool_chain(monkeypatch):
    monkeypatch.setattr(settings, "context_token_budget", None)
    monkeypatch.setattr(model_limits, "context_window", lambda _: 1_000_000)
    messages = [{"role": "user", "content": "Find historical workouts."}]
    for n in range(49):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"t{n}",
                            "name": "search_workouts",
                            "input": {"query": str(n)},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"t{n}",
                            "content": "workout evidence " * 300,
                        }
                    ],
                },
            ]
        )
    request = {"model": settings.chat_model, "system": "coaching profile", "messages": messages}
    assert context_builder.token_estimate(request) > 48_000
    fitted, history = context_builder.fit_tool_context(request, 0)
    assert fitted == request and history == 0
    assert len(fitted["messages"]) == 99
