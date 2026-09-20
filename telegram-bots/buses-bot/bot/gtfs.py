"""data.gov.my GTFS access.

The bot talks to data.gov.my directly rather than through the website's
serverless proxies, so the parsing the Node handlers do in
`main-site/api/bus-*.js` is reproduced here:

  * Static feeds arrive as a GTFS zip. Python has `zipfile` and `csv` in the
    standard library, so this is considerably shorter than the JS version.
  * Realtime feeds arrive as a raw GTFS-realtime protobuf FeedMessage. Rather
    than add a protobuf dependency, the same minimal wire-format reader used on
    the site is ported below. The field numbers match gtfs-realtime.proto.

Static bundles are large and change rarely, so parsed feeds are held in memory
and the raw zip is cached in SQLite. When upstream fails, a stale cache entry
is used instead of showing the user nothing.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
import struct
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

log = logging.getLogger(__name__)

STATIC_PRASARANA = "https://api.data.gov.my/gtfs-static/prasarana?category={category}"
STATIC_MYBAS = "https://api.data.gov.my/gtfs-static/mybas-johor"
REALTIME_PRASARANA = (
    "https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana/?category={category}"
)
REALTIME_MYBAS = "https://api.data.gov.my/gtfs-realtime/vehicle-position/mybas-johor"

PRASARANA_CATEGORIES = ("rapid-bus-kl", "rapid-bus-penang", "rapid-bus-mrtfeeder")
MYBAS_CATEGORY = "mybas-johor"
ALL_OPERATORS = PRASARANA_CATEGORIES + (MYBAS_CATEGORY,)

OPERATOR_LABELS = {
    "rapid-bus-kl": "Rapid Bus KL",
    "rapid-bus-penang": "Rapid Bus Penang",
    "rapid-bus-mrtfeeder": "Rapid Bus MRT Feeder",
    "mybas-johor": "myBAS Johor",
}


def operator_label(operator: str) -> str:
    return OPERATOR_LABELS.get(operator, operator)


# ---------------------------------------------------------------------------
# Data holders
# ---------------------------------------------------------------------------


@dataclass
class Stop:
    stop_id: str
    stop_name: str
    lat: float | None
    lon: float | None


@dataclass
class Route:
    route_id: str
    short_name: str
    long_name: str

    @property
    def display(self) -> str:
        if self.short_name and self.long_name:
            return f"{self.short_name} {self.long_name}"
        return self.short_name or self.long_name or self.route_id


@dataclass
class Trip:
    trip_id: str
    route_id: str
    headsign: str


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
class Feed:
    """One operator's parsed static GTFS bundle."""

    operator: str
    fetched_at: float = 0.0
    stops: dict[str, Stop] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)
    trips: dict[str, Trip] = field(default_factory=dict)
    trip_stops: dict[str, list[StopTime]] = field(default_factory=dict)
    route_stops: dict[str, set[str]] = field(default_factory=dict)
    stop_routes: dict[str, set[str]] = field(default_factory=dict)
    # stop_id -> route_id -> sorted list of HH:MM:SS
    stop_schedule: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.stops and not self.routes

    def search_stops(self, query: str, limit: int = 25) -> list[Stop]:
        q = query.strip().lower()
        if not q:
            return []
        exact: list[Stop] = []
        starts: list[Stop] = []
        contains: list[Stop] = []
        for stop in self.stops.values():
            name = stop.stop_name.lower()
            if name == q or stop.stop_id.lower() == q:
                exact.append(stop)
            elif name.startswith(q):
                starts.append(stop)
            elif q in name:
                contains.append(stop)
            if len(exact) + len(starts) + len(contains) > limit * 4:
                break
        ordered = exact + starts + contains
        return ordered[:limit]

    def search_routes(self, query: str, limit: int = 25) -> list[Route]:
        q = query.strip().lower()
        if not q:
            return []
        exact: list[Route] = []
        starts: list[Route] = []
        contains: list[Route] = []
        for route in self.routes.values():
            short = route.short_name.lower()
            hay = f"{route.short_name} {route.long_name} {route.route_id}".lower()
            if short == q or route.route_id.lower() == q:
                exact.append(route)
            elif short.startswith(q) or route.long_name.lower().startswith(q):
                starts.append(route)
            elif q in hay:
                contains.append(route)
        return (exact + starts + contains)[:limit]

    def stops_near(self, lat: float, lon: float, radius_m: int, limit: int = 12) -> list[tuple[Stop, float]]:
        out: list[tuple[Stop, float]] = []
        for stop in self.stops.values():
            if stop.lat is None or stop.lon is None:
                continue
            distance = haversine_m(lat, lon, stop.lat, stop.lon)
            if distance <= radius_m:
                out.append((stop, distance))
        out.sort(key=lambda pair: pair[1])
        return out[:limit]

    def routes_for_stop(self, stop_id: str) -> list[Route]:
        ids = self.stop_routes.get(stop_id, set())
        routes = [self.routes[r] for r in ids if r in self.routes]
        routes.sort(key=lambda r: r.short_name or r.route_id)
        return routes

    def trips_for_route(self, route_id: str) -> list[Trip]:
        out = [t for t in self.trips.values() if t.route_id == route_id]

        def first_time(trip: Trip) -> str:
            times = self.trip_stops.get(trip.trip_id) or []
            return times[0].best_time if times else "99:99:99"

        out.sort(key=first_time)
        return out


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


