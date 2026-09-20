"""Tests for the parts that are easy to get quietly wrong.

These run offline. The GTFS tests build small zips in memory rather than
hitting data.gov.my, so the suite is fast and works without a network.
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.database import Database
from bot.gtfs import (
    KTMB,
    RAPID_RAIL_KL,
    Vehicle,
    decode_vehicle_positions,
    has_live,
    haversine_m,
    parse_static_zip,
)
from bot.richtext import RichDoc, _buttons_to_markup, b, to_classic_html
from bot.timeutils import (
    format_time,
    format_wait,
    in_quiet_hours,
    next_occurrence_epoch,
    parse_gtfs_date,
    parse_gtfs_time,
    within_window,
)
from bot.views import _ordered_stations, _upcoming_times
from telethon.tl.custom import Button


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _csv(rows: list[dict[str, str]]) -> str:
    if not rows:
        return ""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def build_zip(files: dict[str, list[dict[str, str]]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, rows in files.items():
            zf.writestr(name, _csv(rows))
    return buffer.getvalue()


def rapid_like_zip() -> bytes:
    """A miniature feed shaped like the real Rapid KL rail bundle.

    Note the trap this reproduces: stop_times.txt carries a route_id column
    holding the SHORT name ("AGL"), while routes.txt uses the id ("AG"). Only
    the trips.txt join gives the right answer.
    """

    return build_zip(
        {
            "calendar.txt": [
                {
                    "service_id": "MonFri",
                    "monday": "1", "tuesday": "1", "wednesday": "1",
                    "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
                    "start_date": "20200101", "end_date": "20301231",
                },
                {
                    "service_id": "Sun",
                    "monday": "0", "tuesday": "0", "wednesday": "0",
                    "thursday": "0", "friday": "0", "saturday": "0", "sunday": "1",
                    "start_date": "20200101", "end_date": "20301231",
                },
            ],
            "routes.txt": [
                {
                    "route_id": "AG", "route_short_name": "AGL",
                    "route_long_name": "LRT Ampang Line", "route_type": "1",
                    "category": "LRT", "status": "valid", "route_color": "FF0000",
                }
            ],
            "stops.txt": [
                {
                    "stop_id": "AG18", "stop_name": "AMPANG",
                    "stop_lat": "3.150318", "stop_lon": "101.760049",
                    "category": "LRT", "status": "valid",
                },
                {
                    "stop_id": "AG17", "stop_name": "CAHAYA",
                    "stop_lat": "3.140575", "stop_lon": "101.756677",
                    "category": "LRT", "status": "valid",
                },
                {
                    "stop_id": "AG16", "stop_name": "CEMPAKA",
                    "stop_lat": "3.138324", "stop_lon": "101.752979",
                    "category": "LRT", "status": "withdrawn",
                },
            ],
            "trips.txt": [
                {
                    "route_id": "AG", "service_id": "MonFri",
                    "trip_id": "AGL_MonFri_0", "trip_headsign": "To Sentul Timur",
                    "direction_id": "0",
                },
                {
                    "route_id": "AG", "service_id": "Sun",
                    "trip_id": "AGL_Sun_0", "trip_headsign": "To Sentul Timur",
                    "direction_id": "0",
                },
            ],
            "stop_times.txt": [
                {
                    "route_id": "AGL", "direction_id": "0", "trip_id": "AGL_MonFri_0",
                    "arrival_time": "6:00:00", "departure_time": "6:00:00",
                    "stop_id": "AG18", "stop_sequence": "1",
                },
                {
                    "route_id": "AGL", "direction_id": "0", "trip_id": "AGL_MonFri_0",
                    "arrival_time": "6:02:00", "departure_time": "6:02:00",
                    "stop_id": "AG17", "stop_sequence": "2",
                },
                {
                    "route_id": "AGL", "direction_id": "0", "trip_id": "AGL_Sun_0",
                    "arrival_time": "8:00:00", "departure_time": "8:00:00",
                    "stop_id": "AG18", "stop_sequence": "1",
                },
                {
                    "route_id": "AGL", "direction_id": "0", "trip_id": "AGL_Sun_0",
                    "arrival_time": "8:02:00", "departure_time": "8:02:00",
                    "stop_id": "AG17", "stop_sequence": "2",
                },
            ],
            # 06:00 to 07:00 every 10 minutes is 6 departures.
            "frequencies.txt": [
                {
                    "trip_id": "AGL_MonFri_0", "start_time": "6:00:00",
                    "end_time": "7:00:00", "headway_secs": "600",
                }
            ],
        }
    )


def ktmb_like_zip() -> bytes:
    """A miniature feed shaped like KTMB: exact times, no frequencies."""

    return build_zip(
        {
            "calendar.txt": [
                {
                    "service_id": "komuter_weekday",
                    "monday": "1", "tuesday": "1", "wednesday": "1",
                    "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
                    "start_date": "20200101", "end_date": "20301231",
                }
            ],
            "calendar_dates.txt": [
                # A public holiday on a Wednesday removes the weekday service.
                {"service_id": "komuter_weekday", "date": "20260930", "exception_type": "2"},
            ],
            "routes.txt": [
                {
                    "route_id": "KC05_KB18", "route_short_name": "Seremban Line",
                    "route_long_name": "KTM Batu Caves - Pulau Sebang",
                    "route_type": "0",
                }
            ],
            "stops.txt": [
                {"stop_id": "19100", "stop_name": "KL SENTRAL",
                 "stop_lat": "3.134", "stop_lon": "101.686"},
                {"stop_id": "50600", "stop_name": "BATU CAVES",
                 "stop_lat": "3.237796", "stop_lon": "101.681215"},
            ],
            "trips.txt": [
                {"route_id": "KC05_KB18", "service_id": "komuter_weekday",
                 "trip_id": "2000", "direction_id": "0"},
            ],
            "stop_times.txt": [
                {"trip_id": "2000", "arrival_time": "07:00:00",
                 "departure_time": "07:00:00", "stop_id": "50600", "stop_sequence": "1"},
                {"trip_id": "2000", "arrival_time": "07:30:00",
                 "departure_time": "07:30:00", "stop_id": "19100", "stop_sequence": "2"},
            ],
        }
    )


# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------


def test_parse_gtfs_time_handles_hours_past_midnight():
    assert parse_gtfs_time("00:00:00") == 0
    assert parse_gtfs_time("07:30:00") == 27000
    # 25:10 belongs to the previous service day and must not wrap to 01:10.
    assert parse_gtfs_time("25:10:00") == 25 * 3600 + 600
    assert parse_gtfs_time("6:05") == 6 * 3600 + 300


def test_parse_gtfs_time_rejects_rubbish():
    assert parse_gtfs_time("") is None
    assert parse_gtfs_time("not a time") is None
    assert parse_gtfs_time("10:75:00") is None


def test_format_time_marks_next_day():
    assert format_time("07:05:00", "24h") == "07:05"
    assert format_time("07:05:00", "12h") == "7:05am"
    assert format_time("19:05:00", "12h") == "7:05pm"
    assert format_time("00:30:00", "12h") == "12:30am"
    assert format_time("25:10:00", "24h") == "01:10 (+1d)"


def test_format_wait_phrasing():
    assert format_wait(0) == "departing now"
    assert format_wait(60) == "in 1 minute"
    assert format_wait(600) == "in 10 minutes"
    assert format_wait(3600) == "in 1 hour"
    assert format_wait(5400) == "in 1h 30m"


def test_parse_gtfs_date():
    assert parse_gtfs_date("20260921") == date(2026, 9, 21)
    assert parse_gtfs_date("") is None
    assert parse_gtfs_date("2026-09-21") is None
    assert parse_gtfs_date("20261345") is None


def test_quiet_hours_wrap_midnight():
    user = {"quiet_enabled": 1, "quiet_from": "23:00", "quiet_to": "06:00"}
    from datetime import datetime

    from bot.timeutils import MYT

    assert in_quiet_hours(user, datetime(2026, 9, 21, 23, 30, tzinfo=MYT))
    assert in_quiet_hours(user, datetime(2026, 9, 21, 2, 0, tzinfo=MYT))
    assert not in_quiet_hours(user, datetime(2026, 9, 21, 12, 0, tzinfo=MYT))
    assert not in_quiet_hours(
        {"quiet_enabled": 0, "quiet_from": "23:00", "quiet_to": "06:00"},
        datetime(2026, 9, 21, 23, 30, tzinfo=MYT),
    )


def test_within_window_handles_both_directions():
    from datetime import datetime

    from bot.timeutils import MYT

    noon = datetime(2026, 9, 21, 12, 0, tzinfo=MYT)
    assert within_window("05:00", "23:59", noon)
    assert not within_window("22:00", "23:00", noon)
    # A window that wraps midnight.
    assert within_window("22:00", "13:00", noon)


def test_next_occurrence_is_in_the_future():
    from datetime import datetime

    from bot.timeutils import MYT

    reference = datetime(2026, 9, 21, 12, 0, tzinfo=MYT)
    later = next_occurrence_epoch("18:00:00", reference)
    assert later is not None and later > reference.timestamp()
    # A time already past today rolls to tomorrow.
    earlier = next_occurrence_epoch("06:00:00", reference)
    assert earlier is not None and earlier > reference.timestamp()


# ---------------------------------------------------------------------------
# GTFS parsing
# ---------------------------------------------------------------------------


def test_rapid_feed_joins_trips_not_the_stop_times_route_column():
    """The stop_times route_id is the short name and must not be used to join."""

    feed = parse_static_zip(RAPID_RAIL_KL, rapid_like_zip())
    # "AG" is the real route id. If the join used stop_times.route_id we would
    # have a phantom "AGL" route instead.
    assert set(feed.routes) == {"AG"}
    assert "AGL" not in feed.route_stops
    assert feed.route_stops["AG"] == {"AG18", "AG17"}


def test_withdrawn_stations_are_dropped():
    feed = parse_static_zip(RAPID_RAIL_KL, rapid_like_zip())
    assert "AG16" not in feed.stops


def test_station_names_are_title_cased():
    feed = parse_static_zip(RAPID_RAIL_KL, rapid_like_zip())
    assert feed.stops["AG18"].display == "Ampang"
    ktmb = parse_static_zip(KTMB, ktmb_like_zip())
    # "KL" stays upper, the rest is softened.
    assert ktmb.stops["19100"].display == "KL Sentral"


def test_title_case_handles_separators_and_acronyms():
    from bot.gtfs import _title_case

    # A second name after a slash or hyphen must be capitalised too.
    assert _title_case("PULAU SEBANG/TAMPIN") == "Pulau Sebang/Tampin"
    assert _title_case("JALAN TEMPLER-TAMAN") == "Jalan Templer-Taman"
    assert _title_case("TAMAN BAHAGIA (BRT)") == "Taman Bahagia (BRT)"
    # Acronyms and codes stay as they are.
    assert _title_case("KL SENTRAL") == "KL Sentral"
    assert _title_case("KLCC") == "KLCC"
    assert _title_case("USJ7") == "USJ7"
    # Mixed-case names are left alone, since the feed meant them.
    assert _title_case("KL Sentral - Redone") == "KL Sentral - Redone"


def test_speed_units_are_not_double_converted():
    """The KTMB feed has been seen reporting km/h where GTFS specifies m/s."""

    # A plausible m/s reading is converted.
    assert Vehicle(speed=20.0).speed_kmh == pytest.approx(72.0)
    # A value only sensible as km/h is passed through, not multiplied to 800.
    assert Vehicle(speed=223.0).speed_kmh == pytest.approx(223.0)
    # Nothing to report.
    assert Vehicle(speed=0.0).speed_kmh is None
    assert Vehicle(speed=None).speed_kmh is None


def test_departure_times_are_zero_padded_consistently():
    """Feeds mix '6:00:00' and '06:00:00'; the schedule must not."""

    feed = parse_static_zip(RAPID_RAIL_KL, rapid_like_zip())
    sunday = feed.departures("AG18", "AG", date(2026, 9, 20))
    assert sunday == ["08:00:00"]
    for value in feed.departures("AG18", "AG", date(2026, 9, 21)):
        assert len(value) == 8 and value[2] == ":"


def test_frequencies_expand_into_real_departures():
    """One template trip plus a 10 minute headway is six departures, not one."""

    feed = parse_static_zip(RAPID_RAIL_KL, rapid_like_zip())
    monday = date(2026, 9, 21)
    departures = feed.departures("AG18", "AG", monday)
    assert departures == [
        "06:00:00", "06:10:00", "06:20:00", "06:30:00", "06:40:00", "06:50:00",
    ]


def test_frequency_expansion_shifts_downstream_stops():
    feed = parse_static_zip(RAPID_RAIL_KL, rapid_like_zip())
    monday = date(2026, 9, 21)
    # The second station is two minutes down the line on every run.
    assert feed.departures("AG17", "AG", monday)[:3] == [
        "06:02:00", "06:12:00", "06:22:00",
    ]


def test_service_calendar_filters_by_weekday():
    feed = parse_static_zip(RAPID_RAIL_KL, rapid_like_zip())
    monday = date(2026, 9, 21)
    sunday = date(2026, 9, 20)

    weekday = feed.departures("AG18", "AG", monday)
    weekend = feed.departures("AG18", "AG", sunday)

    assert "06:00:00" in weekday and "08:00:00" not in weekday
    # The Sunday trip has no frequencies entry, so it stays a single departure.
    assert weekend == ["08:00:00"]


def test_calendar_dates_exception_removes_service():
    feed = parse_static_zip(KTMB, ktmb_like_zip())
    normal_wednesday = date(2026, 9, 23)
    holiday_wednesday = date(2026, 9, 30)

    assert feed.departures("50600", "KC05_KB18", normal_wednesday) == ["07:00:00"]
    assert feed.departures("50600", "KC05_KB18", holiday_wednesday) == []


def test_unknown_service_is_treated_as_running():
    """A missing calendar entry must not silently hide every train."""

    feed = parse_static_zip(KTMB, ktmb_like_zip())
    assert feed.runs_today("no_such_service", date(2026, 9, 21))


def test_trips_for_route_respects_the_calendar():
    feed = parse_static_zip(RAPID_RAIL_KL, rapid_like_zip())
    monday = [t.trip_id for t in feed.trips_for_route("AG", date(2026, 9, 21))]
    sunday = [t.trip_id for t in feed.trips_for_route("AG", date(2026, 9, 20))]
    assert monday == ["AGL_MonFri_0"]
    assert sunday == ["AGL_Sun_0"]


def test_search_is_case_and_space_insensitive():
    feed = parse_static_zip(KTMB, ktmb_like_zip())
    assert [s.stop_id for s in feed.search_stops("kl sentral")] == ["19100"]
    assert [s.stop_id for s in feed.search_stops("SENTRAL")] == ["19100"]
    assert [r.route_id for r in feed.search_routes("seremban")] == ["KC05_KB18"]


def test_ordered_stations_follow_the_line():
    feed = parse_static_zip(RAPID_RAIL_KL, rapid_like_zip())
    # Travel order, not alphabetical: Ampang comes before Cahaya.
    assert _ordered_stations(feed, "AG") == ["AG18", "AG17"]


def test_haversine_matches_a_known_distance():
    # KL Sentral to Batu Caves is roughly 12 km apart.
    metres = haversine_m(3.134, 101.686, 3.237796, 101.681215)
    assert 11000 < metres < 13000


def test_live_only_for_ktmb():
    assert has_live(KTMB)
    assert not has_live(RAPID_RAIL_KL)


# ---------------------------------------------------------------------------
# Realtime decoding
# ---------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _delimited(field: int, body: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(body)) + body


def test_decode_vehicle_positions_reads_a_handmade_feed():
    import struct

    position = (
        _tag(1, 5) + struct.pack("<f", 3.1390)
        + _tag(2, 5) + struct.pack("<f", 101.6869)
        + _tag(5, 5) + struct.pack("<f", 20.0)
    )
    trip = _delimited(1, b"2000") + _delimited(5, b"KC05_KB18")
    descriptor = _delimited(1, b"V123") + _delimited(2, b"Komuter 2000")
    vehicle = (
        _delimited(1, trip)
        + _delimited(2, position)
        + _tag(5, 0) + _varint(1758400000)
        + _delimited(8, descriptor)
    )
    entity = _delimited(1, b"e1") + _delimited(4, vehicle)
    feed = _delimited(2, entity)

    vehicles = decode_vehicle_positions(feed)
    assert len(vehicles) == 1
    got = vehicles[0]
    assert got.trip_id == "2000"
    assert got.route_id == "KC05_KB18"
    assert got.label == "Komuter 2000"
    assert got.has_position
    assert round(got.lat, 3) == 3.139
    assert got.timestamp == 1758400000
    assert got.speed_kmh is not None and round(got.speed_kmh) == 72


def test_decode_empty_feed_is_not_an_error():
    assert decode_vehicle_positions(b"") == []


# ---------------------------------------------------------------------------
# Rich messages
# ---------------------------------------------------------------------------


def test_rich_doc_builds_expected_html():
    doc = RichDoc().heading("Title", 3).para(b("bold")).bullets(["one", "two"])
    html = doc.to_html()
    assert "<h3>Title</h3>" in html
    assert "<b>bold</b>" in html
    assert "<li>one</li>" in html


def test_rich_doc_escapes_user_content():
    doc = RichDoc().heading("<script>alert(1)</script>", 3)
    assert "<script>" not in doc.to_html()
    assert "&lt;script&gt;" in doc.to_html()


def test_classic_fallback_strips_rich_only_tags():
    doc = RichDoc()
    doc.heading("Departures", 3)
    doc.table(["Line", "Time"], [["Ampang", "07:00"]])
    doc.bullets(["first", "second"])
    classic = to_classic_html(doc.to_html())

    for tag in ("<h3>", "<table", "<td>", "<ul>", "<li>"):
        assert tag not in classic
    # The content survives, only the markup is reduced.
    assert "Departures" in classic
    assert "Ampang | 07:00" in classic
    assert "• first" in classic


def test_classic_fallback_keeps_links_and_bold():
    html = RichDoc().para('<a href="https://example.com">Maps</a> and <b>bold</b>').to_html()
    classic = to_classic_html(html)
    assert '<a href="https://example.com">Maps</a>' in classic
    assert "<b>bold</b>" in classic


def test_buttons_convert_to_bot_api_markup():
    """The rich path sends buttons as JSON, so the translation must hold."""

    buttons = [
        [Button.inline("Tap me", b"token123")],
        [Button.url("Open", "https://example.com")],
    ]
    markup = _buttons_to_markup(buttons)
    assert markup == {
        "inline_keyboard": [
            [{"text": "Tap me", "callback_data": "token123"}],
            [{"text": "Open", "url": "https://example.com"}],
        ]
    }


def test_buttons_markup_is_none_when_empty():
    assert _buttons_to_markup(None) is None
    assert _buttons_to_markup([]) is None


def test_upcoming_times_wraps_to_the_next_service_day():
    times = ["06:00:00", "07:00:00", "23:00:00"]
    # Whatever the hour, the user always gets something to look at.
    assert len(_upcoming_times(times, limit=3)) == 3


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def test_callback_tokens_survive_and_resolve(db):
    token = await db.make_callback("stop:view", {"op": KTMB, "stop": "19100"}, 42)
    assert len(token) <= 64  # Telegram's callback_data limit

    resolved = await db.resolve_callback(token)
    assert resolved is not None
    action, payload, owner = resolved
    assert action == "stop:view"
    assert payload == {"op": KTMB, "stop": "19100"}
    assert owner == 42


async def test_callback_tokens_are_reused_not_duplicated(db):
    first = await db.make_callback("stop:view", {"stop": "19100"}, 1)
    second = await db.make_callback("stop:view", {"stop": "19100"}, 1)
    assert first == second
    counts = await db.counts()
    assert counts["callbacks"] == 1


async def test_callback_payload_order_does_not_create_duplicates(db):
    first = await db.make_callback("x", {"a": 1, "b": 2}, 1)
    second = await db.make_callback("x", {"b": 2, "a": 1}, 1)
    assert first == second


async def test_callbacks_outlive_a_reconnect(tmp_path):
    """A button tapped after a restart must still resolve."""

    path = tmp_path / "persist.sqlite3"
    first = Database(path)
    await first.connect()
    token = await first.make_callback("route:view", {"route": "AG"}, 7)
    await first.close()

    second = Database(path)
    await second.connect()
    resolved = await second.resolve_callback(token)
    await second.close()

    assert resolved is not None
    assert resolved[0] == "route:view"
    assert resolved[1] == {"route": "AG"}


async def test_unknown_callback_resolves_to_none(db):
    assert await db.resolve_callback("nope") is None


async def test_favourites_round_trip(db):
    await db.ensure_user(1, 1, "user", "Name")
    assert await db.add_favourite(1, KTMB, "19100", "KL Sentral")
    # The same station twice is not stored twice.
    assert not await db.add_favourite(1, KTMB, "19100", "KL Sentral")

    favourites = await db.list_favourites(1)
    assert len(favourites) == 1

    found = await db.find_favourite(1, KTMB, "19100")
    assert found is not None
    assert await db.remove_favourite(1, found["id"])
    assert await db.list_favourites(1) == []


async def test_removing_a_favourite_removes_its_subscriptions(db):
    await db.ensure_user(1, 1)
    await db.add_favourite(1, KTMB, "19100", "KL Sentral")
    favourite = (await db.list_favourites(1))[0]
    await db.add_subscription(1, "departure", favourite["id"])

    await db.remove_favourite(1, favourite["id"])
    assert await db.list_subscriptions(1) == []


async def test_home_station_is_stored_and_cleared(db):
    await db.ensure_user(1, 1)
    await db.set_home_station(1, KTMB, "19100", "KL Sentral")
    user = await db.get_user(1)
    assert user["home_stop_id"] == "19100"
    assert user["home_stop_name"] == "KL Sentral"

    await db.set_home_station(1, "", "", "")
    user = await db.get_user(1)
    assert user["home_stop_id"] == ""


async def test_set_pref_rejects_unknown_columns(db):
    """The setter interpolates the column name, so it must stay allowlisted."""

    await db.ensure_user(1, 1)
    with pytest.raises(ValueError):
        await db.set_pref(1, "user_id = 2 WHERE 1=1 --", "x")


async def test_scheduled_jobs_dedupe_and_drain(db):
    await db.ensure_user(1, 1)
    assert await db.schedule_job("departure", 1000, 1, {"a": 1}, "dep:1")
    # The same dedupe key does not queue a second copy.
    assert not await db.schedule_job("departure", 1000, 1, {"a": 1}, "dep:1")

    due = await db.due_jobs(now=2000)
    assert len(due) == 1
    await db.delete_job(due[0]["id"])
    assert await db.due_jobs(now=2000) == []


async def test_future_jobs_are_not_due_yet(db):
    await db.ensure_user(1, 1)
    await db.schedule_job("digest", 5000, 1, {}, "digest:1")
    assert await db.due_jobs(now=4000) == []
    assert len(await db.due_jobs(now=6000)) == 1


async def test_live_alert_cooldown_suppresses_repeats(db):
    await db.ensure_user(1, 1)
    assert await db.should_alert_vehicle(1, "19100:V1", 600)
    # A second check inside the cooldown is refused.
    assert not await db.should_alert_vehicle(1, "19100:V1", 600)
    # A different train still alerts.
    assert await db.should_alert_vehicle(1, "19100:V2", 600)


async def test_cache_respects_max_age(db):
    await db.cache_put("static:ktmb", b"payload")
    assert await db.cache_get("static:ktmb", 3600) == b"payload"
    # max_age of zero always counts as expired.
    assert await db.cache_get("static:ktmb", 0) is None
    # The stale read still returns the bytes, which is the upstream-down path.
    stale = await db.cache_get_any_age("static:ktmb")
    assert stale is not None and stale[0] == b"payload"


async def test_legacy_database_with_duplicate_rows_still_opens(tmp_path):
    """An older database must not be able to stop the bot from starting.

    Before the partial index existed, toggling an account-wide subscription
    repeatedly stacked duplicate rows. Opening such a file has to clean them up
    rather than fail on the new unique index.
    """

    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            favourite_id INTEGER,
            days TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',
            window_from TEXT NOT NULL DEFAULT '05:00',
            window_to TEXT NOT NULL DEFAULT '23:59',
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            UNIQUE(user_id, kind, favourite_id)
        );
        """
    )
    for _ in range(3):
        legacy.execute(
            "INSERT INTO subscriptions (user_id, kind, favourite_id, created_at) "
            "VALUES (1, 'digest', NULL, 0)"
        )
    legacy.commit()
    legacy.close()

    database = Database(path)
    await database.connect()
    try:
        assert len(await database.list_subscriptions(1)) == 1
        # The columns added after the first release are present afterwards.
        await database.ensure_user(1, 1)
        user = await database.get_user(1)
        assert "home_stop_id" in user.keys()
    finally:
        await database.close()


