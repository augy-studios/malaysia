"""Tests for the parsing, persistence and message-building layers.

Nothing here touches the network or Telegram. The feeds are exercised against
recorded response shapes, which is what catches a field rename upstream before
it reaches a user.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from telethon import events

from bot.database import Database
from bot.main import WeatherBot
from bot.feeds import (
    FLOOD_RANK,
    normalise,
    parse_flood,
    parse_forecast,
    parse_quakes,
    parse_warnings,
    score_match,
    search_locations,
    search_stations,
    haversine_m,
    translate_forecast,
    translate_when,
)
from bot.richtext import RichDoc, to_classic_html, _buttons_to_markup
from bot.timeutils import (
    MYT,
    format_day,
    in_quiet_hours,
    is_active_window,
    parse_iso,
    within_window,
)
from bot.views import (
    digest_doc,
    favourites_doc,
    flood_alert_doc,
    forecast_doc,
    quakes_doc,
    start_doc,
    station_doc,
    warnings_doc,
)


# ---------------------------------------------------------------------------
# Sample payloads, shaped like the real data.gov.my responses
# ---------------------------------------------------------------------------

FORECAST_ROWS = [
    {
        "location": {"location_id": "St001", "location_name": "Kuala Lumpur"},
        "date": "2026-09-21",
        # The feed sends Malay even on the English endpoint, so the samples
        # carry the wording MET actually publishes.
        "morning_forecast": "Tiada Hujan",
        "afternoon_forecast": "Ribut petir di beberapa tempat",
        "night_forecast": "Tiada Hujan",
        "summary_forecast": "Ribut petir di beberapa tempat",
        "summary_when": "Petang dan Malam",
        "min_temp": 24,
        "max_temp": 33,
    },
    {
        "location": {"location_id": "St001", "location_name": "Kuala Lumpur"},
        "date": "2026-09-22",
        "morning_forecast": "Tiada Hujan",
        "afternoon_forecast": "Tiada Hujan",
        "night_forecast": "Tiada Hujan",
        "summary_forecast": "Tiada Hujan",
        "summary_when": "Sepanjang Hari",
        "min_temp": 25,
        "max_temp": 34,
    },
    # The flattened shape, which the feed also emits.
    {
        "location_id": "St002",
        "location_name": "Ipoh",
        "date": "2026-09-21",
        "summary_forecast": "Jerebu",
        "min_temp": 23,
        "max_temp": 32,
    },
]

WARNING_ROWS = [
    {
        "heading_en": "Continuous Rain Warning",
        "text_en": "Heavy rain is expected over Selangor and Kuala Lumpur.",
        "valid_from": "2026-09-21T00:00:00+08:00",
        "valid_to": "2026-09-30T23:59:00+08:00",
        "warning_issue": {"title_en": "Amaran Hujan"},
    },
    {
        "heading_en": "Expired Warning",
        "text_en": "This one has already lapsed.",
        "valid_from": "2020-01-01T00:00:00+08:00",
        "valid_to": "2020-01-02T00:00:00+08:00",
    },
]

QUAKE_ROWS = [
    {
        "utcdatetime": "2026-09-21T02:00:00",
        "localdatetime": "2026-09-21 10:00:00",
        "magdefault": 6.4,
        "depth": 10,
        "location": "120 km SW of Banda Aceh",
        "lat": 5.1,
        "lon": 95.3,
        "status": "Reviewed",
    },
    {
        "utcdatetime": "2026-09-20T02:00:00",
        "magdefault": 4.2,
        "depth": 33,
        "location": "Ranau, Sabah",
    },
]

FLOOD_ROWS = [
    {
        "station_id": "1234",
        "station_name": "Sungai Klang",
        "district": "Petaling",
        "state": "Selangor",
        "main_basin": "Klang",
        "sub_basin": "Klang Hilir",
        "water_level_current": 5.2,
        "water_level_normal_level": 2.0,
        "water_level_alert_level": 4.0,
        "water_level_warning_level": 5.0,
        "water_level_danger_level": 6.0,
        "water_level_indicator": "WARNING",
        "water_level_trend": "RISING",
        "water_level_update_datetime": "2026-09-21 09:00:00",
        "latitude": 3.1,
        "longitude": 101.6,
    },
    {
        "station_id": "5678",
        "station_name": "Sungai Perak",
        "district": "Kinta",
        "state": "Perak",
        "water_level_current": 1.1,
        "water_level_danger_level": 5.0,
        "water_level_indicator": "NORMAL",
        "water_level_trend": "STEADY",
        "latitude": 4.6,
        "longitude": 101.1,
    },
]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_forecast_groups_by_location_and_orders_days():
    locations = parse_forecast(FORECAST_ROWS)
    assert len(locations) == 2

    kl = locations[normalise("St001")]
    assert kl.name == "Kuala Lumpur"
    assert len(kl.days) == 2
    # Days must come back in date order, whatever order upstream sent them.
    assert [d.date for d in kl.days] == ["2026-09-21", "2026-09-22"]
    assert kl.today.temp_range == "24 to 33°C"


def test_forecast_accepts_the_flattened_shape():
    locations = parse_forecast(FORECAST_ROWS)
    ipoh = locations[normalise("St002")]
    assert ipoh.name == "Ipoh"
    assert ipoh.today.condition == "Hazy"


def test_parsed_forecast_reaches_the_user_in_english():
    kl = parse_forecast(FORECAST_ROWS)[normalise("St001")]
    assert kl.today.condition == "Thunderstorms in a few places"
    assert kl.today.headline == "Thunderstorms in a few places, afternoon and night"
    assert kl.days[1].condition == "No rain"


def test_wet_day_detection_reads_both_languages():
    locations = parse_forecast(FORECAST_ROWS)
    assert locations[normalise("St001")].days[0].is_wet is True
    assert locations[normalise("St001")].days[1].is_wet is False


def test_malay_forecast_phrases_are_translated():
    """The English endpoint returns Malay text, so these are the real strings."""

    assert translate_forecast("Ribut petir") == "Thunderstorms"
    assert translate_forecast("Tiada Hujan") == "No rain"
    assert translate_forecast("Jerebu") == "Hazy"
    assert (
        translate_forecast("Ribut petir di beberapa tempat")
        == "Thunderstorms in a few places"
    )
    assert (
        translate_forecast("Hujan di kebanyakan tempat") == "Rain in most places"
    )


def test_longer_qualifiers_win_over_the_phrase_they_contain():
    assert (
        translate_forecast("Ribut petir di beberapa tempat di kawasan pedalaman")
        == "Thunderstorms in a few inland areas"
    )
    assert (
        translate_forecast("Hujan di kebanyakan tempat di kawasan pantai")
        == "Rain in most coastal areas"
    )


def test_unknown_forecast_wording_passes_through_untouched():
    assert translate_forecast("Something new from MET") == "Something new from MET"
    assert translate_forecast("") == ""


def test_summary_when_is_translated():
    assert translate_when("Petang dan Malam") == "afternoon and night"
    assert translate_when("Sepanjang Hari") == "all day"


def test_no_advisory_is_not_treated_as_an_active_warning():
    """MET keeps a permanently valid 'No Advisory' row in the warning feed."""

    rows = [
        {
            "heading_en": "No Advisory",
            "text_en": "No Tropical Cyclone system is observed.",
            "valid_from": "2020-01-01T00:00:00+08:00",
            "valid_to": "2030-01-01T00:00:00+08:00",
        }
    ]
    warning = parse_warnings(rows)[0]
    assert warning.is_advisory_only is True
    assert warning.is_active is False


def test_a_real_warning_is_still_active():
    rows = [
        {
            "heading_en": "THUNDERSTORMS WARNING",
            "text_en": "Thunderstorms and strong winds are expected.",
            "valid_from": "2026-09-21T00:00:00+08:00",
            "valid_to": "2030-01-01T00:00:00+08:00",
        }
    ]
    assert parse_warnings(rows)[0].is_active is True


def test_warnings_filter_to_the_active_window():
    warnings = parse_warnings(WARNING_ROWS)
    assert len(warnings) == 2
    active = [w for w in warnings if w.is_active]
    assert len(active) == 1
    assert active[0].title == "Continuous Rain Warning"


def test_warning_mentions_matches_a_place_name():
    warning = parse_warnings(WARNING_ROWS)[0]
    assert warning.mentions("Selangor") is True
    assert warning.mentions("kuala lumpur") is True
    assert warning.mentions("Sabah") is False


def test_quakes_sort_newest_first_and_classify():
    quakes = parse_quakes(QUAKE_ROWS)
    assert quakes[0].magnitude == 6.4
    assert quakes[0].severity == "high"
    assert quakes[1].severity == "low"
    assert quakes[0].magnitude_text == "M6.4"


def test_quake_utc_field_is_read_as_utc():
    """The bulletin's utcdatetime carries no offset but is UTC by definition."""

    quake = parse_quakes(QUAKE_ROWS)[0]
    moment = parse_iso(quake.when)
    # 02:00 UTC is 10:00 in Malaysia.
    assert moment.astimezone(MYT).hour == 10


