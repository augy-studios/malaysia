"""Tests for the parsing, time and persistence layers.

These run without network access and without a Telegram connection. The parts
worth testing are the ones where a subtle mistake would be invisible in
production: GTFS times past midnight, the protobuf decoder, the rich text
downgrade, and the callback registry that keeps buttons alive.
"""

from __future__ import annotations

import io
import struct
import zipfile
from datetime import datetime

import pytest

from bot.database import Database
from bot.gtfs import Feed, decode_vehicle_positions, haversine_m, parse_static_zip
from bot.richtext import RichDoc, to_classic_html
from bot.timeutils import (
    MYT,
    describe_days,
    format_time,
    in_quiet_hours,
    next_occurrence_epoch,
    parse_days,
    parse_gtfs_time,
    within_window,
)


# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------


def test_parse_gtfs_time_handles_past_midnight():
    # GTFS expresses a 1:15am bus on the previous service day as 25:15:00.
    assert parse_gtfs_time("25:15:00") == 25 * 3600 + 15 * 60
    assert parse_gtfs_time("00:00:00") == 0
    assert parse_gtfs_time("13:45") == 13 * 3600 + 45 * 60


def test_parse_gtfs_time_rejects_rubbish():
    assert parse_gtfs_time("") is None
    assert parse_gtfs_time("not a time") is None
    assert parse_gtfs_time("12:99:00") is None


def test_format_time_marks_next_day():
    assert format_time("25:15:00", "12h") == "1:15am (+1d)"
    assert format_time("25:15:00", "24h") == "01:15 (+1d)"
    assert format_time("13:05:00", "12h") == "1:05pm"
    assert format_time("13:05:00", "24h") == "13:05"
    assert format_time("00:30:00", "12h") == "12:30am"


def test_next_occurrence_rolls_to_tomorrow():
    reference = datetime(2026, 9, 20, 18, 0, tzinfo=MYT)
    # A 07:00 departure has already gone by 18:00, so it lands tomorrow.
    epoch = next_occurrence_epoch("07:00:00", reference)
    assert epoch is not None
    assert datetime.fromtimestamp(epoch, MYT).day == 21

    # A 20:00 departure is still ahead today.
    epoch = next_occurrence_epoch("20:00:00", reference)
    assert datetime.fromtimestamp(epoch, MYT).day == 20


def test_quiet_hours_wrap_midnight():
    row = {"quiet_enabled": 1, "quiet_from": "23:00", "quiet_to": "06:00"}
    assert in_quiet_hours(row, datetime(2026, 9, 20, 23, 30, tzinfo=MYT)) is True
    assert in_quiet_hours(row, datetime(2026, 9, 20, 2, 0, tzinfo=MYT)) is True
    assert in_quiet_hours(row, datetime(2026, 9, 20, 12, 0, tzinfo=MYT)) is False

    disabled = {"quiet_enabled": 0, "quiet_from": "23:00", "quiet_to": "06:00"}
    assert in_quiet_hours(disabled, datetime(2026, 9, 20, 23, 30, tzinfo=MYT)) is False


def test_window_and_days():
    assert within_window("05:00", "23:59", datetime(2026, 9, 20, 12, 0, tzinfo=MYT)) is True
    assert within_window("05:00", "10:00", datetime(2026, 9, 20, 12, 0, tzinfo=MYT)) is False
    # A window that wraps midnight.
    assert within_window("22:00", "02:00", datetime(2026, 9, 20, 23, 0, tzinfo=MYT)) is True

    assert parse_days("0,1,2,3,4") == {0, 1, 2, 3, 4}
    assert describe_days("0,1,2,3,4") == "weekdays"
    assert describe_days("5,6") == "weekends"
    assert describe_days("0,1,2,3,4,5,6") == "every day"


# ---------------------------------------------------------------------------
# GTFS static parsing
# ---------------------------------------------------------------------------


def _build_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "S1,Pasar Seni,3.1428,101.6952\n"
            "S2,KLCC,3.1580,101.7123\n"
            "S3,No Coordinates,,\n",
        )
        zf.writestr(
            "routes.txt",
            "route_id,route_short_name,route_long_name\n"
            "R1,780,Pasar Seni to KLCC\n",
        )
        zf.writestr("trips.txt", "trip_id,route_id,trip_headsign\nT1,R1,KLCC\n")
        zf.writestr(
            "stop_times.txt",
            "trip_id,stop_id,stop_sequence,arrival_time,departure_time\n"
            "T1,S1,1,07:00:00,07:00:00\n"
            "T1,S2,2,07:20:00,07:20:00\n",
        )
    return buffer.getvalue()


