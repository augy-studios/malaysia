"""Offline tests. Nothing here touches the network or Discord."""

from __future__ import annotations

import io
import re
import struct
import zipfile
from datetime import datetime

import pytest

from bot.config import Settings
from bot.database import Database
from bot.extras import format_hijri, parse_forex, parse_fuel, parse_prayer_month, parse_zones
from bot.timeutils import MYT, in_quiet_hours, now_myt, parse_gtfs_time, service_epoch
from bot.transit import Vehicle, decode_vehicle_positions, parse_static_zip, title_case
from bot.ui import Button, Link, Option, Screen, Select, TokenButton, TokenSelect, materialise
from bot.weather import FloodStation, Warning, parse_forecast, translate_forecast


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def test_every_command_allows_user_installs_and_every_context(db):
    """The failure mode that broke user installs on the MRT guessing game bot.

    A command declared without user installs, or without private channels,
    never shows in a user install whatever the Developer Portal says.
    """

    from bot.main import MalaysiaBot

    bot = MalaysiaBot(Settings(discord_token="x"), db)
    payloads = [command.to_dict(bot.tree) for command in bot.tree.get_commands()]
    assert payloads
    for payload in payloads:
        assert sorted(payload["contexts"]) == [0, 1, 2], payload["name"]
        assert sorted(payload["integration_types"]) == [0, 1], payload["name"]


async def test_command_names_never_mention_the_bot(db):
    from bot.main import MalaysiaBot

    bot = MalaysiaBot(Settings(discord_token="x"), db)
    text = str([c.to_dict(bot.tree) for c in bot.tree.get_commands()]).lower()
    assert "boleh" not in text
    assert "—" not in text  # no em dashes in copy


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


def test_forecast_translation():
    assert translate_forecast("Tiada hujan") == "No rain"
    assert translate_forecast("Ribut petir di beberapa tempat di kawasan pedalaman") == (
        "Thunderstorms in a few inland areas"
    )
    assert translate_forecast("Hujan di kebanyakan tempat") == "Rain in most places"
    assert translate_forecast("Something new") == "Something new"


def test_dry_day_is_not_wet():
    locations = parse_forecast(
        [
            {
                "location": {"location_id": "Ds001", "location_name": "Ipoh"},
                "date": "2026-09-24",
                "summary_forecast": "Tiada hujan",
                "morning_forecast": "Tiada hujan",
                "afternoon_forecast": "Tiada hujan",
                "night_forecast": "Tiada hujan",
                "min_temp": 24,
                "max_temp": 33,
            }
        ]
    )
    today = next(iter(locations.values())).today
    assert today is not None and not today.is_wet
    assert today.temp_range == "24 to 33°C"


def test_no_advisory_is_not_a_warning():
    standing = Warning("1", "No Advisory", "No tropical cyclone", "2020-01-01", "2099-01-01")
    real = Warning("2", "Thunderstorm warning", "Selangor", "2020-01-01", "2099-01-01")
    assert not standing.is_active
    assert real.is_active
    assert real.mentions("selangor")


def _gauge(updated: str, indicator: str = "DANGER") -> FloodStation:
    return FloodStation(
        "s1", "Sg Klang", "Klang", "SELANGOR", "", "", 3.0, 1, 2, 2.5, 3,
        indicator, "RISING", updated,
    )


def test_old_gauge_readings_never_count_as_elevated():
    fresh = _gauge(now_myt().strftime("%Y-%m-%d %H:%M:%S"))
    stale = _gauge("2024-02-23 14:45:00")
    assert fresh.is_elevated
    assert not stale.is_elevated


def test_state_filter_matches_spelled_out_territories():
    kl = FloodStation("s", "x", "", "WILAYAH PERSEKUTUAN KUALA LUMPUR", "", "", None, None, None, None, None, "", "", "")
    assert kl.in_state("Kuala Lumpur")
    assert not kl.in_state("Selangor")


# ---------------------------------------------------------------------------
# Extras
# ---------------------------------------------------------------------------


def test_fuel_uses_the_published_change_row():
    week = parse_fuel(
        [
            {"date": "2026-09-24", "series_type": "change_weekly", "ron95": 0.2, "ron97": 0.2},
            {"date": "2026-09-24", "series_type": "level", "ron95": 4.57, "ron97": 5.05, "brand_new": 1.0},
            {"date": "2026-09-17", "series_type": "level", "ron95": 4.37, "ron97": 4.85},
        ]
    )
    assert week is not None and week.date == "2026-09-24"
    rows = {label: (price, change) for label, price, change in week.rows()}
    assert rows["RON95"] == (4.57, 0.2)
    # A column added upstream later still shows.
    assert "BRAND_NEW" in rows