def test_flood_severity_and_elevation():
    stations = parse_flood(FLOOD_ROWS)
    klang, perak = stations
    assert klang.severity == "warning"
    assert klang.is_elevated is True
    assert klang.trend_text == "rising"
    assert perak.is_elevated is False
    assert klang.rank > perak.rank


def test_flood_place_and_level_formatting():
    klang = parse_flood(FLOOD_ROWS)[0]
    assert klang.place == "Petaling, Selangor"
    assert klang.level_text == "5.2 m"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_exact_match_outranks_a_partial_one():
    assert score_match("Kuala Lumpur", "Kuala Lumpur") > score_match(
        "Kuala Lumpur", "kuala"
    )
    assert score_match("Kuala Lumpur", "kuala") > score_match("Teluk Kuala", "kuala")


def test_search_is_accent_and_punctuation_insensitive():
    locations = parse_forecast(FORECAST_ROWS)
    assert search_locations(locations, "kuala-lumpur")[0].name == "Kuala Lumpur"
    assert search_locations(locations, "IPOH")[0].name == "Ipoh"


def test_search_returns_nothing_for_an_unrelated_query():
    locations = parse_forecast(FORECAST_ROWS)
    assert search_locations(locations, "Reykjavik") == []


def test_station_search_covers_district_and_basin():
    stations = parse_flood(FLOOD_ROWS)
    assert search_stations(stations, "Petaling")[0].name == "Sungai Klang"
    assert search_stations(stations, "Klang Hilir")[0].name == "Sungai Klang"


