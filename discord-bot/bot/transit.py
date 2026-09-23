"""Rail and bus timetables and live positions from api.data.gov.my.

Six operators, read straight from the open GTFS feeds:

    rail   Rapid KL Rail (LRT, MRT, Monorail, BRT) and KTMB (Komuter, ETS, intercity)
    bus    Rapid Bus KL, Rapid Bus Penang, Rapid Bus MRT Feeder and myBAS Johor

Static feeds arrive as a GTFS zip, parsed with the standard library. Realtime
feeds arrive as a GTFS-realtime protobuf, read by a small wire-format decoder
below rather than a protobuf dependency.

Three quirks shape this module, and each would otherwise be a visible bug:

Frequencies
    Rapid KL Rail publishes 48 template trips plus `frequencies.txt` giving
    the headway for each time band. Read literally a station sees six trains
    a day. Each template is expanded into its real runs, about 250 of them,
    stored as offsets from the template so memory stays flat.

Service calendars
    `calendar.txt`, and KTMB's `calendar_dates.txt` holiday swaps, decide
    which trips run on a given day. Every departure shown is filtered by
    them, so a Sunday timetable never appears on a Tuesday.

The route join
    Rapid KL's `stop_times.txt` carries its own `route_id` column holding
    the route SHORT name ("AGL") rather than the id ("AG"). Joining on it
    matches nothing, so the only path from a stop time to a route used here
    is through `trips.txt`.

Parsed feeds live in memory and the raw zips in SQLite, so a restart does not
download everything again and an upstream outage falls back to the last copy.
"""

from __future__ import annotations

import asyncio
import bisect
import csv
import io
import logging
import math
import re
import struct
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import httpx

from . import config
from .timeutils import (
    CALENDAR_DAY_COLUMNS,
    MYT,
    parse_gtfs_date,
    parse_gtfs_time,
    service_epoch,
)
from .weather import normalise

log = logging.getLogger(__name__)

_STATIC_PRASARANA = "https://api.data.gov.my/gtfs-static/prasarana?category={}"
_REALTIME_PRASARANA = "https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana/?category={}"


@dataclass(frozen=True)
class Operator:
    key: str
    label: str
    mode: str  # "rail" or "bus"
    static_url: str
    realtime_url: str = ""

    @property
    def has_live(self) -> bool:
        return bool(self.realtime_url)


OPERATORS: dict[str, Operator] = {
    op.key: op
    for op in (
        Operator("rapid-rail-kl", "Rapid KL Rail", "rail", _STATIC_PRASARANA.format("rapid-rail-kl")),
        Operator(
            "ktmb",
            "KTMB",
            "rail",
            "https://api.data.gov.my/gtfs-static/ktmb",
            "https://api.data.gov.my/gtfs-realtime/vehicle-position/ktmb",
        ),
        Operator(
            "rapid-bus-kl",
            "Rapid Bus KL",
            "bus",
            _STATIC_PRASARANA.format("rapid-bus-kl"),
            _REALTIME_PRASARANA.format("rapid-bus-kl"),
        ),
        Operator(
            "rapid-bus-penang",
            "Rapid Bus Penang",
            "bus",
            _STATIC_PRASARANA.format("rapid-bus-penang"),
            _REALTIME_PRASARANA.format("rapid-bus-penang"),
        ),
        Operator(
            "rapid-bus-mrtfeeder",
            "Rapid Bus MRT Feeder",
            "bus",
            _STATIC_PRASARANA.format("rapid-bus-mrtfeeder"),
            _REALTIME_PRASARANA.format("rapid-bus-mrtfeeder"),
        ),
        Operator(
            "mybas-johor",
            "myBAS Johor",
            "bus",
            "https://api.data.gov.my/gtfs-static/mybas-johor",
            "https://api.data.gov.my/gtfs-realtime/vehicle-position/mybas-johor",
        ),
    )
}

RAIL = tuple(k for k, op in OPERATORS.items() if op.mode == "rail")
BUS = tuple(k for k, op in OPERATORS.items() if op.mode == "bus")

