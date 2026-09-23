"""Background work, with every pending job kept in SQLite.

There is no in-process timer wheel. Digests, departure reminders and prayer
reminders are rows in `scheduled_jobs` with a `run_at` and a dedupe key, and
one loop claims whatever is due. Stop the bot for a week and nothing is lost:
the rows are still there when it comes back. A job whose moment passed while
the bot was down is dropped rather than sent late, since a reminder for a
train that left an hour ago helps nobody.

The alert watchers are different in kind. They poll the feeds on an interval
and compare what they see against each person's and each server's settings,
using `alert_state` so one storm produces one message rather than one per
poll.

Personal alerts go to the person's DM with the bot. Server alerts go to the
server's chosen channel, and only in servers the bot is a member of. Nothing
here ever posts into a channel reached through a user install, where the bot
has no standing to post.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from typing import Any

from . import config
from .database import guild_target, user_target
from .extras import PRAYER_LABELS, REMINDER_PRAYERS, ExtrasError
from .features.extras import fuel_table
from .features.weather import gauge_line, warning_text
from .timeutils import format_wait, in_quiet_hours, next_clock_epoch, now_myt, parse_iso, today_myt, ts
from .transit import OPERATORS, haversine_m, operator_label
from .ui import (
    AMBER,
    BLUE,
    PURPLE,
    RED,
    SEVERITY_COLOURS,
    SEVERITY_EMOJI,
    TEAL,
    Button,
    Link,
    Screen,
    add_field,
    embed,
    lines_block,
)
from .weather import FLOOD_RANK, FeedError, FloodStation, Quake, Warning

log = logging.getLogger(__name__)

# Keys in alert_state are remembered this long, which is also how long one
# event stays "already sent".
ALERT_MEMORY = 7 * 86400
# A gauge that stays high is mentioned again after this long.
FLOOD_REPEAT = 3 * 3600
SERVER_FLOOD_REPEAT = 12 * 3600
LIVE_REPEAT = 20 * 60
# Proximity for live vehicle alerts. Rail corridors are long.
LIVE_RADIUS_M = {"rail": 2500, "bus": 1500}
# Late by more than this and a job is dropped rather than sent.
GRACE = {"departure": 3 * 60, "prayer": 10 * 60, "digest": 2 * 3600}
# Rapid KL rail runs every few minutes. Without a cap one saved station would
# queue dozens of reminders an hour.
MAX_REMINDERS_PER_ROUTE = 4
FEED_STALE_AFTER = 3 * 3600


class Scheduler:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.db = bot.db
        self._tasks: list[asyncio.Task[Any]] = []
        self._stopping = asyncio.Event()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        loops = {
            "jobs": (self._drain_jobs, config.SCHEDULER_TICK_SECONDS, 5),
            "planner": (self.plan_all, 15 * 60, 60),
            "alerts": (self.check_alerts, config.ALERT_POLL_SECONDS, 90),
            "live": (self._check_live, config.LIVE_POLL_SECONDS, 120),
            "health": (self._check_health, 30 * 60, 300),
        }
        self._tasks = [
            asyncio.create_task(self._loop(name, fn, every, first), name=f"loop-{name}")
            for name, (fn, every, first) in loops.items()
        ]
        log.info("Scheduler started with %d loops", len(self._tasks))

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self, name: str, fn: Any, every: float, first: float) -> None:
        # Until the gateway is ready the server list is empty, and a channel
        # alert would conclude the bot had been removed from every server.
        await self.bot.wait_until_ready()
        # The first pass waits a little, so the feeds can load after a restart.
        if not await self._sleep(first):
            return
        while not self._stopping.is_set():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                log.exception("Scheduler loop %s failed", name)
            if not await self._sleep(every):
                return

    async def _sleep(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return True
        return False

    # -- jobs -------------------------------------------------------------

    async def _drain_jobs(self) -> None:
        now = int(time.time())
        for job in await self.db.due_jobs(now):
            await self.db.delete_job(job["id"])
            try:
                payload = json.loads(job["payload"] or "{}")
            except ValueError:
                payload = {}
            late = now - job["run_at"]
            if late > GRACE.get(job["job_type"], 600):
                log.info("Dropping %s job %s, %ds late", job["job_type"], job["id"], late)
                if job["job_type"] == "digest":
                    await self._queue_digest(int(job["target"][2:]))
                continue
            try:
                await self._run_job(job["job_type"], job["target"], payload)
            except Exception:  # noqa: BLE001
                log.exception("Job %s (%s) failed", job["id"], job["job_type"])

    async def _run_job(self, job_type: str, target: str, payload: dict[str, Any]) -> None:
        if not target.startswith("u:"):
            return
        user_id = int(target[2:])
        user = await self.db.get_user(user_id)
        if user is None:
            return
        subs = await self.db.user_subscriptions(user_id)
        if job_type not in subs:
            return

        if job_type == "digest":
            await self._queue_digest(user_id)
            # No quiet hours check: the person chose this time themselves.
            screen = await self.digest(user)
            if screen is not None:
                await self.bot.send_dm(user_id, screen)
        elif job_type == "departure":
            if not in_quiet_hours(user):
                await self.bot.send_dm(user_id, departure_screen(payload))
        elif job_type == "prayer":
            # Prayer reminders ignore quiet hours: Subuh falls inside almost
            # everyone's, and it is the reminder people most want.
            await self.bot.send_dm(user_id, prayer_reminder_screen(payload))

    # -- planning ---------------------------------------------------------

    async def plan_all(self) -> None:
        for sub_kind, planner in (
            ("digest", self._plan_digest),
            ("departure", self._plan_departures),
            ("prayer", self._plan_prayers),
        ):
            for user in await self.db.subscribers(sub_kind):
                try:
                    await planner(user)
                except Exception:  # noqa: BLE001
                    log.exception("Planning %s for %s failed", sub_kind, user["user_id"])
        await self.db.purge_stale_jobs(int(time.time()) - 86400)
        await self.db.prune_alert_state(int(time.time()) - ALERT_MEMORY)

    async def plan_for(self, user_id: int) -> None:
        """Plan one person's jobs straight away, after they change something."""

        user = await self.db.get_user(user_id)
        if user is None:
            return
        subs = await self.db.user_subscriptions(user_id)
        try:
            if "digest" in subs:
                await self._plan_digest(user)
            if "departure" in subs:
                await self._plan_departures(user)
            if "prayer" in subs:
                await self._plan_prayers(user)
        except Exception:  # noqa: BLE001 - the regular planner will catch up
            log.exception("Planning for %s failed", user_id)

    async def _plan_digest(self, user: Any) -> None:
        await self._queue_digest(int(user["user_id"]), user["digest_time"])

    async def _queue_digest(self, user_id: int, clock: str | None = None) -> None:
        if clock is None:
            user = await self.db.get_user(user_id)
            if user is None:
                return
            clock = user["digest_time"]
        run_at = next_clock_epoch(clock)
        day = time.strftime("%Y-%m-%d", time.gmtime(run_at + 8 * 3600))
        await self.db.schedule_job(
            "digest", run_at, user_target(user_id), dedupe_key=f"digest:{user_id}:{day}"
        )

    async def _plan_departures(self, user: Any) -> None:
        """Queue reminders for departures in the next hour at saved stops."""

        user_id = int(user["user_id"])
        lead = int(user["lead_minutes"] or 10) * 60
        now = time.time()
        horizon = now + 3600
        for fav in await self.db.list_favourites(user_id):
            if fav["kind"] not in ("station", "stop") or fav["operator"] not in OPERATORS:
                continue
            feed = self.bot.transit.peek(fav["operator"])
            if feed is None:
                continue
            per_route: dict[str, int] = {}
            for dep in feed.departures(fav["ref_id"], limit=60, horizon_hours=2):
                fire_at = dep.epoch - lead
                if fire_at <= now or fire_at > horizon:
                    continue
                if per_route.get(dep.route_id, 0) >= MAX_REMINDERS_PER_ROUTE:
                    continue
                per_route[dep.route_id] = per_route.get(dep.route_id, 0) + 1
                route = feed.routes.get(dep.route_id)
                await self.db.schedule_job(
                    "departure",
                    fire_at,
                    user_target(user_id),
                    payload={
                        "stop": fav["label"],
                        "route": route.display if route else dep.route_id,
                        "headsign": dep.headsign,
                        "epoch": dep.epoch,
                        "mode": feed.mode,
                        "operator": fav["operator"],
                        "stop_id": fav["ref_id"],
                    },
                    dedupe_key=f"dep:{user_id}:{fav['operator']}:{fav['ref_id']}:{dep.trip_id}:{dep.epoch}",
                )

    async def _plan_prayers(self, user: Any) -> None:
        zone = user["prayer_zone"]
        if not zone:
            return
        user_id = int(user["user_id"])
        now = time.time()
        for day in (today_myt(), today_myt() + timedelta(days=1)):
            try:
                entry = await self.bot.extras.prayer_day(zone, day)
            except ExtrasError:
                return
            if entry is None:
                continue
            for key in REMINDER_PRAYERS:
                epoch = entry.times.get(key)
                if not epoch or epoch <= now:
                    continue
                await self.db.schedule_job(
                    "prayer",
                    epoch,
                    user_target(user_id),
                    payload={"zone": zone, "prayer": key, "epoch": epoch},
                    dedupe_key=f"prayer:{user_id}:{zone}:{key}:{epoch}",
                )

    # -- the digest -------------------------------------------------------

    async def digest(self, user: Any) -> Screen | None:
        user_id = int(user["user_id"])
        favs = await self.db.list_favourites(user_id)
        body = embed(
            "☀️ Your morning digest",
            f"{now_myt().strftime('%A')}, {ts(time.time(), 'D')}",
            BLUE,
            footer="Change the time or the places in /settings and /fav list",
        )

        towns = [f for f in favs if f["kind"] == "town"]
        if towns:
            try:
                forecast = await self.bot.weather.forecast()
                active = (await self.bot.weather.warnings()).warnings
            except FeedError:
                forecast, active = None, []
            lines = []
            seen: set[str] = set()
            for fav in towns:
                loc = forecast.locations.get(fav["ref_id"]) if forecast else None
                today = loc.today if loc else None
                if today is None:
                    continue
                umbrella = " ☂️" if today.is_wet else ""
                lines.append(f"{today.emoji} **{loc.name}** {today.headline}, {today.temp_range}{umbrella}")
                for warning in active:
                    if warning.mentions(loc.name) and warning.warning_id not in seen:
                        seen.add(warning.warning_id)
                        lines.append(f"  ⚠️ {warning.title}")
            if lines:
                add_field(body, "Weather", "\n".join(lines))

        gauges = [f for f in favs if f["kind"] == "gauge"]
        if gauges:
            try:
                stations = {s.station_id: s for s in (await self.bot.weather.flood()).stations}
            except FeedError:
                stations = {}
            lines = [gauge_line(stations[g["ref_id"]]) for g in gauges if g["ref_id"] in stations]
            if lines:
                add_field(body, "Rivers", "\n".join(lines))

        for fav in [f for f in favs if f["kind"] in ("station", "stop")][:6]:
            feed = self.bot.transit.peek(fav["operator"])
            if feed is None:
                continue
            deps = feed.departures(fav["ref_id"], limit=4)
            if deps:
                add_field(
                    body,
                    f"{'🚆' if feed.mode == 'rail' else '🚌'} {fav['label']}",
                    "\n".join(
                        f"{ts(d.epoch)} {(feed.routes[d.route_id].short_name or feed.routes[d.route_id].display) if d.route_id in feed.routes else d.route_id} to {d.headsign}"
                        for d in deps
                    ),
                )

        if user["prayer_zone"]:
            try:
                entry = await self.bot.extras.prayer_day(user["prayer_zone"], today_myt())
            except ExtrasError:
                entry = None
            if entry is not None:
                add_field(
                    body,
                    f"🕌 Prayer times, {user['prayer_zone']}",
                    "  ".join(
                        f"{PRAYER_LABELS[k]} {ts(entry.times[k])}" for k in REMINDER_PRAYERS if k in entry.times
                    ),
                )

        if not body.fields:
            body.description += (
                "\n\nNothing to report yet. Save towns, gauges, stations or stops with their ⭐ "
                "buttons and they will appear here."
            )
        return Screen(embeds=[body])

    # -- alerts -----------------------------------------------------------

    async def check_alerts(self) -> None:
        for name, check in (
            ("warnings", self._check_warnings),
            ("floods", self._check_floods),
            ("quakes", self._check_quakes),
            ("fuel", self._check_fuel),
        ):
            try:
                await check()
            except (FeedError, ExtrasError) as exc:
                log.info("Skipping %s alerts this round: %s", name, exc)

    async def _check_warnings(self) -> None:
        users = await self.db.subscribers("warning")
        guilds = await self.db.guild_alert_targets("warnings")
        if not users and not guilds:
            return
        warnings = (await self.bot.weather.warnings()).warnings
        if not warnings:
            return

        for user in users:
            quiet = in_quiet_hours(user)
            towns = await self.db.list_favourites(int(user["user_id"]), "town")
            for warning in warnings:
                # Held back during quiet hours rather than dropped: nothing is
                # remembered as sent, so it arrives once they end.
                if quiet and warning.severity != "danger":
                    continue
                named = [t["label"] for t in towns if warning.mentions(t["label"])]
                if not named:
                    continue
                if await self.db.should_alert(user_target(int(user["user_id"])), warning_key(warning), ALERT_MEMORY):
                    await self.bot.send_dm(int(user["user_id"]), warning_screen(warning, named))

        for guild in guilds:
            state = guild["state"]
            for warning in warnings:
                if state and not warning.mentions(state):
                    continue
                if await self.db.should_alert(guild_target(guild["guild_id"]), warning_key(warning), ALERT_MEMORY):
                    await self.bot.send_channel(guild, warning_screen(warning, [state] if state else []))

    async def _check_floods(self) -> None:
        users = await self.db.subscribers("flood")
        national = await self.db.subscribers("national_flood")
        guilds = await self.db.guild_alert_targets("floods")
        if not users and not national and not guilds:
            return
        stations = (await self.bot.weather.flood()).stations
        by_id = {s.station_id: s for s in stations}

        for user in users:
            user_id = int(user["user_id"])
            threshold = FLOOD_RANK.get(str(user["flood_threshold"]).upper(), FLOOD_RANK["WARNING"])
            for fav in await self.db.list_favourites(user_id, "gauge"):
                station = by_id.get(fav["ref_id"])
                if station is None or not station.is_elevated or station.rank < threshold:
                    continue
                urgent = station.severity == "danger"
                if in_quiet_hours(user) and not urgent:
                    continue
                key = f"flood:{station.station_id}:{station.indicator}"
                if await self.db.should_alert(user_target(user_id), key, FLOOD_REPEAT):
                    await self.bot.send_dm(user_id, flood_screen([station], personal=True))

        danger = [s for s in stations if s.severity == "danger" and s.is_elevated]
        if not danger:
            return
        # National watchers and servers get one message listing whatever newly
        # reached danger, not one message per gauge.
        for user in national:
            target = user_target(int(user["user_id"]))
            fresh = [s for s in danger if await self.db.should_alert(target, f"natflood:{s.station_id}", FLOOD_REPEAT)]
            if fresh:
                await self.bot.send_dm(int(user["user_id"]), flood_screen(fresh))
        for guild in guilds:
            target = guild_target(guild["guild_id"])
            scoped = [s for s in danger if s.in_state(guild["state"])]
            fresh = [s for s in scoped if await self.db.should_alert(target, f"flood:{s.station_id}", SERVER_FLOOD_REPEAT)]
            if fresh:
                await self.bot.send_channel(guild, flood_screen(fresh))

    async def _check_quakes(self) -> None:
        users = await self.db.subscribers("quake")
        guilds = await self.db.guild_alert_targets("quakes")
        if not users and not guilds:
            return
        cutoff = time.time() - 86400
        recent = [q for q in (await self.bot.weather.quakes()).quakes if quake_epoch(q) >= cutoff]
        if not recent:
            return

        for user in users:
            threshold = float(user["quake_threshold"])
            for quake in recent:
                if quake.magnitude is None or quake.magnitude < threshold:
                    continue
                if in_quiet_hours(user) and quake.severity != "high":
                    continue
                if await self.db.should_alert(user_target(int(user["user_id"])), f"quake:{quake.quake_id}", ALERT_MEMORY):
                    await self.bot.send_dm(int(user["user_id"]), quake_screen(quake))
        for guild in guilds:
            for quake in recent:
                if quake.magnitude is None or quake.magnitude < float(guild["quake_min"]):
                    continue
                if await self.db.should_alert(guild_target(guild["guild_id"]), f"quake:{quake.quake_id}", ALERT_MEMORY):
                    await self.bot.send_channel(guild, quake_screen(quake))

    async def _check_fuel(self) -> None:
        week, _at, stale = await self.bot.extras.fuel()
        if week is None or stale:
            return
        announced = await self.db.meta_get("fuel_announced")
        if not announced:
            # First run: remember this week without announcing it, or every
            # server would hear about prices that are days old.
            await self.db.meta_set("fuel_announced", week.date)
            return
        if week.date <= announced:
            return
        await self.db.meta_set("fuel_announced", week.date)
        screen = fuel_alert_screen(week)
        for user in await self.db.subscribers("fuel"):
            await self.bot.send_dm(int(user["user_id"]), screen)
        for guild in await self.db.guild_alert_targets("fuel"):
            await self.bot.send_channel(guild, screen)

    # -- live vehicles ------------------------------------------------------

    async def _check_live(self) -> None:
        users = await self.db.subscribers("live")
        if not users:
            return
        watches: list[tuple[Any, Any]] = []
        for user in users:
            if in_quiet_hours(user):
                continue
            for fav in await self.db.list_favourites(int(user["user_id"])):
                op = OPERATORS.get(fav["operator"])
                if fav["kind"] in ("station", "stop") and op is not None and op.has_live:
                    watches.append((user, fav))
        if not watches:
            return

        # One poll per operator, reused for every watcher.
        vehicles = {
            op: await self.bot.transit.vehicles(op, max_age=45)
            for op in {fav["operator"] for _user, fav in watches}
        }
        for user, fav in watches:
            feed = self.bot.transit.peek(fav["operator"])
            stop = feed.stops.get(fav["ref_id"]) if feed else None
            if stop is None or stop.lat is None or stop.lon is None:
                continue
            serving = feed.stop_routes.get(stop.stop_id, set())
            radius = LIVE_RADIUS_M[feed.mode]
            for vehicle in vehicles.get(fav["operator"], []):
                if not vehicle.has_position or (vehicle.route_id and vehicle.route_id not in serving):
                    continue
                distance = haversine_m(stop.lat, stop.lon, vehicle.lat, vehicle.lon)
                if distance > radius:
                    continue
                if time.time() - vehicle.timestamp > 10 * 60:
                    continue  # an old position says nothing about now
                key = f"live:{fav['operator']}:{stop.stop_id}:{vehicle.key}"
                if not await self.db.should_alert(user_target(int(user["user_id"])), key, LIVE_REPEAT):
                    continue
                route = feed.routes.get(vehicle.route_id)
                await self.bot.send_dm(
                    int(user["user_id"]),
                    live_screen(fav["operator"], feed.mode, route.display if route else "", stop.name, distance, vehicle),
                )

    # -- health -----------------------------------------------------------

    async def _check_health(self) -> None:
        """Log a feed that has gone quiet. Nobody is messaged: an outage is
        not something a reader can act on, and every screen already marks a
        stale reading where it shows one."""

        now = int(time.time())
        stale = [
            f"{row['feed']} ({row['last_error'] or 'no recent success'})"
            for row in await self.db.feed_health()
            if row["last_ok_at"] and now - row["last_ok_at"] > FEED_STALE_AFTER
            and (row["last_fail_at"] or 0) > row["last_ok_at"]
        ]
        if stale:
            log.warning("Stale feeds: %s", ", ".join(stale))


