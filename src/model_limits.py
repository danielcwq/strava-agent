"""Resolve context capacity from the configured provider model, not an app constant."""

from functools import cache

from anthropic import Anthropic

from src.config import settings

_client = Anthropic(api_key=settings.anthropic_api_key)


@cache
def context_window(model: str) -> int:
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
    budget = context_window(settings.chat_model) - settings.chat_max_tokens - 4096
    if budget < 8000:
        raise ValueError("configured output allowance leaves insufficient model input capacity")
    return budget
