"""/reset — start a fresh conversation for this chat."""

from src import archive
from src.telegram_commands.base import CommandContext, CommandSpec


def handle(ctx: CommandContext) -> str:
    archive.reset_session(ctx.chat_id)
    return "Started a fresh conversation. Your saved profile and archived history are preserved."


SPEC = CommandSpec(
    name="reset", help="start fresh; preserve profile and archived history", handler=handle
)