def test_forex_per_unit():
    sheet = parse_forex(
        {
            "meta": {"session": "1700"},
            "data": [
                {"currency_code": "JPY", "unit": 100, "rate": {"middle_rate": 2.9, "date": "2026-09-23"}},
                {"currency_code": "USD", "unit": 1, "rate": {"middle_rate": 4.075, "date": "2026-09-23"}},
            ],
        }
    )
    assert sheet.rates["USD"].per_one == pytest.approx(4.075)
    assert sheet.rates["JPY"].per_one == pytest.approx(0.029)


def test_prayer_parsing():
    days = parse_prayer_month(
        {
            "zone": "WLY01",
            "year": 2026,
            "month_number": 9,
            "prayers": [{"day": 1, "hijri": "1448-03-19", "fajr": 100, "dhuhr": 200, "isha": 300}],
        }
    )
    assert days[0].times == {"fajr": 100, "dhuhr": 200, "isha": 300}
    assert days[0].upcoming(150) == ("dhuhr", 200)
    assert format_hijri("1448-03-19") == "19 Rabiulawal 1448H"
    zones = parse_zones([{"jakimCode": "WLY01", "negeri": "WP", "daerah": "Kuala Lumpur"}])
    assert zones[0].code == "WLY01"


# ---------------------------------------------------------------------------
# GTFS
# ---------------------------------------------------------------------------


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buffer.getvalue()


def _feed(today_runs: bool = True):
    today = now_myt().date()
    weekday = [0] * 7
    weekday[today.weekday()] = 1 if today_runs else 0
    days = ",".join(str(d) for d in weekday)
    blob = _zip(
        {
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nA,STESEN A,3.1,101.6\nB,STESEN B,3.2,101.7\n",
            "routes.txt": "route_id,route_short_name,route_long_name\nAG,AGL,AMPANG LINE\n",
            "trips.txt": "route_id,service_id,trip_id,trip_headsign\nAG,S1,T1,STESEN B\nAG,S1,T2,STESEN B\n",
            # The route_id column here holds the SHORT name, as Rapid KL's does.
            "stop_times.txt": (
                "trip_id,route_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,AGL,06:00:00,06:00:00,A,1\nT1,AGL,06:10:00,06:10:00,B,2\n"
                "T2,AGL,25:00:00,25:00:00,A,1\nT2,AGL,25:10:00,25:10:00,B,2\n"
            ),
            "frequencies.txt": "trip_id,start_time,end_time,headway_secs\nT1,06:00:00,07:00:00,600\n",
            "calendar.txt": (
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                f"S1,{days},20200101,20991231\n"
            ),
        }
    )
    return parse_static_zip("rapid-rail-kl", blob)


def test_route_join_goes_through_trips_not_stop_times():
    feed = _feed()
    assert feed.stop_routes["A"] == {"AG"}


def test_headways_expand_into_runs():
    feed = _feed()
    assert len(feed.runs["T1"]) == 6  # every 10 minutes for an hour
    times = [s for s, trip, _ in feed.schedule["A"]["AG"] if trip == "T1"]
    assert times[:2] == [6 * 3600, 6 * 3600 + 600]


def test_departures_follow_the_service_calendar():
    today = now_myt().date()
    at_five = service_epoch(today, 5 * 3600)
    running = _feed(today_runs=True).departures("A", now=at_five, limit=20, horizon_hours=3)
    assert len(running) == 6
    stopped = _feed(today_runs=False).departures("A", now=at_five, limit=20, horizon_hours=3)
    assert stopped == []


def test_after_midnight_departures_belong_to_the_previous_day():
    today = now_myt().date()
    # 00:30 tomorrow is 24:30 on today's service day, so the 25:00 trip is next.
    half_past_midnight = service_epoch(today, 24 * 3600 + 1800)
    deps = _feed().departures("A", now=half_past_midnight, limit=1, horizon_hours=2)
    assert deps and deps[0].trip_id == "T2"
    assert deps[0].epoch == service_epoch(today, 25 * 3600)


def test_terminus_is_not_a_departure():
    today = now_myt().date()
    assert _feed().departures("B", now=service_epoch(today, 5 * 3600), horizon_hours=3) == []


