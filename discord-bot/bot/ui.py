"""Screens, buttons and the dispatcher that makes them survive restarts.

Every reply is a `Screen`: embeds plus rows of buttons, links and select
menus described as plain data. `materialise` turns the rows into a discord.py
view, storing each button's action and payload in SQLite and putting only the
row's token in the custom id:

    mb:b:<token>        a button
    mb:s:<row>          a select menu, whose option values are tokens

Both are `DynamicItem`s, which discord.py matches by pattern against any
component on any message, however old. So a button pressed after a restart
is looked up by token and runs exactly as it would have the day it was sent.
There is no per-message view kept in memory, and nothing to re-register.

Presses edit the message in place. A menu belongs to whoever opened it: when
somebody else presses a navigation button, they get their own private copy
rather than changing the message under its owner, and personal buttons
(settings, favourites) simply refuse them.

All of this goes through the interaction's own token, never the bot's. That
matters for user installs, where the bot is usually not a member of the
channel and could not touch the message any other way.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Union

import discord

log = logging.getLogger(__name__)

# Tokens owned by this id belong to nobody, so every press gets a private
# copy. Used for alerts posted to a server channel.
SHARED = 0

# Embed accent colours per area.
BLUE = 0x2563EB
TEAL = 0x0D9488
AMBER = 0xD97706
RED = 0xDC2626
GREEN = 0x16A34A
PURPLE = 0x7C3AED
GREY = 0x64748B

SEVERITY_COLOURS = {
    "danger": RED,
    "warning": AMBER,
    "alert": 0xEAB308,
    "high": RED,
    "medium": AMBER,
    "low": GREEN,
    "normal": GREEN,
    "unknown": GREY,
}
SEVERITY_EMOJI = {
    "danger": "🔴",
    "warning": "🟠",
    "alert": "🟡",
    "normal": "🟢",
    "unknown": "⚪",
    "high": "🔴",
    "medium": "🟠",
    "low": "🟢",
}


# ---------------------------------------------------------------------------
# Screen description
# ---------------------------------------------------------------------------


@dataclass
class Button:
    label: str
    action: str
    payload: dict[str, Any] | None = None
    style: discord.ButtonStyle = discord.ButtonStyle.secondary
    emoji: str | None = None
    disabled: bool = False


@dataclass
class Link:
    label: str
    url: str
    emoji: str | None = None


@dataclass
class Option:
    label: str
    action: str
    payload: dict[str, Any] | None = None
    description: str | None = None
    emoji: str | None = None


@dataclass
class Select:
    placeholder: str
    options: list[Option]


Row = Union[list[Union[Button, Link]], Select]


@dataclass
class Screen:
    embeds: list[discord.Embed]
    rows: list[Row] = field(default_factory=list)
    # A short private note shown to the presser after the message updates.
    toast: str | None = None
    # Send as a new message rather than editing the one pressed.
    fresh: bool = False


def embed(
    title: str | None = None,
    description: str | None = None,
    colour: int = BLUE,
    footer: str | None = None,
    url: str | None = None,
) -> discord.Embed:
    out = discord.Embed(
        title=clip(title, 256) if title else None,
        description=clip(description, 4096) if description else None,
        colour=colour,
        url=url,
    )
    if footer:
        out.set_footer(text=clip(footer, 2048))
    return out


def clip(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def add_field(target: discord.Embed, name: str, value: str, inline: bool = False) -> None:
    """Add a field, respecting Discord's limits so a long feed never breaks a send."""

    if len(target.fields) >= 25:
        return
    target.add_field(name=clip(name, 256) or "​", value=clip(value, 1024) or "​", inline=inline)


def lines_block(lines: list[str], limit: int = 4000, more: str = "and {n} more") -> str:
    """Join lines up to a size, noting how many were left out."""

    out: list[str] = []
    size = 0
    for index, line in enumerate(lines):
        if size + len(line) + 1 > limit - 40:
            out.append(f"*{more.format(n=len(lines) - index)}*")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def message_screen(text: str, title: str | None = None, colour: int = GREY) -> Screen:
    return Screen(embeds=[embed(title, text, colour)])


