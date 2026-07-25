"""/context — show the private project context Claude reads."""
from src import training_config
from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec


def handle(ctx: CommandContext) -> CommandResult:
    body = training_config.project_context()
    if not body:
        return CommandResult(text="no project context is set")
    # Telegram message cap is 4096 chars; truncate with a marker if longer.
    if len(body) > 3800:
        body = body[:3800] + "\n\n…(truncated; edit the private training profile to update)"
    return CommandResult(text=body, parse_mode="Markdown")


SPEC = CommandSpec(
    name="context",
    help="show the project context Claude uses for race-aware answers",
    handler=handle,
)