def test_trip_calls_are_shifted_by_the_run_offset():
    feed = _feed()
    today = now_myt().date()
    calls = feed.trip_calls("T1", 1200, today)
    assert [epoch for _stop, epoch in calls] == [
        service_epoch(today, 6 * 3600 + 1200),
        service_epoch(today, 6 * 3600 + 1800),
    ]


def test_names_are_softened():
    assert title_case("BANDAR TASIK SELATAN") == "Bandar Tasik Selatan"
    assert title_case("PULAU SEBANG/TAMPIN") == "Pulau Sebang/Tampin"
    assert title_case("KL SENTRAL") == "KL Sentral"
    assert title_case("Already Mixed") == "Already Mixed"
    assert parse_gtfs_time("25:10:00") == 25 * 3600 + 600


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(num: int, wire: int, value: bytes | int) -> bytes:
    tag = _varint((num << 3) | wire)
    if wire == 0:
        return tag + _varint(int(value))
    if wire == 5:
        return tag + bytes(value)
    return tag + _varint(len(value)) + bytes(value)


def test_realtime_protobuf_and_ktmb_speed_units():
    position = (
        _field(1, 5, struct.pack("<f", 3.14))
        + _field(2, 5, struct.pack("<f", 101.69))
        + _field(5, 5, struct.pack("<f", 80.0))
    )
    vehicle = (
        _field(1, 2, _field(1, 2, b"trip1") + _field(5, 2, b"route1"))
        + _field(2, 2, position)
        + _field(5, 0, 1_790_000_000)
        + _field(8, 2, _field(1, 2, b"v9") + _field(2, 2, b"KTM 2101"))
    )
    message = _field(2, 2, _field(1, 2, b"e1") + _field(4, 2, vehicle))
    [decoded] = decode_vehicle_positions(message)
    assert decoded.route_id == "route1" and decoded.label == "KTM 2101"
    assert decoded.lat == pytest.approx(3.14, abs=1e-4)
    # KTMB reports km/h already. Converting would claim 288 km/h.
    assert decoded.speed_kmh("ktmb") == pytest.approx(80.0)
    assert Vehicle(speed=10.0).speed_kmh("rapid-bus-kl") == pytest.approx(36.0)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def test_quiet_hours_wrap_midnight():
    user = {"quiet_enabled": 1, "quiet_from": "23:00", "quiet_to": "06:00"}
    at = lambda h: datetime(2026, 9, 24, h, 0, tzinfo=MYT)  # noqa: E731
    assert in_quiet_hours(user, at(23))
    assert in_quiet_hours(user, at(2))
    assert not in_quiet_hours(user, at(6))
    assert not in_quiet_hours({**user, "quiet_enabled": 0}, at(2))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_button_tokens_survive_a_restart(tmp_path):
    path = tmp_path / "bot.sqlite3"
    db = Database(path)
    await db.connect()
    token = await db.make_token("tr.stop", {"o": "ktmb", "s": "123"}, 42)
    assert token == await db.make_token("tr.stop", {"s": "123", "o": "ktmb"}, 42)
    assert token != await db.make_token("tr.stop", {"o": "ktmb", "s": "123"}, 43)
    await db.close()

    reopened = Database(path)
    await reopened.connect()
    assert await reopened.resolve_token(token) == ("tr.stop", {"o": "ktmb", "s": "123"}, 42)
    assert await reopened.resolve_token("nope") is None
    await reopened.close()


async def test_scheduled_jobs_dedupe_and_survive(tmp_path):
    path = tmp_path / "bot.sqlite3"
    db = Database(path)
    await db.connect()
    assert await db.schedule_job("digest", 100, "u:1", dedupe_key="digest:1:2026-09-24")
    assert not await db.schedule_job("digest", 100, "u:1", dedupe_key="digest:1:2026-09-24")
    await db.close()

    reopened = Database(path)
    await reopened.connect()
    assert [row["job_type"] for row in await reopened.due_jobs(now=200)] == ["digest"]
    await reopened.close()


async def test_alert_dedupe_and_data_deletion(db):
    assert await db.should_alert("u:1", "quake:9", 3600)
    assert not await db.should_alert("u:1", "quake:9", 3600)
    assert await db.should_alert("g:5", "quake:9", 3600)

    await db.add_favourite(1, "town", "ipoh", "Ipoh")
    await db.set_subscription(1, "warning", True)
    await db.schedule_job("prayer", 10, "u:1", dedupe_key="p")
    await db.delete_user_data(1)
    assert await db.list_favourites(1) == []
    assert await db.user_subscriptions(1) == set()
    assert await db.count_jobs() == 0
    with pytest.raises(ValueError):
        await db.set_pref(1, "user_id", 2)


