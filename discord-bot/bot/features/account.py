"""/fav, /alerts, /settings and /stats: everything that belongs to a person or a server."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from ..timeutils import ts
from ..transit import BUS, OPERATORS, RAIL, operator_label
from ..ui import (
    AMBER,
    BLUE,
    GREY,
    PURPLE,
    RED,
    Button,
    Ctx,
    Link,
    Option,
    Refuse,
    Screen,
    Select,
    action,
    add_field,
    embed,
    message_screen,
    page_slice,
    pager,
    send_screen,
)
from ..weather import FLOOD_LEVELS, STATES
from .common import mention, respond

log = logging.getLogger(__name__)

fav = app_commands.Group(name="fav", description="Your saved towns, river gauges, stations and bus stops")
alerts = app_commands.Group(name="alerts", description="Alerts by DM, or posted to a server channel")

KIND_LABELS = {
    "town": ("🌤️", "Towns"),
    "gauge": ("🌊", "River gauges"),
    "station": ("🚆", "Stations"),
    "stop": ("🚌", "Bus stops"),
}

# Personal alerts: (kind, label, what triggers it). All of them are DMs.
ALERT_KINDS = (
    ("warning", "Weather warnings", "A MET warning naming one of your saved towns"),
    ("flood", "River levels", "A saved gauge reaching your flood threshold"),
    ("quake", "Earthquakes", "A quake at or above your magnitude threshold"),
    ("national_flood", "National flood watch", "Any gauge in Malaysia reaching danger level"),
    ("digest", "Morning digest", "Your saved places, once a day at your digest time"),
    ("departure", "Departure reminders", "Before each train or bus at a saved stop"),
    ("live", "Live vehicle alerts", "A train or bus nearing a saved stop"),
    ("prayer", "Prayer reminders", "At each of the five prayer times in your zone"),
    ("fuel", "Weekly fuel prices", "When the new week's prices are announced"),
)
ALERT_LABELS = {kind: label for kind, label, _ in ALERT_KINDS}

QUIET_PRESETS = (
    ("off", "Off", "Alerts at any hour"),
    ("22:00-06:00", "10pm to 6am", None),
    ("23:00-06:00", "11pm to 6am", None),
    ("23:00-07:00", "11pm to 7am", None),
    ("00:00-07:00", "Midnight to 7am", None),
    ("00:00-08:00", "Midnight to 8am", None),
)
DIGEST_PRESETS = ("05:30", "06:00", "06:30", "07:00", "07:30", "08:00", "09:00", "12:00", "18:00", "21:00")
LEAD_PRESETS = (5, 10, 15, 20, 30, 45)
QUAKE_PRESETS = (4.0, 4.5, 5.0, 5.5, 6.0)


# ---------------------------------------------------------------------------
# Favourites
# ---------------------------------------------------------------------------


def fav_open_option(row: Any) -> Option:
    emoji, _ = KIND_LABELS.get(row["kind"], ("⭐", ""))
    kind = row["kind"]
    if kind == "town":
        target = ("wx.town", {"k": row["ref_id"]})
    elif kind == "gauge":
        target = ("wx.gauge", {"id": row["ref_id"]})
    else:
        target = ("tr.stop", {"o": row["operator"], "s": row["ref_id"]})
    description = operator_label(row["operator"]) if row["operator"] else KIND_LABELS[kind][1][:-1]
    return Option(row["label"], target[0], target[1], description=description, emoji=emoji)


async def favourites_screen(bot: Any, user_id: int, page: int = 0, removing: bool = False) -> Screen:
    rows = await bot.db.list_favourites(user_id)
    if not rows:
        return Screen(
            embeds=[
                embed(
                    "⭐ No favourites yet",
                    "Save a place with its ⭐ button, or add one straight away with "
                    f"{mention(bot, 'fav add')}.\n\nFavourites drive your alerts: warnings follow "
                    "saved towns, river alerts follow saved gauges, and departure reminders "
                    "follow saved stations and stops.",
                    AMBER,
                )
            ]
        )

    body = embed(
        f"⭐ Your favourites ({len(rows)})",
        "Pick one below to remove it." if removing else "Pick one below to open it.",
        RED if removing else AMBER,
    )
    grouped: dict[str, list[str]] = {}
    for row in rows:
        suffix = f" ({operator_label(row['operator'])})" if row["operator"] else ""
        grouped.setdefault(row["kind"], []).append(f"{row['label']}{suffix}")
    for kind, (emoji, label) in KIND_LABELS.items():
        if kind in grouped:
            add_field(body, f"{emoji} {label}", "\n".join(grouped[kind]))

    items, page = page_slice(list(rows), page, 25)
    if removing:
        picker = Select(
            "Remove which favourite?",
            [
                Option(row["label"], "fav.remove", {"id": row["id"], "p": page}, emoji="🗑️")
                for row in items
            ],
        )
        toggle = Button("Done", "fav.list", {"p": page}, emoji="✅")
    else:
        picker = Select("Open a favourite", [fav_open_option(row) for row in items])
        toggle = Button("Remove some", "fav.list", {"p": page, "rm": True}, emoji="🗑️")
    return Screen(
        embeds=[body],
        rows=[
            picker,
            pager("fav.list", {"rm": removing}, page, len(rows), 25),
            [toggle],
        ],
    )


@action("fav.list", personal=True)
async def _fav_list(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await favourites_screen(ctx.bot, ctx.user_id, int(p.get("p", 0)), bool(p.get("rm")))


@action("fav.remove", personal=True)
async def _fav_remove(ctx: Ctx, p: dict[str, Any]) -> Screen:
    row = await ctx.db.get_favourite(ctx.user_id, int(p.get("id", 0)))
    if row is not None:
        await ctx.db.remove_favourite(ctx.user_id, row["id"])
    screen = await favourites_screen(ctx.bot, ctx.user_id, int(p.get("p", 0)), removing=True)
    screen.toast = f"Removed {row['label']}." if row else "That one was already gone."
    return screen


async def any_place_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Towns, gauges, stations and stops together, from what is already loaded."""

    bot: Any = interaction.client
    if len(current.strip()) < 2:
        return []
    choices: list[app_commands.Choice[str]] = []
    for loc in bot.weather.peek_locations(current, 6):
        choices.append(app_commands.Choice(name=f"🌤️ {loc.name} (forecast town)"[:100], value=f"town|{loc.key}"[:100]))
    for st in bot.weather.peek_stations(current, 6):
        choices.append(app_commands.Choice(name=f"🌊 {st.name} (river gauge)"[:100], value=f"gauge|{st.station_id}"[:100]))
    for op, stop in bot.transit.peek_stops(RAIL, current, 7):
        choices.append(app_commands.Choice(name=f"🚆 {stop.name} ({operator_label(op)})"[:100], value=f"station|{op}|{stop.stop_id}"[:100]))
    for op, stop in bot.transit.peek_stops(BUS, current, 6):
        choices.append(app_commands.Choice(name=f"🚌 {stop.name} ({operator_label(op)})"[:100], value=f"stop|{op}|{stop.stop_id}"[:100]))
    return choices[:25]


