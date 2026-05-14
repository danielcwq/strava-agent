"""/help — list every registered slash command."""
from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec


def handle(ctx: CommandContext) -> CommandResult:
    # Late import to avoid a circular dep with src.telegram_commands.__init__.
    from src.telegram_commands import REGISTRY

    if not REGISTRY:
        return CommandResult(text="no commands registered")

    lines = ["*Commands*", ""]
    for name in sorted(REGISTRY):
        lines.append(f"`/{name}` — {REGISTRY[name].help}")
    return CommandResult(text="\n".join(lines), parse_mode="Markdown")


SPEC = CommandSpec(name="help", help="list all commands", handler=handle)
