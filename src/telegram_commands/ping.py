"""/ping — bot liveness check."""
from src.telegram_commands.base import CommandContext, CommandSpec


def handle(ctx: CommandContext) -> str:
    return "pong"


SPEC = CommandSpec(name="ping", help="check the bot is alive", handler=handle)