async def favourite_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    bot: Any = interaction.client
    rows = await bot.db.list_favourites(interaction.user.id)
    needle = current.strip().lower()
    return [
        app_commands.Choice(
            name=f"{KIND_LABELS.get(row['kind'], ('⭐',))[0]} {row['label']}"[:100], value=str(row["id"])
        )
        for row in rows
        if needle in row["label"].lower()
    ][:25]


@fav.command(name="list", description="Everything you have saved, with buttons to open or remove each")
async def fav_list(interaction: discord.Interaction) -> None:
    bot: Any = interaction.client
    await respond(interaction, lambda: favourites_screen(bot, interaction.user.id), ephemeral=True)


@fav.command(name="add", description="Save a town, river gauge, station or bus stop")
@app_commands.describe(place="Start typing a name and pick from the list")
@app_commands.autocomplete(place=any_place_autocomplete)
async def fav_add(interaction: discord.Interaction, place: str) -> None:
    bot: Any = interaction.client
    uid = interaction.user.id

    async def build() -> Screen:
        kind, _, rest = place.partition("|")
        label = ""
        operator = ""
        ref = rest
        if kind == "town":
            location, _ = await bot.weather.location(rest)
            if location:
                ref, label = location.key, location.name
        elif kind == "gauge":
            station, _ = await bot.weather.station(rest)
            if station:
                ref, label = station.station_id, station.name
        elif kind in ("station", "stop"):
            operator, _, ref = rest.partition("|")
            if operator in OPERATORS:
                feed = await bot.transit.feed(operator)
                stop = feed.stops.get(ref)
                if stop:
                    label = stop.name
        if not label:
            return message_screen(
                "Pick a place from the list as you type, so there is no doubt which one you mean.",
                "Which place?",
            )
        added = await bot.db.add_favourite(uid, kind, ref, label, operator)
        emoji, _ = KIND_LABELS[kind]
        text = f"{emoji} **{label}** is saved." if added else f"{emoji} **{label}** was already saved."
        return Screen(
            embeds=[embed("⭐ Favourites", text + f"\n\nSee them all with {mention(bot, 'fav list')}.", AMBER)],
            rows=[
                Select(
                    "Open it",
                    [fav_open_option({"kind": kind, "ref_id": ref, "label": label, "operator": operator})],
                )
            ],
        )

    await respond(interaction, build, ephemeral=True)


