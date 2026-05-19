"""Anthropic Claude client. Loads prompts/system.md fresh on every call."""
import json

from anthropic import Anthropic

from src.config import PROMPTS_DIR, settings

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1500

_client = Anthropic(api_key=settings.anthropic_api_key)

# Tool-use schema. Forces Claude to produce structured output matching the brief
# format — no more "thinking out loud" preambles or markdown-wrapped JSON.
_BRIEF_TOOL = {
    "name": "deliver_morning_brief",
    "description": "Deliver the morning brief to the runner via Telegram.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": "One short line — the Telegram preview the runner sees first.",
            },
            "body": {
                "type": "string",
                "description": (
                    "Line 1: stat line (e.g. 'Sleep 7h 12m (84). HRV 58ms (-0.4σ). "
                    "RHR 47. TSB +4.'). Then a blank line, then 2-3 prose sentences."
                ),
            },
            "flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Short tags for anomalies, e.g. 'HRV -1.5σ', 'sleep deficit'. "
                    "May be empty."
                ),
            },
        },
        "required": ["headline", "body", "flags"],
    },
}


def _read_prompt(name: str) -> str | None:
    """Read an optional prompt file, returning None if it doesn't exist."""
    try:
        return (PROMPTS_DIR / name).read_text()
    except FileNotFoundError:
        return None


def load_system_prompt(include_project_context: bool = False) -> str:
    """Assemble the system prompt fresh on every call so prompt edits take effect
    without a restart.

    training_principles.md (the stable decision layer — weekly rhythm, phase,
    progression principles) is always included. project_context.md (race
    narrative) is included only when include_project_context is True; the morning
    brief sets that on Mondays so the race goal isn't repeated every day.
    """
    parts = [(PROMPTS_DIR / "system.md").read_text()]
    principles = _read_prompt("training_principles.md")
    if principles:
        parts.append(principles)
    if include_project_context:
        ctx = _read_prompt("project_context.md")
        if ctx:
            parts.append(ctx)
    return "\n\n---\n\n".join(parts)


def synthesize(user_context: dict, model: str = MODEL, include_project_context: bool = False) -> dict:
    """Call Claude and force it to call the deliver_morning_brief tool.

    Returns parsed args: {"headline": str, "body": str, "flags": list[str]}.
    Raises RuntimeError if the model fails to call the tool.

    include_project_context: when True, the project_context.md is appended to
    the system prompt. The caller (pipeline.morning_brief) sets this only on
    Mondays.
    """
    user_message = (
        "Here is today's training data. Call deliver_morning_brief with the brief.\n\n"
        f"{json.dumps(user_context, indent=2, default=str)}"
    )
    response = _client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=load_system_prompt(include_project_context=include_project_context),
        tools=[_BRIEF_TOOL],
        tool_choice={"type": "tool", "name": "deliver_morning_brief"},
        messages=[{"role": "user", "content": user_message}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "deliver_morning_brief":
            return block.input
    raise RuntimeError("Claude did not call deliver_morning_brief; response: %r" % response.content)
