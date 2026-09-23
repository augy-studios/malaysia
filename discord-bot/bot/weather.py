"""Weather, earthquake and flood data from api.data.gov.my.

Four public endpoints, all open and unauthenticated:

    /weather/forecast/                 7-day town forecasts from MET Malaysia
    /weather/warning/                  active MET weather warnings
    /weather/warning/earthquake/       earthquake bulletins
    /flood-warning/                    river gauge levels from JPS

Responses are cached in SQLite, so a restart does not send a burst of requests
upstream. When a fetch fails, the stale copy is served and marked as stale:
slightly old flood levels are far more useful than none, and data.gov.my rate
limits under load.

The last parsed copy of each feed is also held in memory. Discord gives an
autocomplete handler three seconds to answer, which is not always enough to
fetch 3000 forecast rows, so autocomplete reads that copy instead.
"""

from __future__ import annotations

import json
import logging
import math
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Iterable, Sequence

import httpx

from . import config
from .timeutils import is_active_window, parse_iso

log = logging.getLogger(__name__)

API_ROOT = "https://api.data.gov.my"
FORECAST_URL = f"{API_ROOT}/weather/forecast/"
WARNING_URL = f"{API_ROOT}/weather/warning/"
QUAKE_URL = f"{API_ROOT}/weather/warning/earthquake/"
FLOOD_URL = f"{API_ROOT}/flood-warning/"

# The forecast is a little over 3000 rows. Asking for too few does not drop
# towns, it silently truncates the far end of the week, so this sits well
# clear of the real size.
FORECAST_LIMIT = 6000
FLOOD_LIMIT = 3000
WARNING_LIMIT = 200
QUAKE_LIMIT = 100

# Flood severity from calm to worst. The upstream indicator uses these exact
# words, and the ordering lets the bot ask "at WARNING or above".
FLOOD_LEVELS = ("NORMAL", "ALERT", "WARNING", "DANGER")
FLOOD_RANK = {name: index for index, name in enumerate(FLOOD_LEVELS)}
ELEVATED_RANK = FLOOD_RANK["ALERT"]

# Named the way MET writes them in warnings. The flood feed spells the
# territories out ("WILAYAH PERSEKUTUAN KUALA LUMPUR"), which still contains
# these names, so one list filters both feeds.
STATES = (
    "Johor",
    "Kedah",
    "Kelantan",
    "Kuala Lumpur",
    "Labuan",
    "Melaka",
    "Negeri Sembilan",
    "Pahang",
    "Perak",
    "Perlis",
    "Pulau Pinang",
    "Putrajaya",
    "Sabah",
    "Sarawak",
    "Selangor",
    "Terengganu",
)

# Some gauges stopped reporting years ago and still carry their last
# indicator, DANGER included. A reading older than this is not treated as
# current, so it neither raises an alert nor tops the list.
FLOOD_READING_MAX_AGE = 2 * 86400


class FeedError(RuntimeError):
    """Raised when upstream fails and no cached copy can be served."""


# ---------------------------------------------------------------------------
# Forecast wording
# ---------------------------------------------------------------------------

# MET publishes forecast text in Malay even on the English endpoint, while the
# warnings are English. The vocabulary is a condition plus an optional
# qualifier, so the parts are translated separately and recombined. A
# combination MET has not used before still comes out in English.

_CONDITIONS = {
    "ribut petir": "Thunderstorms",
    "hujan": "Rain",
    "tiada hujan": "No rain",
    "jerebu": "Hazy",
}

_QUALIFIERS = {
    "di beberapa tempat di kawasan pedalaman": "in a few inland areas",
    "di beberapa tempat di kawasan pantai": "in a few coastal areas",
    "di kebanyakan tempat di kawasan pedalaman": "in most inland areas",
    "di kebanyakan tempat di kawasan pantai": "in most coastal areas",
    "di beberapa tempat": "in a few places",
    "di kebanyakan tempat": "in most places",
    "menyeluruh": "widespread",
}