@fav.command(name="remove", description="Remove one of your favourites")
@app_commands.describe(item="Pick from your favourites")
@app_commands.autocomplete(item=favourite_autocomplete)
async def fav_remove(interaction: discord.Interaction, item: str) -> None:
    bot: Any = interaction.client

    async def build() -> Screen:
        try:
            favourite_id = int(item)
        except ValueError:
            favourite_id = 0
        row = await bot.db.get_favourite(interaction.user.id, favourite_id)
        if row is None:
            return message_screen("Pick one of your favourites from the list as you type.", "Which one?")
        await bot.db.remove_favourite(interaction.user.id, favourite_id)
        return message_screen(f"Removed **{row['label']}** from your favourites.", "⭐ Favourites", AMBER)

    await respond(interaction, build, ephemeral=True)


# ---------------------------------------------------------------------------
# Personal alerts
# ---------------------------------------------------------------------------


async def alerts_screen(bot: Any, user_id: int) -> Screen:
    user = await bot.db.ensure_user(user_id)
    active = await bot.db.user_subscriptions(user_id)
    favs = await bot.db.list_favourites(user_id)
    kinds = {row["kind"] for row in favs}

    lines = []
    for kind, label, detail in ALERT_KINDS:
        mark = "🟢" if kind in active else "⚫"
        lines.append(f"{mark} **{label}**: {detail}")
    body = embed("🔔 Your alerts", "\n".join(lines), PURPLE)
    body.description += "\n\nAlerts arrive as DMs from me. Press a button to switch one on or off."

    notes = []
    if not user["dm_ok"]:
        notes.append(
            "⚠️ My last DM to you was refused. Allow direct messages from this app, or from "
            "server members, then switch an alert off and on again to test."
        )
    if ("warning" in active or "digest" in active) and "town" not in kinds:
        notes.append(f"Save a town with {mention(bot, 'weather forecast')} for warnings and the digest to cover.")
    if "flood" in active and "gauge" not in kinds:
        notes.append(f"Save a river gauge with {mention(bot, 'weather flood')} for river alerts.")
    if ("departure" in active or "live" in active) and not ({"station", "stop"} & kinds):
        notes.append(f"Save a station or stop with {mention(bot, 'train station')} or {mention(bot, 'bus stop')}.")
    if "prayer" in active and not user["prayer_zone"]:
        notes.append(f"Pick your zone with {mention(bot, 'prayer')} and press Set as my zone.")
    if notes:
        add_field(body, "Before these can reach you", "\n".join(notes))
    add_field(
        body,
        "Your thresholds",
        f"Quiet hours: {quiet_text(user)} (prayer reminders and danger level alerts still come through)\n"
        f"Digest at {user['digest_time']} Malaysia time, reminders {user['lead_minutes']} min ahead\n"
        f"Rivers from {user['flood_threshold'].lower()}, quakes from M{user['quake_threshold']:.1f}\n"
        f"Change these in {mention(bot, 'settings')}.",
    )

    buttons = [
        Button(
            label,
            "al.toggle",
            {"k": kind},
            style=discord.ButtonStyle.success if kind in active else discord.ButtonStyle.secondary,
        )
        for kind, label, _ in ALERT_KINDS
    ]
    return Screen(
        embeds=[body],
        rows=[
            buttons[0:5],
            buttons[5:10],
            [Button("Turn everything off", "al.off", {}, style=discord.ButtonStyle.danger, disabled=not active)],
        ],
    )