# ---------------------------------------------------------------------------
# Alert screens
# ---------------------------------------------------------------------------


def warning_key(warning: Warning) -> str:
    # A reissued or extended warning carries a new end, so it is news again.
    return f"warn:{warning.warning_id}:{warning.valid_to}"


def quake_epoch(quake: Quake) -> float:
    moment = parse_iso(quake.when)
    return moment.timestamp() if moment else 0.0


def warning_screen(warning: Warning, areas: list[str]) -> Screen:
    body = embed(
        f"{SEVERITY_EMOJI[warning.severity]} {warning.title}",
        warning_text(warning),
        SEVERITY_COLOURS[warning.severity],
        footer="MET Malaysia via data.gov.my",
        url=config.WEATHER_PAGE,
    )
    if areas:
        add_field(body, "Names", ", ".join(areas))
    return Screen(
        embeds=[body],
        rows=[[Button("All warnings", "wx.warnings", {"p": 0}, emoji="⚠️"), Link("Open on the web", config.WEATHER_PAGE)]],
    )


def flood_screen(stations: list[FloodStation], personal: bool = False) -> Screen:
    stations = sorted(stations, key=lambda s: (-s.rank, s.name))
    worst = stations[0]
    if len(stations) == 1:
        title = f"{SEVERITY_EMOJI[worst.severity]} {worst.name} is at {worst.severity} level"
        text = gauge_line(worst)
    else:
        title = f"{SEVERITY_EMOJI[worst.severity]} {len(stations)} rivers at danger level"
        text = lines_block([gauge_line(s) for s in stations])
    body = embed(
        title,
        text,
        SEVERITY_COLOURS.get(worst.severity, TEAL),
        footer="JPS flood warning via data.gov.my · follow the local authorities",
        url=config.FLOOD_PAGE,
    )
    row: list[Any] = [Button("Gauge", "wx.gauge", {"id": worst.station_id}, emoji="🌊")]
    if not personal:
        row.append(Button("All rivers", "wx.flood", {"s": "", "p": 0}, emoji="🗺️"))
    row.append(Link("Open on the web", config.FLOOD_PAGE))
    return Screen(embeds=[body], rows=[row])


