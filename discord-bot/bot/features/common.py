"""Helpers shared by every command module."""

from __future__ import annotations

import logging
import time
import traceback
from typing import Any, Awaitable, Callable

import discord

from ..extras import ExtrasError
from ..timeutils import format_relative
from ..ui import Screen, error_screen, message_screen, send_screen
from ..weather import FeedError

log = logging.getLogger(__name__)


async def respond(
    interaction: discord.Interaction,
    build: Callable[[], Awaitable[Screen]],
    *,
    ephemeral: bool = False,
) -> None:
    """Defer, build a screen, and send it, turning upstream trouble into a message.

    Deferring first matters: Discord allows three seconds for the first
    answer, and a cold GTFS feed takes longer than that to load.
    """

    await interaction.response.defer(thinking=True, ephemeral=ephemeral)
    try:
        screen = await build()
    except (FeedError, ExtrasError) as exc:
        screen = error_screen(f"{exc} Try again in a minute.")
    except Exception as exc:  # noqa: BLE001 - the asker always gets an answer
        log.error("Command failed:\n%s", "".join(traceback.format_exception(exc)))
        screen = error_screen("Something went wrong on this side. Try again in a minute.")
    await send_screen(interaction, screen, ephemeral=ephemeral)


def mention(bot: Any, path: str) -> str:
    """A clickable command mention such as </weather forecast:123>.

    Falls back to plain text before the command ids are known. Commands are
    never wrapped in code formatting, which would stop them being clickable.
    """

    ids: dict[str, int] = getattr(bot, "command_ids", {}) or {}
    command_id = ids.get(path.split()[0])
    return f"</{path}:{command_id}>" if command_id else f"/{path}"


def updated_footer(source: str, fetched_at: float, stale: bool = False) -> str:
    age = format_relative(time.time() - fetched_at) if fetched_at else "unknown"
    note = f"{source} · updated {age}"
    if stale:
        note += " · upstream unreachable, showing the last good copy"
    return note


def loading_screen(what: str) -> Screen:
    return message_screen(
        f"The {what} timetable is still loading after a restart. Try again in a minute.",
        "Still warming up",
    )


def is_dm_with_bot(interaction: discord.Interaction) -> bool:
    """True in the bot's own DM. A DM between friends, reached through a user
    install, is a private channel instead, and so is a group DM."""

    return bool(interaction.context.dm_channel)