async def test_subscription_toggle_is_idempotent(db):
    await db.ensure_user(1, 1)
    await db.add_subscription(1, "digest", None)
    await db.add_subscription(1, "digest", None)
    assert await db.has_subscription(1, "digest")
    assert len(await db.list_subscriptions(1)) == 1

    await db.remove_subscriptions_of_kind(1, "digest")
    assert not await db.has_subscription(1, "digest")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


async def test_every_card_offers_a_way_out(db):
    """No view may edit itself into a message with nothing to tap.

    Callbacks replace the message in place, so a card without buttons leaves
    the user stuck with no earlier message to return to.
    """

    from bot import views

    await db.ensure_user(1, 1)

    docs = [
        await views.search_results_doc(db, "nothing matches", [], [], 1),
        await views.favourites_doc(db, [], 1),
        await views.subscriptions_doc(db, [], 1, 10, "07:00"),
        await views.menu_doc(db, 1),
    ]
    for _doc, buttons in docs:
        assert buttons, "a card was rendered with no keyboard at all"


async def test_nav_row_is_menu_only_without_a_back_target(db):
    from bot import views

    row = await views.nav_row(db, 1)
    assert len(row) == 1

    row = await views.nav_row(db, 1, ("Back to line", "route:view", {"op": "x"}))
    assert len(row) == 2
    assert row[0].text.startswith("◀")


