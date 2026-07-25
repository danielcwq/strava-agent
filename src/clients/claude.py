"""Anthropic Claude client. Loads the public prompt and private profile context."""
import json

from anthropic import Anthropic

from src import training_config
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


def load_system_prompt(include_project_context: bool = False) -> str:
    """Assemble the base prompt and private training profile.

    Training principles are always included. Project context is included only
    when requested; the morning brief does that on Mondays so race goals are not
    repeated every day.
    """
    parts = [(PROMPTS_DIR / "system.md").read_text()]
    principles = training_config.training_principles()
    if principles:
        parts.append(principles)
    if include_project_context:
        ctx = training_config.project_context()
        if ctx:
            parts.append(ctx)
    return "\n\n---\n\n".join(parts)


def synthesize(
    user_context: dict,
    model: str = MODEL,
    include_project_context: bool = False,
) -> dict:
    """Call Claude and force it to call the deliver_morning_brief tool.

    Returns parsed args: {"headline": str, "body": str, "flags": list[str]}.
    Raises RuntimeError if the model fails to call the tool.

    include_project_context: when True, the private project context is appended
    to the system prompt. The caller sets this only on Mondays.
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
    raise RuntimeError(f"Claude did not call deliver_morning_brief; response: {response.content!r}")
