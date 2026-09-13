"""/context — readable saved preferences, with paged access to longer notes."""

import re
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from src import training_config
from src.config import settings
from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_ROLES = {"key": "Quality / track", "long": "Long run", "support": "Easy / support", "rest": "Rest"}
_SECTIONS = {
    "instructions": ("Coaching instructions", "training_principles"),
    "background": ("Training background", "project_context"),
}
_USAGE = "Use /context, /context instructions, /context background, or /context race."


def _render(text: str) -> str:
    """Render the small Markdown subset used in saved notes as safe Telegram HTML."""
    text = escape(text, quote=False)
    # One pass avoids creating crossed tags when saved Markdown is malformed.
    def inline(match):
        value = match.group()
        if value.startswith("`"):
            return "<code>" + value[1:-1] + "</code>"
        if value.startswith("**"):
            return "<b>" + value[2:-2] + "</b>"
        return "<i>" + value[1:-1] + "</i>"

    text = re.sub(
        r"`[^`\n]+`|\*\*[^*\n]+\*\*|(?<!\*)\*[^*\n]+\*(?!\*)", inline, text
    )
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.M)
    return re.sub(r"^\s*[-*] ", "• ", text, flags=re.M)


def _pages(text: str) -> list[str]:
    """Split before rendering so tags/entities never cross Telegram messages."""
    pages = []
    remaining = text.strip() or "Nothing saved yet."
    while remaining:
        low, high = 1, min(len(remaining), 3200)
        while low < high:
            mid = (low + high + 1) // 2
            if len(_render(remaining[:mid]).encode("utf-16-le")) // 2 <= 3200:
                low = mid
            else:
                high = mid - 1
        cut = low
        if cut < len(remaining):
            for separator in ("\n\n", "\n", " "):
                boundary = remaining.rfind(separator, 0, cut)
                if boundary >= cut // 2:
                    cut = boundary + len(separator)
                    break
        pages.append(_render(remaining[:cut].rstrip()))
        remaining = remaining[cut:].lstrip()
    return pages


def _value(value: str, limit: int = 180) -> str:
    text = " ".join(value.split()) if value else "Not set"
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _race_lines(race: dict, *, compact: bool) -> list[str]:
    lines = []
    for key, label in (
        ("name", "Race"),
        ("date", "Date"),
        ("distance", "Distance"),
        ("target_time", "Target time"),
        ("intentions", "Intentions"),
    ):
        value = race.get(key) or "Not set"
        if key == "date" and race.get(key):
            try:
                when = datetime.strptime(value, "%Y-%m-%d").date()
                value = when.strftime("%d %b %Y")
                if when < datetime.now(ZoneInfo(settings.timezone)).date():
                    value += " (past)"
            except ValueError:
                pass
        lines.append(f"{label}: {_value(value) if compact else value}")
    return lines


def handle(ctx: CommandContext) -> CommandResult:
    args = list(ctx.args)
    section = args.pop(0).lower() if args and not args[0].isdigit() else "overview"
    if section not in {"overview", "race", *_SECTIONS}:
        return CommandResult(text=_USAGE)
    if len(args) > 1 or (args and (not args[0].isdigit() or len(args[0]) > 6)):
        return CommandResult(text=_USAGE + " Add a page number at the end if needed.")
    page = int(args[0]) if args else 1
    profile = training_config.get_profile()
    document = profile["document"]
    if section in _SECTIONS:
        title, key = _SECTIONS[section]
        body = document["prompts"].get(key, "")
    elif section == "race":
        title = "Race goal"
        body = "\n".join(_race_lines(document.get("race", {}), compact=False))
    else:
        title = "Your coaching profile"
        lines = [
            "## Training phase",
            _value(document["training"].get("current_phase")),
            "",
            "## Weekly schedule",
        ]
        for index, day in enumerate(_DAYS):
            role = document["weekly_roles"].get(str(index), "")
            lines.append(f"{day}: {_ROLES.get(role, role or 'Not set')}")
        lines.extend(["", "## Race goal", *_race_lines(document.get("race", {}), compact=True)])
        lines.extend(
            [
                "",
                "## Saved notes",
                "/context instructions — coaching instructions",
                "/context background — training background",
                "/context race — full race details",
                "",
                "To edit, say: “Update my coaching instructions: …”",
            ]
        )
        body = "\n".join(lines)
    pages = _pages(body)
    if not 1 <= page <= len(pages):
        return CommandResult(text=f"Choose a page from 1 to {len(pages)}. {_USAGE}")
    header = f"<b>{title}</b> · revision {profile['revision']}"
    footer = ""
    if len(pages) > 1:
        header += f" · {page}/{len(pages)}"
        if page < len(pages):
            command = "/context" + (f" {section}" if section != "overview" else "")
            footer = f"\n\nNext: {command} {page + 1}"
    if section != "overview":
        footer += "\n\n/context — back to your profile"
    return CommandResult(text=f"{header}\n\n{pages[page - 1]}{footer}", parse_mode="HTML")


SPEC = CommandSpec(
    name="context",
    help="view saved schedule, race goal, and coaching instructions",
    handler=handle,
)