def test_parse_static_zip():
    feed = parse_static_zip("rapid-bus-kl", _build_zip())

    assert len(feed.stops) == 3
    assert feed.stops["S1"].stop_name == "Pasar Seni"
    assert feed.stops["S1"].lat == pytest.approx(3.1428)
    # A stop with blank coordinates parses without raising.
    assert feed.stops["S3"].lat is None

    assert feed.routes["R1"].display == "780 Pasar Seni to KLCC"
    assert feed.route_stops["R1"] == {"S1", "S2"}
    assert feed.stop_routes["S1"] == {"R1"}
    assert feed.stop_schedule["S1"]["R1"] == ["07:00:00"]

    # Stop times come back ordered by sequence.
    assert [st.stop_id for st in feed.trip_stops["T1"]] == ["S1", "S2"]


def test_feed_search_and_nearby():
    feed = parse_static_zip("rapid-bus-kl", _build_zip())

    assert [s.stop_id for s in feed.search_stops("pasar")] == ["S1"]
    assert [s.stop_id for s in feed.search_stops("KLCC")] == ["S2"]
    assert feed.search_stops("nothing here") == []

    assert [r.route_id for r in feed.search_routes("780")] == ["R1"]

    # Standing at Pasar Seni, S1 is the closest stop.
    near = feed.stops_near(3.1428, 101.6952, 5000)
    assert near[0][0].stop_id == "S1"
    assert near[0][1] == pytest.approx(0, abs=5)
    # The stop without coordinates is skipped rather than crashing.
    assert all(stop.stop_id != "S3" for stop, _ in near)


def test_haversine_is_sane():
    # Pasar Seni to KLCC is roughly 2.5 km.
    distance = haversine_m(3.1428, 101.6952, 3.1580, 101.7123)
    assert 2000 < distance < 3500


# ---------------------------------------------------------------------------
# GTFS-realtime protobuf decoding
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


def _build_feed_message() -> bytes:
    trip = _delimited(1, b"TRIP1") + _delimited(5, b"R1")
    position = (
        _tag(1, 5) + struct.pack("<f", 3.1428)
        + _tag(2, 5) + struct.pack("<f", 101.6952)
    )
    descriptor = _delimited(1, b"V123") + _delimited(2, b"WWW 1234")

    vehicle = (
        _delimited(1, trip)
        + _delimited(2, position)
        + _tag(5, 0) + _varint(1758300000)
        + _delimited(8, descriptor)
    )
    entity = _delimited(1, b"E1") + _delimited(4, vehicle)
    header = _delimited(1, b"2.0")
    return _delimited(1, header) + _delimited(2, entity)


def test_decode_vehicle_positions():
    vehicles = decode_vehicle_positions(_build_feed_message())

    assert len(vehicles) == 1
    vehicle = vehicles[0]
    assert vehicle.route_id == "R1"
    assert vehicle.trip_id == "TRIP1"
    assert vehicle.vehicle_id == "V123"
    assert vehicle.label == "WWW 1234"
    assert vehicle.lat == pytest.approx(3.1428, abs=1e-4)
    assert vehicle.lon == pytest.approx(101.6952, abs=1e-4)
    assert vehicle.timestamp == 1758300000
    assert vehicle.has_position is True
    assert vehicle.name == "WWW 1234"


def test_decode_empty_feed():
    assert decode_vehicle_positions(b"") == []


# ---------------------------------------------------------------------------
# Rich text
# ---------------------------------------------------------------------------


def test_rich_doc_builds_expected_html():
    doc = RichDoc()
    doc.heading("Title", 3).text("Body").bullets(["one", "two"])
    html = doc.to_html()

    assert "<h3>Title</h3>" in html
    assert "<p>Body</p>" in html
    assert "<ul><li>one</li><li>two</li></ul>" in html


def test_rich_doc_escapes_user_content():
    # A stop name containing markup must not break out into real tags.
    doc = RichDoc().heading("<script>alert(1)</script>", 3)
    assert "<script>" not in doc.to_html()
    assert "&lt;script&gt;" in doc.to_html()


def test_classic_downgrade_keeps_text_readable():
    doc = RichDoc()
    doc.heading("Departures", 3)
    doc.text("Next buses")
    doc.bullets(["7:00am", "7:20am"])
    doc.table(["Stop", "Time"], [["Pasar Seni", "7:00am"]])
    doc.divider()

    classic = to_classic_html(doc.to_html())

    # Rich-only tags are gone.
    for tag in ("<h3>", "<ul>", "<li>", "<table>", "<td>", "<hr>"):
        assert tag not in classic
    # The content survives.
    assert "Departures" in classic
    assert "7:00am" in classic
    assert "Pasar Seni" in classic
    # Headings become bold, which classic HTML does support.
    assert "<b>Departures</b>" in classic


