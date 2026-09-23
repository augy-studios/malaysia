"""/train and /bus: timetables, lines, following one trip, and live positions.

Both groups share one implementation. Autocomplete values carry the operator
and the id together ("ktmb|12345") so a station name that exists on two
networks still resolves to the one the person picked.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import discord
from discord import app_commands

from .. import config
from ..timeutils import format_relative, format_wait, ts
from ..transit import BUS, OPERATORS, RAIL, Feed, Route, Stop, haversine_m, operator_label
from ..ui import (
    BLUE,
    GREEN,
    PURPLE,
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
from .common import loading_screen, mention, respond

train = app_commands.Group(name="train", description="LRT, MRT, Monorail, KTM Komuter, ETS and intercity rail")
bus = app_commands.Group(name="bus", description="Rapid Bus KL, Penang, MRT Feeder and myBAS Johor")

SOURCE = "Prasarana and KTMB GTFS via data.gov.my"


def page_url(operator: str) -> str:
    return config.TRAINS_PAGE if OPERATORS[operator].mode == "rail" else config.BUS_PAGE


def fav_kind(operator: str) -> str:
    return "station" if OPERATORS[operator].mode == "rail" else "stop"


def mode_icon(operator: str) -> str:
    return "🚆" if OPERATORS[operator].mode == "rail" else "🚌"


def split_ref(value: str) -> tuple[str, str]:
    operator, _, ref = (value or "").partition("|")
    return operator, ref


async def feed_or_none(bot: Any, operator: str) -> Feed | None:
    if operator not in OPERATORS:
        return None
    return await bot.transit.feed(operator)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


def departure_line(feed: Feed, dep: Any, show_route: bool = True) -> str:
    route = feed.routes.get(dep.route_id)
    label = route.short_name or route.display if route else dep.route_id
    wait = format_wait(dep.epoch - time.time())
    head = f"**{label}** to {dep.headsign}" if show_route else f"to {dep.headsign}"
    return f"{ts(dep.epoch)} {head} ({wait})"


async def stop_screen(bot: Any, user_id: int, operator: str, stop_id: str) -> Screen:
    feed = await feed_or_none(bot, operator)
    if feed is None:
        return loading_screen(operator_label(operator))
    stop = feed.stops.get(stop_id)
    if stop is None:
        return message_screen("That stop is no longer in the timetable.", "Not found")

    routes = feed.routes_at(stop_id)
    body = embed(
        f"{mode_icon(operator)} {stop.name}",
        f"{operator_label(operator)}, {len(routes)} {'line' if feed.mode == 'rail' else 'route'}"
        f"{'s' if len(routes) != 1 else ''} stop here. Times show in your own time zone.",
        BLUE if feed.mode == "rail" else GREEN,
        footer=f"{SOURCE} · scheduled times, not live predictions",
        url=page_url(operator),
    )
    shown = 0
    for route in routes:
        if shown >= 10:
            add_field(body, "More", f"{len(routes) - shown} more routes serve this stop.")
            break
        deps = feed.departures(stop_id, route.route_id, limit=4)
        value = (
            "\n".join(departure_line(feed, d, show_route=False) for d in deps)
            if deps
            else "No more departures in the next 24 hours."
        )
        add_field(body, route.display, value)
        shown += 1

    saved = await bot.db.is_favourite(user_id, fav_kind(operator), stop_id, operator)
    user = await bot.db.ensure_user(user_id)
    is_home = user["home_operator"] == operator and user["home_stop_id"] == stop_id

    actions: list[Any] = [
        Button(
            "Saved" if saved else "Save",
            "tr.fav",
            {"o": operator, "s": stop_id, "on": not saved},
            style=discord.ButtonStyle.success if saved else discord.ButtonStyle.secondary,
            emoji="⭐",
        ),
    ]
    if feed.mode == "rail":
        actions.append(
            Button(
                "Home station" if is_home else "Set as home",
                "tr.home",
                {"o": operator, "s": stop_id},
                style=discord.ButtonStyle.success if is_home else discord.ButtonStyle.secondary,
                emoji="🏠",
                disabled=is_home,
            )
        )
    actions.append(Button("Refresh", "tr.stop", {"o": operator, "s": stop_id}, emoji="🔄"))
    if stop.maps_url:
        actions.append(Link("Map", stop.maps_url, emoji="📍"))
    actions.append(Link("Open on the web", page_url(operator)))

    rows: list[Any] = [actions]
    if routes:
        rows.insert(
            0,
            Select(
                "Open a line" if feed.mode == "rail" else "Open a route",
                [
                    Option(r.display, "tr.route", {"o": operator, "r": r.route_id, "p": 0})
                    for r in routes[:25]
                ],
            ),
        )
    return Screen(embeds=[body], rows=rows)


async def next_screen(bot: Any, user_id: int, operator: str, stop_id: str) -> Screen:
    """The next departures from one station, all lines together."""

    feed = await feed_or_none(bot, operator)
    if feed is None:
        return loading_screen(operator_label(operator))
    stop = feed.stops.get(stop_id)
    if stop is None:
        return message_screen("That station is no longer in the timetable.", "Not found")

    deps = feed.departures(stop_id, limit=15)
    body = embed(
        f"{mode_icon(operator)} Next from {stop.name}",
        lines_block([departure_line(feed, d) for d in deps])
        if deps
        else "Nothing leaves here in the next 24 hours.",
        BLUE,
        footer=f"{operator_label(operator)} · {SOURCE}",
        url=page_url(operator),
    )
    row: list[Any] = [
        Button("Refresh", "tr.next", {"o": operator, "s": stop_id}, emoji="🔄"),
        Button("Station", "tr.stop", {"o": operator, "s": stop_id}, emoji="🚉"),
    ]
    if OPERATORS[operator].has_live:
        row.append(Button("Live", "tr.live", {"o": operator, "p": 0}, emoji="📡"))
    return Screen(embeds=[body], rows=[row])


async def route_screen(bot: Any, operator: str, route_id: str, page: int = 0) -> Screen:
    feed = await feed_or_none(bot, operator)
    if feed is None:
        return loading_screen(operator_label(operator))
    route = feed.routes.get(route_id)
    if route is None:
        return message_screen("That line is no longer in the timetable.", "Not found")

    stops = feed.route_stops(route_id)
    per_page = 25
    items, page = page_slice(stops, page, per_page)
    start = page * per_page
    lines = [f"`{start + i + 1:>2}` {stop.name}" for i, stop in enumerate(items)]
    body = embed(
        f"{mode_icon(operator)} {route.display}",
        f"{operator_label(operator)}, {len(stops)} stops.\n\n" + lines_block(lines),
        route.colour_int or PURPLE,
        footer=SOURCE,
        url=page_url(operator),
    )
    rows: list[Any] = []
    if items:
        rows.append(
            Select(
                "Open a stop",
                [Option(s.name, "tr.stop", {"o": operator, "s": s.stop_id}) for s in items],
            )
        )
    rows.append(pager("tr.route", {"o": operator, "r": route_id}, page, len(stops), per_page))
    row: list[Any] = [
        Button(
            "Follow a train" if feed.mode == "rail" else "Follow a bus",
            "tr.runs",
            {"o": operator, "r": route_id},
            emoji="🧭",
        )
    ]
    if OPERATORS[operator].has_live:
        row.append(Button("Live", "tr.live", {"o": operator, "p": 0, "r": route_id}, emoji="📡"))
    row.append(Link("Open on the web", page_url(operator)))
    rows.append(row)
    return Screen(embeds=[body], rows=rows)


async def runs_screen(bot: Any, operator: str, route_id: str) -> Screen:
    feed = await feed_or_none(bot, operator)
    if feed is None:
        return loading_screen(operator_label(operator))
    route = feed.routes.get(route_id)
    if route is None:
        return message_screen("That line is no longer in the timetable.", "Not found")

    runs = feed.upcoming_runs(route_id, limit=25)
    if not runs:
        return message_screen(
            f"No trips on {route.display} in the next 24 hours.", "Nothing running"
        )
    first_stops = {}
    for run in runs:
        stops = feed.trip_stops.get(run.trip_id) or []
        first = feed.stops.get(stops[0][0]) if stops else None
        first_stops[id(run)] = first.name if first else "the first stop"

    body = embed(
        f"🧭 {route.display}",
        "Pick a trip to see every stop it calls at, with times.\n\n"
        + lines_block(
            [
                f"{ts(run.epoch)} from {first_stops[id(run)]} to **{run.headsign}**"
                for run in runs
            ]
        ),
        route.colour_int or PURPLE,
        footer=SOURCE,
    )
    return Screen(
        embeds=[body],
        rows=[
            Select(
                "Choose a trip",
                [
                    Option(
                        f"to {run.headsign}"[:100],
                        "tr.trip",
                        {
                            "o": operator,
                            "t": run.trip_id,
                            "f": run.offset,
                            "d": run.service_day.isoformat(),
                        },
                        description=f"Leaves {first_stops[id(run)]} "
                        + time.strftime("%H:%M", time.gmtime(run.epoch + 8 * 3600))
                        + " Malaysia time",
                    )
                    for run in runs
                ],
            ),
            [Button("Back to the line", "tr.route", {"o": operator, "r": route_id, "p": 0}, emoji="↩️")],
        ],
    )


async def trip_screen(bot: Any, operator: str, trip_id: str, offset: int, day: str) -> Screen:
    feed = await feed_or_none(bot, operator)
    if feed is None:
        return loading_screen(operator_label(operator))
    trip = feed.trips.get(trip_id)
    if trip is None:
        return message_screen("That trip is no longer in the timetable.", "Not found")
    try:
        service_day = date.fromisoformat(day)
    except ValueError:
        return message_screen("That trip has expired. Pick a fresh one.", "Expired")

    calls = feed.trip_calls(trip_id, offset, service_day)
    now = time.time()
    lines = []
    for stop, epoch in calls:
        marker = "✅" if epoch < now - 30 else "⬜"
        lines.append(f"{marker} {ts(epoch)} {stop.name}")
    route = feed.routes.get(trip.route_id)
    body = embed(
        f"🧭 {route.display if route else trip.route_id} to {feed.headsign(trip)}",
        lines_block(lines, limit=4000),
        (route.colour_int if route else None) or PURPLE,
        footer=f"{operator_label(operator)} · scheduled times · ✅ already passed",
    )
    return Screen(
        embeds=[body],
        rows=[
            [
                Button("Refresh", "tr.trip", {"o": operator, "t": trip_id, "f": offset, "d": day}, emoji="🔄"),
                Button("Other trips", "tr.runs", {"o": operator, "r": trip.route_id}, emoji="🧭"),
            ]
        ],
    )


async def live_screen(bot: Any, operator: str, page: int = 0, route_id: str = "") -> Screen:
    op = OPERATORS.get(operator)
    if op is None or not op.has_live:
        return message_screen(
            f"{operator_label(operator)} does not broadcast vehicle positions, so only its "
            "timetable is available.",
            "No live data",
        )
    vehicles = [v for v in await bot.transit.vehicles(operator) if v.has_position]
    feed = bot.transit.peek(operator)
    if route_id:
        vehicles = [v for v in vehicles if v.route_id == route_id]

    per_page = 12
    items, page = page_slice(vehicles, page, per_page)
    now = time.time()
    lines = []
    for vehicle in items:
        route = feed.routes.get(vehicle.route_id) if feed else None
        name = route.display if route else (vehicle.route_id or "Unassigned")
        speed = vehicle.speed_kmh(operator)
        bits = [f"[{vehicle.name}]({vehicle.maps_url})", name]
        if speed:
            bits.append(f"{speed:.0f} km/h")
        if vehicle.timestamp:
            bits.append(format_relative(now - vehicle.timestamp))
        lines.append(", ".join(bits))

    title_route = ""
    if route_id and feed and route_id in feed.routes:
        title_route = f" on {feed.routes[route_id].display}"
    body = embed(
        f"📡 {len(vehicles)} {operator_label(operator)} {'trains' if op.mode == 'rail' else 'buses'} reporting{title_route}",
        lines_block(lines) if lines else "Nothing is reporting a position right now.",
        GREEN,
        footer="Live GTFS positions via data.gov.my · a vehicle not reporting is not necessarily missing",
        url=page_url(operator),
    )
    return Screen(
        embeds=[body],
        rows=[
            pager("tr.live", {"o": operator, "r": route_id}, page, len(vehicles), per_page),
            [
                Button("Refresh", "tr.live", {"o": operator, "p": page, "r": route_id}, emoji="🔄"),
                Link("Open on the web", page_url(operator)),
            ],
        ],
    )


def stop_picker(operator_stops: list[tuple[str, Stop]], query: str) -> Screen:
    if not operator_stops:
        return message_screen(
            f"Nothing matches “{query}”. Try part of the name, such as “sentral”.",
            "Nothing found",
        )
    return Screen(
        embeds=[embed("Which one?", f"{len(operator_stops)} places match “{query}”.", BLUE)],
        rows=[
            Select(
                "Choose one",
                [
                    Option(
                        stop.name,
                        "tr.stop",
                        {"o": op, "s": stop.stop_id},
                        description=operator_label(op),
                        emoji=mode_icon(op),
                    )
                    for op, stop in operator_stops[:25]
                ],
            )
        ],
    )


def route_picker(operator_routes: list[tuple[str, Route]], query: str) -> Screen:
    if not operator_routes:
        return message_screen(f"No line or route matches “{query}”.", "Nothing found")
    return Screen(
        embeds=[embed("Which one?", f"{len(operator_routes)} match “{query}”.", BLUE)],
        rows=[
            Select(
                "Choose one",
                [
                    Option(
                        route.display,
                        "tr.route",
                        {"o": op, "r": route.route_id, "p": 0},
                        description=operator_label(op),
                    )
                    for op, route in operator_routes[:25]
                ],
            )
        ],
    )


# ---------------------------------------------------------------------------
# Resolving what was typed
# ---------------------------------------------------------------------------


async def resolve_stop(bot: Any, operators: tuple[str, ...], value: str) -> tuple[str, str] | list[tuple[str, Stop]]:
    """An (operator, stop id) pair, or the candidates for free text."""

    operator, ref = split_ref(value)
    if operator in operators and ref:
        feed = await bot.transit.feed(operator)
        if ref in feed.stops:
            return operator, ref
    for op in operators:
        await bot.transit.feed(op)
    matches = bot.transit.peek_stops(operators, value)
    if len(matches) == 1:
        return matches[0][0], matches[0][1].stop_id
    return matches


async def resolve_route(bot: Any, operators: tuple[str, ...], value: str) -> tuple[str, str] | list[tuple[str, Route]]:
    operator, ref = split_ref(value)
    if operator in operators and ref:
        feed = await bot.transit.feed(operator)
        if ref in feed.routes:
            return operator, ref
    for op in operators:
        await bot.transit.feed(op)
    matches = bot.transit.peek_routes(operators, value)
    if len(matches) == 1:
        return matches[0][0], matches[0][1].route_id
    return matches


def stop_autocomplete(operators: tuple[str, ...]):
    async def complete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        bot: Any = interaction.client
        if not current.strip():
            return []
        return [
            app_commands.Choice(
                name=f"{stop.name} ({operator_label(op)})"[:100],
                value=f"{op}|{stop.stop_id}"[:100],
            )
            for op, stop in bot.transit.peek_stops(operators, current)
        ]

    return complete


def route_autocomplete(operators: tuple[str, ...]):
    async def complete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        bot: Any = interaction.client
        return [
            app_commands.Choice(
                name=f"{route.display} ({operator_label(op)})"[:100],
                value=f"{op}|{route.route_id}"[:100],
            )
            for op, route in bot.transit.peek_routes(operators, current)
        ]

    return complete


# ---------------------------------------------------------------------------
# Button actions
# ---------------------------------------------------------------------------


@action("tr.stop")
async def _stop(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await stop_screen(ctx.bot, ctx.user_id, str(p.get("o")), str(p.get("s")))


@action("tr.next")
async def _next(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await next_screen(ctx.bot, ctx.user_id, str(p.get("o")), str(p.get("s")))


@action("tr.route")
async def _route(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await route_screen(ctx.bot, str(p.get("o")), str(p.get("r")), int(p.get("p", 0)))


@action("tr.runs")
async def _runs(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await runs_screen(ctx.bot, str(p.get("o")), str(p.get("r")))


@action("tr.trip")
async def _trip(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await trip_screen(
        ctx.bot, str(p.get("o")), str(p.get("t")), int(p.get("f", 0)), str(p.get("d", ""))
    )


@action("tr.live")
async def _live(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await live_screen(ctx.bot, str(p.get("o")), int(p.get("p", 0)), str(p.get("r", "")))


@action("tr.fav")
async def _fav(ctx: Ctx, p: dict[str, Any]) -> Screen:
    operator, stop_id = str(p.get("o")), str(p.get("s"))
    feed = await feed_or_none(ctx.bot, operator)
    stop = feed.stops.get(stop_id) if feed else None
    if stop is None:
        return message_screen("That stop is no longer in the timetable.", "Not found")
    if p.get("on"):
        await ctx.db.add_favourite(ctx.user_id, fav_kind(operator), stop_id, stop.name, operator)
        toast = (
            f"Saved {stop.name}. Departure reminders and live alerts follow your saved stops, "
            f"see {mention(ctx.bot, 'alerts on')}."
        )
    else:
        await ctx.db.remove_favourite_ref(ctx.user_id, fav_kind(operator), stop_id, operator)
        toast = f"Removed {stop.name} from your favourites."
    screen = await stop_screen(ctx.bot, ctx.user_id, operator, stop_id)
    screen.toast = toast
    return screen


@action("tr.home")
async def _home(ctx: Ctx, p: dict[str, Any]) -> Screen:
    operator, stop_id = str(p.get("o")), str(p.get("s"))
    feed = await feed_or_none(ctx.bot, operator)
    stop = feed.stops.get(stop_id) if feed else None
    if stop is None:
        return message_screen("That station is no longer in the timetable.", "Not found")
    await ctx.db.set_pref(ctx.user_id, "home_operator", operator)
    await ctx.db.set_pref(ctx.user_id, "home_stop_id", stop_id)
    await ctx.db.set_pref(ctx.user_id, "home_stop_name", stop.name)
    screen = await stop_screen(ctx.bot, ctx.user_id, operator, stop_id)
    screen.toast = f"{stop.name} is now your home station. {mention(ctx.bot, 'train next')} goes straight there."
    return screen


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def _stop_command(interaction: discord.Interaction, operators: tuple[str, ...], value: str) -> None:
    bot: Any = interaction.client

    async def build() -> Screen:
        resolved = await resolve_stop(bot, operators, value)
        if isinstance(resolved, list):
            return stop_picker(resolved, value)
        return await stop_screen(bot, interaction.user.id, *resolved)

    await respond(interaction, build)


async def _route_command(interaction: discord.Interaction, operators: tuple[str, ...], value: str, runs: bool) -> None:
    bot: Any = interaction.client

    async def build() -> Screen:
        resolved = await resolve_route(bot, operators, value)
        if isinstance(resolved, list):
            return route_picker(resolved, value)
        if runs:
            return await runs_screen(bot, *resolved)
        return await route_screen(bot, *resolved)

    await respond(interaction, build)


@train.command(name="next", description="Next departures from your home station, or any station")
@app_commands.describe(station="Leave empty for your home station")
@app_commands.autocomplete(station=stop_autocomplete(RAIL))
async def train_next(interaction: discord.Interaction, station: str | None = None) -> None:
    bot: Any = interaction.client

    async def build() -> Screen:
        if station:
            resolved = await resolve_stop(bot, RAIL, station)
            if isinstance(resolved, list):
                return stop_picker(resolved, station)
            return await next_screen(bot, interaction.user.id, *resolved)
        user = await bot.db.ensure_user(interaction.user.id)
        if not user["home_stop_id"]:
            return message_screen(
                f"You have no home station yet. Open one with {mention(bot, 'train station')} "
                "and press Set as home.",
                "No home station",
            )
        return await next_screen(bot, interaction.user.id, user["home_operator"], user["home_stop_id"])

    await respond(interaction, build)


@train.command(name="station", description="A station's timetable, line by line")
@app_commands.describe(station="Station name, such as KL Sentral or Kajang")
@app_commands.autocomplete(station=stop_autocomplete(RAIL))
async def train_station(interaction: discord.Interaction, station: str) -> None:
    await _stop_command(interaction, RAIL, station)


@train.command(name="line", description="Every station on a line, in order")
@app_commands.describe(line="Line name, such as Kelana Jaya or Seremban")
@app_commands.autocomplete(line=route_autocomplete(RAIL))
async def train_line(interaction: discord.Interaction, line: str) -> None:
    await _route_command(interaction, RAIL, line, runs=False)


@train.command(name="trip", description="Follow one train stop by stop")
@app_commands.describe(line="The line the train runs on")
@app_commands.autocomplete(line=route_autocomplete(RAIL))
async def train_trip(interaction: discord.Interaction, line: str) -> None:
    await _route_command(interaction, RAIL, line, runs=True)


@train.command(name="live", description="Live KTMB train positions with map links")
async def train_live(interaction: discord.Interaction) -> None:
    bot: Any = interaction.client
    await respond(interaction, lambda: live_screen(bot, "ktmb"))


@bus.command(name="stop", description="A bus stop's next departures, route by route")
@app_commands.describe(stop="Stop name, such as Pasar Seni or Komtar")
@app_commands.autocomplete(stop=stop_autocomplete(BUS))
async def bus_stop(interaction: discord.Interaction, stop: str) -> None:
    await _stop_command(interaction, BUS, stop)


@bus.command(name="route", description="Every stop on a bus route, in order")
@app_commands.describe(route="Route number or name, such as 780 or T789")
@app_commands.autocomplete(route=route_autocomplete(BUS))
async def bus_route(interaction: discord.Interaction, route: str) -> None:
    await _route_command(interaction, BUS, route, runs=False)


@bus.command(name="trip", description="Follow one bus stop by stop")
@app_commands.describe(route="The route the bus runs on")
@app_commands.autocomplete(route=route_autocomplete(BUS))
async def bus_trip(interaction: discord.Interaction, route: str) -> None:
    await _route_command(interaction, BUS, route, runs=True)


@bus.command(name="live", description="Live bus positions for an operator, with map links")
@app_commands.describe(operator="Which operator", route="Only buses on this route")
@app_commands.choices(
    operator=[app_commands.Choice(name=operator_label(op), value=op) for op in BUS]
)
@app_commands.autocomplete(route=route_autocomplete(BUS))
async def bus_live(
    interaction: discord.Interaction,
    operator: app_commands.Choice[str],
    route: str | None = None,
) -> None:
    bot: Any = interaction.client
    route_id = ""
    if route:
        op, ref = split_ref(route)
        route_id = ref if op == operator.value else route
    await respond(interaction, lambda: live_screen(bot, operator.value, 0, route_id))


__all__ = ["train", "bus", "fav_kind", "haversine_m", "stop_screen", "departure_line"]