def test_haversine_is_roughly_right():
    # Kuala Lumpur to Ipoh is about 180 km.
    metres = haversine_m(3.139, 101.687, 4.597, 101.090)
    assert 150_000 < metres < 220_000


# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------


def test_quiet_hours_wrap_midnight():
    user = {"quiet_enabled": 1, "quiet_from": "23:00", "quiet_to": "06:00"}
    assert in_quiet_hours(user, datetime(2026, 9, 21, 23, 30, tzinfo=MYT)) is True
    assert in_quiet_hours(user, datetime(2026, 9, 21, 2, 0, tzinfo=MYT)) is True
    assert in_quiet_hours(user, datetime(2026, 9, 21, 12, 0, tzinfo=MYT)) is False


def test_quiet_hours_off_is_always_false():
    user = {"quiet_enabled": 0, "quiet_from": "23:00", "quiet_to": "06:00"}
    assert in_quiet_hours(user, datetime(2026, 9, 21, 23, 30, tzinfo=MYT)) is False


def test_active_window_treats_missing_bounds_as_open():
    assert is_active_window("", "") is True
    future = (datetime.now(MYT) + timedelta(days=1)).isoformat()
    assert is_active_window("", future) is True
    past = (datetime.now(MYT) - timedelta(days=1)).isoformat()
    assert is_active_window("", past) is False


def test_format_day_names_today_and_tomorrow():
    today = datetime.now(MYT).date().isoformat()
    tomorrow = (datetime.now(MYT).date() + timedelta(days=1)).isoformat()
    assert format_day(today) == "Today"
    assert format_day(tomorrow) == "Tomorrow"


def test_parse_iso_handles_the_space_separated_flood_format():
    moment = parse_iso("2026-09-21 09:00:00")
    assert moment is not None and moment.hour == 9


# ---------------------------------------------------------------------------
# Rich messages
# ---------------------------------------------------------------------------


def test_rich_tables_downgrade_to_readable_lines():
    doc = RichDoc().table(["Day", "Outlook"], [["Today", "Rain"]])
    classic = to_classic_html(doc.to_html())
    assert "<table" not in classic
    assert "Day | Outlook" in classic
    assert "Today | Rain" in classic


