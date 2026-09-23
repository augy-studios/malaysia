"""/weather: forecasts, warnings, earthquakes and river levels."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from .. import config
from ..timeutils import format_day, parse_iso, ts
from ..ui import (
    AMBER,
    BLUE,
    GREEN,
    RED,
    SEVERITY_COLOURS,
    SEVERITY_EMOJI,
    TEAL,
    Button,
    Ctx,
    Link,
    Option,
    Screen,
    Select,
    action,
    add_field,
    embed,
    lines_block,
    message_screen,
    page_slice,
    pager,
)
from ..weather import (
    FLOOD_LEVELS,
    FLOOD_RANK,
    STATES,
    FloodStation,
    Location,
    Warning,
    normalise,
)
from .common import mention, respond, updated_footer

MET = "MET Malaysia via data.gov.my"
JPS = "JPS flood warning via data.gov.my"

group = app_commands.Group(
    name="weather", description="Forecasts, warnings, earthquakes and river levels across Malaysia"
)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


async def town_screen(bot: Any, user_id: int, key_or_name: str) -> Screen:
    location, candidates = await bot.weather.location(key_or_name)
    if location is None:
        return town_picker(key_or_name, candidates)

    snapshot = await bot.weather.forecast()
    warnings = [w for w in (await bot.weather.warnings()).warnings if w.mentions(location.name)]
    await bot.db.set_pref(user_id, "last_town", location.key)
    saved = await bot.db.is_favourite(user_id, "town", location.key)
    return render_town(location, warnings, saved, snapshot.fetched_at, snapshot.stale)


def render_town(
    location: Location, warnings: list[Warning], saved: bool, fetched_at: float, stale: bool
) -> Screen:
    today = location.today
    if today is None:
        body = embed(location.name, "MET has no forecast for this town right now.", BLUE)
    else:
        umbrella = (
            "☂️ Take an umbrella today."
            if today.is_wet
            else "No umbrella needed today."
        )
        body = embed(
            f"{today.emoji} {location.name}",
            f"**{today.headline}**\n{today.temp_range}\n{umbrella}",
            RED if warnings else BLUE,
            footer=updated_footer(MET, fetched_at, stale),
            url=config.WEATHER_PAGE,
        )
        add_field(body, "Morning", today.morning or "No data", inline=True)
        add_field(body, "Afternoon", today.afternoon or "No data", inline=True)
        add_field(body, "Night", today.night or "No data", inline=True)
        week = [
            f"**{format_day(day.date)}** {day.emoji} {day.headline}"
            + (f", {day.temp_range}" if day.temp_range else "")
            for day in location.days[1:]
        ]
        if week:
            add_field(body, "The week ahead", "\n".join(week))
    if warnings:
        add_field(
            body,
            "⚠️ Warnings naming this area",
            "\n".join(f"{SEVERITY_EMOJI[w.severity]} {w.title}" for w in warnings[:5]),
        )

    return Screen(
        embeds=[body],
        rows=[
            [
                Button(
                    "Saved" if saved else "Save",
                    "wx.fav_town",
                    {"k": location.key, "on": not saved},
                    style=discord.ButtonStyle.success if saved else discord.ButtonStyle.secondary,
                    emoji="⭐",
                ),
                Button("Warnings", "wx.warnings", {"p": 0}, emoji="⚠️"),
                Button("Refresh", "wx.town", {"k": location.key}, emoji="🔄"),
                Link("Open on the web", config.WEATHER_PAGE),
            ]
        ],
    )


def town_picker(query: str, candidates: list[Location]) -> Screen:
    if not candidates:
        return message_screen(
            f"No forecast town matches “{query}”. Try a nearby town or district name.",
            "Nothing found",
        )
    return Screen(
        embeds=[
            embed(
                "Which town?",
                f"{len(candidates)} towns match “{query}”. Pick one below.",
                BLUE,
            )
        ],
        rows=[
            Select(
                "Choose a town",
                [Option(loc.name, "wx.town", {"k": loc.key}, emoji="🌤️") for loc in candidates],
            )
        ],
    )


async def warnings_screen(bot: Any, page: int = 0) -> Screen:
    snapshot = await bot.weather.warnings()
    footer = updated_footer(MET, snapshot.fetched_at, snapshot.stale)
    refresh = Button("Refresh", "wx.warnings", {"p": 0}, emoji="🔄")
    if not snapshot.warnings:
        return Screen(
            embeds=[
                embed(
                    "✅ No weather warnings in force",
                    "MET Malaysia has no active warnings right now.",
                    GREEN,
                    footer=footer,
                )
            ],
            rows=[[refresh, Link("Open on the web", config.WEATHER_PAGE)]],
        )

    per_page = 4
    items, page = page_slice(snapshot.warnings, page, per_page)
    worst = "danger" if any(w.severity == "danger" for w in snapshot.warnings) else "warning"
    body = embed(
        f"⚠️ {len(snapshot.warnings)} weather warning{'s' if len(snapshot.warnings) != 1 else ''} in force",
        None,
        SEVERITY_COLOURS[worst],
        footer=footer,
        url=config.WEATHER_PAGE,
    )
    for warning in items:
        add_field(body, f"{SEVERITY_EMOJI[warning.severity]} {warning.title}", warning_text(warning))
    return Screen(
        embeds=[body],
        rows=[
            pager("wx.warnings", {}, page, len(snapshot.warnings), per_page),
            [refresh, Link("Open on the web", config.WEATHER_PAGE)],
        ],
    )


def warning_text(warning: Warning) -> str:
    valid = ""
    start, end = parse_iso(warning.valid_from), parse_iso(warning.valid_to)
    if start and end:
        valid = f"**Valid** {ts(start.timestamp(), 'f')} to {ts(end.timestamp(), 'f')}\n"
    elif end:
        valid = f"**Until** {ts(end.timestamp(), 'f')}\n"
    text = warning.text
    if warning.instruction:
        text += f"\n*{warning.instruction}*"
    return valid + text


async def quakes_screen(bot: Any, page: int = 0) -> Screen:
    snapshot = await bot.weather.quakes()
    footer = updated_footer(MET, snapshot.fetched_at, snapshot.stale)
    per_page = 10
    items, page = page_slice(snapshot.quakes, page, per_page)
    lines = []
    for quake in items:
        moment = parse_iso(quake.when)
        when = ts(moment.timestamp(), "R") if moment else "unknown time"
        place = f"[{quake.location}]({quake.maps_url})" if quake.maps_url else quake.location
        extra = []
        if quake.depth is not None:
            extra.append(f"{quake.depth:g} km deep")
        if quake.distance:
            extra.append(quake.distance)
        lines.append(
            f"{SEVERITY_EMOJI[quake.severity]} **{quake.magnitude_text}** {place}, {when}"
            + (f"\n  {', '.join(extra)}" if extra else "")
        )
    body = embed(
        "🌏 Recent earthquakes",
        lines_block(lines) if lines else "No earthquake bulletins in the feed right now.",
        AMBER,
        footer=footer,
        url=config.QUAKE_PAGE,
    )
    return Screen(
        embeds=[body],
        rows=[
            pager("wx.quakes", {}, page, len(snapshot.quakes), per_page),
            [
                Button("Refresh", "wx.quakes", {"p": 0}, emoji="🔄"),
                Link("Open on the web", config.QUAKE_PAGE),
            ],
        ],
    )


async def flood_screen(bot: Any, state: str = "", page: int = 0) -> Screen:
    snapshot = await bot.weather.flood()
    stations = [st for st in snapshot.stations if st.in_state(state)]
    footer = updated_footer(JPS, snapshot.fetched_at, snapshot.stale)
    counts = {level: 0 for level in FLOOD_LEVELS}
    silent = 0
    for station in stations:
        if not station.is_current:
            silent += 1
        elif station.indicator in counts:
            counts[station.indicator] += 1
    summary = "  ".join(
        f"{SEVERITY_EMOJI[level.lower()]} {counts[level]} {level.lower()}"
        for level in reversed(FLOOD_LEVELS)
    )
    if silent:
        summary += f"\n{silent} gauges have not reported in the last two days and are left out."

    elevated = sorted(
        (st for st in stations if st.is_elevated), key=lambda st: (-st.rank, st.state, st.name)
    )
    per_page = 15
    items, page = page_slice(elevated, page, per_page)
    lines = [gauge_line(st) for st in items]
    worst = elevated[0].severity if elevated else "normal"
    body = embed(
        f"🌊 River levels{f' in {state}' if state else ''}",
        summary
        + "\n\n"
        + (
            lines_block(lines)
            if lines
            else "Every gauge is at normal level. Nothing is at alert or above."
        ),
        SEVERITY_COLOURS.get(worst, TEAL),
        footer=footer,
        url=config.FLOOD_PAGE,
    )
    rows: list[Any] = []
    if items:
        rows.append(
            Select(
                "Open a gauge",
                [
                    Option(
                        st.name,
                        "wx.gauge",
                        {"id": st.station_id},
                        description=f"{st.level_text}, {st.severity}, {st.place}",
                        emoji=SEVERITY_EMOJI[st.severity],
                    )
                    for st in items
                ],
            )
        )
    rows.append(pager("wx.flood", {"s": state}, page, len(elevated), per_page))
    rows.append(
        [
            Button("Refresh", "wx.flood", {"s": state, "p": 0}, emoji="🔄"),
            Link("Open on the web", config.FLOOD_PAGE),
        ]
    )
    return Screen(embeds=[body], rows=rows)


def gauge_line(station: FloodStation) -> str:
    trend = f", {station.trend_text}" if station.trend_text else ""
    return (
        f"{SEVERITY_EMOJI[station.severity]} **{station.name}** "
        f"{station.level_text}{trend}, {station.place}"
    )


async def gauge_screen(bot: Any, user_id: int, id_or_name: str) -> Screen:
    station, candidates = await bot.weather.station(id_or_name)
    if station is None:
        if not candidates:
            return message_screen(
                f"No river gauge matches “{id_or_name}”. Try a river, town or district name.",
                "Nothing found",
            )
        return Screen(
            embeds=[embed("Which gauge?", f"{len(candidates)} gauges match. Pick one below.", TEAL)],
            rows=[
                Select(
                    "Choose a gauge",
                    [
                        Option(
                            st.name,
                            "wx.gauge",
                            {"id": st.station_id},
                            description=f"{st.place}, {st.severity}",
                            emoji=SEVERITY_EMOJI[st.severity],
                        )
                        for st in candidates
                    ],
                )
            ],
        )

    snapshot = await bot.weather.flood()
    saved = await bot.db.is_favourite(user_id, "gauge", station.station_id)
    thresholds = []
    for label, value in (
        ("Normal", station.normal_level),
        ("Alert", station.alert_level),
        ("Warning", station.warning_level),
        ("Danger", station.danger_level),
    ):
        if value is not None:
            marker = " ◀ now" if station.severity == label.lower() else ""
            thresholds.append(f"{SEVERITY_EMOJI[label.lower()]} {label}: {value:g} m{marker}")

    reading = parse_iso(station.updated_at)
    if station.is_current:
        status = (
            f"**{station.level_text}**, {station.severity}"
            + (f" and {station.trend_text}" if station.trend_text else "")
            + f"\n{station.place}"
            + (f"\nRead {ts(reading.timestamp(), 'R')}" if reading else "")
        )
    else:
        status = (
            "**No recent reading.** This gauge has not reported in over two days, so its "
            "level is not shown as current."
            + (f"\nLast reading {ts(reading.timestamp(), 'R')}: {station.level_text}" if reading else "")
            + f"\n{station.place}"
        )
    body = embed(
        f"{SEVERITY_EMOJI[station.severity]} {station.name}",
        status,
        SEVERITY_COLOURS.get(station.severity, TEAL),
        footer=updated_footer(JPS, snapshot.fetched_at, snapshot.stale),
        url=config.FLOOD_PAGE,
    )
    if thresholds:
        add_field(body, "Thresholds", "\n".join(thresholds))
    basin = ", ".join(b for b in (station.main_basin, station.sub_basin) if b)
    if basin:
        add_field(body, "River basin", basin)

    row: list[Any] = [
        Button(
            "Saved" if saved else "Save",
            "wx.fav_gauge",
            {"id": station.station_id, "on": not saved},
            style=discord.ButtonStyle.success if saved else discord.ButtonStyle.secondary,
            emoji="⭐",
        ),
        Button("Refresh", "wx.gauge", {"id": station.station_id}, emoji="🔄"),
        Button("All rivers", "wx.flood", {"s": "", "p": 0}, emoji="🌊"),
    ]
    if station.maps_url:
        row.append(Link("Map", station.maps_url, emoji="📍"))
    return Screen(embeds=[body], rows=[row])


# ---------------------------------------------------------------------------
# Button actions
# ---------------------------------------------------------------------------


@action("wx.town")
async def _town(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await town_screen(ctx.bot, ctx.user_id, str(p.get("k", "")))


@action("wx.fav_town")
async def _fav_town(ctx: Ctx, p: dict[str, Any]) -> Screen:
    key = str(p.get("k", ""))
    location, _ = await ctx.bot.weather.location(key)
    if location is None:
        return message_screen("That town is no longer in the forecast feed.")
    if p.get("on"):
        await ctx.db.add_favourite(ctx.user_id, "town", location.key, location.name)
        toast = f"Saved {location.name}. Weather warnings and the morning digest follow your saved towns, see {mention(ctx.bot, 'alerts on')}."
    else:
        await ctx.db.remove_favourite_ref(ctx.user_id, "town", location.key)
        toast = f"Removed {location.name} from your favourites."
    screen = await town_screen(ctx.bot, ctx.user_id, location.key)
    screen.toast = toast
    return screen


@action("wx.warnings")
async def _warnings(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await warnings_screen(ctx.bot, int(p.get("p", 0)))


@action("wx.quakes")
async def _quakes(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await quakes_screen(ctx.bot, int(p.get("p", 0)))


@action("wx.flood")
async def _flood(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await flood_screen(ctx.bot, str(p.get("s", "")), int(p.get("p", 0)))


@action("wx.gauge")
async def _gauge(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await gauge_screen(ctx.bot, ctx.user_id, str(p.get("id", "")))


@action("wx.fav_gauge")
async def _fav_gauge(ctx: Ctx, p: dict[str, Any]) -> Screen:
    station_id = str(p.get("id", ""))
    station, _ = await ctx.bot.weather.station(station_id)
    if station is None:
        return message_screen("That gauge is no longer in the flood feed.")
    if p.get("on"):
        await ctx.db.add_favourite(ctx.user_id, "gauge", station.station_id, station.name)
        toast = f"Saved {station.name}. River level alerts follow your saved gauges, see {mention(ctx.bot, 'alerts on')}."
    else:
        await ctx.db.remove_favourite_ref(ctx.user_id, "gauge", station.station_id)
        toast = f"Removed {station.name} from your favourites."
    screen = await gauge_screen(ctx.bot, ctx.user_id, station.station_id)
    screen.toast = toast
    return screen


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def town_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    bot: Any = interaction.client
    return [
        app_commands.Choice(name=loc.name[:100], value=loc.key[:100])
        for loc in bot.weather.peek_locations(current)
    ]


async def gauge_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    bot: Any = interaction.client
    return [
        app_commands.Choice(
            name=f"{st.name} ({st.place}, {st.severity})"[:100], value=st.station_id[:100]
        )
        for st in bot.weather.peek_stations(current)
    ]


@group.command(name="forecast", description="Seven day forecast for a town or district")
@app_commands.describe(town="Town or district. Leave empty for the last one you looked up")
@app_commands.autocomplete(town=town_autocomplete)
async def forecast(interaction: discord.Interaction, town: str | None = None) -> None:
    bot: Any = interaction.client

    async def build() -> Screen:
        target = town
        if not target:
            user = await bot.db.ensure_user(interaction.user.id)
            target = user["last_town"]
            if not target:
                saved = await bot.db.list_favourites(interaction.user.id, "town")
                target = saved[0]["ref_id"] if saved else ""
        if not target:
            return message_screen(
                f"Tell me which town, for example {mention(bot, 'weather forecast')} and pick "
                "Kuala Lumpur from the list.",
                "Which town?",
            )
        return await town_screen(bot, interaction.user.id, target)

    await respond(interaction, build)


@group.command(name="warnings", description="MET weather warnings currently in force")
async def warnings(interaction: discord.Interaction) -> None:
    bot: Any = interaction.client
    await respond(interaction, lambda: warnings_screen(bot))


@group.command(name="quake", description="Recent earthquakes in and around Malaysia, newest first")
async def quake(interaction: discord.Interaction) -> None:
    bot: Any = interaction.client
    await respond(interaction, lambda: quakes_screen(bot))


@group.command(name="flood", description="River levels: gauges at alert or above, or one gauge in detail")
@app_commands.describe(
    river="A gauge, river or town. Leave empty for every gauge at alert or above",
    state="Only show gauges in this state",
)
@app_commands.autocomplete(river=gauge_autocomplete)
@app_commands.choices(state=[app_commands.Choice(name=s, value=s) for s in STATES])
async def flood(
    interaction: discord.Interaction,
    river: str | None = None,
    state: app_commands.Choice[str] | None = None,
) -> None:
    bot: Any = interaction.client
    if river:
        await respond(interaction, lambda: gauge_screen(bot, interaction.user.id, river))
    else:
        await respond(interaction, lambda: flood_screen(bot, state.value if state else ""))


__all__ = ["group", "render_town", "warning_text", "gauge_line", "FLOOD_RANK", "normalise"]