_WHEN = {
    "sepanjang hari": "all day",
    "pagi dan petang": "morning and afternoon",
    "petang dan malam": "afternoon and night",
    "pagi dan malam": "morning and night",
    "pagi": "morning",
    "petang": "afternoon",
    "malam": "night",
}


def translate_forecast(text: str) -> str:
    """A Malay forecast phrase in English, left alone when unknown."""

    raw = (text or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()

    # "Tiada Hujan" has to be checked before "hujan", or a dry day reads wet.
    if lowered.startswith("tiada hujan"):
        return "No rain"

    # Longest qualifier first, so the inland and coastal variants win over the
    # shorter phrase they contain.
    for malay, english in sorted(_QUALIFIERS.items(), key=lambda kv: -len(kv[0])):
        if malay in lowered:
            head = _CONDITIONS.get(lowered.replace(malay, "").strip())
            return f"{head} {english}" if head else raw
    return _CONDITIONS.get(lowered, raw)


def translate_when(text: str) -> str:
    return _WHEN.get((text or "").strip().lower(), text or "")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def normalise(text: str) -> str:
    """Fold a name down for forgiving search.

    "kuala lumpur", "K.Lumpur" and "KUALA-LUMPUR" should find the same place.
    Accents are stripped too.
    """

    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(
        "".join(ch if ch.isalnum() else " " for ch in stripped.lower()).split()
    )


@dataclass(frozen=True)
class ForecastDay:
    location_id: str
    location_name: str
    date: str
    morning: str
    afternoon: str
    night: str
    summary: str
    min_temp: float | None
    max_temp: float | None
    when: str = ""

    @property
    def condition(self) -> str:
        return self.summary or self.afternoon or self.morning or "No data"

    @property
    def headline(self) -> str:
        if self.when and self.summary and self.summary.lower() != "no rain":
            return f"{self.condition}, {self.when}"
        return self.condition

    @property
    def temp_range(self) -> str:
        if self.min_temp is None or self.max_temp is None:
            return ""
        return f"{self.min_temp:g} to {self.max_temp:g}°C"

    @property
    def is_wet(self) -> bool:
        """True when the day's wording suggests rain or a thunderstorm.

        Dry days read "No rain", so negations are stripped first. Otherwise a
        plain substring test reports an umbrella day for exactly the forecast
        that rules one out.
        """

        haystack = " ".join((self.summary, self.morning, self.afternoon, self.night)).lower()
        for negation in ("no rain", "tiada hujan"):
            haystack = haystack.replace(negation, " ")
        return any(w in haystack for w in ("rain", "shower", "thunder", "ribut", "hujan"))

    @property
    def emoji(self) -> str:
        text = self.condition.lower()
        if "thunder" in text:
            return "⛈️"
        if "no rain" in text:
            return "☀️"
        if "rain" in text:
            return "🌧️"
        if "haz" in text:
            return "🌫️"
        return "🌤️"


@dataclass(frozen=True)
class Location:
    location_id: str
    name: str
    days: tuple[ForecastDay, ...] = ()

    @property
    def key(self) -> str:
        return normalise(self.location_id or self.name)

    @property
    def today(self) -> ForecastDay | None:
        return self.days[0] if self.days else None


@dataclass(frozen=True)
class Warning:
    warning_id: str
    title: str
    text: str
    valid_from: str
    valid_to: str
    instruction: str = ""

    @property
    def is_advisory_only(self) -> bool:
        """True for MET's standing "No Advisory" row.

        It is permanently valid, so a date check alone treats it as a live
        warning and the bot would announce one when the answer is that there
        is none.
        """

        heading = self.title.strip().lower()
        return heading.startswith("no advisory") or heading.startswith("tiada nasihat")

    @property
    def is_active(self) -> bool:
        return not self.is_advisory_only and is_active_window(self.valid_from, self.valid_to)

    @property
    def severity(self) -> str:
        haystack = f"{self.title} {self.text}".lower()
        if "merah" in haystack or "red" in haystack or "danger" in haystack:
            return "danger"
        if "oren" in haystack or "orange" in haystack:
            return "warning"
        return "alert"

    def mentions(self, needle: str) -> bool:
        if not needle:
            return False
        return normalise(needle) in normalise(f"{self.title} {self.text}")


@dataclass(frozen=True)
class Quake:
    quake_id: str
    magnitude: float | None
    depth: float | None
    location: str
    when: str
    lat: float | None
    lon: float | None
    status: str = ""
    distance: str = ""

    @property
    def severity(self) -> str:
        if self.magnitude is None:
            return "low"
        if self.magnitude >= 6:
            return "high"
        if self.magnitude >= 5:
            return "medium"
        return "low"

    @property
    def magnitude_text(self) -> str:
        return f"M{self.magnitude:.1f}" if self.magnitude is not None else "M?"

    @property
    def maps_url(self) -> str:
        if self.lat is None or self.lon is None:
            return ""
        return f"https://www.google.com/maps?q={self.lat},{self.lon}"


@dataclass(frozen=True)
class FloodStation:
    station_id: str
    name: str
    district: str
    state: str
    main_basin: str
    sub_basin: str
    level: float | None
    normal_level: float | None
    alert_level: float | None
    warning_level: float | None
    danger_level: float | None
    indicator: str
    trend: str
    updated_at: str
    lat: float | None = None
    lon: float | None = None

    @property
    def severity(self) -> str:
        """The level, or "unknown" for a gauge that has stopped reporting.

        A gauge silent since 2024 still carries whatever indicator it last
        sent, so showing that colour would present an old reading as current.
        """

        if self.indicator not in FLOOD_RANK or not self.is_current:
            return "unknown"
        return self.indicator.lower()

    @property
    def rank(self) -> int:
        return FLOOD_RANK.get(self.indicator, -1) if self.is_current else -1

    @property
    def is_current(self) -> bool:
        moment = parse_iso(self.updated_at)
        return moment is not None and time.time() - moment.timestamp() < FLOOD_READING_MAX_AGE

    @property
    def is_elevated(self) -> bool:
        return self.rank >= ELEVATED_RANK and self.is_current

    @property
    def place(self) -> str:
        return ", ".join(p for p in (self.district, self.state) if p) or "Location unknown"

    @property
    def level_text(self) -> str:
        return f"{self.level:g} m" if self.level is not None else "No reading"

    @property
    def trend_text(self) -> str:
        trend = (self.trend or "").upper()
        return {
            "RISING": "rising",
            "FALLING": "falling",
            "RECEDING": "falling",
            "NORMAL": "steady",
            "STEADY": "steady",
            "NO CHANGE": "steady",
        }.get(trend, trend.lower())

    @property
    def maps_url(self) -> str:
        if self.lat is None or self.lon is None:
            return ""
        return f"https://www.google.com/maps?q={self.lat},{self.lon}"

    def search_blob(self) -> str:
        return " ".join((self.name, self.district, self.state, self.main_basin, self.sub_basin))

    def in_state(self, state: str) -> bool:
        return not state or normalise(state) in normalise(self.state)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _first(row: dict[str, Any], *names: str) -> Any:
    """The first present, non-empty value among `names`.

    The feeds rename fields between releases (`location_name` moved inside a
    `location` object at one point), so every read goes through this.
    """

    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def parse_forecast(payload: Sequence[dict[str, Any]]) -> dict[str, Location]:
    grouped: dict[str, list[ForecastDay]] = {}
    names: dict[str, str] = {}

    for row in payload:
        if not isinstance(row, dict):
            continue
        location = row.get("location")
        source = location if isinstance(location, dict) else row
        loc_name = _text(_first(source, "location_name", "name"))
        loc_id = _text(_first(source, "location_id", "id")) or loc_name
        if not loc_name:
            continue

        key = normalise(loc_id)
        names.setdefault(key, loc_name)
        grouped.setdefault(key, []).append(
            ForecastDay(
                location_id=loc_id,
                location_name=loc_name,
                date=_text(_first(row, "date", "forecast_date")),
                morning=translate_forecast(_text(row.get("morning_forecast"))),
                afternoon=translate_forecast(_text(row.get("afternoon_forecast"))),
                night=translate_forecast(_text(row.get("night_forecast"))),
                summary=translate_forecast(_text(row.get("summary_forecast"))),
                min_temp=_number(row.get("min_temp")),
                max_temp=_number(row.get("max_temp")),
                when=translate_when(_text(row.get("summary_when"))),
            )
        )

    out: dict[str, Location] = {}
    for key, days in grouped.items():
        days.sort(key=lambda d: d.date or "")
        out[key] = Location(location_id=days[0].location_id, name=names[key], days=tuple(days))
    return out


def parse_warnings(payload: Sequence[dict[str, Any]]) -> list[Warning]:
    out: list[Warning] = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            continue
        issue = row.get("warning_issue")
        issue_title = (
            _text(_first(issue, "title_en", "title_bm")) if isinstance(issue, dict) else ""
        )
        title = _text(_first(row, "heading_en", "heading_bm")) or issue_title
        text = _text(_first(row, "text_en", "text_bm"))
        if not title and not text:
            continue
        out.append(
            Warning(
                warning_id=_text(_first(row, "id", "warning_id"), f"w{index}"),
                title=title or "Weather warning",
                text=text or "No further details provided.",
                valid_from=_text(_first(row, "valid_from", "period_start")),
                valid_to=_text(_first(row, "valid_to", "period_end")),
                instruction=_text(_first(row, "instruction_en", "instruction_bm")),
            )
        )
    return out


def parse_quakes(payload: Sequence[dict[str, Any]]) -> list[Quake]:
    out: list[Quake] = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            continue
        # `utcdatetime` has no offset but is UTC by definition, so it is read
        # that way and preferred over the local field.
        when = ""
        utc = _text(row.get("utcdatetime"))
        local = _text(row.get("localdatetime"))
        if utc:
            moment = parse_iso(utc, assume=timezone.utc)
            when = moment.isoformat() if moment else ""
        if not when and local:
            moment = parse_iso(local)
            when = moment.isoformat() if moment else ""

        out.append(
            Quake(
                quake_id=_text(_first(row, "id", "event_id"), f"q{index}"),
                magnitude=_number(row.get("magdefault")) or _number(row.get("magnitude")),
                depth=_number(row.get("depth")),
                location=_text(_first(row, "location", "location_original"), "Location unavailable"),
                when=when,
                lat=_number(_first(row, "lat", "latitude")),
                lon=_number(_first(row, "lon", "longitude")),
                status=_text(row.get("status")),
                distance=_text(_first(row, "n_distancemas", "n_distancerest")),
            )
        )
    out.sort(key=lambda q: q.when or "", reverse=True)
    return out


def parse_flood(payload: Sequence[dict[str, Any]]) -> list[FloodStation]:
    out: list[FloodStation] = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            continue
        name = _text(_first(row, "station_name", "name"))
        if not name:
            continue
        out.append(
            FloodStation(
                station_id=_text(_first(row, "station_id", "id"), f"s{index}"),
                name=name,
                district=_text(row.get("district")),
                state=_text(row.get("state")),
                main_basin=_text(row.get("main_basin")),
                sub_basin=_text(row.get("sub_basin")),
                level=_number(row.get("water_level_current")),
                normal_level=_number(row.get("water_level_normal_level")),
                alert_level=_number(row.get("water_level_alert_level")),
                warning_level=_number(row.get("water_level_warning_level")),
                danger_level=_number(row.get("water_level_danger_level")),
                indicator=_text(row.get("water_level_indicator")).upper(),
                trend=_text(row.get("water_level_trend")),
                updated_at=_text(row.get("water_level_update_datetime")),
                lat=_number(_first(row, "latitude", "lat")),
                lon=_number(_first(row, "longitude", "lon")),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


def score_match(haystack: str, query: str) -> int:
    """How well `haystack` answers `query`. Zero means no match.

    A prefix match beats a word match, which beats a scattered one. This is
    what puts Kuala Lumpur above Teluk Kuala for "kuala".
    """

    hay = normalise(haystack)
    needle = normalise(query)
    if not hay or not needle:
        return 0
    if hay == needle:
        return 1000
    if hay.startswith(needle):
        return 500
    if needle in hay:
        return 250

    hay_tokens = set(hay.split())
    needle_tokens = needle.split()
    hits = sum(
        1
        for token in needle_tokens
        if token in hay_tokens or any(word.startswith(token) for word in hay_tokens)
    )
    if hits == len(needle_tokens):
        return 120
    return 40 * hits


def search_locations(
    locations: dict[str, Location], query: str, limit: int = 25
) -> list[Location]:
    ranked = sorted(
        ((score_match(loc.name, query), loc) for loc in locations.values()),
        key=lambda pair: (-pair[0], pair[1].name),
    )
    return [loc for score, loc in ranked if score > 0][:limit]


def search_stations(
    stations: Iterable[FloodStation], query: str, limit: int = 25
) -> list[FloodStation]:
    ranked = sorted(
        ((score_match(st.search_blob(), query), st) for st in stations),
        key=lambda pair: (-pair[0], -pair[1].rank, pair[1].name),
    )
    return [st for score, st in ranked if score > 0][:limit]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    """A parsed feed plus when it was fetched."""

    fetched_at: int = 0
    stale: bool = False
    locations: dict[str, Location] = field(default_factory=dict)
    warnings: list[Warning] = field(default_factory=list)
    quakes: list[Quake] = field(default_factory=list)
    stations: list[FloodStation] = field(default_factory=list)


def _decode(body: bytes) -> list[dict[str, Any]] | None:
    try:
        parsed = json.loads(body.decode())
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, list) else None


class WeatherFeeds:
    """Fetches, caches and parses the four weather endpoints."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self._client = httpx.AsyncClient(
            timeout=config.HTTP_TIMEOUT_SECONDS,
            headers={"accept": "application/json", "user-agent": config.USER_AGENT},
            follow_redirects=True,
        )
        # Last parsed copy of each feed, for autocomplete.
        self._locations: dict[str, Location] = {}
        self._stations: list[FloodStation] = []

    async def close(self) -> None:
        await self._client.aclose()

    async def _fetch(
        self, key: str, url: str, max_age: int, params: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """(rows, fetched_at, stale) for one endpoint."""

        cached = await self.db.cache_get(f"wx:{key}", max_age)
        if cached is not None:
            rows = _decode(cached[0])
            if rows is not None:
                return rows, cached[1], False

        try:
            response = await self._client.get(url, params=params)
            if response.status_code == 429:
                raise httpx.HTTPStatusError(
                    "rate limited", request=response.request, response=response
                )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Fetch failed for %s: %s", key, exc)
            await self.db.record_feed_fail(key, str(exc))
            fallback = await self.db.cache_get_any_age(f"wx:{key}")
            if fallback is not None:
                rows = _decode(fallback[0])
                if rows is not None:
                    return rows, fallback[1], True
            raise FeedError(f"data.gov.my could not be reached for {key}.") from exc

        # The endpoints return a bare array. A dict with `data` appears when
        # data.gov.my wraps a paginated response, so both are accepted.
        if isinstance(payload, dict):
            payload = payload.get("data", [])
        if not isinstance(payload, list):
            await self.db.record_feed_fail(key, "unexpected response shape")
            raise FeedError(f"data.gov.my returned an unexpected shape for {key}.")

        rows = [row for row in payload if isinstance(row, dict)]
        await self.db.cache_put(f"wx:{key}", json.dumps(rows).encode())
        await self.db.record_feed_ok(key)
        cached = await self.db.cache_get_any_age(f"wx:{key}")
        return rows, cached[1] if cached else 0, False

    async def forecast(self) -> Snapshot:
        rows, fetched_at, stale = await self._fetch(
            "forecast",
            FORECAST_URL,
            config.FORECAST_CACHE_SECONDS,
            {"limit": FORECAST_LIMIT, "sort": "date"},
        )
        locations = parse_forecast(rows)
        self._locations = locations
        return Snapshot(fetched_at=fetched_at, stale=stale, locations=locations)

    async def warnings(self, active_only: bool = True) -> Snapshot:
        rows, fetched_at, stale = await self._fetch(
            "warning",
            WARNING_URL,
            config.WARNING_CACHE_SECONDS,
            {"limit": WARNING_LIMIT, "sort": "-valid_from"},
        )
        parsed = parse_warnings(rows)
        if active_only:
            parsed = [w for w in parsed if w.is_active]
        return Snapshot(fetched_at=fetched_at, stale=stale, warnings=parsed)

    async def quakes(self) -> Snapshot:
        rows, fetched_at, stale = await self._fetch(
            "quake",
            QUAKE_URL,
            config.QUAKE_CACHE_SECONDS,
            {"limit": QUAKE_LIMIT, "sort": "-utcdatetime"},
        )
        return Snapshot(fetched_at=fetched_at, stale=stale, quakes=parse_quakes(rows))

    async def flood(self) -> Snapshot:
        rows, fetched_at, stale = await self._fetch(
            "flood", FLOOD_URL, config.FLOOD_CACHE_SECONDS, {"limit": FLOOD_LIMIT}
        )
        stations = parse_flood(rows)
        self._stations = stations
        return Snapshot(fetched_at=fetched_at, stale=stale, stations=stations)

    async def warm(self) -> None:
        """Load the two searchable feeds so autocomplete has something to offer."""

        for loader in (self.forecast, self.flood):
            try:
                await loader()
            except FeedError as exc:
                log.warning("Could not preload weather data: %s", exc)

    # -- autocomplete, from memory only -------------------------------------

    def peek_locations(self, query: str, limit: int = 25) -> list[Location]:
        if not query.strip():
            return sorted(self._locations.values(), key=lambda loc: loc.name)[:limit]
        return search_locations(self._locations, query, limit)

    def peek_stations(self, query: str, limit: int = 25) -> list[FloodStation]:
        if not query.strip():
            worst = sorted(self._stations, key=lambda st: (-st.rank, st.name))
            return worst[:limit]
        return search_stations(self._stations, query, limit)

    # -- lookups ----------------------------------------------------------

    async def location(self, key_or_name: str) -> tuple[Location | None, list[Location]]:
        """An exact location, or else the candidates a free-text query matches."""

        snapshot = await self.forecast()
        exact = snapshot.locations.get(normalise(key_or_name))
        if exact is not None:
            return exact, []
        matches = search_locations(snapshot.locations, key_or_name, 25)
        if len(matches) == 1 or (matches and normalise(matches[0].name) == normalise(key_or_name)):
            return matches[0], []
        return None, matches

    async def station(self, id_or_name: str) -> tuple[FloodStation | None, list[FloodStation]]:
        snapshot = await self.flood()
        for station in snapshot.stations:
            if station.station_id == id_or_name:
                return station, []
        matches = search_stations(snapshot.stations, id_or_name, 25)
        if len(matches) == 1:
            return matches[0], []
        return None, matches
