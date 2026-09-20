"""Message rendering.

Every user-facing message is built here as a RichDoc, keeping presentation out
of the handlers. Buttons are built through `cb()`, which persists the payload
in SQLite and puts only a short token in the callback data, so a keyboard keeps
working indefinitely.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from .gtfs import (
    ALL_OPERATORS,
    KTMB,
    Feed,
    Line,
    Station,
    Vehicle,
    has_live,
    operator_label,
)
from .richtext import Button, RichDoc, b, code, esc, i, link, mark
from .timeutils import (
    format_relative,
    format_time,
    format_wait,
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


def _clip(text: str, limit: int) -> str:
    """Telegram truncates long button labels awkwardly, so do it deliberately."""

    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


def start_doc(first_name: str, web_app_url: str, donation_url: str) -> RichDoc:
    greeting = f"Hello {first_name}" if first_name else "Hello"

    doc = RichDoc()
    doc.heading("Malaysia Trains", 2)
    doc.para(
        f"{esc(greeting)}. This bot brings rail timetables and live train "
        f"positions into Telegram, covering the LRT, MRT, Monorail and BRT "
        f"lines run by Rapid KL together with the KTM Komuter, ETS and "
        f"intercity services run by KTMB. The data comes from the open feeds "
        f"published on {link('data.gov.my', 'https://data.gov.my')}."
    )

    doc.heading("Quickest way to start", 3)
    doc.bullets(
        [
            "Send a station or line name and the search runs automatically.",
            "Share your location and the nearest stations come back sorted by distance.",
            "Save the stations you use with /fav, then let the bot remind you before each train.",
        ]
    )

    # Commands are left as plain text rather than wrapped in <code>, which
    # keeps Telegram's own command autolinking working so they stay tappable.
    doc.heading("Commands", 3)
    doc.table(
        ["Command", "What it does"],
        [
            ["/start", "This overview"],
            ["/next", "Next departures from your home station"],
            ["/stations", "Find a station and read its timetable"],
            ["/lines", "Browse every line and the stations it serves"],
            ["/train", "Follow one train stop by stop"],
            ["/live", "Live KTMB train positions with map links"],
            ["/fav", "Add a station to favourites"],
            ["/unfav", "Remove something from favourites"],
            ["/sub", "Turn on reminders, alerts and digests"],
            ["/unsub", "Turn notifications back off"],
            ["/settings", "Home station, clock format, quiet hours"],
        ],
    )

    doc.raw(
        "<details><summary>A note on the data</summary>"
        "<p>Rapid KL publishes its rail lines as headways rather than a fixed "
        "timetable, so departure times on those lines are worked out from the "
        "published frequency for each part of the day and should be read as "
        "close estimates. KTMB publishes exact times. Only KTMB broadcasts "
        "live positions, so a Rapid KL train will not appear on the live map.</p>"
        "</details>"
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
    stops: Sequence[tuple[str, Station]],
    routes: Sequence[tuple[str, Line]],
    user_id: int,
) -> tuple[RichDoc, list[list[Button]]]:
    doc = RichDoc()
    doc.heading(f"Results for {query}", 3)

    buttons: list[list[Button]] = []

    if not stops and not routes:
        doc.para(
            "Nothing matched that search. Try a shorter piece of the name, such "
            f"as {code('Sentral')} rather than the full station description."
        )
        doc.para(
            "You can also browse everything with /stations and /lines, or share "
            "your location to see what is nearby."
        )
        return doc, buttons

    if stops:
        doc.heading("Stations", 4)
        doc.bullets(
            [
                f"{b(stop.display)} {i(operator_label(operator))}"
                for operator, stop in stops[:8]
            ]
        )
        for operator, stop in stops[:8]:
            buttons.append(
                [
                    await cb(db, f"🚉 {_clip(stop.display, 30)}", "stop:view",
                             {"op": operator, "stop": stop.stop_id}, user_id)
                ]
            )

    if routes:
        doc.heading("Lines", 4)
        doc.bullets(
            [
                f"{b(route.display)} {i(operator_label(operator))}"
                for operator, route in routes[:8]
            ]
        )
        for operator, route in routes[:6]:
            buttons.append(
                [
                    await cb(db, f"🚆 {_clip(route.display, 30)}", "route:view",
                             {"op": operator, "route": route.route_id}, user_id)
                ]
            )

    return doc, buttons


# ---------------------------------------------------------------------------
# Station detail
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
    # Past the last train, show tomorrow's first departures rather than nothing.
    return (upcoming + [raw for _secs, raw in valid])[:limit]


def _next_departure_wait(times: Sequence[str]) -> str | None:
    """How long until the very next departure, phrased for a human."""

    current = seconds_since_midnight()
    upcoming = [
        secs
        for secs in (parse_gtfs_time(t) for t in times)
        if secs is not None and secs >= current
    ]
    if not upcoming:
        return None
    return format_wait(min(upcoming) - current)


async def stop_doc(
    db: Any,
    feed: Feed,
    operator: str,
    stop: Station,
    user_id: int,
    time_format: str = "12h",
    is_favourite: bool = False,
    favourite_id: int | None = None,
    is_home: bool = False,
    day: date | None = None,
) -> tuple[RichDoc, list[list[Button]]]:
    doc = RichDoc()
    doc.heading(stop.display, 3)

    meta = f"{i(operator_label(operator))} · station {code(stop.stop_id)}"
    if stop.category:
        meta = f"{i(operator_label(operator))} · {b(stop.category)} · station {code(stop.stop_id)}"
    doc.para(meta)

    lines = feed.routes_at_stop(stop.stop_id)
    shown_any = False

    for route in lines:
        times = feed.departures(stop.stop_id, route.route_id, day)
        upcoming = _upcoming_times(times)
        if not upcoming:
            continue
        if not shown_any:
            doc.heading("Next departures", 4)
            shown_any = True
        wait = _next_departure_wait(times)
        rendered = " · ".join(format_time(t, time_format) for t in upcoming)
        headline = f"{b(route.display)}"
        if wait:
            headline += f" · {mark(wait)}"
        doc.para(f"{headline}<br>{esc(rendered)}")

    if not shown_any:
        doc.para(
            "No departures are scheduled here for the rest of today. The "
            "timetable for tomorrow will be available from midnight."
        )

    if lines:
        doc.raw(
            "<details><summary>Lines serving this station</summary><ul>"
            + "".join(f"<li>{esc(r.display)}</li>" for r in lines)
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

    second: list[Button] = []
    if not is_home:
        second.append(
            await cb(db, "🏠 Set as home", "home:set",
                     {"op": operator, "stop": stop.stop_id}, user_id)
        )
    if has_live(operator):
        second.append(
            await cb(db, "🚆 Live trains here", "stop:live",
                     {"op": operator, "stop": stop.stop_id}, user_id)
        )
    if second:
        buttons.append(second)

    if stop.lat is not None and stop.lon is not None:
        buttons.append(
            [Button.url("📍 Open in Maps", f"https://www.google.com/maps?q={stop.lat},{stop.lon}")]
        )

    return doc, buttons


# ---------------------------------------------------------------------------
# Line detail
# ---------------------------------------------------------------------------


async def route_doc(
    db: Any,
    feed: Feed,
    operator: str,
    route: Line,
    user_id: int,
    page: int = 0,
    page_size: int = 10,
) -> tuple[RichDoc, list[list[Button]]]:
    doc = RichDoc()
    doc.heading(route.display, 3)

    meta = f"{i(operator_label(operator))} · line {code(route.route_id)}"
    if route.badge:
        meta = f"{i(operator_label(operator))} · {b(route.badge)} · line {code(route.route_id)}"
    doc.para(meta)

    # Stations are ordered along the line using the longest trip as the
    # reference, so the list reads as a journey rather than alphabetically.
    ordered = _ordered_stations(feed, route.route_id)
    total = len(ordered)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    window = ordered[page * page_size : (page + 1) * page_size]

    if not window:
        doc.para("No stations are listed for this line in the published schedule.")
    else:
        doc.heading(
            f"Stations {page * page_size + 1} to {page * page_size + len(window)} of {total}", 4
        )
        doc.numbered([esc(feed.stops[s].display) for s in window if s in feed.stops])

    buttons: list[list[Button]] = []
    for stop_id in window[:8]:
        stop = feed.stops.get(stop_id)
        if not stop:
            continue
        buttons.append(
            [await cb(db, f"🚉 {_clip(stop.display, 30)}", "stop:view",
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
            await cb(db, "🚆 Trains on this line", "route:trips",
                     {"op": operator, "route": route.route_id}, user_id),
        ]
    )

    return doc, buttons


def _ordered_stations(feed: Feed, route_id: str) -> list[str]:
    """Stations along a line in travel order.

    The longest trip on the line is used as the running order, which gives the
    full sequence including any stations that short-working trips skip. Any
    remaining stations are appended so nothing is lost.
    """

    best: list[str] = []
    for trip in feed.trips.values():
        if trip.route_id != route_id:
            continue
        times = feed.trip_stops.get(trip.trip_id) or []
        if len(times) > len(best):
            best = [st.stop_id for st in times]

    seen = set(best)
    extras = sorted(feed.route_stops.get(route_id, set()) - seen)
    return best + extras


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------


async def trip_list_doc(
    db: Any, feed: Feed, operator: str, route: Line, user_id: int,
    time_format: str = "12h", page: int = 0, page_size: int = 8,
    day: date | None = None,
) -> tuple[RichDoc, list[list[Button]]]:
    trips = feed.trips_for_route(route.route_id, day)

    doc = RichDoc()
    doc.heading(f"Trains on {route.display}", 3)

    if not trips:
        doc.para("No individual trains are published for this line today.")
        return doc, []

    pages = max(1, (len(trips) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    window = trips[page * page_size : (page + 1) * page_size]

    doc.para(f"Showing page {page + 1} of {pages}. Pick a train to see every stop it calls at.")

    buttons: list[list[Button]] = []
    for trip in window:
        times = feed.trip_stops.get(trip.trip_id) or []
        start = format_time(times[0].best_time, time_format) if times else "Unknown"
        destination = trip.headsign
        if not destination and times:
            last = feed.stops.get(times[-1].stop_id)
            destination = last.display if last else ""
        label = f"{start} → {destination}" if destination else start
        buttons.append(
            [await cb(db, _clip(label, 34), "trip:view",
                      {"op": operator, "trip": trip.trip_id}, user_id)]
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
        doc.para("That train is no longer in the published schedule.")
        return doc

    route = feed.routes.get(trip.route_id)
    doc.heading(route.display if route else trip.route_id, 3)

    first = feed.stops.get(times[0].stop_id)
    last = feed.stops.get(times[-1].stop_id)
    journey = ""
    if first and last:
        journey = f"{b(first.display)} to {b(last.display)}"
    elif trip.headsign:
        journey = f"Heading to {b(trip.headsign)}"
    if journey:
        doc.para(f"{journey} · {i(operator_label(operator))}")

    rows = []
    for index, stop_time in enumerate(times, start=1):
        stop = feed.stops.get(stop_time.stop_id)
        name = stop.display if stop else stop_time.stop_id
        rows.append([str(index), name, format_time(stop_time.best_time, time_format)])

    doc.table(["#", "Station", "Time"], rows)
    return doc


# ---------------------------------------------------------------------------
# Live trains
# ---------------------------------------------------------------------------


def live_doc(
    vehicles: Sequence[Vehicle],
    feed: Feed | None = None,
    limit: int = 10,
    heading: str = "Live trains · KTMB",
) -> RichDoc:
    doc = RichDoc()
    doc.heading(heading, 3)

    if not vehicles:
        doc.para(
            "No trains are reporting a position right now. This is common "
            "outside service hours and whenever KTMB pauses its realtime feed."
        )
        doc.para(
            i("Rapid KL does not broadcast live positions for its rail lines, "
              "so only KTMB services appear here.")
        )
        return doc

    positioned = [v for v in vehicles if v.has_position][:limit]
    doc.para(
        f"{b(str(len(vehicles)))} trains reporting. Showing the "
        f"{len(positioned)} most recently updated."
    )

    now = now_myt().timestamp()
    for vehicle in positioned:
        route_name = vehicle.route_id or "Unknown line"
        if feed is not None and vehicle.route_id:
            route = feed.routes.get(vehicle.route_id)
            if route:
                route_name = route.display
        age = format_relative(now - vehicle.timestamp) if vehicle.timestamp else "unknown age"
        detail = f"{link('Open in Maps', vehicle.maps_url)} · {i(age)}"
        speed = vehicle.speed_kmh
        if speed is not None and speed > 1:
            detail = f"{detail} · {esc(f'{speed:.0f} km/h')}"
        doc.para(f"{b(route_name)} · {esc(vehicle.name)}<br>{detail}")

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
            "You have not saved anything yet. Send a station name, share your "
            f"location, or browse with /stations, then tap {b('Add to favourites')}."
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
        label = _clip(row["stop_name"], 28)
        if for_removal:
            buttons.append(
                [await cb(db, f"✖ {label}", "fav:remove", {"fid": row["id"]}, user_id)]
            )
        else:
            buttons.append(
                [
                    await cb(db, f"🚉 {label}", "stop:view",
                             {"op": row["operator"], "stop": row["stop_id"]}, user_id)
                ]
            )

    return doc, buttons


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

SUB_LABELS = {
    "departure": "Departure reminders",
    "live": "Live train alerts",
    "digest": "Daily digest",
    "health": "Service notices",
}

SUB_DESCRIPTIONS = {
    "departure": "A message shortly before each scheduled train at your favourite stations.",
    "live": "A ping when a KTMB train approaches one of your favourite stations.",
    "digest": "One summary each morning covering the day at your favourite stations.",
    "health": "A note when a feed stops updating or a saved line disappears.",
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
            "at least one station for them to have something to watch."
        )

    if "live" in active:
        doc.para(
            i("Live alerts only cover KTMB, because Rapid KL does not broadcast "
              "train positions.")
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
    home = user["home_stop_name"] or "Not set"

    doc.table(
        ["Setting", "Current value"],
        [
            ["Home station", home],
            ["Preferred operator", operator_label(user["operator"])],
            ["Clock format", "12 hour" if user["time_format"] == "12h" else "24 hour"],
            ["Reminder lead time", f"{user['lead_minutes']} minutes"],
            ["Daily digest time", user["digest_time"]],
            ["Quiet hours", quiet],
        ],
    )

    doc.para(i("All times are Malaysia time (UTC+8)."))

    buttons = [
        [await cb(db, "🏠 Home station", "set:home", {}, user_id)],
        [await cb(db, "🚆 Preferred operator", "set:operator", {}, user_id)],
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
                          extra: dict[str, Any] | None = None,
                          operators: Sequence[str] = ALL_OPERATORS) -> list[list[Button]]:
    buttons: list[list[Button]] = []
    for operator in operators:
        payload: dict[str, Any] = {"op": operator}
        if extra:
            payload.update(extra)
        buttons.append([await cb(db, operator_label(operator), action, payload, user_id)])
    return buttons


# ---------------------------------------------------------------------------
# Next departures
# ---------------------------------------------------------------------------


def next_doc(
    feed: Feed,
    operator: str,
    stop: Station,
    time_format: str = "12h",
    day: date | None = None,
) -> RichDoc:
    """A compact answer to 'when is my next train'."""

    doc = RichDoc()
    doc.heading(f"Next from {stop.display}", 3)
    doc.para(f"{i(operator_label(operator))} · {esc(now_myt().strftime('%a %d %b, %H:%M'))}")

    rows: list[list[str]] = []
    for route in feed.routes_at_stop(stop.stop_id):
        times = feed.departures(stop.stop_id, route.route_id, day)
        upcoming = _upcoming_times(times, limit=3)
        if not upcoming:
            continue
        wait = _next_departure_wait(times) or ""
        rows.append(
            [
                route.display,
                " · ".join(format_time(t, time_format) for t in upcoming),
                wait,
            ]
        )

    if not rows:
        doc.para(
            "Nothing further is scheduled here today. Services resume with "
            "tomorrow's timetable."
        )
        return doc

    doc.table(["Line", "Next departures", "In"], rows)
    return doc


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def departure_reminder_doc(
    stop_name: str, route_name: str, departure: str, minutes: int, time_format: str = "12h"
) -> RichDoc:
    doc = RichDoc()
    doc.heading("Train coming up", 3)
    when = "now" if minutes <= 0 else f"in about {minutes} minutes"
    doc.para(
        f"{b(route_name)} departs {b(stop_name)} at "
        f"{mark(format_time(departure, time_format))}, {when}."
    )
    return doc


def live_alert_doc(route_name: str, stop_name: str, distance_m: float, vehicle: Vehicle) -> RichDoc:
    doc = RichDoc()
    doc.heading("Train approaching", 3)
    doc.para(
        f"{b(route_name)} is about {b(f'{int(distance_m)} m')} from {b(stop_name)}."
    )
    if vehicle.has_position:
        doc.para(link("Track it on the map", vehicle.maps_url))
    return doc


def digest_doc(
    entries: Sequence[tuple[str, str, list[str]]], time_format: str = "12h"
) -> RichDoc:
    doc = RichDoc()
    doc.heading("Your trains today", 3)

    if not entries:
        doc.para("Nothing is scheduled at your favourite stations today.")
        return doc

    doc.para(esc(now_myt().strftime("%A %d %B")))

    for stop_name, route_name, times in entries:
        rendered = " · ".join(format_time(t, time_format) for t in times[:10])
        more = "" if len(times) <= 10 else f" and {len(times) - 10} more"
        doc.para(f"{b(stop_name)} · {esc(route_name)}<br>{esc(rendered)}{esc(more)}")

    return doc


def health_doc(stale: Sequence[tuple[str, str]]) -> RichDoc:
    doc = RichDoc()
    doc.heading("Service notice", 3)
    doc.para("These feeds have stopped updating, so their data may be out of date.")
    doc.bullets([f"{b(operator_label(op))}: {esc(reason)}" for op, reason in stale])
    return doc
