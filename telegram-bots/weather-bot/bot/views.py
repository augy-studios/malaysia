"""Message construction.

Every screen the bot shows is built here as a `RichDoc`, so handlers deal with
data and this module deals with wording and layout. Nothing here touches the
network or the database.

Two rules run through all of it:

  * No screen is a dead end. Every view is paired with a keyboard that offers
    a way onward and a way back, and `nav_row` exists so that is hard to
    forget.
  * Wording stays plain. Sentences are written out rather than clipped, and
    em dashes are avoided throughout.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .feeds import (
    FloodStation,
    ForecastDay,
    Location,
    Quake,
    Warning,
)
from .richtext import RichDoc, b, i, link
from .timeutils import (
    ago,
    describe_days,
    format_datetime,
    format_day,
    now_myt,
)

# Severity words, rendered as a leading marker. Telegram shows these inline in
# both the rich and the classic path.
SEVERITY_MARK = {
    "danger": "🔴",
    "warning": "🟠",
    "alert": "🟡",
    "normal": "🟢",
    "high": "🔴",
    "medium": "🟠",
    "low": "🟡",
    "unknown": "⚪",
}


def mark(severity: str) -> str:
    return SEVERITY_MARK.get(severity, "⚪")


def _stale_note(snapshot: Any) -> str:
    """A line admitting the data is old, when it is."""

    if getattr(snapshot, "stale", False):
        return i(
            "data.gov.my could not be reached just now, so these are the last "
            f"figures the bot held, from {ago(snapshot.fetched_at)}."
        )
    return ""


def _footer(doc: RichDoc, snapshot: Any) -> RichDoc:
    note = _stale_note(snapshot)
    if note:
        doc.para(note)
    elif getattr(snapshot, "fetched_at", 0):
        doc.para(i(f"Source: data.gov.my, updated {ago(snapshot.fetched_at)}."))
    return doc


# ---------------------------------------------------------------------------
# Start and help
# ---------------------------------------------------------------------------


def start_doc(first_name: str = "") -> RichDoc:
    """The /start screen, which doubles as the help text."""

    greeting = f"Hello {first_name}," if first_name else "Hello,"

    doc = RichDoc()
    doc.heading("Malaysia Weather, Quake and Flood", level=2)
    doc.para(
        f"{greeting} this bot carries official Malaysian weather forecasts, "
        "MET warnings, earthquake bulletins and river levels, straight from "
        "the open data.gov.my feeds."
    )

    doc.heading("Quickest way to start", level=3)
    doc.bullets(
        [
            "Send a town name and the search runs automatically.",
            "Share your location and the nearest forecast area and river "
            "gauges come back sorted by distance.",
            "Save the places you follow with /fav, then let the bot tell you "
            "when a warning is issued or a river rises.",
        ]
    )

    # Commands are left as plain text rather than wrapped in <code>, which
    # keeps Telegram's own command autolinking working so they stay tappable.
    doc.heading("Commands", level=3)
    doc.table(
        ["Command", "What it does"],
        [
            ["/start", "This overview"],
            ["/weather", "Seven day forecast, for the last place or any town"],
            ["/warnings", "Weather warnings currently in force"],
            ["/quake", "Recent earthquakes in and around Malaysia"],
            ["/flood", "River levels and which gauges are rising"],
            ["/fav", "Save a town or river gauge to favourites"],
            ["/unfav", "Remove something from favourites"],
            ["/sub", "Turn alerts on"],
            ["/unsub", "Turn alerts off"],
            ["/settings", "Quiet hours, thresholds and preferences"],
            ["/about", "Where the data comes from"],
        ],
    )
    return doc


def about_doc() -> RichDoc:
    doc = RichDoc()
    doc.heading("About this bot", level=2)
    doc.para(
        "Every figure shown here comes from the Malaysian government open data "
        "portal at data.gov.my, which publishes on behalf of the agencies "
        "below. The bot adds no forecasting of its own, it only relays and "
        "formats what those agencies have released."
    )

    doc.heading("Sources", level=3)
    doc.bullets(
        [
            f"{b('Forecasts and weather warnings')}: MET Malaysia, the "
            "Malaysian Meteorological Department.",
            f"{b('Earthquake bulletins')}: MET Malaysia seismology division.",
            f"{b('River levels')}: the national flood warning feed, sourced "
            "from the Department of Irrigation and Drainage.",
        ]
    )

    doc.heading("Please read this", level=3)
    doc.quote(
        "This bot is an unofficial convenience and is not an emergency "
        "service. During a flood, a storm or an earthquake, follow the "
        "instructions of the local authorities and the official agency "
        "channels rather than relying on a Telegram message."
    )
    doc.para(
        "Data is cached briefly to stay within the rate limits of the public "
        "feeds, so a reading may lag the source by a few minutes."
    )
    return doc


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


def _day_row(day: ForecastDay) -> list[str]:
    return [
        format_day(day.date),
        day.condition,
        day.temp_range or "No reading",
    ]


def forecast_doc(location: Location, snapshot: Any, days: int = 7) -> RichDoc:
    """The seven day forecast for one town."""

    doc = RichDoc()
    doc.heading(location.name, level=2)

    today = location.today
    if today is not None:
        temps = f", {today.temp_range}" if today.temp_range else ""
        doc.para(f"{b('Today')}: {today.headline}{temps}.")

        # The breakdown is only worth showing when it says more than the
        # summary line already did.
        parts = [
            (label, value)
            for label, value in (
                ("Morning", today.morning),
                ("Afternoon", today.afternoon),
                ("Night", today.night),
            )
            if value
        ]
        if parts:
            doc.bullets([f"{b(label)}: {value}" for label, value in parts])

    upcoming = location.days[:days]
    if upcoming:
        doc.heading("The week ahead", level=3)
        doc.table(
            ["Day", "Outlook", "Temperature"],
            [_day_row(day) for day in upcoming],
        )

    wet_days = [format_day(d.date) for d in upcoming if d.is_wet]
    if wet_days:
        doc.para(
            "Rain or thunderstorms are expected on "
            + _join(wet_days)
            + ". An umbrella is worth carrying."
        )
    elif upcoming:
        doc.para("No rain is being forecast for this area over the coming week.")

    return _footer(doc, snapshot)


def location_choice_doc(query: str, matches: Sequence[Location]) -> RichDoc:
    doc = RichDoc()
    doc.heading("Which place did you mean?", level=3)
    doc.para(
        f"Several forecast areas match {b(query)}. Choose one from the buttons "
        "below."
    )
    doc.bullets([loc.name for loc in matches])
    return doc


def no_match_doc(query: str) -> RichDoc:
    doc = RichDoc()
    doc.heading("Nothing found", level=3)
    doc.para(
        f"No forecast area or river gauge matched {b(query)}. MET publishes "
        "forecasts by town and district, so a state name or a neighbourhood "
        "may not appear."
    )
    doc.para("Try a nearby town, or share your location to see what is closest.")
    return doc


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def warnings_doc(warnings: Sequence[Warning], snapshot: Any, place: str = "") -> RichDoc:
    doc = RichDoc()
    scope = f" for {place}" if place else ""
    doc.heading(f"Weather warnings{scope}", level=2)

    if not warnings:
        doc.para(
            f"MET Malaysia has no weather warnings in force{scope} at the "
            "moment. That is the good news it sounds like."
        )
        return _footer(doc, snapshot)

    count = len(warnings)
    noun = "warning is" if count == 1 else "warnings are"
    doc.para(f"{b(str(count))} {noun} currently in force{scope}.")

    for warning in warnings:
        doc.divider()
        doc.para(f"{mark(warning.severity)} {b(warning.title)}")
        doc.para(warning.text)
        if warning.valid_from or warning.valid_to:
            doc.para(
                i(
                    "Valid from "
                    + format_datetime(warning.valid_from)
                    + " until "
                    + format_datetime(warning.valid_to)
                    + "."
                )
            )
        if warning.instruction:
            doc.quote(warning.instruction)

    return _footer(doc, snapshot)


# ---------------------------------------------------------------------------
# Earthquakes
# ---------------------------------------------------------------------------


def quakes_doc(quakes: Sequence[Quake], snapshot: Any, limit: int = 8) -> RichDoc:
    doc = RichDoc()
    doc.heading("Recent earthquakes", level=2)

    if not quakes:
        doc.para(
            "MET Malaysia has not published any earthquake bulletins recently."
        )
        return _footer(doc, snapshot)

    shown = list(quakes[:limit])
    doc.para(
        f"The {len(shown)} most recent bulletins are listed below, newest first."
    )

    doc.table(
        ["When", "Magnitude", "Location"],
        [
            [
                ago(q.when),
                q.magnitude_text,
                q.location,
            ]
            for q in shown
        ],
    )

    strongest = max(
        (q for q in shown if q.magnitude is not None),
        key=lambda q: q.magnitude or 0,
        default=None,
    )
    if strongest is not None and strongest.severity in ("high", "medium"):
        doc.divider()
        doc.para(
            f"{mark(strongest.severity)} The strongest of these was "
            f"{b(strongest.magnitude_text)} near {strongest.location}, "
            f"{ago(strongest.when)}."
        )

    return _footer(doc, snapshot)


def quake_detail_doc(quake: Quake) -> RichDoc:
    doc = RichDoc()
    doc.heading(f"{quake.magnitude_text} earthquake", level=2)
    doc.para(f"{mark(quake.severity)} {b(quake.location)}")

    rows = [["When", format_datetime(quake.when)], ["Magnitude", quake.magnitude_text]]
    if quake.depth is not None:
        rows.append(["Depth", f"{quake.depth:g} km"])
    if quake.lat is not None and quake.lon is not None:
        rows.append(["Coordinates", f"{quake.lat:.3f}, {quake.lon:.3f}"])
    if quake.distance:
        rows.append(["Distance", quake.distance])
    if quake.status:
        rows.append(["Status", quake.status])
    doc.table(["Detail", "Value"], rows)

    if quake.severity == "high":
        doc.quote(
            "A quake of this size can be felt at considerable distance. If you "
            "are in the affected area, follow the guidance of the local "
            "authorities."
        )
    return doc


# ---------------------------------------------------------------------------
# Floods
# ---------------------------------------------------------------------------


def flood_overview_doc(
    stations: Sequence[FloodStation], snapshot: Any, limit: int = 10
) -> RichDoc:
    doc = RichDoc()
    doc.heading("River levels", level=2)

    if not stations:
        doc.para("The flood warning feed returned no stations just now.")
        return _footer(doc, snapshot)

    elevated = [st for st in stations if st.is_elevated]
    danger = [st for st in stations if st.severity == "danger"]

    if not elevated:
        doc.para(
            f"{mark('normal')} All {b(str(len(stations)))} monitored stations "
            "are reporting normal levels."
        )
        return _footer(doc, snapshot)

    noun = "station is" if len(elevated) == 1 else "stations are"
    doc.para(
        f"{mark('danger' if danger else 'alert')} {b(str(len(elevated)))} "
        f"{noun} at alert level or above, out of {len(stations)} monitored "
        "nationwide."
    )
    if danger:
        word = "station has" if len(danger) == 1 else "stations have"
        doc.para(f"{mark('danger')} {b(str(len(danger)))} {word} reached danger level.")

    ranked = sorted(elevated, key=lambda st: (-st.rank, st.name))[:limit]
    doc.heading("Highest readings", level=3)
    doc.table(
        ["Station", "Level", "Status"],
        [
            [st.name, st.level_text, f"{mark(st.severity)} {st.indicator.title()}"]
            for st in ranked
        ],
    )
    if len(elevated) > limit:
        doc.para(i(f"{len(elevated) - limit} further stations are also elevated."))

    return _footer(doc, snapshot)


def station_doc(station: FloodStation, snapshot: Any = None) -> RichDoc:
    doc = RichDoc()
    doc.heading(station.name, level=2)
    doc.para(f"{mark(station.severity)} {b(station.indicator.title() or 'Unknown')} at {station.place}")

    rows = [["Current level", station.level_text]]
    if station.trend_text:
        rows.append(["Trend", station.trend_text.title()])
    for label, value in (
        ("Normal below", station.normal_level),
        ("Alert at", station.alert_level),
        ("Warning at", station.warning_level),
        ("Danger at", station.danger_level),
    ):
        if value is not None:
            rows.append([label, f"{value:g} m"])
    if station.sub_basin or station.main_basin:
        rows.append(["Basin", station.sub_basin or station.main_basin])
    if station.updated_at:
        rows.append(["Reading taken", format_datetime(station.updated_at)])
    doc.table(["Detail", "Value"], rows)

    if station.severity == "danger":
        doc.quote(
            "This gauge is at danger level. Flooding is possible nearby, so "
            "move valuables to higher ground and follow the instructions of "
            "the local authorities."
        )
    elif station.severity == "warning":
        doc.quote(
            "This gauge is at warning level. Keep an eye on the water and be "
            "ready to move if it continues to rise."
        )

    return _footer(doc, snapshot) if snapshot else doc


def station_list_doc(title: str, stations: Sequence[FloodStation], snapshot: Any) -> RichDoc:
    doc = RichDoc()
    doc.heading(title, level=2)
    if not stations:
        doc.para("No river gauges matched that search.")
        return _footer(doc, snapshot)

    doc.table(
        ["Station", "Level", "Status"],
        [
            [st.name, st.level_text, f"{mark(st.severity)} {st.indicator.title() or 'Unknown'}"]
            for st in stations
        ],
    )
    doc.para("Choose one below to see its full reading.")
    return _footer(doc, snapshot)


# ---------------------------------------------------------------------------
# Nearby
# ---------------------------------------------------------------------------


def nearby_doc(
    location: Location | None,
    stations: Sequence[tuple[FloodStation, float]],
    snapshot: Any,
) -> RichDoc:
    doc = RichDoc()
    doc.heading("Closest to you", level=2)

    if location is not None and location.today is not None:
        today = location.today
        temps = f", {today.temp_range}" if today.temp_range else ""
        doc.para(
            f"The nearest forecast area is {b(location.name)}. Today looks like "
            f"{today.condition.lower()}{temps}."
        )
    else:
        doc.para(
            "No forecast area could be matched to that position. The forecast "
            "feed covers towns and districts, so a remote spot may have no "
            "entry nearby."
        )

    if stations:
        doc.heading("River gauges near you", level=3)
        doc.table(
            ["Station", "Distance", "Status"],
            [
                [
                    st.name,
                    _distance(distance),
                    f"{mark(st.severity)} {st.indicator.title() or 'Unknown'}",
                ]
                for st, distance in stations
            ],
        )
    else:
        doc.para("There are no monitored river gauges within range of that position.")

    return _footer(doc, snapshot)


# ---------------------------------------------------------------------------
# Favourites and subscriptions
# ---------------------------------------------------------------------------


def favourites_doc(favourites: Sequence[Any]) -> RichDoc:
    doc = RichDoc()
    doc.heading("Your favourites", level=2)

    if not favourites:
        doc.para(
            "You have not saved anything yet. Look up a town with /weather or "
            "a river gauge with /flood, then use the Save button on the result."
        )
        return doc

    towns = [f for f in favourites if f["kind"] == "location"]
    gauges = [f for f in favourites if f["kind"] == "station"]

    if towns:
        doc.heading("Forecast areas", level=3)
        doc.bullets([b(f["label"]) for f in towns])
    if gauges:
        doc.heading("River gauges", level=3)
        doc.bullets(
            [f"{b(f['label'])}{(', ' + f['detail']) if f['detail'] else ''}" for f in gauges]
        )

    doc.para("Choose one below to open it, or use /unfav to remove one.")
    return doc


SUBSCRIPTION_LABELS = {
    "warning": "Weather warnings for your saved areas",
    "flood": "River levels at your saved gauges",
    "quake": "Earthquakes above your magnitude threshold",
    "digest": "A morning digest of your favourites",
    "national_flood": "Any station nationwide reaching danger level",
    "national_quake": "Strong earthquakes anywhere, magnitude 6 and above",
}


def subscriptions_doc(subs: Sequence[Any], user: Any) -> RichDoc:
    doc = RichDoc()
    doc.heading("Your alerts", level=2)

    active = {row["kind"] for row in subs}

    if not active:
        doc.para(
            "You have no alerts switched on. Use the buttons below to choose "
            "what the bot should tell you about."
        )
    else:
        doc.para("The bot will message you about the following.")
        doc.bullets(
            [
                SUBSCRIPTION_LABELS.get(kind, kind)
                for kind in SUBSCRIPTION_LABELS
                if kind in active
            ]
        )

    doc.heading("Current thresholds", level=3)
    doc.table(
        ["Setting", "Value"],
        [
            ["Earthquake magnitude", f"{float(user['quake_threshold']):g} and above"],
            ["River level", f"{str(user['flood_threshold']).title()} and above"],
            ["Digest time", str(user["digest_time"])],
            [
                "Quiet hours",
                f"{user['quiet_from']} to {user['quiet_to']}"
                if int(user["quiet_enabled"])
                else "Off",
            ],
        ],
    )
    doc.para(
        "Quiet hours hold back routine alerts. A station at danger level still "
        "comes through, because that is the kind of thing you would want "
        "waking up for."
    )
    return doc


def settings_doc(user: Any) -> RichDoc:
    doc = RichDoc()
    doc.heading("Settings", level=2)
    doc.para("These preferences apply to everything the bot sends you.")

    doc.table(
        ["Setting", "Value"],
        [
            ["Clock", "24 hour" if user["time_format"] == "24h" else "12 hour"],
            [
                "Quiet hours",
                f"{user['quiet_from']} to {user['quiet_to']}"
                if int(user["quiet_enabled"])
                else "Off",
            ],
            ["Digest time", str(user["digest_time"])],
            ["Quake threshold", f"M{float(user['quake_threshold']):g}"],
            ["Flood threshold", str(user["flood_threshold"]).title()],
            ["Home area", str(user["home_location"]) or "Not set"],
        ],
    )
    doc.para("Use the buttons below to change any of these.")
    return doc


# ---------------------------------------------------------------------------
# Scheduled and alert messages
# ---------------------------------------------------------------------------


def digest_doc(
    forecasts: Sequence[tuple[str, ForecastDay]],
    warnings: Sequence[Warning],
    elevated: Sequence[FloodStation],
) -> RichDoc:
    doc = RichDoc()
    doc.heading("Your morning briefing", level=2)
    doc.para(i(now_myt().strftime("%A, %d %B %Y")))

    if forecasts:
        doc.heading("Today where you are watching", level=3)
        doc.table(
            ["Area", "Outlook", "Temperature"],
            [
                [name, day.condition, day.temp_range or "No reading"]
                for name, day in forecasts
            ],
        )
        wet = [name for name, day in forecasts if day.is_wet]
        if wet:
            doc.para("Rain is expected around " + _join(wet) + ", so take an umbrella.")

    if warnings:
        doc.heading("Warnings in force", level=3)
        for warning in warnings[:5]:
            doc.para(f"{mark(warning.severity)} {b(warning.title)}")
            doc.para(warning.text)

    if elevated:
        doc.heading("River gauges to watch", level=3)
        doc.table(
            ["Station", "Level", "Status"],
            [
                [st.name, st.level_text, f"{mark(st.severity)} {st.indicator.title()}"]
                for st in elevated[:8]
            ],
        )

    if not forecasts and not warnings and not elevated:
        doc.para(
            "Nothing needs your attention this morning. No warnings are in "
            "force and your saved gauges are all reporting normal levels."
        )

    return doc


def warning_alert_doc(warning: Warning, place: str = "") -> RichDoc:
    doc = RichDoc()
    doc.heading("Weather warning", level=2)
    scope = f" affecting {place}" if place else ""
    doc.para(f"{mark(warning.severity)} {b(warning.title)}{scope}")
    doc.para(warning.text)
    if warning.valid_to:
        doc.para(i(f"In force until {format_datetime(warning.valid_to)}."))
    if warning.instruction:
        doc.quote(warning.instruction)
    return doc


def flood_alert_doc(station: FloodStation, previous: str = "") -> RichDoc:
    doc = RichDoc()
    doc.heading("River level rising", level=2)
    doc.para(
        f"{mark(station.severity)} {b(station.name)} at {station.place} is now "
        f"at {b(station.indicator.title())} level."
    )

    rows = [["Current level", station.level_text]]
    if previous and previous != station.indicator:
        rows.append(["Previously", previous.title()])
    if station.trend_text:
        rows.append(["Trend", station.trend_text.title()])
    if station.danger_level is not None:
        rows.append(["Danger at", f"{station.danger_level:g} m"])
    if station.updated_at:
        rows.append(["Reading taken", format_datetime(station.updated_at)])
    doc.table(["Detail", "Value"], rows)

    if station.severity == "danger":
        doc.quote(
            "This gauge has reached danger level. Move valuables up, avoid "
            "river crossings and follow the instructions of the local "
            "authorities."
        )
    else:
        doc.quote(
            "Keep an eye on the water and be ready to move if the level keeps "
            "climbing."
        )
    return doc


def quake_alert_doc(quake: Quake) -> RichDoc:
    doc = RichDoc()
    doc.heading("Earthquake reported", level=2)
    doc.para(
        f"{mark(quake.severity)} {b(quake.magnitude_text)} near "
        f"{b(quake.location)}, {ago(quake.when)}."
    )

    rows = [["When", format_datetime(quake.when)], ["Magnitude", quake.magnitude_text]]
    if quake.depth is not None:
        rows.append(["Depth", f"{quake.depth:g} km"])
    if quake.distance:
        rows.append(["Distance", quake.distance])
    doc.table(["Detail", "Value"], rows)

    if quake.severity == "high":
        doc.quote(
            "A quake of this size can be felt far from its centre and may "
            "prompt official advisories. Follow the local authorities for "
            "guidance."
        )
    return doc


def feed_health_doc(stale: Sequence[tuple[str, str]]) -> RichDoc:
    doc = RichDoc()
    doc.heading("Data feed trouble", level=3)
    doc.para(
        "The bot has not been able to refresh the following feeds for some "
        "time, so readings may be out of date."
    )
    doc.table(
        ["Feed", "Reason"],
        [[name.title(), reason] for name, reason in stale],
    )
    doc.para("Alerts resume automatically once data.gov.my responds again.")
    return doc


def stats_doc(stats: dict[str, Any]) -> RichDoc:
    doc = RichDoc()
    doc.heading("Bot statistics", level=2)
    doc.table(
        ["Metric", "Value"],
        [[key, str(value)] for key, value in stats.items()],
    )
    return doc


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _join(items: Sequence[str]) -> str:
    """Join names into readable prose rather than a comma soup."""

    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _distance(metres: float) -> str:
    if metres < 1000:
        return f"{int(round(metres / 10) * 10)} m"
    return f"{metres / 1000:.1f} km"


def error_doc(message: str, detail: str = "") -> RichDoc:
    doc = RichDoc()
    doc.heading("That did not work", level=3)
    doc.para(message)
    if detail:
        doc.para(i(detail))
    doc.para("Use the buttons below to go back and try again.")
    return doc


__all__ = [
    "start_doc",
    "about_doc",
    "forecast_doc",
    "location_choice_doc",
    "no_match_doc",
    "warnings_doc",
    "quakes_doc",
    "quake_detail_doc",
    "flood_overview_doc",
    "station_doc",
    "station_list_doc",
    "nearby_doc",
    "favourites_doc",
    "subscriptions_doc",
    "settings_doc",
    "digest_doc",
    "warning_alert_doc",
    "flood_alert_doc",
    "quake_alert_doc",
    "feed_health_doc",
    "stats_doc",
    "error_doc",
    "SUBSCRIPTION_LABELS",
    "mark",
]