async def test_with_nav_appends_rather_than_replacing(db):
    from bot import views

    original = [[await views.cb(db, "Keep me", "stop:view", {}, 1)]]
    rows = await views.with_nav(db, original, 1)

    assert len(rows) == 2
    assert rows[0][0].text == "Keep me"
    # The caller's own list must not be mutated, or repeated renders would
    # stack one navigation row on top of another.
    assert len(original) == 1


def test_stop_back_target_follows_where_the_user_came_from():
    """A station opened from a line goes back to that line, not to the menu."""

    from bot.main import TrainsBot

    resolve = TrainsBot._stop_back_target

    assert resolve(None, {"op": "ktmb", "stop": "KL01"}) is None

    label, action, payload = resolve(
        None, {"op": "ktmb", "stop": "KL01", "from": "route", "route": "L1"}
    )
    assert action == "route:view"
    assert payload == {"op": "ktmb", "route": "L1"}

    _label, action, _payload = resolve(None, {"op": "ktmb", "from": "fav"})
    assert action == "menu:favourites"

    _label, action, payload = resolve(
        None, {"op": "ktmb", "from": "search", "q": "sentral"}
    )
    assert action == "search:again"
    assert payload == {"q": "sentral"}


def test_stop_back_target_ignores_an_incomplete_origin():
    """A truncated payload must not build a button that leads nowhere."""

    from bot.main import TrainsBot

    resolve = TrainsBot._stop_back_target

    assert resolve(None, {"op": "ktmb", "from": "route"}) is None
    assert resolve(None, {"op": "ktmb", "from": "search"}) is None
