"""Upstream data from api.data.gov.my.

Four public endpoints back this bot, all of them open and unauthenticated:

    /weather/forecast/                 7-day town forecasts from MET Malaysia
    /weather/warning/                  active MET weather warnings
    /weather/warning/earthquake/       earthquake bulletins
    /flood-warning/                    river station water levels

Responses are cached in SQLite rather than in memory, so a restart does not
send a burst of requests upstream. When a fetch fails, a stale cache entry is
served instead of an error: slightly old flood levels are far more useful than
no flood levels, and data.gov.my rate limits under load.

Every record is normalised into a small dataclass here, so the view layer never
has to defend against the shape differences between feeds.
"""

from __future__ import annotations

import json
import logging
import math
import unicodedata
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Iterable, Sequence

import httpx

from .timeutils import is_active_window, parse_iso

log = logging.getLogger(__name__)

API_ROOT = "https://api.data.gov.my"

FORECAST_URL = f"{API_ROOT}/weather/forecast/"
WARNING_URL = f"{API_ROOT}/weather/warning/"
QUAKE_URL = f"{API_ROOT}/weather/warning/earthquake/"
FLOOD_URL = f"{API_ROOT}/flood-warning/"

# data.gov.my paginates and defaults to a small page. The forecast covers every
# town for seven days, which is a little over 3000 rows in practice. Asking for
# too few does not drop towns, it silently truncates the far end of the week,
# so this is set well clear of the real size.
FORECAST_LIMIT = 6000
FLOOD_LIMIT = 3000
WARNING_LIMIT = 200
QUAKE_LIMIT = 100

# Flood severity, ordered from calm to worst. The upstream indicator field uses
# these exact words, and the ordering is what lets the bot ask "at ALERT or
# above" without hard-coding each case.
FLOOD_LEVELS = ("NORMAL", "ALERT", "WARNING", "DANGER")
FLOOD_RANK = {name: index for index, name in enumerate(FLOOD_LEVELS)}
# Anything at this rank or higher is worth telling a subscriber about.
ELEVATED_RANK = FLOOD_RANK["ALERT"]


class FeedError(RuntimeError):
    """Raised when upstream fails and no cached copy can be served."""


# ---------------------------------------------------------------------------
# Forecast wording
# ---------------------------------------------------------------------------

# MET publishes the forecast text in Malay even on the English endpoint, while
# the warning feed is already English. Rather than show one language in the
# forecast and another in the warning beside it, the phrases are translated
# here.
#
# The vocabulary is a small closed set built from a condition plus an optional
# qualifier, so the parts are translated separately and recombined. That way a
# combination MET has not used before still comes out as English rather than
# falling through untranslated.

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

# The `summary_when` field, which says when the day's weather is expected.
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
    """Render a Malay forecast phrase in English, leaving it be if unknown."""

    raw = (text or "").strip()
    if not raw:
        return ""

    lowered = raw.lower()

    # "Tiada Hujan" has to be checked before "hujan", or a dry day reads as wet.
    if lowered.startswith("tiada hujan"):
        return "No rain"

    # Longest qualifier first, so the inland and coastal variants win over the
    # shorter phrase they contain.
    for malay, english in sorted(
        _QUALIFIERS.items(), key=lambda kv: -len(kv[0])
    ):
        if malay in lowered:
            condition = lowered.replace(malay, "").strip()
            head = _CONDITIONS.get(condition)
            if head:
                return f"{head} {english}"
            return raw

    return _CONDITIONS.get(lowered, raw)


def translate_when(text: str) -> str:
    """Render the `summary_when` field in English."""

    lowered = (text or "").strip().lower()
    return _WHEN.get(lowered, text or "")


# ---------------------------------------------------------------------------
# Normalised records
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

    Users type "kuala lumpur", "K.Lumpur" or "KUALA-LUMPUR" and expect the same
    place. Accents are stripped too, since several station names carry them.
    """

    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch if ch.isalnum() else " " for ch in stripped.lower()).strip()


def _tokens(text: str) -> list[str]:
    return [tok for tok in normalise(text).split() if tok]


@dataclass(frozen=True)
class ForecastDay:
    """One town on one day."""

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
        """The condition, with the time of day MET expects it."""

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

        MET writes dry days as "No rain", so a plain substring test would
        report an umbrella day for exactly the forecast that rules one out.
        Negations are stripped before the wet words are looked for.
        """

        haystack = " ".join(
            (self.summary, self.morning, self.afternoon, self.night)
        ).lower()
        for negation in ("no rain", "tiada hujan"):
            haystack = haystack.replace(negation, " ")

        return any(
            word in haystack
            for word in ("rain", "shower", "thunder", "ribut", "hujan")
        )


@dataclass(frozen=True)
class Location:
    """A forecast town, with its days attached."""

    location_id: str
    name: str
    days: tuple[ForecastDay, ...] = ()

    @property
    def today(self) -> ForecastDay | None:
        return self.days[0] if self.days else None


