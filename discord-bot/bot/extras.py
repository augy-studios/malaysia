"""Prayer times, fuel prices and exchange rates.

Three open REST APIs, none needing a key:

    Prayer times   api.waktusolat.app, an open source mirror of JAKIM e-Solat
    Fuel prices    api.data.gov.my data catalogue, the weekly `fuelprice` set
    Exchange rates api.bnm.gov.my, Bank Negara Malaysia's public API

Each is cached in SQLite the same way as the other feeds, and serves its last
good copy when the upstream is down.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

from . import config
from .timeutils import MYT, now_myt
from .weather import normalise, score_match

log = logging.getLogger(__name__)

ZONES_URL = "https://api.waktusolat.app/zones"
PRAYER_URL = "https://api.waktusolat.app/v2/solat/{zone}"
FUEL_URL = "https://api.data.gov.my/data-catalogue/"
FOREX_URL = "https://api.bnm.gov.my/public/exchange-rate"

# BNM rejects requests without its versioned media type.
BNM_ACCEPT = "application/vnd.BNM.API.v1+json"


class ExtrasError(RuntimeError):
    """Raised when an upstream fails and no cached copy exists."""


# ---------------------------------------------------------------------------
# Prayer times
# ---------------------------------------------------------------------------

PRAYERS = (
    ("imsak", "Imsak"),
    ("fajr", "Subuh"),
    ("syuruk", "Syuruk"),
    ("dhuha", "Dhuha"),
    ("dhuhr", "Zohor"),
    ("asr", "Asar"),
    ("maghrib", "Maghrib"),
    ("isha", "Isyak"),
)
# The five obligatory prayers, which is what a reminder is for. Imsak,
# Syuruk and Dhuha are markers rather than prayers to be called to.
REMINDER_PRAYERS = ("fajr", "dhuhr", "asr", "maghrib", "isha")
PRAYER_LABELS = dict(PRAYERS)


@dataclass(frozen=True)
class Zone:
    code: str
    state: str
    districts: str

    @property
    def label(self) -> str:
        return f"{self.code} {self.state}: {self.districts}"


@dataclass(frozen=True)
class PrayerDay:
    zone: str
    day: date
    hijri: str
    times: dict[str, int]

    def upcoming(self, now: float) -> tuple[str, int] | None:
        for key, _label in PRAYERS:
            epoch = self.times.get(key)
            if epoch and epoch > now:
                return key, epoch
        return None


def parse_zones(payload: Any) -> list[Zone]:
    out: list[Zone] = []
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("jakimCode") or "").strip()
        if code:
            out.append(
                Zone(
                    code=code,
                    state=str(row.get("negeri") or "").strip(),
                    districts=str(row.get("daerah") or "").strip(),
                )
            )
    return out


def parse_prayer_month(payload: Any) -> list[PrayerDay]:
    if not isinstance(payload, dict):
        return []
    zone = str(payload.get("zone") or "")
    year = int(payload.get("year") or 0)
    month = int(payload.get("month_number") or 0)
    out: list[PrayerDay] = []
    for row in payload.get("prayers") or []:
        if not isinstance(row, dict):
            continue
        try:
            day = date(year, month, int(row.get("day") or 0))
        except (TypeError, ValueError):
            continue
        times = {
            key: int(row[key])
            for key, _label in PRAYERS
            if isinstance(row.get(key), (int, float))
        }
        out.append(PrayerDay(zone=zone, day=day, hijri=str(row.get("hijri") or ""), times=times))
    return out


def format_hijri(hijri: str) -> str:
    months = (
        "Muharram", "Safar", "Rabiulawal", "Rabiulakhir", "Jamadilawal", "Jamadilakhir",
        "Rejab", "Syaaban", "Ramadan", "Syawal", "Zulkaedah", "Zulhijjah",
    )
    try:
        year, month, day = (int(part) for part in hijri.split("-"))
        return f"{day} {months[month - 1]} {year}H"
    except (ValueError, IndexError):
        return hijri


# ---------------------------------------------------------------------------
# Fuel
# ---------------------------------------------------------------------------

# Labels for the columns the catalogue publishes today. A column added later
# still shows, under its raw name, rather than being dropped.
FUEL_LABELS = {
    "ron95": "RON95",
    "ron95_budi95": "RON95 BUDI95",
    "ron95_skps": "RON95 SKPS",
    "ron97": "RON97",
    "diesel": "Diesel (Peninsular)",
    "diesel_eastmsia": "Diesel (Sabah, Sarawak, Labuan)",
    "diesel_budi": "Diesel BUDI",
    "diesel_skds": "Diesel SKDS",
}


@dataclass(frozen=True)
class FuelWeek:
    date: str
    prices: dict[str, float]
    changes: dict[str, float]

    def rows(self) -> list[tuple[str, float, float]]:
        ordered = [k for k in FUEL_LABELS if k in self.prices]
        ordered += sorted(k for k in self.prices if k not in FUEL_LABELS)
        return [
            (FUEL_LABELS.get(k, k.upper()), self.prices[k], self.changes.get(k, 0.0))
            for k in ordered
        ]


def parse_fuel(payload: Any) -> FuelWeek | None:
    """The newest week, with its change from the week before."""

    rows = [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []
    levels = sorted(
        (r for r in rows if r.get("series_type") == "level"),
        key=lambda r: str(r.get("date") or ""),
        reverse=True,
    )
    if not levels:
        return None
    latest = levels[0]
    week = str(latest.get("date") or "")

    def numbers(row: dict[str, Any]) -> dict[str, float]:
        return {
            key: round(float(value), 2)
            for key, value in row.items()
            if key not in ("date", "series_type") and isinstance(value, (int, float))
        }

    change_row = next(
        (r for r in rows if r.get("series_type") == "change_weekly" and r.get("date") == week),
        None,
    )
    if change_row is not None:
        changes = numbers(change_row)
    elif len(levels) > 1:
        before = numbers(levels[1])
        changes = {k: round(v - before.get(k, v), 2) for k, v in numbers(latest).items()}
    else:
        changes = {}
    return FuelWeek(date=week, prices=numbers(latest), changes=changes)


# ---------------------------------------------------------------------------
# Exchange rates
# ---------------------------------------------------------------------------

CURRENCY_NAMES = {
    "USD": "US dollar", "SGD": "Singapore dollar", "EUR": "Euro", "GBP": "Pound sterling",
    "AUD": "Australian dollar", "JPY": "Japanese yen", "CNY": "Chinese yuan",
    "HKD": "Hong Kong dollar", "THB": "Thai baht", "IDR": "Indonesian rupiah",
    "PHP": "Philippine peso", "INR": "Indian rupee", "KRW": "South Korean won",
    "TWD": "New Taiwan dollar", "NZD": "New Zealand dollar", "CAD": "Canadian dollar",
    "CHF": "Swiss franc", "SAR": "Saudi riyal", "AED": "UAE dirham", "BND": "Brunei dollar",
    "VND": "Vietnamese dong", "PKR": "Pakistani rupee", "NPR": "Nepalese rupee",
    "MMK": "Myanmar kyat", "KHR": "Cambodian riel", "EGP": "Egyptian pound",
    "SDR": "IMF special drawing right",
}
# What the overview shows, in this order.
MAJOR_CURRENCIES = ("USD", "SGD", "EUR", "GBP", "CNY", "JPY", "AUD", "THB", "IDR", "HKD", "INR", "KRW")


@dataclass(frozen=True)
class Rate:
    code: str
    unit: int
    buying: float | None
    selling: float | None
    middle: float | None
    date: str

    @property
    def name(self) -> str:
        return CURRENCY_NAMES.get(self.code, self.code)

    @property
    def per_one(self) -> float | None:
        """Ringgit for one unit of the currency, whatever unit BNM quotes."""

        return self.middle / self.unit if self.middle and self.unit else None


@dataclass(frozen=True)
class RateSheet:
    session: str
    updated: str
    rates: dict[str, Rate]


def parse_forex(payload: Any) -> RateSheet:
    if not isinstance(payload, dict):
        return RateSheet("", "", {})
    meta = payload.get("meta") or {}
    rates: dict[str, Rate] = {}
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("currency_code") or "").upper()
        rate = row.get("rate") or {}
        if not code or not isinstance(rate, dict):
            continue

        def num(value: Any) -> float | None:
            return float(value) if isinstance(value, (int, float)) else None

        rates[code] = Rate(
            code=code,
            unit=int(row.get("unit") or 1),
            buying=num(rate.get("buying_rate")),
            selling=num(rate.get("selling_rate")),
            middle=num(rate.get("middle_rate")),
            date=str(rate.get("date") or ""),
        )
    return RateSheet(
        session=str(meta.get("session") or ""),
        updated=str(meta.get("last_updated") or ""),
        rates=rates,
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Extras:
    def __init__(self, db: Any) -> None:
        self.db = db
        self._client = httpx.AsyncClient(
            timeout=config.HTTP_TIMEOUT_SECONDS,
            headers={"user-agent": config.USER_AGENT},
            follow_redirects=True,
        )
        self._zones: list[Zone] = []

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_json(
        self,
        key: str,
        url: str,
        max_age: int,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[Any, int, bool]:
        """(payload, fetched_at, stale), from cache when fresh enough."""

        cached = await self.db.cache_get(key, max_age)
        if cached is not None:
            try:
                return json.loads(cached[0]), cached[1], False
            except ValueError:
                pass
        try:
            response = await self._client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Fetch failed for %s: %s", key, exc)
            await self.db.record_feed_fail(key.split(":")[0], str(exc))
            stale = await self.db.cache_get_any_age(key)
            if stale is not None:
                try:
                    return json.loads(stale[0]), stale[1], True
                except ValueError:
                    pass
            raise ExtrasError(f"Could not reach the {key.split(':')[0]} service.") from exc

        await self.db.cache_put(key, json.dumps(payload).encode())
        await self.db.record_feed_ok(key.split(":")[0])
        return payload, int(now_myt().timestamp()), False

    # -- prayer -----------------------------------------------------------

    async def zones(self) -> list[Zone]:
        payload, _at, _stale = await self._get_json("prayer:zones", ZONES_URL, 7 * 86400)
        zones = parse_zones(payload)
        if zones:
            self._zones = zones
        return zones

    def peek_zones(self, query: str, limit: int = 25) -> list[Zone]:
        if not query.strip():
            return self._zones[:limit]
        q = normalise(query)
        exact = [z for z in self._zones if z.code.lower() == q.replace(" ", "")]
        if exact:
            return exact
        scored = sorted(
            ((score_match(f"{z.state} {z.districts}", query), z) for z in self._zones),
            key=lambda pair: (-pair[0], pair[1].code),
        )
        return [z for score, z in scored if score > 0][:limit]

    async def zone(self, code_or_name: str) -> tuple[Zone | None, list[Zone]]:
        zones = await self.zones()
        code = code_or_name.strip().upper()
        for zone in zones:
            if zone.code == code:
                return zone, []
        matches = self.peek_zones(code_or_name)
        if len(matches) == 1:
            return matches[0], []
        return None, matches

    async def prayer_month(self, zone: str, year: int, month: int) -> list[PrayerDay]:
        payload, _at, _stale = await self._get_json(
            f"prayer:{zone}:{year}-{month:02d}",
            PRAYER_URL.format(zone=zone),
            config.PRAYER_CACHE_SECONDS,
            params={"year": year, "month": month},
        )
        return parse_prayer_month(payload)

    async def prayer_day(self, zone: str, day: date) -> PrayerDay | None:
        for entry in await self.prayer_month(zone, day.year, day.month):
            if entry.day == day:
                return entry
        return None

    # -- fuel -------------------------------------------------------------

    async def fuel(self) -> tuple[FuelWeek | None, int, bool]:
        payload, fetched_at, stale = await self._get_json(
            "fuel:weekly",
            FUEL_URL,
            config.FUEL_CACHE_SECONDS,
            params={"id": "fuelprice", "limit": 6, "sort": "-date"},
        )
        return parse_fuel(payload), fetched_at, stale

    # -- forex ------------------------------------------------------------

    async def forex(self) -> tuple[RateSheet, int, bool]:
        payload, fetched_at, stale = await self._get_json(
            "forex:latest",
            FOREX_URL,
            config.FOREX_CACHE_SECONDS,
            headers={"accept": BNM_ACCEPT},
        )
        return parse_forex(payload), fetched_at, stale


def epoch_to_myt(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, MYT)
