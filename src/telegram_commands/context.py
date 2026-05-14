"""/context — show the project context file Claude reads for race-aware answers."""
from src.config import PROMPTS_DIR
from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec


def handle(ctx: CommandContext) -> CommandResult:
    path = PROMPTS_DIR / "project_context.md"
    if not path.exists():
        return CommandResult(text="no project_context.md is set")
    body = path.read_text()
    # Telegram message cap is 4096 chars; truncate with a marker if longer.
    if len(body) > 3800:
        body = body[:3800] + "\n\n…(truncated; edit prompts/project_context.md to update)"
    return CommandResult(text=body, parse_mode="Markdown")


SPEC = CommandSpec(
    name="context",
    help="show the project context Claude uses for race-aware answers",
    handler=handle,
)