def quake_screen(quake: Quake) -> Screen:
    moment = parse_iso(quake.when)
    details = [f"**{quake.magnitude_text}** {quake.location}"]
    if moment:
        details.append(f"{ts(moment.timestamp(), 'f')}, {ts(moment.timestamp(), 'R')}")
    if quake.depth is not None:
        details.append(f"{quake.depth:g} km deep")
    if quake.distance:
        details.append(quake.distance)
    body = embed(
        f"🌏 {quake.magnitude_text} earthquake",
        "\n".join(details),
        SEVERITY_COLOURS[quake.severity],
        footer="MET Malaysia via data.gov.my",
        url=config.QUAKE_PAGE,
    )
    row: list[Any] = [Button("Recent quakes", "wx.quakes", {"p": 0}, emoji="🌏")]
    if quake.maps_url:
        row.append(Link("Map", quake.maps_url, emoji="📍"))
    return Screen(embeds=[body], rows=[row])


def fuel_alert_screen(week: Any) -> Screen:
    return Screen(
        embeds=[embed(f"⛽ New fuel prices from {week.date}", fuel_table(week), AMBER, footer="data.gov.my")],
        rows=[[Button("Refresh", "fu.show", {}, emoji="🔄")]],
    )


def departure_screen(p: dict[str, Any]) -> Screen:
    epoch = int(p.get("epoch", time.time()))
    icon = "🚆" if p.get("mode") == "rail" else "🚌"
    body = embed(
        f"{icon} {p.get('route', 'Your service')} to {p.get('headsign', '')}",
        f"Leaves **{p.get('stop', 'your stop')}** at {ts(epoch)}, {format_wait(epoch - time.time())} from now.",
        BLUE,
        footer=f"{operator_label(str(p.get('operator', '')))} timetable · scheduled, not a live prediction",
    )
    rows: list[Any] = []
    if p.get("operator") in OPERATORS and p.get("stop_id"):
        rows.append([Button("Next departures", "tr.next", {"o": p["operator"], "s": p["stop_id"]}, emoji="🕒")])
    return Screen(embeds=[body], rows=rows)


