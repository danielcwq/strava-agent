"""Auto-discovering slash-command registry.

To add a new command: create `src/telegram_commands/<name>.py` exporting a
`SPEC = CommandSpec(...)` at module level. It will be registered automatically
the next time the app starts.
"""
import importlib
import logging
from pathlib import Path

from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec

logger = logging.getLogger(__name__)

REGISTRY: dict[str, CommandSpec] = {}


def _discover() -> None:
    pkg_dir = Path(__file__).parent
    for path in sorted(pkg_dir.glob("*.py")):
        if path.stem in ("__init__", "base"):
            continue
        mod_name = f"src.telegram_commands.{path.stem}"
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            logger.exception("failed to import command module %s", mod_name)
            continue
        spec = getattr(mod, "SPEC", None)
        if isinstance(spec, CommandSpec):
            REGISTRY[spec.name] = spec
        else:
            logger.warning("module %s does not export a CommandSpec; skipping", mod_name)


_discover()


def dispatch(text: str, chat_id: str, message_id: int) -> CommandResult | None:
    """Parse `/cmd arg1 arg2 ...` and run the registered handler.

    Returns None if the text is not a slash command, so the caller can fall
    back (e.g. echo or AI handling later). Returns a CommandResult to send back
    to the user otherwise.
    """
    if not text.startswith("/"):
        return None

    parts = text[1:].split()
    if not parts:
        return None

    name, *args = parts
    spec = REGISTRY.get(name)
    if spec is None:
        known = ", ".join(sorted("/" + n for n in REGISTRY))
        return CommandResult(text=f"unknown command: /{name}\nknown: {known}")

    ctx = CommandContext(chat_id=chat_id, message_id=message_id, user_text=text, args=args)
    try:
        result = spec.handler(ctx)
    except Exception as e:
        from src import archive

        archive.event("command.error", {"name": name, "error_type": type(e).__name__})
        logger.exception("error running /%s", name)
        return CommandResult(text=f"error running /{name}: {type(e).__name__}", failed=True)

    if isinstance(result, CommandResult):
        return result
    return CommandResult(text=str(result))
