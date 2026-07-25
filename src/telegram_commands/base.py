"""Types for the slash-command framework.

A command lives in its own file under src/telegram_commands/. Each file exports
a single module-level `SPEC = CommandSpec(...)` and a `handle(ctx) -> str | CommandResult`
function. The registry auto-discovers everything on import.
"""
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandContext:
    """Everything a command handler needs to know about the inbound message."""

    chat_id: str
    message_id: int
    user_text: str           # full message text, including the leading slash
    args: list[str]          # whitespace-split tokens after the command name


@dataclass
class CommandResult:
    """Return type for a command handler that needs control over formatting.

    Handlers may also return a plain `str`, which the framework wraps as
    CommandResult(text=that_str, parse_mode=None).
    """

    text: str
    parse_mode: str | None = None   # "Markdown" | "MarkdownV2" | None


@dataclass(frozen=True)
class CommandSpec:
    """Registration record for one slash command."""

    name: str                       # without leading slash, e.g. "last"
    help: str                       # one-line description shown by /help
    handler: Callable[[CommandContext], "str | CommandResult"]