def prayer_reminder_screen(p: dict[str, Any]) -> Screen:
    label = PRAYER_LABELS.get(str(p.get("prayer")), "Prayer")
    epoch = int(p.get("epoch", time.time()))
    return Screen(
        embeds=[
            embed(
                f"🕌 It is time for {label}",
                f"{label} in {p.get('zone', '')} began at {ts(epoch)}.",
                TEAL,
                footer="JAKIM e-Solat via waktusolat.app",
            )
        ],
        rows=[[Button("Today's times", "pr.show", {"z": p.get("zone", "")}, emoji="🕌")]],
    )


def live_screen(operator: str, mode: str, route: str, stop: str, distance: float, vehicle: Any) -> Screen:
    kind = "train" if mode == "rail" else "bus"
    what = f"A {route} {kind}" if route else f"A {kind}"
    return Screen(
        embeds=[
            embed(
                f"{'🚆' if mode == 'rail' else '🚌'} {what} is near {stop}",
                f"{vehicle.name} is {distance / 1000:.1f} km from {stop}, reported {ts(vehicle.timestamp, 'R')}.",
                PURPLE,
                footer=f"{operator_label(operator)} live positions via data.gov.my",
            )
        ],
        rows=[
            [
                Link("Where it is", vehicle.maps_url, emoji="📍"),
                Button("Live list", "tr.live", {"o": operator, "p": 0}, emoji="📡"),
            ]
        ],
    )


__all__ = ["Scheduler", "RED"]