def error_screen(text: str) -> Screen:
    return Screen(embeds=[embed("Could not load that", text, RED)])


def pager(
    action: str, payload: dict[str, Any], page: int, total: int, per_page: int
) -> list[Button]:
    """Previous and next buttons for a paged list, empty when one page fits."""

    pages = max(1, -(-total // per_page))
    if pages <= 1:
        return []
    return [
        Button("Previous", action, {**payload, "p": page - 1}, emoji="◀️", disabled=page <= 0),
        Button(f"Page {page + 1} of {pages}", action, {**payload, "p": page}, disabled=True),
        Button("Next", action, {**payload, "p": page + 1}, emoji="▶️", disabled=page >= pages - 1),
    ]


def page_slice(items: list[Any], page: int, per_page: int) -> tuple[list[Any], int]:
    """The items on one page, with the page clamped into range."""

    pages = max(1, -(-len(items) // per_page))
    page = max(0, min(int(page or 0), pages - 1))
    return items[page * per_page : (page + 1) * per_page], page


# ---------------------------------------------------------------------------
# Persistent components
# ---------------------------------------------------------------------------


class TokenButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"mb:b:(?P<token>[A-Za-z0-9_\-]+)(?::\d+)?",
):
    def __init__(self, custom_id: str, token: str, **button: Any) -> None:
        row = button.pop("row", None)
        super().__init__(discord.ui.Button(custom_id=custom_id, **button), row=row)
        self.token = token

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any
    ) -> "TokenButton":
        return cls(item.custom_id or "", match["token"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await dispatch(interaction, self.token)


class TokenSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"mb:s:(?P<row>\d+)"):
    def __init__(self, custom_id: str, **select: Any) -> None:
        row = select.pop("row", None)
        select.setdefault("options", [discord.SelectOption(label="-")])
        super().__init__(discord.ui.Select(custom_id=custom_id, **select), row=row)

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Select, match: Any
    ) -> "TokenSelect":
        return cls(item.custom_id or "")

    async def callback(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if values:
            await dispatch(interaction, str(values[0]))


async def materialise(db: Any, screen: Screen, owner_id: int | None) -> discord.ui.View | None:
    """Build the view for a screen, registering every button in SQLite."""

    rows = [row for row in screen.rows if row][:5]
    if not rows:
        return None

    view = discord.ui.View(timeout=None)
    used: set[str] = set()
    for index, row in enumerate(rows):
        if isinstance(row, Select):
            options: list[discord.SelectOption] = []
            seen: set[str] = set()
            for option in row.options[:25]:
                token = await db.make_token(option.action, option.payload, owner_id)
                if token in seen:
                    continue
                seen.add(token)
                options.append(
                    discord.SelectOption(
                        label=clip(option.label, 100),
                        value=token,
                        description=clip(option.description, 100) if option.description else None,
                        emoji=option.emoji,
                    )
                )
            if options:
                view.add_item(
                    TokenSelect(
                        f"mb:s:{index}",
                        placeholder=clip(row.placeholder, 150),
                        options=options,
                        row=index,
                    )
                )
            continue

        for item in row[:5]:
            if isinstance(item, Link):
                view.add_item(
                    discord.ui.Button(
                        style=discord.ButtonStyle.link,
                        label=clip(item.label, 80),
                        url=item.url,
                        emoji=item.emoji,
                        row=index,
                    )
                )
                continue
            token = await db.make_token(item.action, item.payload, owner_id)
            custom_id = f"mb:b:{token}"
            # Two identical buttons on one message, such as a disabled page
            # label, still need distinct custom ids.
            suffix = 1
            while custom_id in used:
                custom_id = f"mb:b:{token}:{suffix}"
                suffix += 1
            used.add(custom_id)
            view.add_item(
                TokenButton(
                    custom_id,
                    token,
                    label=clip(item.label, 80),
                    style=item.style,
                    emoji=item.emoji,
                    disabled=item.disabled,
                    row=index,
                )
            )
    return view if view.children else None


def _kwargs(screen: Screen, view: discord.ui.View | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"embeds": screen.embeds[:10]}
    if view is not None:
        kwargs["view"] = view
    return kwargs


async def send_screen(
    interaction: discord.Interaction,
    screen: Screen,
    *,
    ephemeral: bool = False,
    owner_id: int | None = None,
) -> None:
    """Answer an interaction with a screen, whether or not it was deferred."""

    bot: Any = interaction.client
    owner = interaction.user.id if owner_id is None else owner_id
    view = await materialise(bot.db, screen, owner)
    kwargs = _kwargs(screen, view)
    if interaction.response.is_done():
        await interaction.followup.send(ephemeral=ephemeral, **kwargs)
    else:
        await interaction.response.send_message(ephemeral=ephemeral, **kwargs)
    if screen.toast:
        await interaction.followup.send(screen.toast, ephemeral=True)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@dataclass
class Ctx:
    bot: Any
    interaction: discord.Interaction

    @property
    def user_id(self) -> int:
        return self.interaction.user.id

    @property
    def db(self) -> Any:
        return self.bot.db


ActionFn = Callable[[Ctx, dict[str, Any]], Awaitable[Screen | None]]
ACTIONS: dict[str, tuple[ActionFn, bool]] = {}

# Raised by an action to show the presser a private message and change
# nothing else.
class Refuse(Exception):
    pass


def action(name: str, *, personal: bool = False) -> Callable[[ActionFn], ActionFn]:
    """Register a button action. Personal actions only answer the menu's owner."""

    def decorate(fn: ActionFn) -> ActionFn:
        if name in ACTIONS:
            raise RuntimeError(f"Action {name!r} registered twice.")
        ACTIONS[name] = (fn, personal)
        return fn

    return decorate


EXPIRED = (
    "This button is no longer available. Run the command again for a fresh one."
)
NOT_YOURS = (
    "This menu belongs to someone else. Run the same command to open your own."
)


async def dispatch(interaction: discord.Interaction, token: str) -> None:
    bot: Any = interaction.client
    resolved = await bot.db.resolve_token(token)
    entry = ACTIONS.get(resolved[0]) if resolved else None
    if resolved is None or entry is None:
        await interaction.response.send_message(EXPIRED, ephemeral=True)
        return

    name, payload, owner = resolved
    fn, personal = entry
    foreign = owner is not None and owner != interaction.user.id
    if foreign and personal:
        await interaction.response.send_message(NOT_YOURS, ephemeral=True)
        return

    ctx = Ctx(bot, interaction)
    if foreign:
        await interaction.response.defer(ephemeral=True, thinking=True)
    else:
        await interaction.response.defer()

    try:
        screen = await fn(ctx, payload)
    except Refuse as refusal:
        await interaction.followup.send(str(refusal), ephemeral=True)
        return
    except Exception as exc:  # noqa: BLE001 - the presser always gets an answer
        log.error("Action %s failed:\n%s", name, "".join(traceback.format_exception(exc)))
        await interaction.followup.send("Something went wrong there. Try again.", ephemeral=True)
        return

    if screen is None:
        return

    view = await materialise(bot.db, screen, interaction.user.id)
    kwargs = _kwargs(screen, view)
    if foreign or screen.fresh:
        await interaction.followup.send(ephemeral=foreign, **kwargs)
    else:
        if view is None:
            kwargs["view"] = None
        await interaction.edit_original_response(content=None, **kwargs)
    if screen.toast:
        await interaction.followup.send(screen.toast, ephemeral=True)
