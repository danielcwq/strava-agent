"""Resolve context capacity from the configured provider model, not an app constant."""

from functools import cache

from anthropic import Anthropic

from src.config import settings

_client = Anthropic(api_key=settings.anthropic_api_key) if settings.model_backend == "api" else None


@cache
def context_window(model: str) -> int:
    if _client is None:
        raise RuntimeError("API model metadata is unavailable")
    info = _client.models.retrieve(model)
    limit = getattr(info, "max_input_tokens", None)
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError(f"provider did not report an input limit for {model}")
    return limit


def input_budget() -> int:
    # Explicit overrides remain available for controlled tests/deployments. Normal
    # operation uses the model's capacity, reserving output and framing headroom.
    if settings.context_token_budget is not None:
        return settings.context_token_budget
    if settings.model_backend == "claude_code":
        if settings.chat_model != "claude-opus-5-5":
            raise ValueError("Set CONTEXT_TOKEN_BUDGET for a different Claude Code chat model")
        # Opus 5.5's window is used only to seed the session with archived dialogue.
        capacity = 1_000_000
    else:
        capacity = context_window(settings.chat_model)
    budget = capacity - settings.chat_max_tokens - 4096
    if budget < 8000:
        raise ValueError("configured output allowance leaves insufficient model input capacity")
    return budget