def test_headings_and_bullets_survive_the_downgrade():
    doc = RichDoc().heading("Warnings").bullets(["First", "Second"])
    classic = to_classic_html(doc.to_html())
    assert "<b>Warnings</b>" in classic
    assert "• First" in classic


def test_buttons_convert_to_bot_api_markup():
    from telethon.tl.custom import Button

    markup = _buttons_to_markup([[Button.inline("Press", b"tok")]])
    assert markup == {"inline_keyboard": [[{"text": "Press", "callback_data": "tok"}]]}


def test_url_buttons_convert_too():
    from telethon.tl.custom import Button

    markup = _buttons_to_markup([[Button.url("Donate", "https://example.org")]])
    assert markup["inline_keyboard"][0][0]["url"] == "https://example.org"


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class _Snapshot:
    stale = False
    fetched_at = 0


def test_no_view_contains_an_em_dash():
    """House style: em dashes are not used anywhere in user-facing text."""

    locations = parse_forecast(FORECAST_ROWS)
    stations = parse_flood(FLOOD_ROWS)
    quakes = parse_quakes(QUAKE_ROWS)
    warnings = [w for w in parse_warnings(WARNING_ROWS) if w.is_active]
    snapshot = _Snapshot()

    docs = [
        start_doc("Augy"),
        forecast_doc(locations[normalise("St001")], snapshot),
        warnings_doc(warnings, snapshot),
        quakes_doc(quakes, snapshot),
        station_doc(stations[0]),
        flood_alert_doc(stations[0]),
        digest_doc(
            [("Kuala Lumpur", locations[normalise("St001")].today)],
            warnings,
            [stations[0]],
        ),
    ]
    for doc in docs:
        assert "—" not in doc.to_html()


def test_start_lists_every_command_without_naming_the_bot():
    html = start_doc().to_html()
    for command in (
        "/weather",
        "/warnings",
        "/quake",
        "/flood",
        "/fav",
        "/unfav",
        "/sub",
        "/unsub",
        "/settings",
    ):
        assert command in html
    # The bot's username must not appear in any command.
    assert "@" not in html


def test_start_presents_the_commands_as_a_table():
    """The sibling bots use a table here, so this one matches them."""

    html = start_doc().to_html()
    assert "<table" in html
    assert "<th>Command</th>" in html


def test_commands_are_never_wrapped_in_markup():
    """Telegram autolinks a bare /command, and a code or bold span kills that."""

    import re

    docs = [start_doc("Augy"), favourites_doc([])]
    for doc in docs:
        html = doc.to_html()
        assert "<code>" not in html
        wrapped = re.findall(r"<(?:code|b)>\s*/[a-z]+\s*</(?:code|b)>", html)
        assert not wrapped, wrapped


def test_forecast_view_reports_rain_days():
    locations = parse_forecast(FORECAST_ROWS)
    html = forecast_doc(locations[normalise("St001")], _Snapshot()).to_html()
    assert "umbrella" in html.lower()


def test_empty_warnings_reads_as_reassurance_not_an_error():
    html = warnings_doc([], _Snapshot()).to_html()
    assert "no weather warnings" in html.lower()


def test_flood_alert_names_the_danger_action():
    stations = parse_flood(FLOOD_ROWS)
    danger = stations[0].__class__(**{**stations[0].__dict__, "indicator": "DANGER"})
    html = flood_alert_doc(danger).to_html()
    assert "danger level" in html.lower()


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
async def test_user_upsert_keeps_one_row(db):
    await db.ensure_user(1, 1, "augy", "Augy")
    await db.ensure_user(1, 1, "augy2", "Augy")
    assert await db.count_users() == 1
    user = await db.get_user(1)
    assert user["username"] == "augy2"


@pytest.mark.asyncio
async def test_favourites_are_unique_per_user(db):
    await db.ensure_user(1, 1)
    assert await db.add_favourite(1, "location", "St001", "Kuala Lumpur") is True
    assert await db.add_favourite(1, "location", "St001", "Kuala Lumpur") is False
    assert len(await db.list_favourites(1)) == 1


@pytest.mark.asyncio
async def test_removing_a_favourite_takes_its_subscriptions(db):
    await db.ensure_user(1, 1)
    await db.add_favourite(1, "station", "1234", "Sungai Klang")
    fav = (await db.list_favourites(1))[0]
    await db.add_subscription(1, "flood", int(fav["id"]))

    await db.remove_favourite(1, int(fav["id"]))
    assert await db.list_favourites(1) == []
    assert await db.list_subscriptions(1) == []


