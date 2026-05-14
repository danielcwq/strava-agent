"""/reset — clear the conversation history for this chat."""
from src import db
from src.telegram_commands.base import CommandContext, CommandSpec


def handle(ctx: CommandContext) -> str:
    n = db.reset_conversation(ctx.chat_id)
    return f"cleared {n} message{'s' if n != 1 else ''} from history"


SPEC = CommandSpec(name="reset", help="clear conversation history", handler=handle)