async def test_turning_an_alert_off_drops_its_queued_jobs(db):
    await db.set_subscription(1, "prayer", True)
    await db.schedule_job("prayer", 10, "u:1", dedupe_key="p1")
    await db.schedule_job("digest", 10, "u:1", dedupe_key="d1")
    await db.set_subscription(1, "prayer", False)
    assert await db.count_jobs("prayer") == 0
    assert await db.count_jobs("digest") == 1


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


async def test_materialised_components_match_the_persistent_patterns(db):
    screen = Screen(
        embeds=[],
        rows=[
            Select("Pick", [Option("One", "wx.town", {"k": "ipoh"}), Option("Two", "wx.town", {"k": "kl"})]),
            [
                Button("Page 1", "wx.quakes", {"p": 0}, disabled=True),
                Button("Page 1", "wx.quakes", {"p": 0}, disabled=True),
                Link("Web", "https://example.com"),
            ],
        ],
    )
    view = await materialise(db, screen, 42)
    assert view is not None
    ids = [item.custom_id for item in view.children if getattr(item, "custom_id", None)]
    assert len(ids) == len(set(ids)), "custom ids must be unique within a message"
    button_re = TokenButton.__discord_ui_compiled_template__
    select_re = TokenSelect.__discord_ui_compiled_template__
    for custom_id in ids:
        assert len(custom_id) <= 100
        assert re.fullmatch(button_re, custom_id) or re.fullmatch(select_re, custom_id)
    select = next(item for item in view.children if item.custom_id.startswith("mb:s:"))
    for option in select.item.options:
        assert (await db.resolve_token(option.value))[0] == "wx.town"


# ---------------------------------------------------------------------------
# Pressing a button
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs):
        self._done = True
        self.calls.append(("defer", kwargs))

    async def send_message(self, *args, **kwargs):
        self._done = True
        self.calls.append(("send_message", {"args": args, **kwargs}))


class _Followup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, *args, **kwargs):
        self.sent.append({"args": args, **kwargs})


def _interaction(bot, user_id: int):
    from types import SimpleNamespace

    edits: list[dict] = []

    async def edit_original_response(**kwargs):
        edits.append(kwargs)

    return SimpleNamespace(
        client=bot,
        user=SimpleNamespace(id=user_id),
        response=_Response(),
        followup=_Followup(),
        edit_original_response=edit_original_response,
        edits=edits,
    )


async def test_pressing_buttons_after_a_restart(db):
    """Owner presses edit in place, other people get a private copy, personal
    menus refuse other people, and a token nobody stored says so."""

    from types import SimpleNamespace

    from bot.ui import ACTIONS, EXPIRED, NOT_YOURS, action, dispatch, embed

    if "test.nav" not in ACTIONS:

        @action("test.nav")
        async def _nav(ctx, payload):
            return Screen(embeds=[embed("Page", str(payload.get("p")))], rows=[[Button("Next", "test.nav", {"p": 2})]])

        @action("test.mine", personal=True)
        async def _mine(ctx, payload):
            return Screen(embeds=[embed("Settings")])

    bot = SimpleNamespace(db=db)
    nav = await db.make_token("test.nav", {"p": 1}, 42)
    mine = await db.make_token("test.mine", {}, 42)

    owner = _interaction(bot, 42)
    await dispatch(owner, nav)
    assert owner.response.calls[0] == ("defer", {})
    assert owner.edits and owner.edits[0]["embeds"][0].description == "1"
    assert owner.edits[0]["view"] is not None

    stranger = _interaction(bot, 7)
    await dispatch(stranger, nav)
    assert stranger.response.calls[0] == ("defer", {"ephemeral": True, "thinking": True})
    assert not stranger.edits
    assert stranger.followup.sent[0]["ephemeral"] is True

    stranger = _interaction(bot, 7)
    await dispatch(stranger, mine)
    assert stranger.response.calls == [("send_message", {"args": (NOT_YOURS,), "ephemeral": True})]

    lost = _interaction(bot, 42)
    await dispatch(lost, "never-issued")
    assert lost.response.calls == [("send_message", {"args": (EXPIRED,), "ephemeral": True})]


def test_presence_counts_guilds():
    from bot.main import presence_text

    assert presence_text(1) == "Truly Asia in 1 guild"
    assert presence_text(12) == "Truly Asia in 12 guilds"