# A pathological headway band cannot blow up memory.
MAX_RUNS_PER_TRIP = 400


def operator_label(key: str) -> str:
    op = OPERATORS.get(key)
    return op.label if op else key


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_SEPARATOR_RE = re.compile(r"([\s/\-,()]+)")
_FROM_TO_RE = re.compile(r"^\s*from\s+.+?\s+to\s+(.+?)\s*$", re.IGNORECASE)

_KEEP_UPPER = {
    "KL", "KLCC", "KTM", "LRT", "MRT", "BRT", "ETS", "TBS", "UKM", "UM", "USJ",
    "PWTC", "IOI", "MRR2", "AU2", "KLIA", "KLIA2", "UITM", "HUKM", "JB", "PJ",
    "CIQ", "JKR", "KPJ", "LRT3", "PPR", "SMK", "SK", "UTM", "USM", "BSI", "TNB",
}


def title_case(name: str) -> str:
    """'BANDAR TASIK SELATAN' as 'Bandar Tasik Selatan'.

    Acronyms that read badly in title case are kept, and names already in
    mixed case are left alone since the feed clearly meant them.
    """

    raw = (name or "").strip()
    if not raw or raw != raw.upper():
        return raw

    def cap(token: str) -> str:
        if not token or not token[0].isalpha():
            return token
        if token in _KEEP_UPPER or (len(token) <= 2 and token.isalpha()):
            return token
        if any(ch.isdigit() for ch in token):
            return token
        return token.capitalize()

    # Splitting on every separator at once keeps names like
    # "PULAU SEBANG/TAMPIN" right on both sides of the slash.
    return "".join(
        part if _SEPARATOR_RE.fullmatch(part) else cap(part)
        for part in _SEPARATOR_RE.split(raw)
    )


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Stop:
    stop_id: str
    name: str
    lat: float | None
    lon: float | None

    @property
    def maps_url(self) -> str:
        if self.lat is None or self.lon is None:
            return ""
        return f"https://www.google.com/maps?q={self.lat},{self.lon}"


@dataclass(slots=True)
class Route:
    route_id: str
    short_name: str
    long_name: str
    category: str = ""
    colour: str = ""

    @property
    def display(self) -> str:
        long_name = title_case(self.long_name)
        if self.short_name and long_name and self.short_name not in long_name:
            return f"{self.short_name} {long_name}"
        return long_name or self.short_name or self.route_id

    @property
    def colour_int(self) -> int | None:
        try:
            return int(self.colour.lstrip("#"), 16) if self.colour else None
        except ValueError:
            return None


@dataclass(slots=True)
class Trip:
    trip_id: str
    route_id: str
    headsign: str
    service_id: str
    direction_id: str


@dataclass(slots=True)
class Service:
    """One calendar.txt row plus its calendar_dates.txt exceptions."""

    days: set[int] = field(default_factory=set)
    start: date | None = None
    end: date | None = None
    added: set[date] = field(default_factory=set)
    removed: set[date] = field(default_factory=set)
    # A service known only from calendar_dates.txt runs on its added dates
    # alone. One from calendar.txt runs on its weekdays too.
    from_calendar: bool = False

    def runs_on(self, day: date) -> bool:
        if day in self.removed:
            return False
        if day in self.added:
            return True
        if not self.from_calendar:
            return False
        if self.start and day < self.start:
            return False
        if self.end and day > self.end:
            return False
        return day.weekday() in self.days


@dataclass(slots=True)
class Departure:
    epoch: int
    route_id: str
    trip_id: str
    offset: int
    headsign: str
    service_day: date