def quiet_text(user: Any) -> str:
    if not user["quiet_enabled"]:
        return "off"
    return f"{user['quiet_from']} to {user['quiet_to']}"


async def check_dm(bot: Any, user: discord.abc.User) -> bool:
    """Send a first DM, which shows whether alerts can reach this person at all."""

    try:
        await user.send(
            embed=embed(
                "🔔 Alerts will arrive here",
                f"This is where your alerts will come. Manage them any time with {mention(bot, 'alerts on')}.",
                PURPLE,
            )
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.info("DM check failed for %s: %s", user.id, exc)
        await bot.db.set_pref(user.id, "dm_ok", 0)
        return False
    await bot.db.set_pref(user.id, "dm_ok", 1)
    return True


@action("al.toggle", personal=True)
async def _toggle(ctx: Ctx, p: dict[str, Any]) -> Screen:
    kind = str(p.get("k"))
    if kind not in ALERT_LABELS:
        raise Refuse("That alert no longer exists.")
    active = await ctx.db.user_subscriptions(ctx.user_id)
    turning_on = kind not in active
    await ctx.db.set_subscription(ctx.user_id, kind, turning_on)
    toast = None
    if turning_on:
        user = await ctx.db.ensure_user(ctx.user_id)
        # The first alert switched on, or DMs that failed before, get a test.
        if not active or not user["dm_ok"]:
            if not await check_dm(ctx.bot, ctx.interaction.user):
                toast = (
                    "I could not DM you, so alerts cannot reach you yet. Open your privacy "
                    "settings and allow direct messages, then press the button again."
                )
        await ctx.bot.scheduler.plan_for(ctx.user_id)
    screen = await alerts_screen(ctx.bot, ctx.user_id)
    screen.toast = toast or f"{ALERT_LABELS[kind]} {'on' if turning_on else 'off'}."
    return screen


@action("al.off", personal=True)
async def _off(ctx: Ctx, p: dict[str, Any]) -> Screen:
    await ctx.db.clear_subscriptions(ctx.user_id)
    screen = await alerts_screen(ctx.bot, ctx.user_id)
    screen.toast = "Every alert is off."
    return screen


@alerts.command(name="on", description="Choose which alerts reach you by DM")
async def alerts_on(interaction: discord.Interaction) -> None:
    bot: Any = interaction.client
    await respond(interaction, lambda: alerts_screen(bot, interaction.user.id), ephemeral=True)


@alerts.command(name="off", description="Turn every personal alert off at once")
async def alerts_off(interaction: discord.Interaction) -> None:
    bot: Any = interaction.client

    async def build() -> Screen:
        count = await bot.db.clear_subscriptions(interaction.user.id)
        text = (
            f"All {count} of your alerts are off. Turn any back on with {mention(bot, 'alerts on')}."
            if count
            else f"You had no alerts on. {mention(bot, 'alerts on')} lists them."
        )
        return message_screen(text, "🔕 Alerts off")

    await respond(interaction, build, ephemeral=True)


# ---------------------------------------------------------------------------
# Server alert channel
# ---------------------------------------------------------------------------


def invite_url(bot: Any) -> str:
    permissions = discord.Permissions(
        view_channel=True, send_messages=True, send_messages_in_threads=True, embed_links=True
    )
    return discord.utils.oauth_url(
        bot.application_id, permissions=permissions, scopes=("bot", "applications.commands")
    )


def can_manage(interaction: discord.Interaction) -> bool:
    permissions = interaction.permissions
    return bool(permissions.manage_guild or permissions.administrator)


async def server_screen(bot: Any, guild_id: int) -> Screen:
    row = await bot.db.get_guild_alerts(guild_id)
    if row is None:
        return message_screen(
            f"Nothing is posted in this server. Run {mention(bot, 'alerts channel')} with a channel to start.",
            "Server alerts off",
        )
    flags = (
        ("warnings", "Weather warnings"),
        ("floods", "Rivers at danger"),
        ("quakes", "Earthquakes"),
        ("fuel", "Weekly fuel prices"),
    )
    lines = [f"{'🟢' if row[f] else '⚫'} {label}" for f, label in flags]
    lines.append(f"Earthquakes from M{row['quake_min']:.1f}")
    lines.append(f"Area: {row['state'] or 'all of Malaysia'}")
    body = embed(
        "📣 Server alerts",
        f"Posting to <#{row['channel_id']}>.\n\n" + "\n".join(lines),
        PURPLE,
        footer="Warnings and river levels can be limited to one state. Earthquakes and fuel prices are national.",
    )
    buttons = [
        Button(
            label,
            "sv.flag",
            {"g": guild_id, "f": f},
            style=discord.ButtonStyle.success if row[f] else discord.ButtonStyle.secondary,
        )
        for f, label in flags
    ]
    buttons.append(
        Button(f"Quakes from M{row['quake_min']:.1f}", "sv.quake", {"g": guild_id}, emoji="🌏")
    )
    state_options = [Option("All of Malaysia", "sv.state", {"g": guild_id, "s": ""}, emoji="🇲🇾")]
    state_options += [Option(s, "sv.state", {"g": guild_id, "s": s}) for s in STATES]
    return Screen(
        embeds=[body],
        rows=[
            buttons,
            Select("Limit warnings and rivers to one state", state_options),
            [Button("Stop posting here", "sv.stop", {"g": guild_id}, style=discord.ButtonStyle.danger)],
        ],
    )


def _server_guard(ctx: Ctx, guild_id: int) -> None:
    if ctx.interaction.guild_id != guild_id or not can_manage(ctx.interaction):
        raise Refuse("Only someone with Manage Server can change this server's alerts.")


@action("sv.flag", personal=True)
async def _server_flag(ctx: Ctx, p: dict[str, Any]) -> Screen:
    guild_id, flag = int(p.get("g", 0)), str(p.get("f"))
    _server_guard(ctx, guild_id)
    row = await ctx.db.get_guild_alerts(guild_id)
    if row is not None and flag in ("warnings", "floods", "quakes", "fuel"):
        await ctx.db.set_guild_flag(guild_id, flag, 0 if row[flag] else 1)
    return await server_screen(ctx.bot, guild_id)


@action("sv.quake", personal=True)
async def _server_quake(ctx: Ctx, p: dict[str, Any]) -> Screen:
    guild_id = int(p.get("g", 0))
    _server_guard(ctx, guild_id)
    row = await ctx.db.get_guild_alerts(guild_id)
    if row is not None:
        current = float(row["quake_min"])
        following = next((q for q in QUAKE_PRESETS if q > current), QUAKE_PRESETS[0])
        await ctx.db.set_guild_flag(guild_id, "quake_min", following)
    return await server_screen(ctx.bot, guild_id)


@action("sv.state", personal=True)
async def _server_state(ctx: Ctx, p: dict[str, Any]) -> Screen:
    guild_id = int(p.get("g", 0))
    _server_guard(ctx, guild_id)
    state = str(p.get("s", ""))
    if state and state not in STATES:
        raise Refuse("Unknown state.")
    await ctx.db.set_guild_flag(guild_id, "state", state)
    return await server_screen(ctx.bot, guild_id)


@action("sv.stop", personal=True)
async def _server_stop(ctx: Ctx, p: dict[str, Any]) -> Screen:
    guild_id = int(p.get("g", 0))
    _server_guard(ctx, guild_id)
    await ctx.db.delete_guild_alerts(guild_id)
    return message_screen(
        f"Nothing more will be posted in this server. {mention(ctx.bot, 'alerts channel')} starts it again.",
        "Server alerts off",
    )


@alerts.command(name="channel", description="Post public alerts in a channel of this server. Needs Manage Server")
@app_commands.describe(channel="Where to post. Leave empty to see or change the current setup")
async def alerts_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel | discord.Thread | None = None,
) -> None:
    bot: Any = interaction.client

    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Server alerts are set up inside a server. For alerts to yourself, use "
            f"{mention(bot, 'alerts on')}.",
            ephemeral=True,
        )
        return
    if not can_manage(interaction):
        await interaction.response.send_message(
            "Only someone with Manage Server can set up alerts for this server. "
            f"For alerts to yourself, use {mention(bot, 'alerts on')}.",
            ephemeral=True,
        )
        return

    guild = bot.get_guild(interaction.guild_id)
    if guild is None:
        # Reached through a user install: the commands work here, but the bot
        # is not a member, so it has no way to post in a channel on its own.
        await send_screen(
            interaction,
            Screen(
                embeds=[
                    embed(
                        "Add me to this server first",
                        "Your commands work here through your own account, but posting alerts "
                        "on a schedule needs me to be a member of the server.",
                        AMBER,
                    )
                ],
                rows=[[Link("Add to this server", invite_url(bot), emoji="➕")]],
            ),
            ephemeral=True,
        )
        return

    async def build() -> Screen:
        if channel is not None:
            target = guild.get_channel_or_thread(channel.id)
            me = guild.me
            if target is None or me is None:
                return message_screen("I cannot see that channel. Check my role can view it.", "Cannot post there", RED)
            permissions = target.permissions_for(me)
            missing = [
                name
                for name, ok in (
                    ("View Channel", permissions.view_channel),
                    ("Send Messages", permissions.send_messages_in_threads if isinstance(target, discord.Thread) else permissions.send_messages),
                    ("Embed Links", permissions.embed_links),
                )
                if not ok
            ]
            if missing:
                return message_screen(
                    f"I need {', '.join(missing)} in {target.mention} before I can post there.",
                    "Cannot post there",
                    RED,
                )
            await bot.db.set_guild_channel(guild.id, target.id, interaction.user.id)
        return await server_screen(bot, guild.id)

    await respond(interaction, build, ephemeral=True)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


