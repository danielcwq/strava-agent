"""Anthropic Claude client. Loads the public prompt and private profile context."""

import json

from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict, Field

from src import claude_code, model_calls, training_config
from src.config import PROMPTS_DIR, settings

_client = Anthropic(api_key=settings.anthropic_api_key) if settings.model_backend == "api" else None


class MorningBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    headline: str = Field(description="One short line for the Telegram preview.")
    body: str = Field(description="A stat line, blank line, then 2–3 concise coaching sentences.")
    flags: list[str] = Field(description="Short anomaly tags; may be empty.")


def load_system_prompt(include_project_context: bool = False) -> str:
    """Every brief sees the current profile; inclusion is independent of weekday."""
    return "\n\n---\n\n".join(
        [
            (PROMPTS_DIR / "system.md").read_text(),
            training_config.profile_context(),
        ]
    )


def synthesize(
    user_context: dict,
    model: str | None = None,
    include_project_context: bool = True,
) -> dict:
    """Produce and validate native structured JSON; never deliver partial output."""
    user_message = (
        "Write today's concise morning brief using the supplied training data.\n\n"
        f"{json.dumps(user_context, indent=2, default=str)}"
    )
    model = model or settings.brief_model
    system = load_system_prompt(include_project_context=include_project_context)
    schema = MorningBrief.model_json_schema()
    if settings.model_backend == "claude_code":
        result = claude_code.run(
            purpose="brief", model=model, effort=settings.brief_effort, system=system,
            prompt=user_message, max_tokens=settings.brief_max_tokens, schema=schema,
            max_turns=6,
        )
        return MorningBrief.model_validate(result.structured_output).model_dump()
    response = model_calls.create(
        _client,
        purpose="brief",
        model=model,
        max_tokens=settings.brief_max_tokens,
        thinking={"type": "adaptive"},
        output_config={
            "effort": settings.brief_effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"brief was incomplete or refused: {response.stop_reason}")
    text = "".join(block.text for block in response.content if block.type == "text")
    return MorningBrief.model_validate_json(text).model_dump()