@dataclass
class Feed:
    """One operator's parsed static GTFS bundle."""

    operator: str
    fetched_at: float = 0.0
    stops: dict[str, Stop] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)
    trips: dict[str, Trip] = field(default_factory=dict)
    services: dict[str, Service] = field(default_factory=dict)
    # trip_id -> [(stop_id, seconds)] in stop order.
    trip_stops: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    # trip_id -> run offsets in seconds, only for headway-based trips.
    runs: dict[str, list[int]] = field(default_factory=dict)
    route_trips: dict[str, list[str]] = field(default_factory=dict)
    stop_routes: dict[str, set[str]] = field(default_factory=dict)
    # stop_id -> route_id -> sorted [(seconds, trip_id, offset)].
    schedule: dict[str, dict[str, list[tuple[int, str, int]]]] = field(default_factory=dict)

    @property
    def mode(self) -> str:
        return OPERATORS[self.operator].mode

    def runs_on(self, trip: Trip, day: date) -> bool:
        """True when the trip's service operates on `day`.

        An unknown service counts as running, so a feed that omits a calendar
        entry does not make its trips vanish.
        """

        service = self.services.get(trip.service_id)
        return True if service is None else service.runs_on(day)

    def headsign(self, trip: Trip) -> str:
        if trip.headsign:
            # Rapid KL writes "From Ampang to Sentul Timur", and only the
            # destination belongs after "to" in a departure line.
            match = _FROM_TO_RE.match(trip.headsign)
            return title_case(match.group(1) if match else trip.headsign)
        times = self.trip_stops.get(trip.trip_id)
        if times:
            last = self.stops.get(times[-1][0])
            if last is not None:
                return title_case(last.name)
        return "Unknown destination"

    def routes_at(self, stop_id: str) -> list[Route]:
        routes = [self.routes[r] for r in self.stop_routes.get(stop_id, ()) if r in self.routes]
        routes.sort(key=lambda r: (r.short_name or r.route_id, r.long_name))
        return routes

    # -- departures -------------------------------------------------------

    def departures(
        self,
        stop_id: str,
        route_id: str | None = None,
        now: float | None = None,
        limit: int = 10,
        horizon_hours: int = 24,
    ) -> list[Departure]:
        """The next departures from a stop, across midnight in both directions.

        Yesterday's service day is included because a GTFS time of 25:10
        belongs to it, and tomorrow's so a late evening query still has an
        answer. Arrivals at the end of the line are left out, since a train
        terminating here is not one anyone can board.
        """

        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now, MYT).date()
        by_route = self.schedule.get(stop_id, {})
        route_ids = [route_id] if route_id else list(by_route)
        horizon = now + horizon_hours * 3600
        out: list[Departure] = []

        for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
            base = service_epoch(day, 0)
            earliest = int(now - base) - 60
            for rid in route_ids:
                entries = by_route.get(rid)
                if not entries:
                    continue
                start = bisect.bisect_left(entries, (earliest,))
                for seconds, trip_id, offset in entries[start:]:
                    epoch = base + seconds
                    if epoch > horizon:
                        break
                    trip = self.trips.get(trip_id)
                    if trip is None or not self.runs_on(trip, day):
                        continue
                    stops = self.trip_stops.get(trip_id)
                    if stops and stops[-1][0] == stop_id:
                        continue
                    out.append(
                        Departure(epoch, rid, trip_id, offset, self.headsign(trip), day)
                    )

        out.sort(key=lambda d: d.epoch)
        return out[:limit]

    def upcoming_runs(
        self, route_id: str, now: float | None = None, limit: int = 25
    ) -> list[Departure]:
        """The next trips along one line, by when they leave their first stop."""

        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now, MYT).date()
        out: list[Departure] = []
        for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
            base = service_epoch(day, 0)
            for trip_id in self.route_trips.get(route_id, ()):
                trip = self.trips[trip_id]
                stops = self.trip_stops.get(trip_id)
                if not stops or not self.runs_on(trip, day):
                    continue
                first, last = stops[0][1], stops[-1][1]
                for offset in self.runs.get(trip_id, (0,)):
                    # Include trips already under way, so "follow a train"
                    # can pick up one that has left but not arrived.
                    if base + last + offset < now:
                        continue
                    epoch = base + first + offset
                    if epoch > now + 24 * 3600:
                        continue
                    out.append(
                        Departure(epoch, route_id, trip_id, offset, self.headsign(trip), day)
                    )
        out.sort(key=lambda d: d.epoch)
        return out[:limit]

    def trip_calls(self, trip_id: str, offset: int, day: date) -> list[tuple[Stop, int]]:
        """Every stop a run calls at, with the epoch of each call."""

        base = service_epoch(day, 0)
        out: list[tuple[Stop, int]] = []
        for stop_id, seconds in self.trip_stops.get(trip_id, ()):
            stop = self.stops.get(stop_id)
            if stop is not None:
                out.append((stop, base + seconds + offset))
        return out

    def route_stops(self, route_id: str) -> list[Stop]:
        """Stops along a line in order, taken from its longest trip."""

        best: list[tuple[str, int]] = []
        for trip_id in self.route_trips.get(route_id, ()):
            stops = self.trip_stops.get(trip_id, [])
            if len(stops) > len(best):
                best = stops
        return [self.stops[s] for s, _ in best if s in self.stops]

    # -- search -----------------------------------------------------------

    def search_stops(self, query: str, limit: int = 25) -> list[Stop]:
        q = normalise(query)
        if not q:
            return []
        exact: list[Stop] = []
        starts: list[Stop] = []
        contains: list[Stop] = []
        for stop in self.stops.values():
            name = normalise(stop.name)
            if name == q or stop.stop_id.lower() == q:
                exact.append(stop)
            elif name.startswith(q):
                starts.append(stop)
            elif q in name:
                contains.append(stop)
        return (exact + starts + contains)[:limit]

    def search_routes(self, query: str, limit: int = 25) -> list[Route]:
        q = normalise(query)
        routes = sorted(self.routes.values(), key=lambda r: (r.short_name or r.route_id))
        if not q:
            return routes[:limit]
        exact: list[Route] = []
        starts: list[Route] = []
        contains: list[Route] = []
        for route in routes:
            short = normalise(route.short_name)
            if short == q or route.route_id.lower() == q:
                exact.append(route)
            elif short.startswith(q) or normalise(route.long_name).startswith(q):
                starts.append(route)
            elif q in normalise(f"{route.short_name} {route.long_name} {route.category}"):
                contains.append(route)
        return (exact + starts + contains)[:limit]