def _read_csv(zf: zipfile.ZipFile, name: str) -> Iterable[dict[str, str]]:
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


def parse_static_zip(operator: str, blob: bytes) -> Feed:
    """Parse a GTFS zip into a Feed. CPU bound, so callers run it in a thread."""

    feed = Feed(operator=operator, fetched_at=time.time())

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for row in _read_csv(zf, "stops.txt"):
            stop_id = (row.get("stop_id") or "").strip()
            if not stop_id:
                continue
            feed.stops[stop_id] = Stop(
                stop_id=stop_id,
                stop_name=(row.get("stop_name") or stop_id).strip(),
                lat=_float_or_none(row.get("stop_lat")),
                lon=_float_or_none(row.get("stop_lon")),
            )

        for row in _read_csv(zf, "routes.txt"):
            route_id = (row.get("route_id") or "").strip()
            if not route_id:
                continue
            feed.routes[route_id] = Route(
                route_id=route_id,
                short_name=(row.get("route_short_name") or "").strip(),
                long_name=(row.get("route_long_name") or "").strip(),
            )

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
            )

        schedule_sets: dict[str, dict[str, set[str]]] = {}
        for row in _read_csv(zf, "stop_times.txt"):
            trip_id = (row.get("trip_id") or "").strip()
            stop_id = (row.get("stop_id") or "").strip()
            route_id = trip_route.get(trip_id)
            if not route_id or not stop_id:
                continue

            feed.route_stops.setdefault(route_id, set()).add(stop_id)
            feed.stop_routes.setdefault(stop_id, set()).add(route_id)

            arrival = (row.get("arrival_time") or "").strip()
            departure = (row.get("departure_time") or "").strip()
            try:
                sequence = int(row.get("stop_sequence") or 0)
            except ValueError:
                sequence = 0

            feed.trip_stops.setdefault(trip_id, []).append(
                StopTime(stop_id=stop_id, sequence=sequence, arrival=arrival, departure=departure)
            )

            time_value = departure or arrival
            if time_value:
                schedule_sets.setdefault(stop_id, {}).setdefault(route_id, set()).add(time_value)

        for times in feed.trip_stops.values():
            times.sort(key=lambda st: st.sequence)

        feed.stop_schedule = {
            stop_id: {route_id: sorted(values) for route_id, values in routes.items()}
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
        return self.label or self.vehicle_id or self.entity_id or "Unknown vehicle"


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
    """Fetches, caches and serves the GTFS feeds."""

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
            headers={"User-Agent": "malaysia-buses-bot/1.0 (+https://malaysia.uwuapps.org)"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -- static -----------------------------------------------------------

    def _static_url(self, operator: str) -> str:
        if operator == MYBAS_CATEGORY:
            return STATIC_MYBAS
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
                except Exception as exc:  # noqa: BLE001 - upstream can fail any number of ways
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
                    "Loaded %s: %d stops, %d routes",
                    operator_label(operator),
                    len(feed.stops),
                    len(feed.routes),
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Could not preload %s: %s", operator, exc)

    # -- realtime ---------------------------------------------------------

    def _realtime_url(self, operator: str) -> str:
        if operator == MYBAS_CATEGORY:
            return REALTIME_MYBAS
        return REALTIME_PRASARANA.format(category=operator)

    async def get_vehicles(self, operator: str, max_age: int = 20) -> list[Vehicle]:
        """Return live vehicle positions, cached briefly to spare upstream."""

        cache_key = f"realtime:{operator}"
        blob = await self._db.cache_get(cache_key, max_age)

        if blob is None:
            try:
                response = await self._client.get(self._realtime_url(operator))
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

    async def search_all_stops(self, query: str, limit: int = 8) -> list[tuple[str, Stop]]:
        out: list[tuple[str, Stop]] = []
        for operator in ALL_OPERATORS:
            try:
                feed = await self.get_feed(operator)
            except Exception:  # noqa: BLE001
                continue
            for stop in feed.search_stops(query, limit):
                out.append((operator, stop))
        return out[: limit * 2]

    async def search_all_routes(self, query: str, limit: int = 8) -> list[tuple[str, Route]]:
        out: list[tuple[str, Route]] = []
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
    ) -> list[tuple[str, Stop, float]]:
        out: list[tuple[str, Stop, float]] = []
        for operator in ALL_OPERATORS:
            try:
                feed = await self.get_feed(operator)
            except Exception:  # noqa: BLE001
                continue
            for stop, distance in feed.stops_near(lat, lon, radius_m, limit):
                out.append((operator, stop, distance))
        out.sort(key=lambda row: row[2])
        return out[:limit]