async def settings_screen(bot: Any, user_id: int, confirm_delete: bool = False) -> Screen:
    user = await bot.db.ensure_user(user_id)
    if confirm_delete:
        return Screen(
            embeds=[
                embed(
                    "Delete everything?",
                    "This removes your favourites, alerts, reminders and settings. It cannot be undone.",
                    RED,
                )
            ],
            rows=[
                [
                    Button("Yes, delete it all", "st.delete", {"yes": True}, style=discord.ButtonStyle.danger),
                    Button("Keep my data", "st.show", {}, emoji="↩️"),
                ]
            ],
        )

    home = user["home_stop_name"]
    home_text = f"{home} ({operator_label(user['home_operator'])})" if home else "not set"
    body = embed(
        "⚙️ Settings",
        "Times are Malaysia time. Everything else shows in your own time zone.",
        GREY,
    )
    add_field(body, "Quiet hours", quiet_text(user), inline=True)
    add_field(body, "Morning digest", user["digest_time"], inline=True)
    add_field(body, "Reminder lead", f"{user['lead_minutes']} minutes", inline=True)
    add_field(body, "River alerts from", user["flood_threshold"].title(), inline=True)
    add_field(body, "Quakes from", f"M{user['quake_threshold']:.1f}", inline=True)
    add_field(body, "Prayer zone", user["prayer_zone"] or "not set", inline=True)
    add_field(
        body,
        "Home station",
        f"{home_text}. Set it from any station in {mention(bot, 'train station')}.",
    )

    current_quiet = "off" if not user["quiet_enabled"] else f"{user['quiet_from']}-{user['quiet_to']}"
    quiet = Select(
        f"Quiet hours: {quiet_text(user)}",
        [
            Option(label + (" ✓" if value == current_quiet else ""), "st.set", {"f": "quiet", "v": value}, description=desc)
            for value, label, desc in QUIET_PRESETS
        ],
    )
    digest = Select(
        f"Digest time: {user['digest_time']}",
        [
            Option(t + (" ✓" if t == user["digest_time"] else ""), "st.set", {"f": "digest_time", "v": t})
            for t in DIGEST_PRESETS
        ],
    )
    lead = Select(
        f"Remind me {user['lead_minutes']} minutes before",
        [
            Option(f"{m} minutes before" + (" ✓" if m == user["lead_minutes"] else ""), "st.set", {"f": "lead_minutes", "v": m})
            for m in LEAD_PRESETS
        ],
    )
    return Screen(
        embeds=[body],
        rows=[
            quiet,
            digest,
            lead,
            [
                Button(f"Rivers: {user['flood_threshold'].title()}", "st.cycle", {"f": "flood_threshold"}, emoji="🌊"),
                Button(f"Quakes: M{user['quake_threshold']:.1f}", "st.cycle", {"f": "quake_threshold"}, emoji="🌏"),
                Button("Delete my data", "st.delete", {}, style=discord.ButtonStyle.danger, emoji="🗑️"),
            ],
        ],
    )


