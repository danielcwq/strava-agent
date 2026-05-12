"""Anthropic Claude client. Loads prompts/system.md fresh on every call."""
import json

from anthropic import Anthropic

from src.config import PROMPTS_DIR, settings

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024

_client = Anthropic(api_key=settings.anthropic_api_key)


def load_system_prompt() -> str:
    # Read fresh each call so prompt edits take effect without restart.
    return (PROMPTS_DIR / "system.md").read_text()


def synthesize(user_context: dict, model: str = MODEL) -> dict:
    """Call Claude with the system prompt + JSON-serialized context.

    Returns parsed JSON: {"headline": str, "body": str, "flags": list[str]}.
    Raises json.JSONDecodeError if the model returns malformed output.
    """
    user_message = (
        "Here is today's training data. Reply with the JSON object specified "
        "in the system prompt — no other text, no markdown fences.\n\n"
        f"{json.dumps(user_context, indent=2, default=str)}"
    )
    response = _client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=load_system_prompt(),
        messages=[{"role": "user", "content": user_message}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline != -1 else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    return json.loads(text.strip())
