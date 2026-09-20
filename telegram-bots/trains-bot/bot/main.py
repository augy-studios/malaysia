"""Bot entry point and Telethon event handlers.

Run with `python -m bot.main` from the project root, inside tmux on the VPS.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

from telethon import TelegramClient, events
from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    UserIsBlockedError,
)

from .config import ConfigError, Settings, load_settings
from .database import Database
from .gtfs import KTMB, LIVE_OPERATORS, GTFSManager, has_live, operator_label
from .richtext import RichDoc, RichSender, b, esc, i
from .scheduler import Scheduler
from . import views

log = logging.getLogger("trains-bot")

# Commands that must not be treated as a free-text search.
KNOWN_COMMANDS = {
    "start", "fav", "unfav", "sub", "unsub", "settings",
    "live", "lines", "stations", "train", "next", "stats",
}


class TrainsBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.database_path)
        self.client = TelegramClient(
            str(settings.session_path),
            settings.api_id,
            settings.api_hash,
        )
        self.gtfs = GTFSManager(self.db, settings)
        self.rich = RichSender(settings.bot_token, settings.http_timeout_seconds)
        self.scheduler = Scheduler(self)
        self._shutdown = asyncio.Event()

    # -- sending ----------------------------------------------------------

    async def send(
        self,
        chat_id: Any,
        doc: RichDoc | str,
        buttons: Any = None,
        reply_to: int | None = None,
    ) -> Any:
        """Send a rich message, tolerating the usual delivery failures."""

        try:
            return await self.rich.send(self.client, chat_id, doc, buttons=buttons,
                                        reply_to=reply_to)
        except FloodWaitError as exc:
            log.warning("Flood wait of %ss when messaging %s", exc.seconds, chat_id)
            await asyncio.sleep(min(exc.seconds, 60))
        except (UserIsBlockedError, ChatWriteForbiddenError):
            log.info("Cannot message %s, the bot was blocked or removed.", chat_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Send to %s failed: %s", chat_id, exc)
        return None

    async def edit(self, chat_id: Any, message_id: int, doc: RichDoc,
                   buttons: Any = None) -> Any:
        try:
            return await self.rich.edit(self.client, chat_id, message_id, doc, buttons=buttons)
        except Exception as exc:  # noqa: BLE001
            log.debug("Edit failed: %s", exc)
            return None

    async def reply_to_button(self, event: Any, doc: RichDoc,
                              buttons: Any = None) -> Any:
        """Replace the message a button lives on, rather than sending a new one.

        Every callback answers in place so a session stays one message the user
        can scroll back to, instead of a column of near-identical cards. An edit
        can still legitimately fail - the message may be too old to edit, or
        Telegram may reject the new content - so a failed edit falls back to
        sending, which keeps the button working either way.
        """

        edited = await self.edit(event.chat_id, event.message_id, doc, buttons=buttons)
        if edited is None:
            return await self.send(event.chat_id, doc, buttons=buttons)
        return edited

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        await self.db.connect()
        log.info("Database ready at %s", self.settings.database_path)

        await self.client.start(bot_token=self.settings.bot_token)
        me = await self.client.get_me()
        log.info("Signed in as @%s", me.username)

        self._register_handlers()

        # Warm the feeds in the background so startup is not blocked by the
        # download and parse.
        asyncio.create_task(self._warm_feeds())

        self.scheduler.start()
        log.info("Bot is running. Press Ctrl+C to stop.")

    async def _warm_feeds(self) -> None:
        try:
            await self.gtfs.warm()
        except Exception as exc:  # noqa: BLE001
            log.error("Feed warm-up failed: %s", exc)

    async def stop(self) -> None:
        log.info("Shutting down.")
        await self.scheduler.stop()
        await self.gtfs.close()
        await self.rich.close()
        await self.db.close()
        if self.client.is_connected():
            await self.client.disconnect()

    async def run_forever(self) -> None:
        await self.start()
        await self._shutdown.wait()
        await self.stop()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    # -- helpers ----------------------------------------------------------

    async def _user_of(self, event: Any) -> Any:
        sender = await event.get_sender()
        return await self.db.ensure_user(
            user_id=event.sender_id,
            chat_id=event.chat_id,
            username=getattr(sender, "username", None),
            first_name=getattr(sender, "first_name", None),
        )

    async def _render_stop(self, user: Any, operator: str, stop_id: str) -> tuple[Any, Any] | None:
        """Build the station card, resolving favourite and home state."""

        feed = await self.gtfs.get_feed(operator)
        stop = feed.stops.get(stop_id)
        if stop is None:
            return None

        user_id = user["user_id"]
        favourite = await self.db.find_favourite(user_id, operator, stop_id)
        is_home = (
            user["home_stop_id"] == stop_id and user["home_operator"] == operator
        )

        return await views.stop_doc(
            self.db, feed, operator, stop, user_id,
            time_format=user["time_format"],
            is_favourite=favourite is not None,
            favourite_id=favourite["id"] if favourite else None,
            is_home=is_home,
        )

    # -- handler registration ---------------------------------------------

    def _register_handlers(self) -> None:
        client = self.client

        client.add_event_handler(self.on_start, events.NewMessage(pattern=r"^/start(?:@\w+)?\s*$"))
        client.add_event_handler(self.on_fav, events.NewMessage(pattern=r"^/fav(?:@\w+)?(?:\s+(.*))?$"))
        client.add_event_handler(self.on_unfav, events.NewMessage(pattern=r"^/unfav(?:@\w+)?\s*$"))
        client.add_event_handler(self.on_sub, events.NewMessage(pattern=r"^/sub(?:@\w+)?\s*$"))
        client.add_event_handler(self.on_unsub, events.NewMessage(pattern=r"^/unsub(?:@\w+)?\s*$"))
        client.add_event_handler(self.on_settings, events.NewMessage(pattern=r"^/settings(?:@\w+)?\s*$"))
        client.add_event_handler(self.on_next, events.NewMessage(pattern=r"^/next(?:@\w+)?(?:\s+(.*))?$"))
        client.add_event_handler(self.on_live, events.NewMessage(pattern=r"^/live(?:@\w+)?\s*$"))
        client.add_event_handler(self.on_lines, events.NewMessage(pattern=r"^/lines(?:@\w+)?(?:\s+(.*))?$"))
        client.add_event_handler(self.on_stations, events.NewMessage(pattern=r"^/stations(?:@\w+)?(?:\s+(.*))?$"))
        client.add_event_handler(self.on_train, events.NewMessage(pattern=r"^/train(?:@\w+)?(?:\s+(.*))?$"))
        client.add_event_handler(self.on_stats, events.NewMessage(pattern=r"^/stats(?:@\w+)?\s*$"))

        client.add_event_handler(self.on_location, events.NewMessage(func=lambda e: bool(e.geo)))
        client.add_event_handler(self.on_text, events.NewMessage)
        client.add_event_handler(self.on_callback, events.CallbackQuery)

    # -- commands ---------------------------------------------------------

    async def on_start(self, event: Any) -> None:
        user = await self._user_of(event)
        doc = views.start_doc(
            user["first_name"] or "",
            self.settings.web_app_url,
            self.settings.donation_url,
        )
        await self.send(
            event.chat_id,
            doc,
            buttons=views.start_buttons(self.settings.web_app_url, self.settings.donation_url),
        )
        raise events.StopPropagation

    async def on_fav(self, event: Any) -> None:
        user = await self._user_of(event)
        query = (event.pattern_match.group(1) or "").strip()

        if query:
            await self._run_search(event, query, user)
            raise events.StopPropagation

        favourites = await self.db.list_favourites(user["user_id"])
        doc, buttons = await views.favourites_doc(self.db, favourites, user["user_id"])
        if favourites:
            doc.para(
                "To add another, send a station name or share your location, "
                f"then tap {b('Add to favourites')}."
            )
        await self.send(event.chat_id, doc, buttons=buttons or None)
        raise events.StopPropagation

    async def on_unfav(self, event: Any) -> None:
        user = await self._user_of(event)
        favourites = await self.db.list_favourites(user["user_id"])

        if not favourites:
            doc = RichDoc().heading("Nothing to remove", 3).para(
                "You have not saved any favourites yet."
            )
            await self.send(event.chat_id, doc)
            raise events.StopPropagation

        doc, buttons = await views.favourites_doc(
            self.db, favourites, user["user_id"], for_removal=True
        )
        doc.para("Tap an entry to remove it.")
        await self.send(event.chat_id, doc, buttons=buttons)
        raise events.StopPropagation

    async def on_sub(self, event: Any) -> None:
        user = await self._user_of(event)
        subs = await self.db.list_subscriptions(user["user_id"])
        doc, buttons = await views.subscriptions_doc(
            self.db, subs, user["user_id"], user["lead_minutes"], user["digest_time"]
        )
        await self.send(event.chat_id, doc, buttons=buttons)
        raise events.StopPropagation

    async def on_unsub(self, event: Any) -> None:
        user = await self._user_of(event)
        removed = await self.db.remove_all_subscriptions(user["user_id"])

        doc = RichDoc()
        if removed:
            doc.heading("Notifications off", 3)
            doc.para(
                f"All {b(str(removed))} of your notification settings have been "
                "switched off. Your favourites are untouched, so you can turn "
                "notifications back on any time with /sub."
            )
        else:
            doc.heading("Nothing to switch off", 3)
            doc.para("You had no notifications enabled. Use /sub to set them up.")

        await self.send(event.chat_id, doc)
        raise events.StopPropagation

    async def on_settings(self, event: Any) -> None:
        user = await self._user_of(event)
        doc, buttons = await views.settings_doc(self.db, user, user["user_id"])
        await self.send(event.chat_id, doc, buttons=buttons)
        raise events.StopPropagation

    async def on_next(self, event: Any) -> None:
        """Next departures from the home station, or from a named station."""

        user = await self._user_of(event)
        query = (event.pattern_match.group(1) or "").strip()

        if query:
            await self._run_search(event, query, user)
            raise events.StopPropagation

        if not user["home_stop_id"]:
            doc = RichDoc().heading("No home station yet", 3)
            doc.para(
                "Set one and this command will answer with your next trains "
                "straight away. Search for a station, open it, then tap "
                f"{b('Set as home')}."
            )
            doc.para("You can also pick one from /settings.")
            await self.send(event.chat_id, doc)
            raise events.StopPropagation

        operator, stop_id = user["home_operator"], user["home_stop_id"]
        try:
            feed = await self.gtfs.get_feed(operator)
        except Exception:  # noqa: BLE001
            await self.send(
                event.chat_id,
                RichDoc().heading("Timetable unavailable", 3).para(
                    "The schedule feed could not be reached just now. Please try again shortly."
                ),
            )
            raise events.StopPropagation

        stop = feed.stops.get(stop_id)
        if stop is None:
            doc = RichDoc().heading("Home station missing", 3).para(
                "Your home station is no longer in the published schedule. "
                "Pick a new one from /settings."
            )
            await self.send(event.chat_id, doc)
            raise events.StopPropagation

        doc = views.next_doc(feed, operator, stop, user["time_format"])
        buttons = [
            [
                await views.cb(self.db, "🔄 Refresh", "next:home", {}, user["user_id"]),
                await views.cb(self.db, "🚉 Open station", "stop:view",
                               {"op": operator, "stop": stop_id}, user["user_id"]),
            ]
        ]
        await self.send(event.chat_id, doc, buttons=buttons)
        raise events.StopPropagation

    async def on_live(self, event: Any) -> None:
        user = await self._user_of(event)
        await self._send_live(event.chat_id, user["user_id"])
        raise events.StopPropagation

    async def _send_live(self, chat_id: Any, user_id: int) -> None:
        vehicles = await self.gtfs.get_vehicles(KTMB)
        feed = None
        try:
            feed = await self.gtfs.get_feed(KTMB)
        except Exception:  # noqa: BLE001
            pass
        buttons = [
            [await views.cb(self.db, "🔄 Refresh", "live:show", {}, user_id)]
        ]
        await self.send(chat_id, views.live_doc(vehicles, feed), buttons=buttons)

    async def on_lines(self, event: Any) -> None:
        user = await self._user_of(event)
        query = (event.pattern_match.group(1) or "").strip()

        if query:
            await self._run_search(event, query, user)
            raise events.StopPropagation

        buttons = await views.operator_picker(self.db, "lines:list", user["user_id"])
        doc = RichDoc().heading("Browse lines", 3).para(
            "Pick an operator, or send a line name directly such as "
            f"{b('Kelana Jaya')} or {b('ETS')}."
        )
        await self.send(event.chat_id, doc, buttons=buttons)
        raise events.StopPropagation

    async def on_stations(self, event: Any) -> None:
        user = await self._user_of(event)
        query = (event.pattern_match.group(1) or "").strip()

        if query:
            await self._run_search(event, query, user)
            raise events.StopPropagation

        doc = RichDoc().heading("Find a station", 3)
        doc.para(
            "Send the name of a station and the search runs automatically, or "
            "share your location to see what is nearby."
        )
        doc.bullets(
            [
                f"Example: {b('KL Sentral')}",
                f"Example: {b('Bangsar')}",
                "Example: /stations Kajang",
            ]
        )
        await self.send(event.chat_id, doc)
        raise events.StopPropagation

    async def on_train(self, event: Any) -> None:
        user = await self._user_of(event)
        query = (event.pattern_match.group(1) or "").strip()

        if query:
            await self._run_search(event, query, user)
            raise events.StopPropagation

        buttons = await views.operator_picker(self.db, "lines:list", user["user_id"])
        doc = RichDoc().heading("Follow a train", 3).para(
            "Pick an operator, choose a line, then pick a departure to see "
            "every station on that journey."
        )
        await self.send(event.chat_id, doc, buttons=buttons)
        raise events.StopPropagation

    async def on_stats(self, event: Any) -> None:
        if self.settings.admin_ids and event.sender_id not in self.settings.admin_ids:
            return
        counts = await self.db.counts()
        doc = RichDoc().heading("Bot statistics", 3)
        doc.table(
            ["Metric", "Count"],
            [[key.title(), str(value)] for key, value in counts.items()],
        )
        doc.para(
            f"Rich messages: {b('enabled' if self.rich.rich_supported else 'falling back to HTML')}"
        )
        await self.send(event.chat_id, doc)
        raise events.StopPropagation

    # -- free text and location -------------------------------------------

    async def on_location(self, event: Any) -> None:
        user = await self._user_of(event)
        geo = event.geo
        lat, lon = float(geo.lat), float(geo.long)
        await self.db.set_last_location(user["user_id"], lat, lon)

        nearby = await self.gtfs.nearby_all(lat, lon, self.settings.nearby_radius_metres)

        doc = RichDoc().heading("Stations near you", 3)
        if not nearby:
            doc.para(
                "No stations were found within "
                f"{self.settings.nearby_radius_metres} m. The rail network "
                "covers the Klang Valley and the KTM corridors, so coverage "
                "elsewhere is thin."
            )
            await self.send(event.chat_id, doc)
            raise events.StopPropagation

        doc.bullets(
            [
                f"{b(stop.display)} · {int(distance)} m · {i(operator_label(operator))}"
                for operator, stop, distance in nearby
            ]
        )

        buttons = []
        for operator, stop, distance in nearby[:8]:
            label = stop.display if len(stop.display) <= 26 else stop.display[:25] + "…"
            buttons.append(
                [
                    await views.cb(
                        self.db, f"🚉 {label} · {int(distance)}m", "stop:view",
                        {"op": operator, "stop": stop.stop_id}, user["user_id"],
                    )
                ]
            )

        await self.send(event.chat_id, doc, buttons=buttons)
        raise events.StopPropagation

    async def on_text(self, event: Any) -> None:
        """Treat any other message as a search, which is the main entry point."""

        text = (event.raw_text or "").strip()
        if not text or event.geo:
            return

        if text.startswith("/"):
            command = text[1:].split()[0].split("@")[0].lower()
            if command in KNOWN_COMMANDS:
                return  # a dedicated handler already dealt with it
            doc = RichDoc().heading("Unknown command", 3).para(
                "That command is not recognised. Send /start to see everything "
                "on offer, or just send a station name to search."
            )
            await self.send(event.chat_id, doc)
            return

        if len(text) < 2:
            return

        user = await self._user_of(event)
        await self._run_search(event, text, user)

    async def _run_search(self, event: Any, query: str, user: Any) -> None:
        async with self.client.action(event.chat_id, "typing"):
            stops = await self.gtfs.search_all_stops(query)
            routes = await self.gtfs.search_all_routes(query)

        doc, buttons = await views.search_results_doc(
            self.db, query, stops, routes, user["user_id"]
        )
        await self.send(event.chat_id, doc, buttons=buttons or None)

    # -- callbacks --------------------------------------------------------

    async def on_callback(self, event: Any) -> None:
        token = event.data.decode(errors="ignore")
        resolved = await self.db.resolve_callback(token)

        if resolved is None:
            # The registry is durable, so this only happens if the database was
            # replaced. Telling the user plainly beats a silent dead button.
            await event.answer(
                "This button is no longer available. Send /start to begin again.",
                alert=True,
            )
            return

        action, payload, _owner = resolved
        user = await self.db.ensure_user(event.sender_id, event.chat_id)

        try:
            await self._dispatch_callback(event, action, payload, user)
        except Exception as exc:  # noqa: BLE001
            log.exception("Callback %s failed: %s", action, exc)
            await event.answer("Something went wrong handling that.", alert=True)

    async def _dispatch_callback(self, event: Any, action: str,
                                 payload: dict[str, Any], user: Any) -> None:
        user_id = user["user_id"]

        # -- stations -----------------------------------------------------
        if action == "stop:view":
            operator, stop_id = payload["op"], payload["stop"]
            rendered = await self._render_stop(user, operator, stop_id)
            if rendered is None:
                await event.answer("That station is no longer in the schedule.", alert=True)
                return
            doc, buttons = rendered
            await event.answer()
            await self.reply_to_button(event, doc, buttons=buttons)
            return

        if action == "stop:live":
            operator, stop_id = payload["op"], payload["stop"]
            if not has_live(operator):
                await event.answer(
                    f"{operator_label(operator)} does not broadcast live positions.",
                    alert=True,
                )
                return

            feed = await self.gtfs.get_feed(operator)
            stop = feed.stops.get(stop_id)
            await event.answer("Checking live positions")

            vehicles = await self.gtfs.get_vehicles(operator)
            if stop and stop.lat is not None and stop.lon is not None:
                from .gtfs import haversine_m

                near = [
                    v for v in vehicles
                    if v.has_position
                    and haversine_m(stop.lat, stop.lon, v.lat, v.lon) <= 5000
                ]
                doc = RichDoc().heading(f"Live trains near {stop.display}", 3)
                if not near:
                    doc.para(
                        "No trains are reporting a position within 5 km of this station."
                    )
                else:
                    for vehicle in near[:8]:
                        route = feed.routes.get(vehicle.route_id)
                        distance = haversine_m(stop.lat, stop.lon, vehicle.lat, vehicle.lon)
                        doc.para(
                            f"{b(route.display if route else vehicle.route_id or 'Unknown line')} · "
                            f"{int(distance)} m away<br>"
                            f"<a href=\"{vehicle.maps_url}\">Open in Maps</a>"
                        )
            else:
                doc = views.live_doc(vehicles, feed)

            # Editing in place would otherwise strand the user on a view with
            # no keyboard, so carry a way back to the station.
            buttons = [
                [
                    await views.cb(self.db, "🔄 Refresh", "stop:live",
                                   {"op": operator, "stop": stop_id}, user_id),
                    await views.cb(self.db, "◀ Back to station", "stop:view",
                                   {"op": operator, "stop": stop_id}, user_id),
                ]
            ]
            await self.reply_to_button(event, doc, buttons=buttons)
            return

        # -- lines --------------------------------------------------------
        if action == "route:view":
            operator, route_id = payload["op"], payload["route"]
            feed = await self.gtfs.get_feed(operator)
            route = feed.routes.get(route_id)
            if route is None:
                await event.answer("That line is no longer published.", alert=True)
                return
            doc, buttons = await views.route_doc(
                self.db, feed, operator, route, user_id, page=int(payload.get("p", 0))
            )
            await event.answer()
            await self.reply_to_button(event, doc, buttons=buttons)
            return

        if action == "route:trips":
            operator, route_id = payload["op"], payload["route"]
            feed = await self.gtfs.get_feed(operator)
            route = feed.routes.get(route_id)
            if route is None:
                await event.answer("That line is no longer published.", alert=True)
                return
            doc, buttons = await views.trip_list_doc(
                self.db, feed, operator, route, user_id,
                time_format=user["time_format"], page=int(payload.get("p", 0)),
            )
            await event.answer()
            buttons = list(buttons)
            buttons.append(
                [await views.cb(self.db, "◀ Back to line", "route:view",
                                {"op": operator, "route": route_id}, user_id)]
            )
            await self.reply_to_button(event, doc, buttons=buttons)
            return

        if action == "lines:list":
            operator = payload["op"]
            await event.answer("Loading lines")
            feed = await self.gtfs.get_feed(operator)
            routes = sorted(feed.routes.values(), key=lambda r: r.display)

            doc = RichDoc().heading(f"Lines · {operator_label(operator)}", 3)
            page = int(payload.get("p", 0))
            page_size = 10
            pages = max(1, (len(routes) + page_size - 1) // page_size)
            page = max(0, min(page, pages - 1))
            window = routes[page * page_size : (page + 1) * page_size]

            if pages > 1:
                doc.para(f"Page {page + 1} of {pages}.")
            doc.bullets([esc(r.display) for r in window])

            buttons = []
            for route in window:
                label = route.display if len(route.display) <= 30 else route.display[:29] + "…"
                buttons.append(
                    [await views.cb(self.db, f"🚆 {label}", "route:view",
                                    {"op": operator, "route": route.route_id}, user_id)]
                )

            nav = []
            if page > 0:
                nav.append(await views.cb(self.db, "◀ Previous", "lines:list",
                                          {"op": operator, "p": page - 1}, user_id))
            if page < pages - 1:
                nav.append(await views.cb(self.db, "Next ▶", "lines:list",
                                          {"op": operator, "p": page + 1}, user_id))
            if nav:
                buttons.append(nav)

            await self.reply_to_button(event, doc, buttons=buttons)
            return

        # -- trips --------------------------------------------------------
        if action == "trip:view":
            operator, trip_id = payload["op"], payload["trip"]
            feed = await self.gtfs.get_feed(operator)
            await event.answer()

            trip = feed.trips.get(trip_id)
            buttons = []
            if trip is not None:
                buttons.append(
                    [await views.cb(self.db, "◀ Back to line", "route:trips",
                                    {"op": operator, "route": trip.route_id}, user_id)]
                )
            await self.reply_to_button(
                event,
                views.trip_doc(feed, operator, trip_id, user["time_format"]),
                buttons=buttons or None,
            )
            return

        # -- live ---------------------------------------------------------
        if action == "live:show":
            await event.answer("Fetching live positions")
            vehicles = await self.gtfs.get_vehicles(KTMB)
            feed = None
            try:
                feed = await self.gtfs.get_feed(KTMB)
            except Exception:  # noqa: BLE001
                pass
            buttons = [
                [await views.cb(self.db, "🔄 Refresh", "live:show", {}, user_id)]
            ]
            await self.reply_to_button(event, views.live_doc(vehicles, feed), buttons=buttons)
            return

        # -- next ---------------------------------------------------------
        if action == "next:home":
            if not user["home_stop_id"]:
                await event.answer("No home station is set yet.", alert=True)
                return
            operator, stop_id = user["home_operator"], user["home_stop_id"]
            feed = await self.gtfs.get_feed(operator)
            stop = feed.stops.get(stop_id)
            if stop is None:
                await event.answer("Your home station is no longer published.", alert=True)
                return
            await event.answer()
            doc = views.next_doc(feed, operator, stop, user["time_format"])
            buttons = [
                [
                    await views.cb(self.db, "🔄 Refresh", "next:home", {}, user_id),
                    await views.cb(self.db, "🚉 Open station", "stop:view",
                                   {"op": operator, "stop": stop_id}, user_id),
                ]
            ]
            await self.reply_to_button(event, doc, buttons=buttons)
            return

        # -- home station -------------------------------------------------
        if action == "home:set":
            operator, stop_id = payload["op"], payload["stop"]
            feed = await self.gtfs.get_feed(operator)
            stop = feed.stops.get(stop_id)
            if stop is None:
                await event.answer("That station is no longer in the schedule.", alert=True)
                return

            await self.db.set_home_station(user_id, operator, stop_id, stop.display)
            await event.answer(f"{stop.display} is now your home station")

            refreshed = await self.db.get_user(user_id)
            rendered = await self._render_stop(refreshed or user, operator, stop_id)
            if rendered is not None:
                doc, buttons = rendered
                await self.reply_to_button(event, doc, buttons=buttons)
            return

        if action == "set:home":
            await event.answer()
            favourites = await self.db.list_favourites(user_id)
            doc = RichDoc().heading("Home station", 3)
            doc.para(
                "This is the station /next answers with. Pick one of your "
                "favourites, or open any station and tap "
                f"{b('Set as home')}."
            )
            buttons = []
            for row in favourites[:8]:
                label = row["stop_name"]
                if len(label) > 28:
                    label = label[:27] + "…"
                buttons.append(
                    [await views.cb(self.db, f"🏠 {label}", "home:set",
                                    {"op": row["operator"], "stop": row["stop_id"]}, user_id)]
                )
            if not favourites:
                doc.para("You have no favourites yet, so there is nothing to pick from here.")
            buttons.append([await views.cb(self.db, "◀ Back", "set:back", {}, user_id)])
            await self.reply_to_button(event, doc, buttons=buttons)
            return

        # -- favourites ---------------------------------------------------
        if action == "fav:add":
            operator, stop_id = payload["op"], payload["stop"]
            feed = await self.gtfs.get_feed(operator)
            stop = feed.stops.get(stop_id)
            if stop is None:
                await event.answer("That station is no longer in the schedule.", alert=True)
                return

            added = await self.db.add_favourite(user_id, operator, stop_id, stop.display)
            await event.answer("Saved to favourites" if added else "Already in your favourites")

            # Re-render the station in place so the star flips to "Remove". The
            # toast already confirms the save, so a separate card would only
            # bury the timetable the user was reading.
            rendered = await self._render_stop(user, operator, stop_id)
            if rendered is not None:
                doc, buttons = rendered
                await self.reply_to_button(event, doc, buttons=buttons)
            return

        if action == "fav:remove":
            removed = await self.db.remove_favourite(user_id, int(payload["fid"]))
            await event.answer("Removed" if removed else "That favourite was already gone")
            if removed:
                favourites = await self.db.list_favourites(user_id)
                doc, buttons = await views.favourites_doc(
                    self.db, favourites, user_id, for_removal=True
                )
                await self.reply_to_button(event, doc, buttons=buttons or None)
            return

        # -- subscriptions ------------------------------------------------
        if action == "sub:toggle":
            kind = payload["kind"]
            currently_on = await self.db.has_subscription(user_id, kind)

            if currently_on:
                await self.db.remove_subscriptions_of_kind(user_id, kind)
                await event.answer(f"{views.SUB_LABELS[kind]} switched off")
            else:
                if kind in ("departure", "live"):
                    favourites = await self.db.list_favourites(user_id)
                    if not favourites:
                        await event.answer(
                            "Add a favourite station first, then this can watch it.",
                            alert=True,
                        )
                        return
                    if kind == "live" and not any(
                        has_live(fav["operator"]) for fav in favourites
                    ):
                        await event.answer(
                            "Live alerts need a KTMB favourite, because Rapid KL "
                            "does not broadcast positions.",
                            alert=True,
                        )
                        return
                    for fav in favourites:
                        await self.db.add_subscription(user_id, kind, fav["id"])
                else:
                    await self.db.add_subscription(user_id, kind, None)
                await event.answer(f"{views.SUB_LABELS[kind]} switched on")

                if kind == "digest":
                    await self.scheduler._queue_digest(user)

            subs = await self.db.list_subscriptions(user_id)
            doc, buttons = await views.subscriptions_doc(
                self.db, subs, user_id, user["lead_minutes"], user["digest_time"]
            )
            await self.reply_to_button(event, doc, buttons=buttons)
            return

        # -- settings -----------------------------------------------------
        if action == "set:operator":
            await event.answer()
            buttons = await views.operator_picker(self.db, "set:operator:pick", user_id)
            buttons.append([await views.cb(self.db, "◀ Back", "set:back", {}, user_id)])
            doc = RichDoc().heading("Preferred operator", 3).para(
                "This is the operator shown first when a command does not name one."
            )
            await self.reply_to_button(event, doc, buttons=buttons)
            return

        if action == "set:operator:pick":
            await self.db.set_pref(user_id, "operator", payload["op"])
            await event.answer(f"Preference set to {operator_label(payload['op'])}")
            await self._show_settings(event, user_id)
            return

        if action == "set:timefmt":
            new_format = "24h" if user["time_format"] == "12h" else "12h"
            await self.db.set_pref(user_id, "time_format", new_format)
            await event.answer(f"Clock set to {'24 hour' if new_format == '24h' else '12 hour'}")
            await self._show_settings(event, user_id)
            return

        if action == "set:lead":
            await event.answer()
            buttons = [
                [
                    await views.cb(self.db, f"{minutes} min", "set:lead:pick",
                                   {"m": minutes}, user_id)
                    for minutes in (5, 10, 15)
                ],
                [
                    await views.cb(self.db, f"{minutes} min", "set:lead:pick",
                                   {"m": minutes}, user_id)
                    for minutes in (20, 30, 45)
                ],
                [await views.cb(self.db, "◀ Back", "set:back", {}, user_id)],
            ]
            doc = RichDoc().heading("Reminder lead time", 3).para(
                "How far ahead of each departure should a reminder arrive?"
            )
            await self.reply_to_button(event, doc, buttons=buttons)
            return

        if action == "set:lead:pick":
            await self.db.set_pref(user_id, "lead_minutes", int(payload["m"]))
            await event.answer(f"Reminders will arrive {payload['m']} minutes ahead")
            await self._show_settings(event, user_id)
            return

        if action == "set:quiet":
            enabled = not bool(user["quiet_enabled"])
            await self.db.set_pref(user_id, "quiet_enabled", 1 if enabled else 0)
            await event.answer("Quiet hours on" if enabled else "Quiet hours off")
            await self._show_settings(event, user_id)
            return

        if action == "set:digest":
            await event.answer()
            buttons = [
                [
                    await views.cb(self.db, when, "set:digest:pick", {"t": when}, user_id)
                    for when in ("06:00", "06:30", "07:00")
                ],
                [
                    await views.cb(self.db, when, "set:digest:pick", {"t": when}, user_id)
                    for when in ("07:30", "08:00", "09:00")
                ],
                [await views.cb(self.db, "◀ Back", "set:back", {}, user_id)],
            ]
            doc = RichDoc().heading("Daily digest time", 3).para(
                "When should the morning summary arrive? All times are Malaysia time."
            )
            await self.reply_to_button(event, doc, buttons=buttons)
            return

        if action == "set:digest:pick":
            await self.db.set_pref(user_id, "digest_time", payload["t"])
            await event.answer(f"Digest set for {payload['t']}")
            await self._show_settings(event, user_id)
            return

        if action == "set:wipe":
            await event.answer()
            buttons = [
                [
                    await views.cb(self.db, "Yes, delete everything", "set:wipe:confirm",
                                   {}, user_id),
                    await views.cb(self.db, "Cancel", "set:wipe:cancel", {}, user_id),
                ]
            ]
            doc = RichDoc().heading("Delete your data", 3)
            doc.para(
                "This removes your favourites, notification settings, home "
                "station and preferences. It cannot be undone."
            )
            await self.reply_to_button(event, doc, buttons=buttons)
            return

        if action == "set:wipe:confirm":
            for fav in await self.db.list_favourites(user_id):
                await self.db.remove_favourite(user_id, fav["id"])
            await self.db.remove_all_subscriptions(user_id)
            await self.db.set_home_station(user_id, "", "", "")
            await event.answer("Your data has been deleted")
            doc = RichDoc().heading("Data deleted", 3).para(
                "Everything stored about you is gone. Send /start whenever you "
                "want to begin again."
            )
            # No keyboard here on purpose: every button would point at data
            # that has just been deleted.
            await self.reply_to_button(event, doc)
            return

        if action == "set:wipe:cancel":
            await event.answer("Nothing was deleted")
            await self._show_settings(event, user_id)
            return

        if action == "set:back":
            await event.answer()
            await self._show_settings(event, user_id)
            return

        await event.answer("That action is not available any more.", alert=True)

    async def _show_settings(self, event: Any, user_id: int) -> None:
        """Re-render settings over the message the button was tapped on."""

        user = await self.db.get_user(user_id)
        if user is None:
            return
        doc, buttons = await views.settings_doc(self.db, user, user_id)
        await self.reply_to_button(event, doc, buttons=buttons)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Telethon is chatty at INFO and drowns out the bot's own logging.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def amain() -> int:
    configure_logging()

    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("%s", exc)
        return 1

    bot = TrainsBot(settings)

    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signal_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, bot.request_shutdown)
        except NotImplementedError:
            # Windows does not support add_signal_handler; KeyboardInterrupt
            # still unwinds correctly there.
            pass

    try:
        await bot.run_forever()
    except KeyboardInterrupt:
        await bot.stop()
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