def test_classic_downgrade_preserves_links():
    doc = RichDoc().para('<a href="https://example.com">Map</a>')
    classic = to_classic_html(doc.to_html())
    assert '<a href="https://example.com">Map</a>' in classic


def test_buttons_convert_to_bot_api_markup():
    """Telethon nests the payload under `type`, so this must not silently drop.

    Getting this wrong produces messages with no keyboard at all, which is
    hard to notice in review and obvious to every user.
    """

    from telethon.tl.custom import Button

    from bot.richtext import _buttons_to_markup

    callback = Button.inline("Tap me", b"tok123")
    url = Button.url("Web", "https://example.com")

    markup = _buttons_to_markup([[callback, url]])
    assert markup == {
        "inline_keyboard": [
            [
                {"text": "Tap me", "callback_data": "tok123"},
                {"text": "Web", "url": "https://example.com"},
            ]
        ]
    }

    # A bare button and a flat row are both accepted and wrapped into rows.
    assert _buttons_to_markup(callback)["inline_keyboard"] == [
        [{"text": "Tap me", "callback_data": "tok123"}]
    ]
    assert _buttons_to_markup([callback])["inline_keyboard"] == [
        [{"text": "Tap me", "callback_data": "tok123"}]
    ]
    assert _buttons_to_markup(None) is None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_user_and_preferences(db):
    user = await db.ensure_user(1, 1, "augy", "Augy")
    assert user["operator"] == "rapid-bus-kl"
    assert user["time_format"] == "12h"

    await db.set_pref(1, "time_format", "24h")
    assert (await db.get_user(1))["time_format"] == "24h"

    # Unknown columns are refused rather than injected into the SQL.
    with pytest.raises(ValueError):
        await db.set_pref(1, "user_id; DROP TABLE users", "x")


@pytest.mark.asyncio
async def test_favourites_round_trip(db):
    await db.ensure_user(1, 1)

    assert await db.add_favourite(1, "rapid-bus-kl", "S1", "Pasar Seni") is True
    # The same stop twice is a no-op rather than a duplicate row.
    assert await db.add_favourite(1, "rapid-bus-kl", "S1", "Pasar Seni") is False

    favourites = await db.list_favourites(1)
    assert len(favourites) == 1

    assert await db.remove_favourite(1, favourites[0]["id"]) is True
    assert await db.list_favourites(1) == []


@pytest.mark.asyncio
async def test_removing_favourite_clears_its_subscriptions(db):
    await db.ensure_user(1, 1)
    await db.add_favourite(1, "rapid-bus-kl", "S1", "Pasar Seni")
    favourite = (await db.list_favourites(1))[0]

    await db.add_subscription(1, "departure", favourite["id"])
    assert len(await db.list_subscriptions(1)) == 1

    await db.remove_favourite(1, favourite["id"])
    assert await db.list_subscriptions(1) == []


@pytest.mark.asyncio
async def test_callbacks_survive_and_deduplicate(db):
    token = await db.make_callback("stop:view", {"op": "rapid-bus-kl", "stop": "S1"}, 1)

    # Telegram limits callback data to 64 bytes, so the token must stay short.
    assert len(token.encode()) <= 64

    # The same button rendered again reuses its token rather than growing the table.
    assert await db.make_callback("stop:view", {"op": "rapid-bus-kl", "stop": "S1"}, 1) == token

    action, payload, owner = await db.resolve_callback(token)
    assert action == "stop:view"
    assert payload == {"op": "rapid-bus-kl", "stop": "S1"}
    assert owner == 1

    assert await db.resolve_callback("nonexistent") is None


@pytest.mark.asyncio
async def test_callbacks_persist_across_reconnect(db, tmp_path):
    """The whole point of the registry: a button still works after a restart."""

    token = await db.make_callback("stop:view", {"stop": "S1"}, 1)
    path = db.path
    await db.close()

    reopened = Database(path)
    await reopened.connect()
    try:
        resolved = await reopened.resolve_callback(token)
        assert resolved is not None
        assert resolved[1] == {"stop": "S1"}
    finally:
        await reopened.close()


# ---------------------------------------------------------------------------
# Editing in place
# ---------------------------------------------------------------------------


class _FakeEvent:
    """The two attributes reply_to_button reads off a CallbackQuery."""

    def __init__(self) -> None:
        self.chat_id = 42
        self.message_id = 7


def _bot_with(edit_result):
    """A BusesBot with its send/edit stubbed, recording what each was called with."""

    from bot.main import BusesBot

    bot = BusesBot.__new__(BusesBot)  # no Telegram connection wanted here
    calls: dict[str, list] = {"edit": [], "send": []}

    async def fake_edit(chat_id, message_id, doc, buttons=None):
        calls["edit"].append((chat_id, message_id, buttons))
        return edit_result

    async def fake_send(chat_id, doc, buttons=None, reply_to=None):
        calls["send"].append((chat_id, buttons))
        return "sent"

    bot.edit = fake_edit
    bot.send = fake_send
    return bot, calls