@pytest.mark.asyncio
async def test_account_wide_subscriptions_cannot_duplicate(db):
    """The partial index is what stops NULL favourite_id stacking rows."""

    await db.ensure_user(1, 1)
    assert await db.add_subscription(1, "digest") is True
    assert await db.add_subscription(1, "digest") is False
    assert len(await db.list_subscriptions(1)) == 1


@pytest.mark.asyncio
async def test_subscription_toggles_off_and_on(db):
    await db.ensure_user(1, 1)
    await db.add_subscription(1, "quake")
    assert await db.has_subscription(1, "quake") is True
    await db.remove_subscription(1, "quake")
    assert await db.has_subscription(1, "quake") is False


@pytest.mark.asyncio
async def test_callback_tokens_are_stable_and_resolve(db):
    first = await db.make_callback("loc", {"r": "St001"})
    second = await db.make_callback("loc", {"r": "St001"})
    assert first == second, "an identical menu must reuse its token"

    action, payload, _owner = await db.resolve_callback(first)
    assert action == "loc"
    assert payload == {"r": "St001"}


@pytest.mark.asyncio
async def test_callback_survives_a_reconnect(db, tmp_path):
    """A button must still work after the bot restarts."""

    token = await db.make_callback("stn", {"r": "1234"})
    await db.close()

    reopened = Database(tmp_path / "test.sqlite3")
    await reopened.connect()
    resolved = await reopened.resolve_callback(token)
    assert resolved is not None
    assert resolved[0] == "stn"
    await reopened.close()


@pytest.mark.asyncio
async def test_unknown_callback_resolves_to_none(db):
    assert await db.resolve_callback("not-a-real-token") is None


@pytest.mark.asyncio
async def test_set_user_field_refuses_an_unknown_column(db):
    await db.ensure_user(1, 1)
    with pytest.raises(ValueError):
        await db.set_user_field(1, "user_id; DROP TABLE users", "x")


@pytest.mark.asyncio
async def test_jobs_are_deduplicated_by_key(db):
    await db.ensure_user(1, 1)
    await db.schedule_job("digest", 100, user_id=1, dedupe_key="digest:1:today")
    await db.schedule_job("digest", 100, user_id=1, dedupe_key="digest:1:today")
    assert await db.count_jobs() == 1


@pytest.mark.asyncio
async def test_due_jobs_only_returns_what_is_ready(db):
    import time as _time

    await db.ensure_user(1, 1)
    await db.schedule_job("digest", int(_time.time()) - 10, user_id=1, dedupe_key="past")
    await db.schedule_job("digest", int(_time.time()) + 600, user_id=1, dedupe_key="future")
    due = await db.due_jobs()
    assert len(due) == 1
    assert due[0]["dedupe_key"] == "past"


@pytest.mark.asyncio
async def test_alert_cooldown_suppresses_a_repeat(db):
    await db.ensure_user(1, 1)
    assert await db.should_alert(1, "flood:1234:WARNING", 3600) is True
    assert await db.should_alert(1, "flood:1234:WARNING", 3600) is False
    # A different level is a different event and must get through.
    assert await db.should_alert(1, "flood:1234:DANGER", 3600) is True


@pytest.mark.asyncio
async def test_cache_respects_age_and_falls_back_when_stale(db):
    await db.cache_put("forecast", b"[]")
    assert await db.cache_get("forecast", 3600) == b"[]"
    # max_age of zero always counts as expired, forcing a refetch.
    assert await db.cache_get("forecast", 0) is None
    # The stale copy is still retrievable for the upstream-failure path.
    fallback = await db.cache_get_any_age("forecast")
    assert fallback is not None and fallback[0] == b"[]"


@pytest.mark.asyncio
async def test_last_location_is_remembered_for_a_bare_weather(db):
    """/weather with no argument relies on this column being stored."""

    await db.ensure_user(1, 1)
    user = await db.get_user(1)
    # A brand new user has nothing remembered, which is what sends them to
    # the prompt rather than to a stale forecast.
    assert user["last_location"] == ""

    await db.set_user_field(1, "last_location", "St001")
    user = await db.get_user(1)
    assert user["last_location"] == "St001"