@dataclass(frozen=True)
class Warning:
    """An active MET weather warning."""

    warning_id: str
    title: str
    text: str
    valid_from: str
    valid_to: str
    instruction: str = ""

    @property
    def is_advisory_only(self) -> bool:
        """True for the standing "No Advisory" entry.

        MET keeps a permanently valid row saying there is no tropical cyclone
        under observation. It is genuinely in force, so a date check alone
        treats it as a live warning and the bot would announce a warning when
        the real answer is that there is none.
        """

        heading = self.title.strip().lower()
        return heading.startswith("no advisory") or heading.startswith("tiada nasihat")

    @property
    def is_active(self) -> bool:
        if self.is_advisory_only:
            return False
        return is_active_window(self.valid_from, self.valid_to)

    @property
    def severity(self) -> str:
        """Classify by the colour words MET uses in its headings."""

        haystack = f"{self.title} {self.text}".lower()
        if "merah" in haystack or "red" in haystack or "danger" in haystack:
            return "danger"
        if "oren" in haystack or "orange" in haystack:
            return "warning"
        return "alert"

    def mentions(self, needle: str) -> bool:
        """True when this warning names the given place."""

        if not needle:
            return False
        return normalise(needle) in normalise(f"{self.title} {self.text}")


@dataclass(frozen=True)
class Quake:
    """One earthquake bulletin."""

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