# ---------------------------------------------------------------------------
# Static feed parsing
# ---------------------------------------------------------------------------


def _read_csv(zf: zipfile.ZipFile, name: str) -> Iterable[dict[str, str]]:
    try:
        raw = zf.read(name)
    except KeyError:
        return []
    return csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _valid(row: dict[str, str]) -> bool:
    # Rapid KL marks withdrawn stations and routes with a status column.
    return (row.get("status") or "valid").strip().lower() in ("", "valid")


def _parse_services(zf: zipfile.ZipFile) -> dict[str, Service]:
    services: dict[str, Service] = {}
    for row in _read_csv(zf, "calendar.txt"):
        service_id = (row.get("service_id") or "").strip()
        if not service_id:
            continue
        services[service_id] = Service(
            days={
                index
                for index, column in enumerate(CALENDAR_DAY_COLUMNS)
                if (row.get(column) or "").strip() == "1"
            },
            start=parse_gtfs_date(row.get("start_date", "")),
            end=parse_gtfs_date(row.get("end_date", "")),
            from_calendar=True,
        )
    # exception_type 1 adds a date and 2 removes one. KTMB expresses public
    # holidays this way, swapping a weekday service for a weekend one.
    for row in _read_csv(zf, "calendar_dates.txt"):
        service_id = (row.get("service_id") or "").strip()
        day = parse_gtfs_date(row.get("date", ""))
        if not service_id or day is None:
            continue
        service = services.setdefault(service_id, Service())
        if (row.get("exception_type") or "").strip() == "2":
            service.removed.add(day)
        else:
            service.added.add(day)
    return services


