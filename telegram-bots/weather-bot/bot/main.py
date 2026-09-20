"""The bot itself: handlers, routing and the keyboards that tie them together.

Two behaviours are enforced here rather than left to each handler.

`_render` is the single way a screen reaches the user. Given a message it edits
that message in place, and given a plain chat it sends a new one. Because every
callback goes through it, pressing a button rewrites the message you pressed
rather than pushing a new one into the chat, and a long session stays a single
tidy message.

`nav` builds the bottom row of every keyboard. A view either offers a Back
button to wherever it came from or a Home button to the main menu, and usually
both, so there is no screen a user can reach and then be stuck on.

Button payloads live in SQLite, so a keyboard in a message from last month
still resolves after any number of restarts.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any, Sequence

from telethon import TelegramClient, events
from telethon.tl.custom import Button

from .config import Settings, load_settings
from .database import Database
from .feeds import (
    ELEVATED_RANK,
    FLOOD_RANK,
    FeedError,
    FeedManager,
    FloodStation,
    Location,
    haversine_m,
    normalise,
    search_locations,
    search_stations,
)
from .richtext import RichDoc, RichSender
from .scheduler import Scheduler
from .timeutils import now_myt
from .views import (
    SUBSCRIPTION_LABELS,
    about_doc,
    error_doc,
    favourites_doc,
    flood_overview_doc,
    forecast_doc,
    location_choice_doc,
    nearby_doc,
    no_match_doc,
    quake_detail_doc,
    quakes_doc,
    settings_doc,
    start_doc,
    station_doc,
    station_list_doc,
    stats_doc,
    subscriptions_doc,
    warnings_doc,
)

log = logging.getLogger(__name__)

# How many results a chooser screen offers at once.
CHOICE_LIMIT = 8
# How far out /nearby will look for river gauges.
NEARBY_STATION_LIMIT = 5


class WeatherBot:
    """Owns the Telegram client, the database and the background loops."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.database_path)
        self.rich = RichSender(settings.bot_token, settings.http_timeout_seconds)
        self.feeds: FeedManager | None = None
        self.client: TelegramClient | None = None
        self.scheduler: Scheduler | None = None
        self._stopping = asyncio.Event()

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        await self.db.connect()
        self.feeds = FeedManager(self.db, self.settings)

        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(self.settings.session_path),
            self.settings.api_id,
            self.settings.api_hash,
        )
        await self.client.start(bot_token=self.settings.bot_token)

        me = await self.client.get_me()
        log.info("Signed in as @%s", getattr(me, "username", "unknown"))

        self._register_handlers(self.client)

        self.scheduler = Scheduler(self)
        self.scheduler.start()

    async def run(self) -> None:
        await self.start()
        assert self.client is not None

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stopping.set)
            except NotImplementedError:
                # Windows does not support add_signal_handler. Development
                # happens there, deployment does not, so a plain
                # KeyboardInterrupt is good enough on that platform.
                pass

        log.info("Bot is running. Press Ctrl+C to stop.")
        await self._stopping.wait()
        await self.stop()

    async def stop(self) -> None:
        log.info("Shutting down")
        if self.scheduler is not None:
            await self.scheduler.stop()
        if self.feeds is not None:
            await self.feeds.close()
        await self.rich.close()
        if self.client is not None:
            await self.client.disconnect()
        await self.db.close()

    # -- sending ----------------------------------------------------------

    async def send(self, chat_id: Any, doc: RichDoc, buttons: Any = None) -> Any:
        """Send a new rich message. Used by the scheduler for alerts."""

        assert self.client is not None
        return await self.rich.send(self.client, chat_id, doc, buttons=buttons)

    async def _render(self, event: Any, doc: RichDoc, buttons: Any = None) -> Any:
        """Put `doc` in front of the user, editing in place where possible.

        A callback query carries the message the button belongs to, so the
        result of pressing a button replaces that message. A command has no
        such message, so it sends a new one. Handlers never choose between the
        two, which is what keeps the behaviour consistent everywhere.
        """

        assert self.client is not None

        message = getattr(event, "message", None)
        is_callback = isinstance(event, events.CallbackQuery.Event)

        if is_callback and message is not None:
            return await self.rich.edit(
                self.client,
                await event.get_chat(),
                message.id,
                doc,
                buttons=buttons,
            )

        return await self.rich.send(self.client, await event.get_chat(), doc, buttons=buttons)

    # -- keyboards --------------------------------------------------------

    async def _cb(self, action: str, **payload: Any) -> bytes:
        """Mint a persistent callback token for one button."""

        token = await self.db.make_callback(action, payload or {})
        return token.encode()

    async def nav(
        self,
        back: str | None = None,
        back_payload: dict[str, Any] | None = None,
        back_label: str = "Back",
        extra: Sequence[Any] = (),
        home: bool = True,
    ) -> list[Any]:
        """Build the navigation row every screen ends with.

        `back` names the action to return to. `home` adds the main menu. A
        screen that passes neither would be a dead end, so `home` defaults to
        True and only the main menu itself turns it off.
        """

        row: list[Any] = list(extra)
        if back:
            row.append(
                Button.inline(
                    f"« {back_label}", await self._cb(back, **(back_payload or {}))
                )
            )
        if home:
            row.append(Button.inline("Main menu", await self._cb("home")))
        return row

    async def main_menu_buttons(self) -> list[list[Any]]:
        """The home keyboard, also used as the Back target from most screens."""

        rows = [
            [
                Button.inline("Forecast", await self._cb("menu_weather")),
                Button.inline("Warnings", await self._cb("warnings")),
            ],
            [
                Button.inline("Earthquakes", await self._cb("quakes")),
                Button.inline("River levels", await self._cb("floods")),
            ],
            [
                Button.inline("Favourites", await self._cb("favourites")),
                Button.inline("Alerts", await self._cb("subs")),
            ],
            [
                Button.inline("Settings", await self._cb("settings")),
                Button.inline("About", await self._cb("about")),
            ],
        ]

        links: list[Any] = []
        if self.settings.web_app_url:
            links.append(Button.url("Open the web app", self.settings.web_app_url))
        if self.settings.donation_url:
            links.append(Button.url("Support this project", self.settings.donation_url))
        if links:
            rows.append(links)
        return rows

    # -- handler registration ---------------------------------------------

    def _register_handlers(self, client: TelegramClient) -> None:
        def command(pattern: str) -> Any:
            # The bot name is deliberately absent from the patterns. Telegram
            # appends @username to commands in groups, so an optional suffix is
            # matched and discarded rather than advertised anywhere.
            return events.NewMessage(pattern=rf"^/{pattern}(?:@\w+)?(?:\s+(?P<args>.*))?$")

        client.add_event_handler(self.on_start, command("start"))
        client.add_event_handler(self.on_weather, command("weather"))
        client.add_event_handler(self.on_warnings, command("warnings"))
        client.add_event_handler(self.on_quake, command("quake"))
        client.add_event_handler(self.on_flood, command("flood"))
        client.add_event_handler(self.on_fav, command("fav"))
        client.add_event_handler(self.on_unfav, command("unfav"))
        client.add_event_handler(self.on_sub, command("sub"))
        client.add_event_handler(self.on_unsub, command("unsub"))
        client.add_event_handler(self.on_settings, command("settings"))
        client.add_event_handler(self.on_about, command("about"))
        client.add_event_handler(self.on_stats, command("stats"))

        client.add_event_handler(self.on_location, events.NewMessage(func=_has_location))
        # Free text search runs last so it never shadows a command.
        client.add_event_handler(self.on_text, events.NewMessage(pattern=r"^(?!/)"))
        client.add_event_handler(self.on_callback, events.CallbackQuery)

    async def _user_of(self, event: Any) -> Any:
        sender = await event.get_sender()
        chat = await event.get_chat()
        return await self.db.ensure_user(
            user_id=event.sender_id,
            chat_id=getattr(chat, "id", event.sender_id),
            username=getattr(sender, "username", None),
            first_name=getattr(sender, "first_name", None),
        )

    # -- commands ---------------------------------------------------------

    async def on_start(self, event: Any) -> None:
        user = await self._user_of(event)
        await self._render(
            event,
            start_doc(str(user["first_name"] or "")),
            await self.main_menu_buttons(),
        )

    async def on_about(self, event: Any) -> None:
        await self._user_of(event)
        await self._render(event, about_doc(), [await self.nav(back="home")])

    async def on_weather(self, event: Any) -> None:
        """A bare /weather answers with wherever the user last looked.

        Asking "which town?" when the bot already knows the answer from a
        moment ago is a step the user should not have to repeat, so the menu
        only appears when there is genuinely nothing remembered.
        """

        user = await self._user_of(event)
        query = _args(event)
        if query:
            await self._show_location_search(event, query)
            return

        remembered = str(user["last_location"] or "")
        if remembered:
            location = await self.feeds.location_by_id(remembered)
            if location is not None:
                await self._open_location(event, location)
                return

        await self._show_weather_menu(event)

    async def on_warnings(self, event: Any) -> None:
        await self._user_of(event)
        await self._show_warnings(event)

    async def on_quake(self, event: Any) -> None:
        await self._user_of(event)
        await self._show_quakes(event)

    async def on_flood(self, event: Any) -> None:
        await self._user_of(event)
        query = _args(event)
        if query:
            await self._show_station_search(event, query)
        else:
            await self._show_floods(event)

    async def on_fav(self, event: Any) -> None:
        await self._user_of(event)
        query = _args(event)
        if query:
            # "/fav Ipoh" searches, then the result carries a Save button.
            await self._show_location_search(event, query)
            return
        await self._show_favourites(event)

    async def on_unfav(self, event: Any) -> None:
        await self._user_of(event)
        await self._show_favourites(event, removing=True)

    async def on_sub(self, event: Any) -> None:
        await self._user_of(event)
        await self._show_subs(event)

    async def on_unsub(self, event: Any) -> None:
        await self._user_of(event)
        await self._show_subs(event)

    async def on_settings(self, event: Any) -> None:
        await self._user_of(event)
        await self._show_settings(event)

    async def on_stats(self, event: Any) -> None:
        await self._user_of(event)
        if not self.settings.is_admin(event.sender_id):
            # Saying nothing would look broken, so the refusal is plain.
            await self._render(
                event,
                error_doc("That command is limited to the bot administrators."),
                [await self.nav(back="home")],
            )
            return

        stats = {
            "Users": await self.db.count_users(),
            "Favourites": await self.db.count_favourites(),
            "Active alerts": await self.db.count_subscriptions(),
            "Queued jobs": await self.db.count_jobs(),
            "Rich messages": "on" if self.rich.rich_supported else "fallback",
            "Time": now_myt().strftime("%d %b %Y, %H:%M"),
        }
        await self._render(event, stats_doc(stats), [await self.nav(back="home")])

    # -- free text and location -------------------------------------------

    async def on_text(self, event: Any) -> None:
        text = (event.raw_text or "").strip()
        if not text or text.startswith("/"):
            return
        await self._user_of(event)
        await self._show_search(event, text)

    async def on_location(self, event: Any) -> None:
        user = await self._user_of(event)
        geo = event.message.geo
        lat, lon = float(geo.lat), float(geo.long)
        await self.db.set_last_position(int(user["user_id"]), lat, lon)
        await self._show_nearby(event, lat, lon)

    # -- screens ----------------------------------------------------------

    async def _show_weather_menu(self, event: Any) -> None:
        """Offer favourites and a prompt, rather than a bare 'send a name'."""

        user = await self._user_of(event)
        favourites = await self.db.list_favourites(int(user["user_id"]), "location")

        doc = RichDoc()
        doc.heading("Forecasts", level=2)
        if favourites:
            doc.para(
                "Choose one of your saved areas, send any town name, or share "
                "your location to get the closest forecast."
            )
        else:
            doc.para(
                "Send a town name such as Ipoh, Kuantan or Kota Kinabalu and "
                "the bot will find its forecast. Sharing your location through "
                "the Telegram attachment menu works too, at any time."
            )

        rows = [
            [Button.inline(str(fav["label"]), await self._cb("loc", r=str(fav["ref_id"])))]
            for fav in favourites[:CHOICE_LIMIT]
        ]
        rows.append(await self.nav(back="home"))
        await self._render(event, doc, rows)

    async def _show_search(self, event: Any, query: str) -> None:
        """Search both forecast areas and river gauges for a typed phrase."""

        try:
            forecast = await self.feeds.forecast()
            flood = await self.feeds.flood()
        except FeedError as exc:
            await self._feed_error(event, exc)
            return

        locations = search_locations(forecast.locations, query, CHOICE_LIMIT)
        stations = search_stations(flood.stations, query, CHOICE_LIMIT)

        if len(locations) == 1 and not stations:
            await self._open_location(event, locations[0])
            return
        if not locations and len(stations) == 1:
            await self._open_station(event, stations[0])
            return
        if not locations and not stations:
            await self._render(
                event, no_match_doc(query), [await self.nav(back="home")]
            )
            return

        doc = RichDoc()
        doc.heading(f"Results for {query}", level=2)
        if locations:
            doc.para(f"{len(locations)} forecast area(s) matched.")
        if stations:
            doc.para(f"{len(stations)} river gauge(s) matched.")
        doc.para("Choose one below.")

        rows: list[list[Any]] = []
        for loc in locations[:5]:
            rows.append(
                [Button.inline(f"🌤 {loc.name}", await self._cb("loc", r=loc.location_id))]
            )
        for station in stations[:5]:
            rows.append(
                [
                    Button.inline(
                        f"🌊 {station.name}", await self._cb("stn", r=station.station_id)
                    )
                ]
            )
        rows.append(await self.nav(back="home"))
        await self._render(event, doc, rows)

    async def _show_location_search(self, event: Any, query: str) -> None:
        try:
            snapshot = await self.feeds.forecast()
        except FeedError as exc:
            await self._feed_error(event, exc)
            return

        matches = search_locations(snapshot.locations, query, CHOICE_LIMIT)
        if not matches:
            await self._render(event, no_match_doc(query), [await self.nav(back="home")])
            return
        if len(matches) == 1:
            await self._open_location(event, matches[0])
            return

        rows = [
            [Button.inline(loc.name, await self._cb("loc", r=loc.location_id))]
            for loc in matches
        ]
        rows.append(await self.nav(back="home"))
        await self._render(event, location_choice_doc(query, matches), rows)

    async def _open_location(self, event: Any, location: Location) -> None:
        try:
            snapshot = await self.feeds.forecast()
            warnings = await self.feeds.warnings()
        except FeedError as exc:
            await self._feed_error(event, exc)
            return

        doc = forecast_doc(location, snapshot)

        # A warning naming this town belongs on the forecast, not two screens
        # away where it might be missed.
        relevant = [w for w in warnings.warnings if w.mentions(location.name)]
        if relevant:
            doc.divider()
            doc.para(
                f"There {'is' if len(relevant) == 1 else 'are'} "
                f"{len(relevant)} active warning(s) naming this area. Use the "
                "button below to read them."
            )

        user = await self._user_of(event)

        # Remembered so a bare /weather can answer straight away next time
        # rather than asking where the user means.
        await self.db.set_user_field(
            int(user["user_id"]), "last_location", location.location_id
        )

        saved = await self.db.find_favourite(
            int(user["user_id"]), "location", location.location_id
        )

        top: list[Any] = []
        if saved:
            top.append(
                Button.inline(
                    "Remove from favourites",
                    await self._cb("unfav_do", f=int(saved["id"])),
                )
            )
        else:
            top.append(
                Button.inline(
                    "Save to favourites",
                    await self._cb(
                        "fav_loc", r=location.location_id, n=location.name[:48]
                    ),
                )
            )
        top.append(
            Button.inline("Refresh", await self._cb("loc", r=location.location_id, f=1))
        )

        rows = [top]
        if relevant:
            rows.append(
                [
                    Button.inline(
                        "Warnings for this area",
                        await self._cb("warnings", p=location.name[:48]),
                    )
                ]
            )
        rows.append(await self.nav(back="menu_weather", back_label="Forecasts"))
        await self._render(event, doc, rows)

    async def _show_warnings(self, event: Any, place: str = "") -> None:
        try:
            snapshot = await self.feeds.warnings()
        except FeedError as exc:
            await self._feed_error(event, exc)
            return

        warnings = snapshot.warnings
        if place:
            warnings = [w for w in warnings if w.mentions(place)]

        rows = [
            [
                Button.inline(
                    "Refresh", await self._cb("warnings", p=place[:48], f=1)
                )
            ]
        ]
        if place:
            rows[0].append(
                Button.inline("Show all warnings", await self._cb("warnings"))
            )
        rows.append(await self.nav(back="home"))
        await self._render(event, warnings_doc(warnings, snapshot, place), rows)

    async def _show_quakes(self, event: Any) -> None:
        try:
            snapshot = await self.feeds.quakes()
        except FeedError as exc:
            await self._feed_error(event, exc)
            return

        rows: list[list[Any]] = []
        for quake in snapshot.quakes[:5]:
            rows.append(
                [
                    Button.inline(
                        f"{quake.magnitude_text} {quake.location[:40]}",
                        await self._cb("quake", r=quake.quake_id),
                    )
                ]
            )
        rows.append([Button.inline("Refresh", await self._cb("quakes", f=1))])
        rows.append(await self.nav(back="home"))
        await self._render(event, quakes_doc(snapshot.quakes, snapshot), rows)

    async def _show_floods(self, event: Any) -> None:
        try:
            snapshot = await self.feeds.flood()
        except FeedError as exc:
            await self._feed_error(event, exc)
            return

        elevated = sorted(
            (st for st in snapshot.stations if st.is_elevated),
            key=lambda st: (-st.rank, st.name),
        )

        rows: list[list[Any]] = []
        for station in elevated[:5]:
            rows.append(
                [
                    Button.inline(
                        f"{station.name[:40]}",
                        await self._cb("stn", r=station.station_id),
                    )
                ]
            )
        rows.append([Button.inline("Refresh", await self._cb("floods", f=1))])
        rows.append(await self.nav(back="home"))
        await self._render(event, flood_overview_doc(snapshot.stations, snapshot), rows)

    async def _show_station_search(self, event: Any, query: str) -> None:
        try:
            snapshot = await self.feeds.flood()
        except FeedError as exc:
            await self._feed_error(event, exc)
            return

        matches = search_stations(snapshot.stations, query, CHOICE_LIMIT)
        if not matches:
            await self._render(event, no_match_doc(query), [await self.nav(back="floods", back_label="River levels")])
            return
        if len(matches) == 1:
            await self._open_station(event, matches[0])
            return

        rows = [
            [Button.inline(st.name[:40], await self._cb("stn", r=st.station_id))]
            for st in matches
        ]
        rows.append(await self.nav(back="floods", back_label="River levels"))
        await self._render(
            event, station_list_doc(f"Gauges matching {query}", matches, snapshot), rows
        )

    async def _open_station(self, event: Any, station: FloodStation) -> None:
        user = await self._user_of(event)
        saved = await self.db.find_favourite(
            int(user["user_id"]), "station", station.station_id
        )

        top: list[Any] = []
        if saved:
            top.append(
                Button.inline(
                    "Remove from favourites",
                    await self._cb("unfav_do", f=int(saved["id"])),
                )
            )
        else:
            top.append(
                Button.inline(
                    "Save to favourites",
                    await self._cb(
                        "fav_stn",
                        r=station.station_id,
                        n=station.name[:48],
                        d=station.place[:48],
                    ),
                )
            )
        top.append(
            Button.inline("Refresh", await self._cb("stn", r=station.station_id, f=1))
        )

        rows = [top, await self.nav(back="floods", back_label="River levels")]
        await self._render(event, station_doc(station), rows)

    async def _show_nearby(self, event: Any, lat: float, lon: float) -> None:
        try:
            forecast = await self.feeds.forecast()
            flood = await self.feeds.flood()
        except FeedError as exc:
            await self._feed_error(event, exc)
            return

        # The forecast feed carries no coordinates, so the closest town is
        # found through the river gauges, which do have them, and their
        # district and state names are matched back against the forecast areas.
        ranked: list[tuple[FloodStation, float]] = []
        for station in flood.stations:
            if station.lat is None or station.lon is None:
                continue
            distance = haversine_m(lat, lon, station.lat, station.lon)
            if distance <= self.settings.nearby_radius_metres:
                ranked.append((station, distance))
        ranked.sort(key=lambda pair: pair[1])
        nearest = ranked[:NEARBY_STATION_LIMIT]

        location: Location | None = None
        for station, _distance in nearest:
            for candidate in (station.district, station.sub_basin, station.state):
                if not candidate:
                    continue
                matches = search_locations(forecast.locations, candidate, 1)
                if matches:
                    location = matches[0]
                    break
            if location is not None:
                break

        rows: list[list[Any]] = []
        if location is not None:
            rows.append(
                [
                    Button.inline(
                        f"Full forecast for {location.name[:32]}",
                        await self._cb("loc", r=location.location_id),
                    )
                ]
            )
        for station, _distance in nearest[:3]:
            rows.append(
                [Button.inline(station.name[:40], await self._cb("stn", r=station.station_id))]
            )
        rows.append(await self.nav(back="home"))

        await self._render(event, nearby_doc(location, nearest, flood), rows)

    async def _show_favourites(self, event: Any, removing: bool = False) -> None:
        user = await self._user_of(event)
        favourites = await self.db.list_favourites(int(user["user_id"]))

        rows: list[list[Any]] = []
        for fav in favourites[:CHOICE_LIMIT]:
            if removing:
                rows.append(
                    [
                        Button.inline(
                            f"Remove {fav['label'][:34]}",
                            await self._cb("unfav_do", f=int(fav["id"])),
                        )
                    ]
                )
            else:
                action = "loc" if fav["kind"] == "location" else "stn"
                icon = "🌤" if fav["kind"] == "location" else "🌊"
                rows.append(
                    [
                        Button.inline(
                            f"{icon} {fav['label'][:38]}",
                            await self._cb(action, r=str(fav["ref_id"])),
                        )
                    ]
                )

        if favourites and not removing:
            rows.append([Button.inline("Remove one", await self._cb("unfav"))])
        elif favourites and removing:
            rows.append([Button.inline("Done removing", await self._cb("favourites"))])

        rows.append(await self.nav(back="home"))
        doc = favourites_doc(favourites)
        if removing and favourites:
            doc.para("Choose a favourite below to remove it.")
        await self._render(event, doc, rows)

    async def _show_subs(self, event: Any) -> None:
        user = await self._user_of(event)
        user_id = int(user["user_id"])
        subs = await self.db.list_subscriptions(user_id)
        active = {row["kind"] for row in subs}

        rows: list[list[Any]] = []
        for kind in SUBSCRIPTION_LABELS:
            on = kind in active
            label = ("Turn off " if on else "Turn on ") + _short_label(kind)
            rows.append(
                [
                    Button.inline(
                        f"{'✅' if on else '⬜'} {label}",
                        await self._cb("sub_toggle", k=kind),
                    )
                ]
            )

        rows.append(
            [Button.inline("Adjust thresholds", await self._cb("settings"))]
        )
        rows.append(await self.nav(back="home"))
        await self._render(event, subscriptions_doc(subs, user), rows)

    async def _show_settings(self, event: Any) -> None:
        user = await self._user_of(event)
        rows = [
            [
                Button.inline(
                    "Clock: " + ("24 hour" if user["time_format"] == "24h" else "12 hour"),
                    await self._cb("set_clock"),
                ),
                Button.inline(
                    "Quiet hours: " + ("on" if int(user["quiet_enabled"]) else "off"),
                    await self._cb("set_quiet"),
                ),
            ],
            [
                Button.inline(
                    f"Quake: M{float(user['quake_threshold']):g}",
                    await self._cb("set_quake"),
                ),
                Button.inline(
                    f"Flood: {str(user['flood_threshold']).title()}",
                    await self._cb("set_flood"),
                ),
            ],
            [
                Button.inline(
                    f"Digest at {user['digest_time']}", await self._cb("set_digest")
                )
            ],
            [Button.inline("Your alerts", await self._cb("subs"))],
            await self.nav(back="home"),
        ]
        await self._render(event, settings_doc(user), rows)

    async def _feed_error(self, event: Any, exc: Exception) -> None:
        await self._render(
            event,
            error_doc(
                "The official data feed could not be reached just now.",
                str(exc),
            ),
            [await self.nav(back="home")],
        )

    # -- callbacks --------------------------------------------------------

    async def on_callback(self, event: Any) -> None:
        """Resolve a button token and run whatever it points at.

        Tokens live in SQLite for good, so a button pressed long after the
        message was sent still works. An unknown token therefore means
        something genuinely odd, and the user is routed home rather than left
        with a silent failure.
        """

        token = event.data.decode(errors="ignore")
        resolved = await self.db.resolve_callback(token)

        if resolved is None:
            await event.answer("That button is no longer recognised.", alert=True)
            await self._render(
                event,
                error_doc("That button could not be matched to an action."),
                [await self.nav(home=True)],
            )
            return

        action, payload, _owner = resolved
        await self._user_of(event)

        try:
            await self._dispatch(event, action, payload)
        except FeedError as exc:
            await self._feed_error(event, exc)
        except Exception as exc:  # noqa: BLE001 - a handler must never wedge
            log.exception("Callback %s failed: %s", action, exc)
            await self._render(
                event,
                error_doc("Something went wrong handling that button."),
                [await self.nav(back="home")],
            )
        finally:
            # Telegram spins the button until the query is answered.
            try:
                await event.answer()
            except Exception:  # noqa: BLE001
                pass

    async def _dispatch(self, event: Any, action: str, payload: dict[str, Any]) -> None:
        user = await self._user_of(event)
        user_id = int(user["user_id"])

        if action == "home":
            await self._render(
                event,
                start_doc(str(user["first_name"] or "")),
                await self.main_menu_buttons(),
            )

        elif action == "about":
            await self._render(event, about_doc(), [await self.nav(back="home")])

        elif action == "menu_weather":
            await self._show_weather_menu(event)

        elif action == "loc":
            location = await self.feeds.location_by_id(str(payload.get("r", "")))
            if location is None:
                await self._render(
                    event,
                    no_match_doc(str(payload.get("r", ""))),
                    [await self.nav(back="menu_weather", back_label="Forecasts")],
                )
            else:
                await self._open_location(event, location)

        elif action == "warnings":
            await self._show_warnings(event, str(payload.get("p", "")))

        elif action == "quakes":
            await self._show_quakes(event)

        elif action == "quake":
            snapshot = await self.feeds.quakes()
            wanted = str(payload.get("r", ""))
            match = next((q for q in snapshot.quakes if q.quake_id == wanted), None)
            if match is None:
                await self._show_quakes(event)
            else:
                await self._render(
                    event,
                    quake_detail_doc(match),
                    [await self.nav(back="quakes", back_label="Earthquakes")],
                )

        elif action == "floods":
            await self._show_floods(event)

        elif action == "stn":
            station = await self.feeds.station_by_id(str(payload.get("r", "")))
            if station is None:
                await self._show_floods(event)
            else:
                await self._open_station(event, station)

        elif action == "favourites":
            await self._show_favourites(event)

        elif action == "unfav":
            await self._show_favourites(event, removing=True)

        elif action == "fav_loc":
            added = await self.db.add_favourite(
                user_id, "location", str(payload.get("r", "")), str(payload.get("n", ""))
            )
            await event.answer(
                "Saved to your favourites." if added else "That is already saved."
            )
            location = await self.feeds.location_by_id(str(payload.get("r", "")))
            if location is not None:
                await self._open_location(event, location)
            else:
                await self._show_favourites(event)

        elif action == "fav_stn":
            added = await self.db.add_favourite(
                user_id,
                "station",
                str(payload.get("r", "")),
                str(payload.get("n", "")),
                str(payload.get("d", "")),
            )
            await event.answer(
                "Saved to your favourites." if added else "That is already saved."
            )
            station = await self.feeds.station_by_id(str(payload.get("r", "")))
            if station is not None:
                await self._open_station(event, station)
            else:
                await self._show_favourites(event)

        elif action == "unfav_do":
            removed = await self.db.remove_favourite(user_id, int(payload.get("f", 0)))
            await event.answer(
                "Removed from your favourites." if removed else "That was not saved."
            )
            await self._show_favourites(event)

        elif action == "subs":
            await self._show_subs(event)

        elif action == "sub_toggle":
            kind = str(payload.get("k", ""))
            if kind not in SUBSCRIPTION_LABELS:
                await self._show_subs(event)
                return
            if await self.db.has_subscription(user_id, kind):
                await self.db.remove_subscription(user_id, kind)
                await event.answer("Those alerts are now off.")
            else:
                await self.db.add_subscription(user_id, kind)
                await event.answer("Those alerts are now on.")
                if kind == "digest" and self.scheduler is not None:
                    # Queue the first one straight away rather than waiting for
                    # the planner's next pass.
                    await self.scheduler.queue_digest(user)
            await self._show_subs(event)

        elif action == "settings":
            await self._show_settings(event)

        elif action == "set_clock":
            new = "24h" if user["time_format"] == "12h" else "12h"
            await self.db.set_user_field(user_id, "time_format", new)
            await self._show_settings(event)

        elif action == "set_quiet":
            await self.db.set_user_field(
                user_id, "quiet_enabled", 0 if int(user["quiet_enabled"]) else 1
            )
            await self._show_settings(event)

        elif action == "set_quake":
            # Cycling beats asking for a number, which would need a
            # conversation state machine for very little gain.
            steps = [3.0, 4.0, 4.5, 5.0, 5.5, 6.0, 7.0]
            current = float(user["quake_threshold"])
            nxt = next((s for s in steps if s > current), steps[0])
            await self.db.set_user_field(user_id, "quake_threshold", nxt)
            await self._show_settings(event)

        elif action == "set_flood":
            order = ["ALERT", "WARNING", "DANGER"]
            current = str(user["flood_threshold"]).upper()
            index = order.index(current) if current in order else 0
            await self.db.set_user_field(
                user_id, "flood_threshold", order[(index + 1) % len(order)]
            )
            await self._show_settings(event)

        elif action == "set_digest":
            times = ["06:00", "06:30", "07:00", "07:30", "08:00", "12:00", "18:00"]
            current = str(user["digest_time"])
            index = times.index(current) if current in times else 0
            chosen = times[(index + 1) % len(times)]
            await self.db.set_user_field(user_id, "digest_time", chosen)
            if self.scheduler is not None:
                refreshed = await self.db.get_user(user_id)
                if refreshed is not None:
                    await self.scheduler.queue_digest(refreshed)
            await self._show_settings(event)

        else:
            log.warning("Unhandled callback action %r", action)
            await self._render(
                event,
                error_doc("That action is not available any more."),
                [await self.nav(back="home")],
            )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _args(event: Any) -> str:
    """The text following a command, if any."""

    match = getattr(event, "pattern_match", None)
    if match is None:
        return ""
    try:
        return (match.group("args") or "").strip()
    except (IndexError, KeyError):
        return ""


def _has_location(event: Any) -> bool:
    message = getattr(event, "message", None)
    return bool(message is not None and getattr(message, "geo", None))


def _short_label(kind: str) -> str:
    return {
        "warning": "weather warnings",
        "flood": "river alerts",
        "quake": "earthquake alerts",
        "digest": "morning digest",
        "national_flood": "national flood watch",
        "national_quake": "strong quake watch",
    }.get(kind, kind)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # Telethon is chatty about connection details at INFO.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = load_settings()
    bot = WeatherBot(settings)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("Interrupted, shutting down")


if __name__ == "__main__":
    main()