@pytest.mark.asyncio
async def test_last_location_column_is_added_to_an_existing_database(tmp_path):
    """An upgrade must not lose the rows a running bot already has."""

    import aiosqlite

    path = tmp_path / "old.sqlite3"
    # A database from before the column existed, with a user already in it.
    async with aiosqlite.connect(path) as old:
        await old.execute(
            """
            CREATE TABLE users (
                user_id         INTEGER PRIMARY KEY,
                chat_id         INTEGER NOT NULL,
                username        TEXT,
                first_name      TEXT,
                time_format     TEXT    NOT NULL DEFAULT '12h',
                quiet_from      TEXT    NOT NULL DEFAULT '23:00',
                quiet_to        TEXT    NOT NULL DEFAULT '06:00',
                quiet_enabled   INTEGER NOT NULL DEFAULT 1,
                digest_time     TEXT    NOT NULL DEFAULT '07:00',
                quake_threshold REAL    NOT NULL DEFAULT 5.0,
                flood_threshold TEXT    NOT NULL DEFAULT 'ALERT',
                home_location   TEXT    NOT NULL DEFAULT '',
                last_lat        REAL,
                last_lon        REAL,
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL
            )
            """
        )
        await old.execute(
            "INSERT INTO users (user_id, chat_id, created_at, updated_at) "
            "VALUES (7, 7, 0, 0)"
        )
        await old.commit()

    database = Database(path)
    await database.connect()
    try:
        user = await database.get_user(7)
        assert user is not None, "the existing row survived the migration"
        assert user["last_location"] == ""
        await database.set_user_field(7, "last_location", "St042")
        assert (await database.get_user(7))["last_location"] == "St042"
    finally:
        await database.close()


# ---------------------------------------------------------------------------
# Rendering: button presses must edit, never send
# ---------------------------------------------------------------------------


class _RecordingRich:
    """Stands in for RichSender, recording which path a render took."""

    def __init__(self, edit_result: object = "edited") -> None:
        self.edit_result = edit_result
        self.calls: list[str] = []
        self.edited_ids: list[int] = []
        self.rich_supported = True

    async def edit(self, client, chat_id, message_id, doc, buttons=None):
        self.calls.append("edit")
        self.edited_ids.append(message_id)
        return self.edit_result

    async def send(self, client, chat_id, doc, buttons=None, reply_to=None, silent=False):
        self.calls.append("send")
        return "sent"


class _FakeCallbackEvent(events.CallbackQuery.Event):
    """A callback event shaped the way Telethon really builds one.

    Telethon's CallbackQuery.Event has no `message` attribute, only
    `message_id`. Subclassing the real class rather than inventing a stub is
    what makes this test able to catch that.
    """

    def __init__(self, message_id: int = 4242) -> None:
        # `message_id` is a read-only property reading this field, so the fake
        # sets what the real class sets and inherits the real accessor.
        self._message_id = message_id

    async def get_chat(self):
        return 99


def _render_with(rich, event):
    bot = WeatherBot.__new__(WeatherBot)
    bot.client = object()
    bot.rich = rich
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        bot._render(event, RichDoc().para("hello"), None)
    )


def test_a_button_press_edits_the_message_it_sits_on():
    """The regression this guards: every tap used to send a new message.

    `getattr(event, "message", None)` is always None on a callback query, so
    the edit branch never ran and each press appended another card.
    """

    rich = _RecordingRich()
    result = _render_with(rich, _FakeCallbackEvent(message_id=777))

    assert rich.calls == ["edit"], "a button press must not send a new message"
    assert rich.edited_ids == [777]
    assert result == "edited"


def test_an_unmodified_edit_does_not_send_a_duplicate():
    """Re-tapping the same button leaves the screen as it is."""

    from bot.richtext import EDIT_UNCHANGED

    rich = _RecordingRich(edit_result=EDIT_UNCHANGED)
    _render_with(rich, _FakeCallbackEvent())

    assert rich.calls == ["edit"], "an unchanged edit already succeeded"


def test_a_failed_edit_still_reaches_the_user():
    """A message too old to edit must not swallow the reply."""

    rich = _RecordingRich(edit_result=None)
    result = _render_with(rich, _FakeCallbackEvent())

    assert rich.calls == ["edit", "send"]
    assert result == "sent"
