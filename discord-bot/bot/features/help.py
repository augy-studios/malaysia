"""/help: what this is, every command, and links to the web app and donations."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from .. import config
from ..ui import BLUE, Link, Screen, add_field, embed, send_screen
from .account import invite_url
from .common import mention

SECTIONS = (
    (
        "🌦️ Weather",
        (
            ("weather forecast", "Seven day forecast for any town"),
            ("weather warnings", "MET warnings in force"),
            ("weather quake", "Recent earthquakes"),
            ("weather flood", "River levels, or one gauge in detail"),
        ),
    ),
    (
        "🚆 Trains",
        (
            ("train next", "Next departures from your home station"),
            ("train station", "A station's timetable"),
            ("train line", "Every station on a line"),
            ("train trip", "Follow one train stop by stop"),
            ("train live", "Live KTMB positions"),
        ),
    ),
    (
        "🚌 Buses",
        (
            ("bus stop", "A stop's next departures"),
            ("bus route", "Every stop on a route"),
            ("bus trip", "Follow one bus stop by stop"),
            ("bus live", "Live bus positions"),
        ),
    ),
    (
        "🇲🇾 Everyday",
        (
            ("prayer", "Prayer times for your zone"),
            ("fuel", "This week's fuel prices"),
            ("forex", "Ringgit exchange rates and conversion"),
        ),
    ),
    (
        "⭐ Yours",
        (
            ("fav list", "Your saved places"),
            ("fav add", "Save a place"),
            ("fav remove", "Remove a saved place"),
            ("alerts on", "Pick which alerts reach you by DM"),
            ("alerts off", "Turn every alert off"),
            ("alerts channel", "Post alerts in a server channel"),
            ("settings", "Quiet hours, digest time and thresholds"),
            ("help", "This page"),
        ),
    ),
)


def user_install_url(bot: Any) -> str:
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={bot.application_id}&integration_type=1&scope=applications.commands"
    )


def help_screen(bot: Any) -> Screen:
    body = embed(
        "Live Malaysian public data, right here in Discord",
        "Official forecasts, weather warnings, earthquakes and river levels, real train and "
        "bus timetables with live positions, prayer times, fuel prices and exchange rates. "
        "Everything comes from open government and community data, mostly "
        "[data.gov.my](https://data.gov.my).\n\n"
        "Save the places you care about and alerts follow them: a warning for your town, a "
        "river rising near home, a reminder before your train. Buttons keep working however "
        "old the message is.",
        BLUE,
        footer="Not an emergency service. In a flood, storm or earthquake, follow the local authorities.",
        url=config.SITE_URL,
    )
    for title, commands in SECTIONS:
        add_field(
            body,
            title,
            "\n".join(f"{mention(bot, path)} {text}" for path, text in commands),
        )

    links = [Link("Open the web app", config.SITE_URL, emoji="🌐")]
    if bot.settings.donation_url:
        links.append(Link("Support the project", bot.settings.donation_url, emoji="☕"))
    links.append(Link("Add to a server", invite_url(bot), emoji="➕"))
    links.append(Link("Add to my apps", user_install_url(bot), emoji="👤"))
    return Screen(embeds=[body], rows=[links])


@app_commands.command(name="help", description="What this does, every command, and links to the web app")
async def help_command(interaction: discord.Interaction) -> None:
    bot: Any = interaction.client
    # Posted for everyone in the channel to see, so it doubles as an
    # introduction when someone asks what the bot does.
    await send_screen(interaction, help_screen(bot))


__all__ = ["help_command", "help_screen"]