@dataclass(frozen=True)
class FloodStation:
    """One river gauge."""

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
        return self.indicator.lower() if self.indicator in FLOOD_RANK else "unknown"

    @property
    def rank(self) -> int:
        return FLOOD_RANK.get(self.indicator, -1)

    @property
    def is_elevated(self) -> bool:
        return self.rank >= ELEVATED_RANK

    @property
    def place(self) -> str:
        parts = [p for p in (self.district, self.state) if p]
        return ", ".join(parts) or "Location unknown"

    @property
    def level_text(self) -> str:
        return f"{self.level:g} m" if self.level is not None else "No reading"

    @property
    def trend_text(self) -> str:
        trend = (self.trend or "").upper()
        return {
            "RISING": "rising",
            "FALLING": "falling",
            "NORMAL": "steady",
            "STEADY": "steady",
            "NO CHANGE": "steady",
        }.get(trend, trend.lower())

    def search_blob(self) -> str:
        return " ".join(
            (self.name, self.district, self.state, self.main_basin, self.sub_basin)
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _first(row: dict[str, Any], *names: str) -> Any:
    """Return the first present, non-empty value among `names`.

    The feeds rename fields between releases (`location_name` moved inside a
    `location` object at one point), so every read goes through this.
    """

    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def parse_forecast(payload: Sequence[dict[str, Any]]) -> dict[str, Location]:
    """Group the flat forecast list into locations, each with ordered days."""

    grouped: dict[str, list[ForecastDay]] = {}
    names: dict[str, str] = {}

    for row in payload:
        if not isinstance(row, dict):
            continue

        # `location` is sometimes a nested object and sometimes flattened.
        location = row.get("location")
        if isinstance(location, dict):
            loc_name = _text(_first(location, "location_name", "name"))
            loc_id = _text(_first(location, "location_id", "id")) or loc_name
        else:
            loc_name = _text(_first(row, "location_name", "name"))
            loc_id = _text(_first(row, "location_id", "id")) or loc_name

        if not loc_name:
            continue

        key = normalise(loc_id or loc_name)
        names.setdefault(key, loc_name)

        grouped.setdefault(key, []).append(
            ForecastDay(
                location_id=loc_id or loc_name,
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
        out[key] = Location(
            location_id=days[0].location_id,
            name=names.get(key, days[0].location_name),
            days=tuple(days),
        )
    return out


def parse_warnings(payload: Sequence[dict[str, Any]]) -> list[Warning]:
    out: list[Warning] = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            continue

        issue = row.get("warning_issue")
        issue_title = ""
        if isinstance(issue, dict):
            issue_title = _text(_first(issue, "title_en", "title_bm"))

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
        # with that assumption and preferred over the local field.
        utc = _text(row.get("utcdatetime"))
        local = _text(row.get("localdatetime"))
        when = ""
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
                location=_text(
                    _first(row, "location", "location_original"), "Location unavailable"
                ),
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
    """Score how well `haystack` answers `query`. Zero means no match.

    A prefix match beats a word match, which beats a scattered substring
    match. This is what makes "kuala" put Kuala Lumpur above Teluk Kuala.
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
    needle_tokens = _tokens(needle)
    if not needle_tokens:
        return 0

    hits = sum(
        1
        for token in needle_tokens
        if token in hay_tokens or any(word.startswith(token) for word in hay_tokens)
    )
    if hits == len(needle_tokens):
        return 120
    return 40 * hits if hits else 0


def search_locations(locations: dict[str, Location], query: str, limit: int = 8) -> list[Location]:
    scored = [
        (score_match(loc.name, query), loc)
        for loc in locations.values()
    ]
    ranked = sorted(
        ((score, loc) for score, loc in scored if score > 0),
        key=lambda pair: (-pair[0], pair[1].name),
    )
    return [loc for _score, loc in ranked[:limit]]


def search_stations(
    stations: Iterable[FloodStation], query: str, limit: int = 8
) -> list[FloodStation]:
    scored = [(score_match(st.search_blob(), query), st) for st in stations]
    ranked = sorted(
        ((score, st) for score, st in scored if score > 0),
        key=lambda pair: (-pair[0], -pair[1].rank, pair[1].name),
    )
    return [st for _score, st in ranked[:limit]]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""

    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------------------
# The fetching layer
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


class FeedManager:
    """Fetches, caches and parses the four data.gov.my endpoints."""

    def __init__(self, db: Any, settings: Any) -> None:
        self.db = db
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={
                "accept": "application/json",
                "user-agent": "malaysiaweather-bot/1.0 (+https://malaysia.uwuapps.org)",
            },
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -- raw fetch --------------------------------------------------------

    async def _fetch_json(
        self, key: str, url: str, max_age: int, params: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Return (rows, fetched_at, stale) for one endpoint.

        A fresh cache entry short-circuits the request. On failure the stale
        entry is returned with `stale=True` so callers can say so rather than
        showing nothing.
        """

        cached = await self.db.cache_get(key, max_age)
        if cached is not None:
            rows = _decode(cached)
            if rows is not None:
                return rows, await self.db.cache_age(key), False

        try:
            response = await self._client.get(url, params=params or {})
            if response.status_code == 429:
                raise FeedError("data.gov.my is rate limiting requests right now.")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Fetch failed for %s: %s", key, exc)
            await self.db.record_feed_fail(key, str(exc))
            fallback = await self.db.cache_get_any_age(key)
            if fallback is not None:
                body, fetched_at = fallback
                rows = _decode(body)
                if rows is not None:
                    return rows, fetched_at, True
            raise FeedError(f"Could not reach data.gov.my for {key}.") from exc

        # The endpoints return a bare array. A dict with a `data` key shows up
        # when data.gov.my wraps a paginated response, so both are accepted.
        if isinstance(payload, dict):
            payload = payload.get("data", [])
        if not isinstance(payload, list):
            await self.db.record_feed_fail(key, "unexpected response shape")
            raise FeedError(f"data.gov.my returned an unexpected shape for {key}.")

        rows = [row for row in payload if isinstance(row, dict)]
        await self.db.cache_put(key, json.dumps(rows).encode())
        await self.db.record_feed_ok(key)
        return rows, await self.db.cache_age(key), False

    # -- typed accessors --------------------------------------------------

    async def forecast(self, force: bool = False) -> Snapshot:
        rows, fetched_at, stale = await self._fetch_json(
            "forecast",
            FORECAST_URL,
            0 if force else self.settings.forecast_cache_seconds,
            {"limit": FORECAST_LIMIT, "sort": "date"},
        )
        return Snapshot(
            fetched_at=fetched_at, stale=stale, locations=parse_forecast(rows)
        )

    async def warnings(self, force: bool = False, active_only: bool = True) -> Snapshot:
        rows, fetched_at, stale = await self._fetch_json(
            "warning",
            WARNING_URL,
            0 if force else self.settings.warning_cache_seconds,
            {"limit": WARNING_LIMIT, "sort": "-valid_from"},
        )
        parsed = parse_warnings(rows)
        if active_only:
            parsed = [w for w in parsed if w.is_active]
        return Snapshot(fetched_at=fetched_at, stale=stale, warnings=parsed)

    async def quakes(self, force: bool = False) -> Snapshot:
        rows, fetched_at, stale = await self._fetch_json(
            "quake",
            QUAKE_URL,
            0 if force else self.settings.quake_cache_seconds,
            {"limit": QUAKE_LIMIT, "sort": "-utcdatetime"},
        )
        return Snapshot(fetched_at=fetched_at, stale=stale, quakes=parse_quakes(rows))

    async def flood(self, force: bool = False) -> Snapshot:
        rows, fetched_at, stale = await self._fetch_json(
            "flood",
            FLOOD_URL,
            0 if force else self.settings.flood_cache_seconds,
            {"limit": FLOOD_LIMIT},
        )
        return Snapshot(fetched_at=fetched_at, stale=stale, stations=parse_flood(rows))

    # -- convenience ------------------------------------------------------

    async def find_location(self, query: str) -> list[Location]:
        snapshot = await self.forecast()
        return search_locations(snapshot.locations, query)

    async def location_by_id(self, location_id: str) -> Location | None:
        snapshot = await self.forecast()
        return snapshot.locations.get(normalise(location_id))

    async def station_by_id(self, station_id: str) -> FloodStation | None:
        snapshot = await self.flood()
        for station in snapshot.stations:
            if station.station_id == station_id:
                return station
        return None


def _decode(body: bytes) -> list[dict[str, Any]] | None:
    try:
        parsed = json.loads(body.decode())
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, list) else None


__all__ = [
    "FeedManager",
    "FeedError",
    "Snapshot",
    "Location",
    "ForecastDay",
    "Warning",
    "Quake",
    "FloodStation",
    "FLOOD_LEVELS",
    "FLOOD_RANK",
    "ELEVATED_RANK",
    "normalise",
    "score_match",
    "search_locations",
    "search_stations",
    "haversine_m",
]