@action("st.show", personal=True)
async def _settings_show(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await settings_screen(ctx.bot, ctx.user_id)


@action("st.set", personal=True)
async def _settings_set(ctx: Ctx, p: dict[str, Any]) -> Screen:
    field, value = str(p.get("f")), p.get("v")
    if field == "quiet":
        if value == "off":
            await ctx.db.set_pref(ctx.user_id, "quiet_enabled", 0)
        else:
            start, _, end = str(value).partition("-")
            await ctx.db.set_pref(ctx.user_id, "quiet_enabled", 1)
            await ctx.db.set_pref(ctx.user_id, "quiet_from", start)
            await ctx.db.set_pref(ctx.user_id, "quiet_to", end)
    elif field == "digest_time" and value in DIGEST_PRESETS:
        await ctx.db.set_pref(ctx.user_id, "digest_time", value)
        # The queued digest was for the old time.
        await ctx.db.delete_jobs(f"u:{ctx.user_id}", "digest")
        await ctx.bot.scheduler.plan_for(ctx.user_id)
    elif field == "lead_minutes" and value in LEAD_PRESETS:
        await ctx.db.set_pref(ctx.user_id, "lead_minutes", int(value))
        await ctx.db.delete_jobs(f"u:{ctx.user_id}", "departure")
        await ctx.bot.scheduler.plan_for(ctx.user_id)
    return await settings_screen(ctx.bot, ctx.user_id)


@action("st.cycle", personal=True)
async def _settings_cycle(ctx: Ctx, p: dict[str, Any]) -> Screen:
    user = await ctx.db.ensure_user(ctx.user_id)
    field = str(p.get("f"))
    if field == "flood_threshold":
        levels = FLOOD_LEVELS[1:]  # NORMAL is never worth an alert
        current = user["flood_threshold"]
        following = levels[(levels.index(current) + 1) % len(levels)] if current in levels else "WARNING"
        await ctx.db.set_pref(ctx.user_id, field, following)
    elif field == "quake_threshold":
        current = float(user["quake_threshold"])
        following = next((q for q in QUAKE_PRESETS if q > current), QUAKE_PRESETS[0])
        await ctx.db.set_pref(ctx.user_id, field, following)
    return await settings_screen(ctx.bot, ctx.user_id)


@action("st.delete", personal=True)
async def _settings_delete(ctx: Ctx, p: dict[str, Any]) -> Screen:
    if not p.get("yes"):
        return await settings_screen(ctx.bot, ctx.user_id, confirm_delete=True)
    await ctx.db.delete_user_data(ctx.user_id)
    return message_screen("Everything stored about you is gone.", "Deleted", GREY)


@app_commands.command(name="settings", description="Quiet hours, digest time, reminder lead and alert thresholds")
async def settings(interaction: discord.Interaction) -> None:
    bot: Any = interaction.client
    await respond(interaction, lambda: settings_screen(bot, interaction.user.id), ephemeral=True)


# ---------------------------------------------------------------------------
# Stats, for the app owner
# ---------------------------------------------------------------------------


@app_commands.command(name="stats", description="Usage figures and feed health. Only for the app owner")
async def stats(interaction: discord.Interaction) -> None:
    bot: Any = interaction.client
    if interaction.user.id not in bot.owner_ids:
        await interaction.response.send_message("This one is only for the app owner.", ephemeral=True)
        return

    async def build() -> Screen:
        counts = await bot.db.counts()
        body = embed("📊 Stats", "\n".join(f"**{v}** {k}" for k, v in counts.items()), BLUE)
        add_field(body, "Servers", str(len(bot.guilds)), inline=True)
        add_field(body, "Timetables loaded", ", ".join(operator_label(o) for o in bot.transit.loaded()) or "none yet", inline=True)
        health = []
        for row in await bot.db.feed_health():
            ok = row["last_ok_at"] or 0
            fail = row["last_fail_at"] or 0
            mark = "🟢" if ok >= fail else "🔴"
            when = ts(ok, "R") if ok else "never"
            health.append(f"{mark} {row['feed']}: last good {when}" + (f", failing: {row['last_error'][:80]}" if fail > ok else ""))
        if health:
            add_field(body, "Feeds", "\n".join(health))
        add_field(body, "Running since", ts(bot.started_at, "R"), inline=True)
        add_field(body, "Queued jobs", str(await bot.db.count_jobs()), inline=True)
        return Screen(embeds=[body])

    await respond(interaction, build, ephemeral=True)


__all__ = ["fav", "alerts", "settings", "stats", "ALERT_KINDS", "invite_url"]