def _parse_frequencies(
    zf: zipfile.ZipFile, trip_stops: dict[str, list[tuple[str, int]]]
) -> dict[str, list[int]]:
    """Headway bands as run offsets from each template trip."""

    runs: dict[str, list[int]] = {}
    for row in _read_csv(zf, "frequencies.txt"):
        trip_id = (row.get("trip_id") or "").strip()
        template = trip_stops.get(trip_id)
        if not template:
            continue
        start = parse_gtfs_time(row.get("start_time", ""))
        end = parse_gtfs_time(row.get("end_time", ""))
        try:
            headway = int(row.get("headway_secs") or 0)
        except ValueError:
            headway = 0
        if start is None or end is None or headway <= 0 or end < start:
            continue
        offsets = runs.setdefault(trip_id, [])
        moment = start
        while moment < end and len(offsets) < MAX_RUNS_PER_TRIP:
            offsets.append(moment - template[0][1])
            moment += headway
    for offsets in runs.values():
        offsets.sort()
    return runs


def parse_static_zip(operator: str, blob: bytes) -> Feed:
    """Parse a GTFS zip. CPU bound, so callers run it in a thread."""

    feed = Feed(operator=operator, fetched_at=time.time())
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        feed.services = _parse_services(zf)

        for row in _read_csv(zf, "stops.txt"):
            stop_id = (row.get("stop_id") or "").strip()
            if stop_id and _valid(row):
                feed.stops[stop_id] = Stop(
                    stop_id=stop_id,
                    name=title_case((row.get("stop_name") or stop_id).strip()),
                    lat=_float(row.get("stop_lat")),
                    lon=_float(row.get("stop_lon")),
                )

        for row in _read_csv(zf, "routes.txt"):
            route_id = (row.get("route_id") or "").strip()
            if route_id and _valid(row):
                feed.routes[route_id] = Route(
                    route_id=route_id,
                    short_name=(row.get("route_short_name") or "").strip(),
                    long_name=(row.get("route_long_name") or "").strip(),
                    category=(row.get("category") or "").strip(),
                    colour=(row.get("route_color") or "").strip(),
                )

        # trips.txt is the only reliable trip to route join. See the module
        # docstring for why stop_times.txt's own route_id is ignored.
        for row in _read_csv(zf, "trips.txt"):
            trip_id = (row.get("trip_id") or "").strip()
            route_id = (row.get("route_id") or "").strip()
            if trip_id and route_id in feed.routes:
                feed.trips[trip_id] = Trip(
                    trip_id=trip_id,
                    route_id=route_id,
                    headsign=(row.get("trip_headsign") or "").strip(),
                    service_id=(row.get("service_id") or "").strip(),
                    direction_id=(row.get("direction_id") or "").strip(),
                )

        sequenced: dict[str, list[tuple[int, str, int]]] = {}
        for row in _read_csv(zf, "stop_times.txt"):
            trip = feed.trips.get((row.get("trip_id") or "").strip())
            stop_id = (row.get("stop_id") or "").strip()
            if trip is None or stop_id not in feed.stops:
                continue
            seconds = parse_gtfs_time(
                (row.get("departure_time") or "").strip()
                or (row.get("arrival_time") or "").strip()
            )
            if seconds is None:
                continue
            try:
                sequence = int(row.get("stop_sequence") or 0)
            except ValueError:
                sequence = 0
            # Interning shares one string per stop id across a million rows.
            sequenced.setdefault(trip.trip_id, []).append(
                (sequence, feed.stops[stop_id].stop_id, seconds)
            )

    for trip_id, rows in sequenced.items():
        rows.sort()
        feed.trip_stops[trip_id] = [(stop_id, seconds) for _seq, stop_id, seconds in rows]

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        feed.runs = _parse_frequencies(zf, feed.trip_stops)

    # Trips with no stop times are dropped, matching the website.
    feed.trips = {tid: t for tid, t in feed.trips.items() if tid in feed.trip_stops}

    schedule: dict[str, dict[str, list[tuple[int, str, int]]]] = {}
    for trip_id, stops in feed.trip_stops.items():
        trip = feed.trips[trip_id]
        feed.route_trips.setdefault(trip.route_id, []).append(trip_id)
        offsets = feed.runs.get(trip_id, (0,))
        for stop_id, seconds in stops:
            feed.stop_routes.setdefault(stop_id, set()).add(trip.route_id)
            bucket = schedule.setdefault(stop_id, {}).setdefault(trip.route_id, [])
            for offset in offsets:
                bucket.append((seconds + offset, trip_id, offset))

    for routes in schedule.values():
        for bucket in routes.values():
            bucket.sort()
    feed.schedule = schedule
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


