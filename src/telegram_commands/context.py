"""/context — show the private project context Claude reads."""
from src import training_config
from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec


def handle(ctx: CommandContext) -> CommandResult:
    return CommandResult(text=training_config.profile_context(), parse_mode=None)


SPEC = CommandSpec(
    name="context",
    help="show the project context Claude uses for race-aware answers",
    handler=handle,
)
