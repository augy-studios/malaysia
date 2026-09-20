"""data.gov.my GTFS access for Malaysian rail.

The bot talks to api.data.gov.my directly. It never calls the website's
serverless proxies, so the parsing those handlers do is reproduced here:

  * Static feeds arrive as a GTFS zip. Python has `zipfile` and `csv` in the
    standard library, so this is considerably shorter than the JS version.
  * The KTMB realtime feed arrives as a raw GTFS-realtime protobuf
    FeedMessage. Rather than add a protobuf dependency, a minimal wire-format
    reader is implemented below. Field numbers match gtfs-realtime.proto.

Two things about the rail feeds differ from the bus feeds and drive the design
of this module.

Frequencies
    Rapid KL rail publishes only 48 template trips and a `frequencies.txt`
    giving the headway for each time band. Taken literally, a station would
    appear to see six trains a day. Expanding the headways turns one template
    into roughly 250 real departures, which is what a rider actually needs, so
    `_expand_frequencies` does that at parse time.

Service calendars
    Both feeds ship `calendar.txt`, and KTMB also ships `calendar_dates.txt`
    holding public holiday swaps. A departure is only shown when its service
    actually runs on the day in question, so a Sunday timetable is never
    presented on a Tuesday.

Static bundles are large and change rarely, so parsed feeds are held in memory
and the raw zip is cached in SQLite. When upstream fails, a stale cache entry
is used rather than showing the user nothing.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
import re
import struct
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

import httpx

from .timeutils import CALENDAR_DAY_COLUMNS, parse_gtfs_date, parse_gtfs_time, today_myt

log = logging.getLogger(__name__)

STATIC_PRASARANA = "https://api.data.gov.my/gtfs-static/prasarana?category={category}"
STATIC_KTMB = "https://api.data.gov.my/gtfs-static/ktmb"
REALTIME_KTMB = "https://api.data.gov.my/gtfs-realtime/vehicle-position/ktmb"

RAPID_RAIL_KL = "rapid-rail-kl"
KTMB = "ktmb"
ALL_OPERATORS = (RAPID_RAIL_KL, KTMB)

# Only KTMB broadcasts vehicle positions. Rapid KL rail publishes a schedule
# but no realtime feed, so the bot says so plainly rather than showing an
# operator picker where one choice is always empty.
LIVE_OPERATORS = (KTMB,)

OPERATOR_LABELS = {
    RAPID_RAIL_KL: "Rapid KL Rail",
    KTMB: "KTMB",
}

# Expanding every headway band across a whole service day is bounded so a
# pathological feed cannot blow up memory.
MAX_EXPANDED_PER_TRIP = 400


def operator_label(operator: str) -> str:
    return OPERATOR_LABELS.get(operator, operator)


def has_live(operator: str) -> bool:
    return operator in LIVE_OPERATORS


# ---------------------------------------------------------------------------
# Data holders
# ---------------------------------------------------------------------------


@dataclass
class Station:
    stop_id: str
    stop_name: str
    lat: float | None
    lon: float | None
    category: str = ""

    @property
    def display(self) -> str:
        """Station names arrive shouted in both feeds, so soften them."""

        return _title_case(self.stop_name)


@dataclass
class Line:
    route_id: str
    short_name: str
    long_name: str
    category: str = ""
    colour: str = ""

    @property
    def display(self) -> str:
        if self.long_name:
            return self.long_name
        return self.short_name or self.route_id

    @property
    def badge(self) -> str:
        return self.category or self.short_name or ""


@dataclass
class Trip:
    trip_id: str
    route_id: str
    headsign: str
    service_id: str = ""
    direction_id: str = ""


@dataclass
class StopTime:
    stop_id: str
    sequence: int
    arrival: str
    departure: str

    @property
    def best_time(self) -> str:
        return self.departure or self.arrival


@dataclass
class Service:
    """One calendar.txt row plus its calendar_dates.txt exceptions."""

    service_id: str
    days: set[int] = field(default_factory=set)
    start: date | None = None
    end: date | None = None
    added: set[date] = field(default_factory=set)
    removed: set[date] = field(default_factory=set)

    def runs_on(self, day: date) -> bool:
        if day in self.removed:
            return False
        if day in self.added:
            return True
        if self.start and day < self.start:
            return False
        if self.end and day > self.end:
            return False
        return day.weekday() in self.days


@dataclass
class Feed:
    """One operator's parsed static GTFS bundle."""

    operator: str
    fetched_at: float = 0.0
    stops: dict[str, Station] = field(default_factory=dict)
    routes: dict[str, Line] = field(default_factory=dict)
    trips: dict[str, Trip] = field(default_factory=dict)
    trip_stops: dict[str, list[StopTime]] = field(default_factory=dict)
    route_stops: dict[str, set[str]] = field(default_factory=dict)
    stop_routes: dict[str, set[str]] = field(default_factory=dict)
    services: dict[str, Service] = field(default_factory=dict)
    # stop_id -> route_id -> sorted list of (HH:MM:SS, service_id)
    stop_schedule: dict[str, dict[str, list[tuple[str, str]]]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.stops and not self.routes

    # -- service-aware schedule access ------------------------------------

    def runs_today(self, service_id: str, day: date | None = None) -> bool:
        """True when this service operates on the given day.

        An unknown service is treated as running. A feed that omits a calendar
        entry should not cause its trains to vanish from the timetable.
        """

        service = self.services.get(service_id)
        if service is None:
            return True
        return service.runs_on(day or today_myt())

    def departures(
        self, stop_id: str, route_id: str, day: date | None = None
    ) -> list[str]:
        """Departure times at a station on one line, for services running that day."""

        day = day or today_myt()
        entries = self.stop_schedule.get(stop_id, {}).get(route_id, [])
        return [t for t, service_id in entries if self.runs_today(service_id, day)]

    def routes_at_stop(self, stop_id: str) -> list[Line]:
        ids = self.stop_routes.get(stop_id, set())
        lines = [self.routes[r] for r in ids if r in self.routes]
        lines.sort(key=lambda r: r.display)
        return lines

    # -- search -----------------------------------------------------------

    def search_stops(self, query: str, limit: int = 25) -> list[Station]:
        q = _normalise(query)
        if not q:
            return []
        exact: list[Station] = []
        starts: list[Station] = []
        contains: list[Station] = []
        for stop in self.stops.values():
            name = _normalise(stop.stop_name)
            if name == q or stop.stop_id.lower() == q:
                exact.append(stop)
            elif name.startswith(q):
                starts.append(stop)
            elif q in name:
                contains.append(stop)
        return (exact + starts + contains)[:limit]

    def search_routes(self, query: str, limit: int = 25) -> list[Line]:
        q = _normalise(query)
        if not q:
            return []
        exact: list[Line] = []
        starts: list[Line] = []
        contains: list[Line] = []
        for route in self.routes.values():
            short = _normalise(route.short_name)
            hay = _normalise(f"{route.short_name} {route.long_name} {route.route_id} {route.category}")
            if short == q or route.route_id.lower() == q:
                exact.append(route)
            elif short.startswith(q) or _normalise(route.long_name).startswith(q):
                starts.append(route)
            elif q in hay:
                contains.append(route)
        return (exact + starts + contains)[:limit]

    def stops_near(
        self, lat: float, lon: float, radius_m: int, limit: int = 12
    ) -> list[tuple[Station, float]]:
        out: list[tuple[Station, float]] = []
        for stop in self.stops.values():
            if stop.lat is None or stop.lon is None:
                continue
            distance = haversine_m(lat, lon, stop.lat, stop.lon)
            if distance <= radius_m:
                out.append((stop, distance))
        out.sort(key=lambda pair: pair[1])
        return out[:limit]

    def trips_for_route(self, route_id: str, day: date | None = None) -> list[Trip]:
        """Template trips on a line, limited to services running that day."""

        day = day or today_myt()
        out = [
            t
            for t in self.trips.values()
            if t.route_id == route_id and self.runs_today(t.service_id, day)
        ]

        def first_time(trip: Trip) -> str:
            times = self.trip_stops.get(trip.trip_id) or []
            return times[0].best_time if times else "99:99:99"

        out.sort(key=first_time)
        return out


def _normalise(value: str) -> str:
    """Lowercase and collapse whitespace so 'KL  Sentral' matches 'kl sentral'."""

    return " ".join((value or "").lower().split())


# Runs of whitespace and punctuation that separate one name part from the
# next. Kept verbatim when re-joining so spacing and hyphenation survive.
_SEPARATOR_RE = re.compile(r"([\s/\-,()]+)")

_KEEP_UPPER = {
    "KL", "KLCC", "KTM", "LRT", "MRT", "BRT", "ETS", "TBS", "UKM", "UM", "USJ",
    "PWTC", "IOI", "MRR2", "AU2", "SS15", "SS18", "USJ7", "USJ21", "KLIA",
    "KLIA2", "UiTM", "HUKM", "IPOH", "JB",
}


def _title_case(name: str) -> str:
    """Turn 'BANDAR TASIK SELATAN' into 'Bandar Tasik Selatan'.

    Acronyms that would read badly in title case are preserved. Names that are
    already mixed case are left alone, since the feed clearly meant them.
    """

    raw = (name or "").strip()
    if not raw or raw != raw.upper():
        return raw

    def cap_token(token: str) -> str:
        if not token or not token[0].isalpha():
            return token
        if token in _KEEP_UPPER or (len(token) <= 2 and token.isalpha()):
            return token
        if any(ch.isdigit() for ch in token):
            return token
        return token.capitalize()

    # Names like "PULAU SEBANG/TAMPIN" and "JALAN TEMPLER-TAMAN" carry a second
    # name after a separator, which needs capitalising too. Splitting on all
    # separators at once keeps each part intact; capitalising them one
    # separator at a time would re-lowercase what the previous pass fixed.
    return "".join(
        part if _SEPARATOR_RE.fullmatch(part) else cap_token(part)
        for part in _SEPARATOR_RE.split(raw)
    )


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""

    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Static feed parsing
# ---------------------------------------------------------------------------


def _read_csv(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    try:
        raw = zf.read(name)
    except KeyError:
        return []
    text = raw.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def _float_or_none(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _seconds_to_gtfs(total: int) -> str:
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _parse_services(zf: zipfile.ZipFile) -> dict[str, Service]:
    """Build the service calendar from calendar.txt and calendar_dates.txt."""

    services: dict[str, Service] = {}

    for row in _read_csv(zf, "calendar.txt"):
        service_id = (row.get("service_id") or "").strip()
        if not service_id:
            continue
        days = {
            index
            for index, column in enumerate(CALENDAR_DAY_COLUMNS)
            if (row.get(column) or "").strip() == "1"
        }
        services[service_id] = Service(
            service_id=service_id,
            days=days,
            start=parse_gtfs_date(row.get("start_date", "")),
            end=parse_gtfs_date(row.get("end_date", "")),
        )

    # exception_type 1 adds a date, 2 removes one. Public holidays in the KTMB
    # feed are expressed this way, swapping a weekday service for a weekend one.
    for row in _read_csv(zf, "calendar_dates.txt"):
        service_id = (row.get("service_id") or "").strip()
        day = parse_gtfs_date(row.get("date", ""))
        if not service_id or day is None:
            continue
        service = services.setdefault(service_id, Service(service_id=service_id))
        if (row.get("exception_type") or "").strip() == "2":
            service.removed.add(day)
        else:
            service.added.add(day)

    return services


def _expand_frequencies(
    zf: zipfile.ZipFile, trip_stops: dict[str, list[StopTime]]
) -> dict[str, list[list[StopTime]]]:
    """Turn headway bands into concrete departures.

    Rapid KL publishes one template trip per line and direction plus a set of
    `start_time`/`end_time`/`headway_secs` bands. Each band is walked at its
    headway, and the template's stop times are shifted so every run gets real
    times. Without this the bot would report six trains a day on a line that
    runs every three minutes in the peak.

    Returns trip_id -> list of runs, each run being a shifted copy of the
    template's stop times.
    """

    rows = _read_csv(zf, "frequencies.txt")
    if not rows:
        return {}

    expanded: dict[str, list[list[StopTime]]] = {}

    for row in rows:
        trip_id = (row.get("trip_id") or "").strip()
        template = trip_stops.get(trip_id)
        if not trip_id or not template:
            continue

        start = parse_gtfs_time(row.get("start_time", ""))
        end = parse_gtfs_time(row.get("end_time", ""))
        try:
            headway = int(row.get("headway_secs") or 0)
        except ValueError:
            headway = 0

        if start is None or end is None or headway <= 0 or end < start:
            continue

        base = parse_gtfs_time(template[0].best_time)
        if base is None:
            continue

        runs = expanded.setdefault(trip_id, [])
        moment = start
        while moment < end and len(runs) < MAX_EXPANDED_PER_TRIP:
            offset = moment - base
            runs.append(
                [
                    StopTime(
                        stop_id=st.stop_id,
                        sequence=st.sequence,
                        arrival=_shift(st.arrival, offset),
                        departure=_shift(st.departure, offset),
                    )
                    for st in template
                ]
            )
            moment += headway

    return expanded


def _shift(value: str, offset: int) -> str:
    total = parse_gtfs_time(value)
    if total is None:
        return value
    return _seconds_to_gtfs(max(0, total + offset))


def parse_static_zip(operator: str, blob: bytes) -> Feed:
    """Parse a GTFS zip into a Feed. CPU bound, so callers run it in a thread."""

    feed = Feed(operator=operator, fetched_at=time.time())

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        feed.services = _parse_services(zf)

        for row in _read_csv(zf, "stops.txt"):
            stop_id = (row.get("stop_id") or "").strip()
            if not stop_id:
                continue
            # Rapid KL marks withdrawn stations with a status column.
            if (row.get("status") or "valid").strip().lower() not in ("", "valid"):
                continue
            feed.stops[stop_id] = Station(
                stop_id=stop_id,
                stop_name=(row.get("stop_name") or stop_id).strip(),
                lat=_float_or_none(row.get("stop_lat")),
                lon=_float_or_none(row.get("stop_lon")),
                category=(row.get("category") or "").strip(),
            )

        for row in _read_csv(zf, "routes.txt"):
            route_id = (row.get("route_id") or "").strip()
            if not route_id:
                continue
            if (row.get("status") or "valid").strip().lower() not in ("", "valid"):
                continue
            feed.routes[route_id] = Line(
                route_id=route_id,
                short_name=(row.get("route_short_name") or "").strip(),
                long_name=(row.get("route_long_name") or "").strip(),
                category=(row.get("category") or "").strip(),
                colour=(row.get("route_color") or "").strip(),
            )

        # trips.txt is the only reliable trip -> route join. Rapid KL's
        # stop_times.txt carries its own route_id column holding the short name
        # ("AGL") rather than the route_id ("AG"), so using it would silently
        # match nothing.
        trip_route: dict[str, str] = {}
        for row in _read_csv(zf, "trips.txt"):
            trip_id = (row.get("trip_id") or "").strip()
            route_id = (row.get("route_id") or "").strip()
            if not trip_id or not route_id:
                continue
            trip_route[trip_id] = route_id
            feed.trips[trip_id] = Trip(
                trip_id=trip_id,
                route_id=route_id,
                headsign=(row.get("trip_headsign") or "").strip(),
                service_id=(row.get("service_id") or "").strip(),
                direction_id=(row.get("direction_id") or "").strip(),
            )

        for row in _read_csv(zf, "stop_times.txt"):
            trip_id = (row.get("trip_id") or "").strip()
            stop_id = (row.get("stop_id") or "").strip()
            route_id = trip_route.get(trip_id)
            if not route_id or not stop_id or stop_id not in feed.stops:
                continue

            feed.route_stops.setdefault(route_id, set()).add(stop_id)
            feed.stop_routes.setdefault(stop_id, set()).add(route_id)

            try:
                sequence = int(row.get("stop_sequence") or 0)
            except ValueError:
                sequence = 0

            feed.trip_stops.setdefault(trip_id, []).append(
                StopTime(
                    stop_id=stop_id,
                    sequence=sequence,
                    arrival=(row.get("arrival_time") or "").strip(),
                    departure=(row.get("departure_time") or "").strip(),
                )
            )

        for times in feed.trip_stops.values():
            times.sort(key=lambda st: st.sequence)

        # Headway-based lines need expanding before the station timetable is
        # built, otherwise it would show only the template departures.
        expanded = _expand_frequencies(zf, feed.trip_stops)

        schedule_sets: dict[str, dict[str, set[tuple[str, str]]]] = {}
        for trip_id, template in feed.trip_stops.items():
            route_id = trip_route.get(trip_id)
            trip = feed.trips.get(trip_id)
            if not route_id or trip is None:
                continue
            service_id = trip.service_id
            runs = expanded.get(trip_id) or [template]
            for run in runs:
                for stop_time in run:
                    value = stop_time.best_time
                    if not value:
                        continue
                    # Feeds mix "6:00:00" and "06:00:00". Normalising here keeps
                    # the sort order correct and the rendering consistent.
                    total = parse_gtfs_time(value)
                    if total is None:
                        continue
                    schedule_sets.setdefault(stop_time.stop_id, {}).setdefault(
                        route_id, set()
                    ).add((_seconds_to_gtfs(total), service_id))

        feed.stop_schedule = {
            stop_id: {
                route_id: sorted(values, key=lambda pair: pair[0])
                for route_id, values in routes.items()
            }
            for stop_id, routes in schedule_sets.items()
        }

    # Drop trips with no stop times, matching the website's behaviour.
    feed.trips = {tid: t for tid, t in feed.trips.items() if feed.trip_stops.get(tid)}
    return feed


# ---------------------------------------------------------------------------
# GTFS-realtime protobuf decoding
# ---------------------------------------------------------------------------


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("Truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("Varint too long")


def _parse_fields(data: bytes, start: int = 0, end: int | None = None) -> list[tuple[int, int, Any]]:
    """Return [(field_number, wire_type, value)] for one protobuf message."""

    if end is None:
        end = len(data)
    fields: list[tuple[int, int, Any]] = []
    pos = start
    while pos < end:
        tag, pos = _read_varint(data, pos)
        field_num = tag >> 3
        wire = tag & 0x7
        if wire == 0:
            value, pos = _read_varint(data, pos)
            fields.append((field_num, wire, value))
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            fields.append((field_num, wire, data[pos : pos + length]))
            pos += length
        elif wire == 5:
            fields.append((field_num, wire, data[pos : pos + 4]))
            pos += 4
        elif wire == 1:
            fields.append((field_num, wire, data[pos : pos + 8]))
            pos += 8
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire}")
    return fields


def _first(fields: list[tuple[int, int, Any]], num: int) -> Any:
    for field_num, _wire, value in fields:
        if field_num == num:
            return value
    return None


def _as_str(raw: Any) -> str | None:
    if not isinstance(raw, (bytes, bytearray)):
        return None
    return bytes(raw).decode("utf-8", errors="replace")


def _as_float(raw: Any) -> float | None:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 4:
        return None
    return struct.unpack("<f", bytes(raw))[0]


@dataclass
class Vehicle:
    entity_id: str = ""
    route_id: str = ""
    trip_id: str = ""
    vehicle_id: str = ""
    label: str = ""
    lat: float | None = None
    lon: float | None = None
    bearing: float | None = None
    speed: float | None = None
    timestamp: int = 0

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def maps_url(self) -> str:
        return f"https://www.google.com/maps?q={self.lat},{self.lon}"

    @property
    def name(self) -> str:
        return self.label or self.vehicle_id or self.entity_id or "Unknown train"

    @property
    def speed_kmh(self) -> float | None:
        """Speed in km/h, guarding against the feed's inconsistent units.

        GTFS-realtime specifies metres per second, but the KTMB feed has been
        observed reporting km/h directly: converting those values yields
        figures over 200 km/h for a Komuter set. Values that are already
        plausible as km/h are therefore passed through, and only genuinely
        small ones are treated as m/s. The cutoff sits above the ~40 m/s
        (145 km/h) an ETS can reach and below the speeds that only appear when
        the units are already km/h.
        """

        if self.speed is None or self.speed <= 0:
            return None
        if self.speed > 45:
            return self.speed
        return self.speed * 3.6


def decode_vehicle_positions(blob: bytes) -> list[Vehicle]:
    """Decode a GTFS-realtime FeedMessage into vehicle positions."""

    vehicles: list[Vehicle] = []
    for field_num, wire, value in _parse_fields(blob):
        if field_num != 2 or wire != 2:
            continue  # only FeedEntity matters here
        entity_fields = _parse_fields(value)
        entity_id = _as_str(_first(entity_fields, 1)) or ""
        vehicle_raw = _first(entity_fields, 4)
        if not isinstance(vehicle_raw, (bytes, bytearray)):
            continue

        vp = _parse_fields(vehicle_raw)
        vehicle = Vehicle(entity_id=entity_id)

        trip_raw = _first(vp, 1)
        if isinstance(trip_raw, (bytes, bytearray)):
            trip_fields = _parse_fields(trip_raw)
            vehicle.trip_id = _as_str(_first(trip_fields, 1)) or ""
            vehicle.route_id = _as_str(_first(trip_fields, 5)) or ""

        pos_raw = _first(vp, 2)
        if isinstance(pos_raw, (bytes, bytearray)):
            pos_fields = _parse_fields(pos_raw)
            vehicle.lat = _as_float(_first(pos_fields, 1))
            vehicle.lon = _as_float(_first(pos_fields, 2))
            vehicle.bearing = _as_float(_first(pos_fields, 3))
            vehicle.speed = _as_float(_first(pos_fields, 5))

        ts = _first(vp, 5)
        if isinstance(ts, int):
            vehicle.timestamp = ts

        desc_raw = _first(vp, 8)
        if isinstance(desc_raw, (bytes, bytearray)):
            desc_fields = _parse_fields(desc_raw)
            vehicle.vehicle_id = _as_str(_first(desc_fields, 1)) or ""
            vehicle.label = _as_str(_first(desc_fields, 2)) or ""

        vehicles.append(vehicle)

    vehicles.sort(key=lambda v: v.timestamp, reverse=True)
    return vehicles


# ---------------------------------------------------------------------------
# Feed manager
# ---------------------------------------------------------------------------


class GTFSManager:
    """Fetches, caches and serves the rail feeds."""

    def __init__(self, db: Any, settings: Any) -> None:
        self._db = db
        self._settings = settings
        self._feeds: dict[str, Feed] = {}
        self._locks: dict[str, asyncio.Lock] = {
            operator: asyncio.Lock() for operator in ALL_OPERATORS
        }
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_seconds, connect=15.0),
            follow_redirects=True,
            headers={"User-Agent": "malaysia-trains-bot/1.0 (+https://malaysia.uwuapps.org)"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -- static -----------------------------------------------------------

    def _static_url(self, operator: str) -> str:
        if operator == KTMB:
            return STATIC_KTMB
        return STATIC_PRASARANA.format(category=operator)

    async def get_feed(self, operator: str, force: bool = False) -> Feed:
        """Return the parsed feed, fetching or reusing cache as needed."""

        if operator not in ALL_OPERATORS:
            raise ValueError(f"Unknown operator {operator!r}")

        max_age = self._settings.static_refresh_hours * 3600
        cached = self._feeds.get(operator)
        if cached and not force and time.time() - cached.fetched_at < max_age:
            return cached

        async with self._locks[operator]:
            cached = self._feeds.get(operator)
            if cached and not force and time.time() - cached.fetched_at < max_age:
                return cached

            cache_key = f"static:{operator}"
            blob: bytes | None = None

            if not force:
                blob = await self._db.cache_get(cache_key, max_age)

            if blob is None:
                try:
                    response = await self._client.get(self._static_url(operator))
                    response.raise_for_status()
                    blob = response.content
                    await self._db.cache_put(cache_key, blob)
                    await self._db.record_feed_ok(operator)
                except Exception as exc:  # noqa: BLE001 - upstream can fail many ways
                    log.warning("Static fetch failed for %s: %s", operator, exc)
                    await self._db.record_feed_fail(operator, str(exc))
                    stale = await self._db.cache_get_any_age(cache_key)
                    if stale is None:
                        if cached:
                            return cached
                        raise
                    blob, _fetched_at = stale

            feed = await asyncio.to_thread(parse_static_zip, operator, blob)
            self._feeds[operator] = feed
            return feed

    async def warm(self, operators: Iterable[str] | None = None) -> None:
        """Preload feeds so the first user command is not slow."""

        for operator in operators or ALL_OPERATORS:
            try:
                feed = await self.get_feed(operator)
                log.info(
                    "Loaded %s: %d stations, %d lines",
                    operator_label(operator),
                    len(feed.stops),
                    len(feed.routes),
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Could not preload %s: %s", operator, exc)

    # -- realtime ---------------------------------------------------------

    async def get_vehicles(self, operator: str = KTMB, max_age: int = 20) -> list[Vehicle]:
        """Return live train positions, cached briefly to spare upstream."""

        if not has_live(operator):
            return []

        cache_key = f"realtime:{operator}"
        blob = await self._db.cache_get(cache_key, max_age)

        if blob is None:
            try:
                response = await self._client.get(REALTIME_KTMB)
                response.raise_for_status()
                blob = response.content
                await self._db.cache_put(cache_key, blob)
                await self._db.record_feed_ok(operator)
            except Exception as exc:  # noqa: BLE001
                log.warning("Realtime fetch failed for %s: %s", operator, exc)
                await self._db.record_feed_fail(operator, str(exc))
                stale = await self._db.cache_get_any_age(cache_key)
                if stale is None:
                    return []
                blob, _ = stale

        try:
            return await asyncio.to_thread(decode_vehicle_positions, blob)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not decode realtime feed for %s: %s", operator, exc)
            return []

    # -- cross-operator helpers -------------------------------------------

    async def search_all_stops(self, query: str, limit: int = 8) -> list[tuple[str, Station]]:
        out: list[tuple[str, Station]] = []
        for operator in ALL_OPERATORS:
            try:
                feed = await self.get_feed(operator)
            except Exception:  # noqa: BLE001
                continue
            for stop in feed.search_stops(query, limit):
                out.append((operator, stop))
        return out[: limit * 2]

    async def search_all_routes(self, query: str, limit: int = 8) -> list[tuple[str, Line]]:
        out: list[tuple[str, Line]] = []
        for operator in ALL_OPERATORS:
            try:
                feed = await self.get_feed(operator)
            except Exception:  # noqa: BLE001
                continue
            for route in feed.search_routes(query, limit):
                out.append((operator, route))
        return out[: limit * 2]

    async def nearby_all(
        self, lat: float, lon: float, radius_m: int, limit: int = 12
    ) -> list[tuple[str, Station, float]]:
        out: list[tuple[str, Station, float]] = []
        for operator in ALL_OPERATORS:
            try:
                feed = await self.get_feed(operator)
            except Exception:  # noqa: BLE001
                continue
            for stop, distance in feed.stops_near(lat, lon, radius_m, limit):
                out.append((operator, stop, distance))
        out.sort(key=lambda row: row[2])
        return out[:limit]