@pytest.mark.asyncio
async def test_button_reply_edits_the_original_message():
    """A tapped button must replace its own message, not add another one."""

    bot, calls = _bot_with(edit_result="edited")

    result = await bot.reply_to_button(_FakeEvent(), RichDoc().text("hi"), buttons=[])

    assert result == "edited"
    assert calls["edit"] == [(42, 7, [])]
    assert calls["send"] == []


@pytest.mark.asyncio
async def test_button_reply_falls_back_to_sending():
    """A message too old to edit still has to get its answer through."""

    bot, calls = _bot_with(edit_result=None)

    result = await bot.reply_to_button(_FakeEvent(), RichDoc().text("hi"))

    assert result == "sent"
    assert len(calls["edit"]) == 1
    assert calls["send"] == [(42, None)]


@pytest.mark.asyncio
async def test_edit_without_buttons_clears_the_old_keyboard():
    """An omitted reply_markup leaves the previous keyboard on the message.

    Editing to a button-less view therefore has to send an empty keyboard
    explicitly, or the user is left tapping buttons for the old content.
    """

    from bot.richtext import RichSender

    sender = RichSender("token")
    posted: dict = {}

    async def fake_post(method, payload):
        posted["method"] = method
        posted["payload"] = payload
        return {"ok": True, "result": {"message_id": 7}}

    sender._post = fake_post
    try:
        await sender.edit(None, 42, 7, RichDoc().text("hi"))
    finally:
        await sender.close()

    assert posted["method"] == "editMessageText"
    assert posted["payload"]["reply_markup"] == {"inline_keyboard": []}


@pytest.mark.asyncio
async def test_scheduled_jobs_deduplicate(db):
    await db.ensure_user(1, 1)

    assert await db.schedule_job("departure", 1000, 1, {"a": 1}, "key-1") is True
    # Planning twice must not produce two identical reminders.
    assert await db.schedule_job("departure", 1000, 1, {"a": 1}, "key-1") is False

    due = await db.due_jobs(2000)
    assert len(due) == 1

    # Jobs in the future are not returned yet.
    await db.schedule_job("departure", 9999999999, 1, {}, "key-2")
    assert len(await db.due_jobs(2000)) == 1

    await db.delete_job(due[0]["id"])
    assert await db.due_jobs(2000) == []


@pytest.mark.asyncio
async def test_live_alert_cooldown(db):
    await db.ensure_user(1, 1)

    assert await db.should_alert_vehicle(1, "S1:V1", 600) is True
    # The same bus within the cooldown does not alert again.
    assert await db.should_alert_vehicle(1, "S1:V1", 600) is False
    # A different bus does.
    assert await db.should_alert_vehicle(1, "S1:V2", 600) is True


@pytest.mark.asyncio
async def test_feed_cache_respects_age(db):
    await db.cache_put("static:test", b"payload")

    assert await db.cache_get("static:test", 3600) == b"payload"
    # Expired by age.
    assert await db.cache_get("static:test", 0) is None
    # But still retrievable when upstream is down.
    stale = await db.cache_get_any_age("static:test")
    assert stale is not None and stale[0] == b"payload"


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_nav_row_is_menu_only_without_a_back_target(db):
    from bot import views

    row = await views.nav_row(db, 1)
    assert len(row) == 1

    row = await views.nav_row(db, 1, ("Back to route", "route:view", {"op": "x"}))
    assert len(row) == 2
    assert row[0].text.startswith("◀")


@pytest.mark.asyncio
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
    """A stop opened from a route goes back to that route, not to the menu."""

    from bot.main import BusesBot

    resolve = BusesBot._stop_back_target

    assert resolve(None, {"op": "rapidkl", "stop": "1001"}) is None

    _label, action, payload = resolve(
        None, {"op": "rapidkl", "stop": "1001", "from": "route", "route": "780"}
    )
    assert action == "route:view"
    assert payload == {"op": "rapidkl", "route": "780"}

    _label, action, _payload = resolve(None, {"op": "rapidkl", "from": "fav"})
    assert action == "menu:favourites"

    _label, action, payload = resolve(
        None, {"op": "rapidkl", "from": "search", "q": "pasar"}
    )
    assert action == "search:again"
    assert payload == {"q": "pasar"}


def test_stop_back_target_ignores_an_incomplete_origin():
    """A truncated payload must not build a button that leads nowhere."""

    from bot.main import BusesBot

    resolve = BusesBot._stop_back_target

    assert resolve(None, {"op": "rapidkl", "from": "route"}) is None
    assert resolve(None, {"op": "rapidkl", "from": "search"}) is None