def _parse_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """[(field_number, wire_type, value)] for one protobuf message."""

    fields: list[tuple[int, int, Any]] = []
    pos = 0
    end = len(data)
    while pos < end:
        tag, pos = _read_varint(data, pos)
        field_num, wire = tag >> 3, tag & 0x7
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


def _as_str(raw: Any) -> str:
    if not isinstance(raw, (bytes, bytearray)):
        return ""
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
        return self.label or self.vehicle_id or self.entity_id or "Unknown vehicle"

    @property
    def key(self) -> str:
        return self.vehicle_id or self.entity_id or self.label

    def speed_kmh(self, operator: str) -> float | None:
        """Speed in km/h, guarding against KTMB's units.

        GTFS-realtime specifies metres per second, but KTMB reports km/h:
        converting its values gives 200 km/h and more for a Komuter set. For
        KTMB, anything above 45 is taken as km/h already. The cut sits above
        the ~40 m/s an ETS reaches and below speeds that only appear in km/h.
        """

        if self.speed is None or self.speed <= 0:
            return None
        if operator == "ktmb" and self.speed > 45:
            return self.speed
        return self.speed * 3.6


def decode_vehicle_positions(blob: bytes) -> list[Vehicle]:
    vehicles: list[Vehicle] = []
    for field_num, wire, value in _parse_fields(blob):
        if field_num != 2 or wire != 2:
            continue  # only FeedEntity matters
        entity = _parse_fields(value)
        raw = _first(entity, 4)
        if not isinstance(raw, (bytes, bytearray)):
            continue
        vp = _parse_fields(raw)
        vehicle = Vehicle(entity_id=_as_str(_first(entity, 1)))

        trip_raw = _first(vp, 1)
        if isinstance(trip_raw, (bytes, bytearray)):
            trip = _parse_fields(trip_raw)
            vehicle.trip_id = _as_str(_first(trip, 1))
            vehicle.route_id = _as_str(_first(trip, 5))

        pos_raw = _first(vp, 2)
        if isinstance(pos_raw, (bytes, bytearray)):
            pos = _parse_fields(pos_raw)
            vehicle.lat = _as_float(_first(pos, 1))
            vehicle.lon = _as_float(_first(pos, 2))
            vehicle.bearing = _as_float(_first(pos, 3))
            vehicle.speed = _as_float(_first(pos, 5))

        stamp = _first(vp, 5)
        if isinstance(stamp, int):
            vehicle.timestamp = stamp

        desc_raw = _first(vp, 8)
        if isinstance(desc_raw, (bytes, bytearray)):
            desc = _parse_fields(desc_raw)
            vehicle.vehicle_id = _as_str(_first(desc, 1))
            vehicle.label = _as_str(_first(desc, 2))

        vehicles.append(vehicle)

    vehicles.sort(key=lambda v: v.timestamp, reverse=True)
    return vehicles


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class Transit:
    """Fetches, caches and serves every transit feed."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._feeds: dict[str, Feed] = {}
        self._locks = {key: asyncio.Lock() for key in OPERATORS}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.HTTP_TIMEOUT_SECONDS, connect=15.0),
            follow_redirects=True,
            headers={"User-Agent": config.USER_AGENT},
        )

    async def close(self) -> None:
        await self._client.aclose()

    def peek(self, operator: str) -> Feed | None:
        """The loaded feed, without fetching. Used by autocomplete."""

        return self._feeds.get(operator)

    def loaded(self) -> list[str]:
        return list(self._feeds)

    async def feed(self, operator: str) -> Feed:
        if operator not in OPERATORS:
            raise ValueError(f"Unknown operator {operator!r}")

        max_age = config.STATIC_REFRESH_HOURS * 3600
        cached = self._feeds.get(operator)
        if cached and time.time() - cached.fetched_at < max_age:
            return cached

        async with self._locks[operator]:
            cached = self._feeds.get(operator)
            if cached and time.time() - cached.fetched_at < max_age:
                return cached

            key = f"static:{operator}"
            hit = await self._db.cache_get(key, max_age)
            blob = hit[0] if hit else None
            if blob is None:
                try:
                    response = await self._client.get(OPERATORS[operator].static_url)
                    response.raise_for_status()
                    blob = response.content
                    await self._db.cache_put(key, blob)
                    await self._db.record_feed_ok(operator)
                except Exception as exc:  # noqa: BLE001 - upstream fails many ways
                    log.warning("Static fetch failed for %s: %s", operator, exc)
                    await self._db.record_feed_fail(operator, str(exc))
                    stale = await self._db.cache_get_any_age(key)
                    if stale is None:
                        if cached:
                            return cached
                        raise
                    blob = stale[0]

            parsed = await asyncio.to_thread(parse_static_zip, operator, blob)
            if hit is not None:
                # Loaded from the SQLite copy, so age it by that copy's date
                # rather than treating it as fresh.
                parsed.fetched_at = float(hit[1])
            self._feeds[operator] = parsed
            return parsed

    async def warm(self) -> None:
        """Load every feed, one at a time, so memory peaks stay modest."""

        for operator in OPERATORS:
            try:
                loaded = await self.feed(operator)
                log.info(
                    "Loaded %s: %d stops, %d routes",
                    operator_label(operator),
                    len(loaded.stops),
                    len(loaded.routes),
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Could not load %s: %s", operator, exc)

    async def vehicles(self, operator: str, max_age: int = config.REALTIME_CACHE_SECONDS) -> list[Vehicle]:
        op = OPERATORS.get(operator)
        if op is None or not op.has_live:
            return []

        key = f"realtime:{operator}"
        hit = await self._db.cache_get(key, max_age)
        blob = hit[0] if hit else None
        if blob is None:
            try:
                response = await self._client.get(op.realtime_url)
                response.raise_for_status()
                blob = response.content
                await self._db.cache_put(key, blob)
                await self._db.record_feed_ok(f"{operator} live")
            except Exception as exc:  # noqa: BLE001
                log.warning("Realtime fetch failed for %s: %s", operator, exc)
                await self._db.record_feed_fail(f"{operator} live", str(exc))
                stale = await self._db.cache_get_any_age(key)
                if stale is None:
                    return []
                blob = stale[0]
        try:
            return await asyncio.to_thread(decode_vehicle_positions, blob)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not decode realtime feed for %s: %s", operator, exc)
            return []

    # -- autocomplete, from memory only -------------------------------------

    def peek_stops(self, operators: Iterable[str], query: str, limit: int = 25) -> list[tuple[str, Stop]]:
        out: list[tuple[str, Stop]] = []
        for operator in operators:
            loaded = self._feeds.get(operator)
            if loaded is None:
                continue
            out.extend((operator, stop) for stop in loaded.search_stops(query, limit))
        # Exact and prefix matches from every operator first.
        q = normalise(query)
        out.sort(key=lambda pair: (not normalise(pair[1].name).startswith(q), pair[1].name))
        return out[:limit]

    def peek_routes(self, operators: Iterable[str], query: str, limit: int = 25) -> list[tuple[str, Route]]:
        out: list[tuple[str, Route]] = []
        for operator in operators:
            loaded = self._feeds.get(operator)
            if loaded is None:
                continue
            out.extend((operator, route) for route in loaded.search_routes(query, limit))
        return out[:limit]
