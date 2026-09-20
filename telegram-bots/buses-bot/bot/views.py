"""Message rendering.

Every user-facing message is built here as a RichDoc, keeping presentation out
of the handlers. Buttons are built through `cb()`, which persists the payload
in SQLite and puts only a short token in the callback data, so a keyboard keeps
working indefinitely.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .gtfs import Feed, Route, Stop, Vehicle, operator_label
from .richtext import Button, RichDoc, b, code, esc, i, link, mark
from .timeutils import (
    describe_days,
    format_relative,
    format_time,
    minutes_until,
    now_myt,
    parse_gtfs_time,
    seconds_since_midnight,
)

MAX_TIMES_SHOWN = 12


async def cb(db: Any, label: str, action: str, payload: dict[str, Any] | None = None,
             user_id: int | None = None) -> Button:
    """Build a callback button whose payload survives restarts."""

    token = await db.make_callback(action, payload or {}, user_id)
    return Button.inline(label, token.encode())


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


def start_doc(first_name: str, web_app_url: str, donation_url: str) -> RichDoc:
    greeting = f"Hello {first_name}" if first_name else "Hello"

    doc = RichDoc()
    doc.heading("Malaysia Buses", 2)
    doc.para(
        f"{esc(greeting)}. This bot brings live bus positions and timetables for "
        f"Rapid Bus KL, Rapid Bus Penang, Rapid Bus MRT Feeder and myBAS Johor "
        f"straight into Telegram, using the open feeds published on "
        f"{link('data.gov.my', 'https://data.gov.my')}."
    )

    doc.heading("Quickest way to start", 3)
    doc.bullets(
        [
            "Send any stop or route name and the search runs automatically.",
            "Share your location and the nearest stops come back sorted by distance.",
            f"Save the stops you use with {code('/fav')}, then let the bot remind you before each bus.",
        ]
    )

    doc.heading("Commands", 3)
    doc.raw(
        "<table bordered striped>"
        "<thead><tr><th>Command</th><th>What it does</th></tr></thead>"
        "<tbody>"
        f"<tr><td>{code('/start')}</td><td>This overview</td></tr>"
        f"<tr><td>{code('/fav')}</td><td>Add a stop or route to favourites</td></tr>"
        f"<tr><td>{code('/unfav')}</td><td>Remove something from favourites</td></tr>"
        f"<tr><td>{code('/sub')}</td><td>Turn on reminders, live alerts and digests</td></tr>"
        f"<tr><td>{code('/unsub')}</td><td>Turn notifications back off</td></tr>"
        f"<tr><td>{code('/live')}</td><td>Live bus positions with map links</td></tr>"
        f"<tr><td>{code('/routes')}</td><td>Browse routes for an operator</td></tr>"
        f"<tr><td>{code('/stops')}</td><td>Browse stops and their timetables</td></tr>"
        f"<tr><td>{code('/trip')}</td><td>Follow one bus stop by stop</td></tr>"
        f"<tr><td>{code('/settings')}</td><td>Operator, clock format, quiet hours</td></tr>"
        "</tbody></table>"
    )

    doc.raw(
        "<details><summary>A note on the data</summary>"
        "<p>Timetables come from the published GTFS schedules and live positions "
        "come from the GTFS-realtime feeds. Coverage depends on what each operator "
        "reports, so a bus without a position is usually one that is not "
        "broadcasting rather than one that is missing.</p></details>"
    )

    return doc


def start_buttons(web_app_url: str, donation_url: str) -> list[list[Button]]:
    return [
        [
            Button.url("Open the web app", web_app_url),
            Button.url("Support the project", donation_url),
        ]
    ]


# ---------------------------------------------------------------------------
# Search results
# ---------------------------------------------------------------------------


async def search_results_doc(
    db: Any,
    query: str,
    stops: Sequence[tuple[str, Stop]],
    routes: Sequence[tuple[str, Route]],
    user_id: int,
) -> tuple[RichDoc, list[list[Button]]]:
    doc = RichDoc()
    doc.heading(f"Results for {query}", 3)

    buttons: list[list[Button]] = []

    if not stops and not routes:
        doc.para(
            "Nothing matched that search. Try a shorter piece of the name, such as "
            f"{code('Pasar Seni')} rather than the full stop description."
        )
        return doc, buttons

    if stops:
        doc.heading("Stops", 4)
        rows = [
            f"{b(stop.stop_name)} {i(operator_label(operator))}"
            for operator, stop in stops[:8]
        ]
        doc.bullets(rows)
        for operator, stop in stops[:8]:
            label = stop.stop_name if len(stop.stop_name) <= 30 else stop.stop_name[:29] + "…"
            buttons.append(
                [
                    await cb(db, f"🚏 {label}", "stop:view",
                             {"op": operator, "stop": stop.stop_id}, user_id)
                ]
            )

    if routes:
        doc.heading("Routes", 4)
        doc.bullets(
            [
                f"{b(route.display)} {i(operator_label(operator))}"
                for operator, route in routes[:8]
            ]
        )
        for operator, route in routes[:6]:
            label = route.display if len(route.display) <= 30 else route.display[:29] + "…"
            buttons.append(
                [
                    await cb(db, f"🚌 {label}", "route:view",
                             {"op": operator, "route": route.route_id}, user_id)
                ]
            )

    return doc, buttons


# ---------------------------------------------------------------------------
# Stop detail
# ---------------------------------------------------------------------------


def _upcoming_times(times: Sequence[str], limit: int = MAX_TIMES_SHOWN) -> list[str]:
    """Return the next departures from now, wrapping to the start of service."""

    current = seconds_since_midnight()
    parsed = [(parse_gtfs_time(t), t) for t in times]
    valid = [(secs, raw) for secs, raw in parsed if secs is not None]
    if not valid:
        return []
    upcoming = [raw for secs, raw in valid if secs >= current]
    if len(upcoming) >= limit:
        return upcoming[:limit]
    # Past the last bus, show tomorrow's first departures rather than nothing.
    return (upcoming + [raw for _secs, raw in valid])[:limit]


async def stop_doc(
    db: Any,
    feed: Feed,
    operator: str,
    stop: Stop,
    user_id: int,
    time_format: str = "12h",
    is_favourite: bool = False,
    favourite_id: int | None = None,
) -> tuple[RichDoc, list[list[Button]]]:
    doc = RichDoc()
    doc.heading(stop.stop_name, 3)
    doc.para(f"{i(operator_label(operator))} · stop {code(stop.stop_id)}")

    schedule = feed.stop_schedule.get(stop.stop_id, {})
    routes = feed.routes_for_stop(stop.stop_id)

    if not schedule:
        doc.para("No scheduled departures are published for this stop.")
    else:
        doc.heading("Next departures", 4)
        for route_id in sorted(schedule.keys()):
            route = feed.routes.get(route_id)
            label = route.display if route else route_id
            upcoming = _upcoming_times(schedule[route_id])
            if not upcoming:
                continue
            rendered = " · ".join(format_time(t, time_format) for t in upcoming)
            doc.para(f"{b(label)}<br>{esc(rendered)}")

    if routes:
        doc.raw(
            "<details><summary>Routes serving this stop</summary>"
            + "<ul>"
            + "".join(f"<li>{esc(r.display)}</li>" for r in routes)
            + "</ul></details>"
        )

    buttons: list[list[Button]] = []
    row: list[Button] = []

    if is_favourite and favourite_id is not None:
        row.append(await cb(db, "★ Remove favourite", "fav:remove",
                            {"fid": favourite_id}, user_id))
    else:
        row.append(await cb(db, "☆ Add to favourites", "fav:add",
                            {"op": operator, "stop": stop.stop_id}, user_id))

    row.append(await cb(db, "🔄 Refresh", "stop:view",
                        {"op": operator, "stop": stop.stop_id}, user_id))
    buttons.append(row)

    if stop.lat is not None and stop.lon is not None:
        buttons.append(
            [Button.url("📍 Open in Maps", f"https://www.google.com/maps?q={stop.lat},{stop.lon}")]
        )

    buttons.append(
        [await cb(db, "🚌 Live buses here", "stop:live",
                  {"op": operator, "stop": stop.stop_id}, user_id)]
    )

    return doc, buttons


# ---------------------------------------------------------------------------
# Route detail
# ---------------------------------------------------------------------------


async def route_doc(
    db: Any,
    feed: Feed,
    operator: str,
    route: Route,
    user_id: int,
    page: int = 0,
    page_size: int = 10,
) -> tuple[RichDoc, list[list[Button]]]:
    doc = RichDoc()
    doc.heading(route.display, 3)
    doc.para(f"{i(operator_label(operator))} · route {code(route.route_id)}")

    stop_ids = sorted(feed.route_stops.get(route.route_id, set()))
    total = len(stop_ids)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    window = stop_ids[page * page_size : (page + 1) * page_size]

    if not window:
        doc.para("No stops are listed for this route in the published schedule.")
    else:
        doc.heading(f"Stops {page * page_size + 1} to {page * page_size + len(window)} of {total}", 4)
        doc.numbered(
            [esc(feed.stops[s].stop_name) if s in feed.stops else esc(s) for s in window]
        )

    buttons: list[list[Button]] = []

    for stop_id in window[:8]:
        stop = feed.stops.get(stop_id)
        if not stop:
            continue
        label = stop.stop_name if len(stop.stop_name) <= 30 else stop.stop_name[:29] + "…"
        buttons.append(
            [await cb(db, f"🚏 {label}", "stop:view",
                      {"op": operator, "stop": stop_id}, user_id)]
        )

    nav: list[Button] = []
    if page > 0:
        nav.append(await cb(db, "◀ Previous", "route:view",
                            {"op": operator, "route": route.route_id, "p": page - 1}, user_id))
    if page < pages - 1:
        nav.append(await cb(db, "Next ▶", "route:view",
                            {"op": operator, "route": route.route_id, "p": page + 1}, user_id))
    if nav:
        buttons.append(nav)

    buttons.append(
        [
            await cb(db, "🚌 Buses on this route", "route:trips",
                     {"op": operator, "route": route.route_id}, user_id),
            await cb(db, "☆ Favourite", "fav:route",
                     {"op": operator, "route": route.route_id}, user_id),
        ]
    )

    return doc, buttons


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------


async def trip_list_doc(
    db: Any, feed: Feed, operator: str, route: Route, user_id: int,
    time_format: str = "12h", page: int = 0, page_size: int = 8,
) -> tuple[RichDoc, list[list[Button]]]:
    trips = feed.trips_for_route(route.route_id)

    doc = RichDoc()
    doc.heading(f"Buses on {route.display}", 3)

    if not trips:
        doc.para("No individual bus trips are published for this route.")
        return doc, []

    pages = max(1, (len(trips) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    window = trips[page * page_size : (page + 1) * page_size]

    doc.para(f"Showing page {page + 1} of {pages}. Pick a departure to see every stop.")

    buttons: list[list[Button]] = []
    for trip in window:
        times = feed.trip_stops.get(trip.trip_id) or []
        start = format_time(times[0].best_time, time_format) if times else "Unknown"
        label = f"{start}" + (f" → {trip.headsign}" if trip.headsign else "")
        if len(label) > 34:
            label = label[:33] + "…"
        buttons.append(
            [await cb(db, label, "trip:view", {"op": operator, "trip": trip.trip_id}, user_id)]
        )

    nav: list[Button] = []
    if page > 0:
        nav.append(await cb(db, "◀ Previous", "route:trips",
                            {"op": operator, "route": route.route_id, "p": page - 1}, user_id))
    if page < pages - 1:
        nav.append(await cb(db, "Next ▶", "route:trips",
                            {"op": operator, "route": route.route_id, "p": page + 1}, user_id))
    if nav:
        buttons.append(nav)

    return doc, buttons


def trip_doc(feed: Feed, operator: str, trip_id: str, time_format: str = "12h") -> RichDoc:
    doc = RichDoc()
    trip = feed.trips.get(trip_id)
    times = feed.trip_stops.get(trip_id) or []

    if not trip or not times:
        doc.para("That trip is no longer in the published schedule.")
        return doc

    route = feed.routes.get(trip.route_id)
    doc.heading(route.display if route else trip.route_id, 3)
    if trip.headsign:
        doc.para(f"Heading to {b(trip.headsign)} · {i(operator_label(operator))}")

    rows = []
    for index, stop_time in enumerate(times, start=1):
        stop = feed.stops.get(stop_time.stop_id)
        name = stop.stop_name if stop else stop_time.stop_id
        rows.append([str(index), name, format_time(stop_time.best_time, time_format)])

    doc.table(["#", "Stop", "Time"], rows)
    return doc


# ---------------------------------------------------------------------------
# Live vehicles
# ---------------------------------------------------------------------------


def live_doc(
    operator: str,
    vehicles: Sequence[Vehicle],
    feed: Feed | None = None,
    route_filter: str = "",
    limit: int = 10,
) -> RichDoc:
    doc = RichDoc()
    title = f"Live buses · {operator_label(operator)}"
    doc.heading(title, 3)

    shown = list(vehicles)
    if route_filter:
        shown = [v for v in shown if v.route_id == route_filter]

    if not shown:
        doc.para(
            "No buses are reporting a position on this feed right now. This is "
            "common outside service hours and when an operator pauses its "
            "realtime broadcast."
        )
        return doc

    positioned = [v for v in shown if v.has_position][:limit]
    doc.para(
        f"{b(str(len(shown)))} vehicles reporting. Showing the {len(positioned)} most recent."
    )

    now = now_myt().timestamp()
    for vehicle in positioned:
        route_name = vehicle.route_id or "Unknown route"
        if feed is not None:
            route = feed.routes.get(vehicle.route_id)
            if route:
                route_name = route.display
        age = format_relative(now - vehicle.timestamp) if vehicle.timestamp else "unknown age"
        doc.para(
            f"{b(route_name)} · {esc(vehicle.name)}<br>"
            f"{link('Open in Maps', vehicle.maps_url)} · {i(age)}"
        )

    return doc


# ---------------------------------------------------------------------------
# Favourites
# ---------------------------------------------------------------------------


async def favourites_doc(
    db: Any, favourites: Sequence[Any], user_id: int, for_removal: bool = False
) -> tuple[RichDoc, list[list[Button]]]:
    doc = RichDoc()
    doc.heading("Your favourites", 3)

    if not favourites:
        doc.para(
            "You have not saved anything yet. Send a stop name, share your "
            f"location, or browse with {code('/stops')}, then tap "
            f"{b('Add to favourites')}."
        )
        return doc, []

    doc.bullets(
        [
            f"{b(row['stop_name'])} {i(operator_label(row['operator']))}"
            + (f" · {esc(row['route_name'])}" if row["route_name"] else "")
            for row in favourites
        ]
    )

    buttons: list[list[Button]] = []
    for row in favourites:
        label = row["stop_name"]
        if len(label) > 28:
            label = label[:27] + "…"
        if for_removal:
            buttons.append(
                [await cb(db, f"✖ {label}", "fav:remove", {"fid": row["id"]}, user_id)]
            )
        else:
            buttons.append(
                [
                    await cb(db, f"🚏 {label}", "stop:view",
                             {"op": row["operator"], "stop": row["stop_id"]}, user_id)
                ]
            )

    return doc, buttons


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

SUB_LABELS = {
    "departure": "Departure reminders",
    "live": "Live bus alerts",
    "digest": "Daily digest",
    "health": "Service notices",
}

SUB_DESCRIPTIONS = {
    "departure": "A message shortly before each scheduled bus at your favourite stops.",
    "live": "A ping when a bus on a favourited route approaches that stop.",
    "digest": "One summary each morning covering the day at your favourite stops.",
    "health": "A note when a feed stops updating or a saved route disappears.",
}


async def subscriptions_doc(
    db: Any, subscriptions: Sequence[Any], user_id: int, lead_minutes: int, digest_time: str
) -> tuple[RichDoc, list[list[Button]]]:
    active = {row["kind"] for row in subscriptions if row["active"]}

    doc = RichDoc()
    doc.heading("Notifications", 3)
    doc.para("Tap any row to switch it on or off.")

    rows = []
    for kind, label in SUB_LABELS.items():
        state = "On" if kind in active else "Off"
        detail = SUB_DESCRIPTIONS[kind]
        if kind == "departure" and kind in active:
            detail = f"{detail} Currently {lead_minutes} minutes ahead."
        if kind == "digest" and kind in active:
            detail = f"{detail} Currently at {digest_time}."
        rows.append([label, state, detail])

    doc.table(["Notification", "State", "Details"], rows)

    if not any(row["favourite_id"] for row in subscriptions) and (
        "departure" in active or "live" in active
    ):
        doc.quote(
            "Departure reminders and live alerts follow your favourites, so add "
            "at least one stop for them to have something to watch."
        )

    buttons: list[list[Button]] = []
    for kind, label in SUB_LABELS.items():
        state = "✅" if kind in active else "⬜"
        buttons.append(
            [await cb(db, f"{state} {label}", "sub:toggle", {"kind": kind}, user_id)]
        )

    return doc, buttons


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


async def settings_doc(db: Any, user: Any, user_id: int) -> tuple[RichDoc, list[list[Button]]]:
    doc = RichDoc()
    doc.heading("Settings", 3)

    quiet = (
        f"{user['quiet_from']} to {user['quiet_to']}"
        if user["quiet_enabled"]
        else "Off"
    )

    doc.table(
        ["Setting", "Current value"],
        [
            ["Default operator", operator_label(user["operator"])],
            ["Clock format", "12 hour" if user["time_format"] == "12h" else "24 hour"],
            ["Reminder lead time", f"{user['lead_minutes']} minutes"],
            ["Daily digest time", user["digest_time"]],
            ["Quiet hours", quiet],
        ],
    )

    doc.para(i("All times are Malaysia time (UTC+8)."))

    buttons = [
        [await cb(db, "🚌 Default operator", "set:operator", {}, user_id)],
        [
            await cb(db, "🕒 Clock format", "set:timefmt", {}, user_id),
            await cb(db, "⏱ Lead time", "set:lead", {}, user_id),
        ],
        [
            await cb(db, "🌙 Quiet hours", "set:quiet", {}, user_id),
            await cb(db, "📰 Digest time", "set:digest", {}, user_id),
        ],
        [await cb(db, "🗑 Delete my data", "set:wipe", {}, user_id)],
    ]

    return doc, buttons


async def operator_picker(db: Any, action: str, user_id: int,
                          extra: dict[str, Any] | None = None) -> list[list[Button]]:
    from .gtfs import ALL_OPERATORS

    buttons: list[list[Button]] = []
    for operator in ALL_OPERATORS:
        payload = {"op": operator}
        if extra:
            payload.update(extra)
        buttons.append([await cb(db, operator_label(operator), action, payload, user_id)])
    return buttons


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def departure_reminder_doc(
    stop_name: str, route_name: str, departure: str, minutes: int, time_format: str = "12h"
) -> RichDoc:
    doc = RichDoc()
    doc.heading("Bus coming up", 3)
    when = "now" if minutes <= 0 else f"in about {minutes} minutes"
    doc.para(
        f"{b(route_name)} departs {b(stop_name)} at "
        f"{mark(format_time(departure, time_format))}, {when}."
    )
    return doc


def live_alert_doc(route_name: str, stop_name: str, distance_m: float, vehicle: Vehicle) -> RichDoc:
    doc = RichDoc()
    doc.heading("Bus approaching", 3)
    doc.para(
        f"{b(route_name)} is about {b(f'{int(distance_m)} m')} from {b(stop_name)}."
    )
    if vehicle.has_position:
        doc.para(link("Track it on the map", vehicle.maps_url))
    return doc


def digest_doc(entries: Sequence[tuple[str, str, list[str]]], time_format: str = "12h") -> RichDoc:
    doc = RichDoc()
    doc.heading("Your buses today", 3)

    if not entries:
        doc.para("Nothing is scheduled at your favourite stops today.")
        return doc

    for stop_name, route_name, times in entries:
        rendered = " · ".join(format_time(t, time_format) for t in times[:10])
        doc.para(f"{b(stop_name)} · {esc(route_name)}<br>{esc(rendered)}")

    return doc


def health_doc(stale: Sequence[tuple[str, str]]) -> RichDoc:
    doc = RichDoc()
    doc.heading("Service notice", 3)
    doc.para("These feeds have stopped updating, so their data may be out of date.")
    doc.bullets([f"{b(operator_label(op))}: {esc(reason)}" for op, reason in stale])
    return doc
