"""Notification scheduling, backed entirely by SQLite.

There is no in-process timer wheel. Pending work lives in `scheduled_jobs`, and
a single loop wakes on a fixed tick, claims whatever is due and runs it. If the
bot is restarted or the VPS reboots, nothing is lost: the rows are still there
and the next tick picks them up.

Jobs are deduplicated by a natural key (user, stop, route, departure) so a
planner that runs twice cannot produce two identical reminders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from .gtfs import GTFSManager, haversine_m, operator_label
from .timeutils import (
    MYT,
    in_quiet_hours,
    minutes_until,
    next_occurrence_epoch,
    now_myt,
    parse_clock,
    parse_days,
    parse_gtfs_time,
    seconds_since_midnight,
    within_window,
)
from .views import departure_reminder_doc, digest_doc, health_doc, live_alert_doc

log = logging.getLogger(__name__)

# How long before a live alert may repeat for the same bus and user.
LIVE_ALERT_COOLDOWN = 20 * 60
# A bus within this distance of a favourite stop triggers an alert.
LIVE_ALERT_RADIUS_M = 1500
# A feed silent for longer than this is treated as stale.
FEED_STALE_AFTER = 3 * 3600


class Scheduler:
    """Owns the background loops."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.db = bot.db
        self.gtfs: GTFSManager = bot.gtfs
        self.settings = bot.settings
        self._tasks: list[asyncio.Task[Any]] = []
        self._stopping = asyncio.Event()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._job_loop(), name="job-loop"),
            asyncio.create_task(self._planner_loop(), name="planner-loop"),
            asyncio.create_task(self._live_loop(), name="live-loop"),
            asyncio.create_task(self._health_loop(), name="health-loop"),
        ]
        log.info("Scheduler started with %d loops", len(self._tasks))

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.warning("Scheduler task ended with %s", exc)
        self._tasks.clear()

    async def _sleep(self, seconds: float) -> bool:
        """Sleep unless shutting down. Returns False when the bot is stopping."""

        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return True
        return False

    # -- job execution ----------------------------------------------------

    async def _job_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._drain_due_jobs()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Job loop error: %s", exc)
            if not await self._sleep(self.settings.scheduler_tick_seconds):
                return

    async def _drain_due_jobs(self) -> None:
        jobs = await self.db.due_jobs()
        for job in jobs:
            try:
                payload = json.loads(job["payload"] or "{}")
            except json.JSONDecodeError:
                payload = {}

            handler = {
                "departure": self._run_departure,
                "digest": self._run_digest,
            }.get(job["job_type"])

            if handler is None:
                await self.db.delete_job(job["id"])
                continue

            try:
                await handler(job, payload)
            except Exception as exc:  # noqa: BLE001
                log.warning("Job %s failed: %s", job["id"], exc)
            finally:
                await self.db.delete_job(job["id"])

    async def _run_departure(self, job: Any, payload: dict[str, Any]) -> None:
        user_id = job["user_id"]
        user = await self.db.get_user(user_id)
        if user is None:
            return
        if in_quiet_hours(user):
            log.debug("Skipping reminder for %s during quiet hours", user_id)
            return

        minutes = minutes_until(int(payload.get("departure_epoch", job["run_at"])))
        doc = departure_reminder_doc(
            stop_name=payload.get("stop_name", "your stop"),
            route_name=payload.get("route_name", "Your bus"),
            departure=payload.get("departure", ""),
            minutes=max(0, minutes),
            time_format=user["time_format"],
        )
        await self.bot.send(user["chat_id"], doc)

    async def _run_digest(self, job: Any, payload: dict[str, Any]) -> None:
        user_id = job["user_id"]
        user = await self.db.get_user(user_id)
        if user is None:
            return

        favourites = await self.db.list_favourites(user_id)
        entries: list[tuple[str, str, list[str]]] = []

        for fav in favourites:
            try:
                feed = await self.gtfs.get_feed(fav["operator"])
            except Exception:  # noqa: BLE001
                continue
            schedule = feed.stop_schedule.get(fav["stop_id"], {})
            route_ids = [fav["route_id"]] if fav["route_id"] else list(schedule.keys())
            for route_id in route_ids:
                times = schedule.get(route_id) or []
                if not times:
                    continue
                route = feed.routes.get(route_id)
                entries.append(
                    (fav["stop_name"], route.display if route else route_id, times)
                )

        if entries:
            await self.bot.send(user["chat_id"], digest_doc(entries, user["time_format"]))

        # Queue tomorrow's digest so the chain continues.
        await self._queue_digest(user)

    # -- planning ---------------------------------------------------------

    async def _planner_loop(self) -> None:
        # Give the feeds a moment to warm before the first planning pass.
        if not await self._sleep(20):
            return
        while not self._stopping.is_set():
            try:
                await self.plan_all()
                await self.db.purge_stale_jobs(int(time.time()) - 3600)
                await self.db.prune_live_alert_state(int(time.time()) - 86400)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Planner error: %s", exc)
            # Planning every 15 minutes keeps the queue about an hour ahead.
            if not await self._sleep(15 * 60):
                return

    async def plan_all(self) -> None:
        await self._plan_departures()
        await self._plan_digests()

    async def _plan_departures(self) -> None:
        """Queue reminders for departures coming up in the next hour."""

        subs = await self.db.subscriptions_of_kind("departure")
        if not subs:
            return

        now = now_myt()
        horizon = now + timedelta(hours=1)
        weekday = now.weekday()

        for sub in subs:
            if sub["favourite_id"] is None or not sub["stop_id"]:
                continue
            if weekday not in parse_days(sub["days"]):
                continue

            lead = int(sub["lead_minutes"] or 10)

            try:
                feed = await self.gtfs.get_feed(sub["operator"])
            except Exception:  # noqa: BLE001
                continue

            schedule = feed.stop_schedule.get(sub["stop_id"], {})
            route_ids = [sub["route_id"]] if sub["route_id"] else list(schedule.keys())

            for route_id in route_ids:
                route = feed.routes.get(route_id)
                route_name = route.display if route else route_id

                for departure in schedule.get(route_id, []):
                    departure_epoch = next_occurrence_epoch(departure, now)
                    if departure_epoch is None:
                        continue
                    fire_at = departure_epoch - lead * 60
                    if fire_at <= now.timestamp() or fire_at > horizon.timestamp():
                        continue
                    departure_local = datetime.fromtimestamp(departure_epoch, MYT)
                    if not within_window(sub["window_from"], sub["window_to"], departure_local):
                        continue

                    await self.db.schedule_job(
                        "departure",
                        int(fire_at),
                        user_id=sub["user_id"],
                        payload={
                            "stop_name": sub["stop_name"],
                            "route_name": route_name,
                            "departure": departure,
                            "departure_epoch": departure_epoch,
                        },
                        dedupe_key=(
                            f"dep:{sub['user_id']}:{sub['stop_id']}:{route_id}:{departure_epoch}"
                        ),
                    )

    async def _plan_digests(self) -> None:
        subs = await self.db.subscriptions_of_kind("digest")
        for sub in subs:
            user = await self.db.get_user(sub["user_id"])
            if user is not None:
                await self._queue_digest(user)

    async def _queue_digest(self, user: Any) -> None:
        target = parse_clock(user["digest_time"]) or parse_clock("07:00")
        assert target is not None

        now = now_myt()
        run_at = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)

        await self.db.schedule_job(
            "digest",
            int(run_at.timestamp()),
            user_id=user["user_id"],
            dedupe_key=f"digest:{user['user_id']}:{run_at.date().isoformat()}",
        )

    # -- live alerts ------------------------------------------------------

    async def _live_loop(self) -> None:
        if not await self._sleep(45):
            return
        while not self._stopping.is_set():
            try:
                await self._check_live_alerts()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Live alert error: %s", exc)
            if not await self._sleep(self.settings.realtime_poll_seconds):
                return

    async def _check_live_alerts(self) -> None:
        subs = [
            sub
            for sub in await self.db.subscriptions_of_kind("live")
            if sub["favourite_id"] is not None and sub["stop_id"]
        ]
        if not subs:
            return

        # Poll each operator once and reuse the result for every watcher.
        operators = {sub["operator"] for sub in subs}
        vehicles_by_operator = {}
        for operator in operators:
            vehicles_by_operator[operator] = await self.gtfs.get_vehicles(operator, max_age=45)

        weekday = now_myt().weekday()

        for sub in subs:
            if weekday not in parse_days(sub["days"]):
                continue
            if not within_window(sub["window_from"], sub["window_to"]):
                continue

            user = await self.db.get_user(sub["user_id"])
            if user is None or in_quiet_hours(user):
                continue

            try:
                feed = await self.gtfs.get_feed(sub["operator"])
            except Exception:  # noqa: BLE001
                continue

            stop = feed.stops.get(sub["stop_id"])
            if stop is None or stop.lat is None or stop.lon is None:
                continue

            for vehicle in vehicles_by_operator.get(sub["operator"], []):
                if not vehicle.has_position:
                    continue
                if sub["route_id"] and vehicle.route_id != sub["route_id"]:
                    continue

                distance = haversine_m(stop.lat, stop.lon, vehicle.lat, vehicle.lon)
                if distance > LIVE_ALERT_RADIUS_M:
                    continue

                vehicle_key = f"{sub['stop_id']}:{vehicle.vehicle_id or vehicle.entity_id}"
                if not await self.db.should_alert_vehicle(
                    sub["user_id"], vehicle_key, LIVE_ALERT_COOLDOWN
                ):
                    continue

                route = feed.routes.get(vehicle.route_id)
                await self.bot.send(
                    user["chat_id"],
                    live_alert_doc(
                        route.display if route else (vehicle.route_id or "A bus"),
                        stop.stop_name,
                        distance,
                        vehicle,
                    ),
                )

    # -- feed health ------------------------------------------------------

    async def _health_loop(self) -> None:
        if not await self._sleep(120):
            return
        while not self._stopping.is_set():
            try:
                await self._check_feed_health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Health check error: %s", exc)
            if not await self._sleep(30 * 60):
                return

    async def _check_feed_health(self) -> None:
        rows = await self.db.feed_health()
        now = int(time.time())

        stale: list[tuple[str, str]] = []
        for row in rows:
            last_ok = row["last_ok_at"] or 0
            if row["notified"]:
                continue
            if last_ok and now - last_ok > FEED_STALE_AFTER:
                reason = row["last_error"] or "no successful update recently"
                stale.append((row["operator"], reason))

        if not stale:
            return

        subs = await self.db.subscriptions_of_kind("health")
        if subs:
            doc = health_doc(stale)
            for sub in subs:
                user = await self.db.get_user(sub["user_id"])
                if user is None or in_quiet_hours(user):
                    continue
                await self.bot.send(user["chat_id"], doc)

        for operator, _reason in stale:
            await self.db.mark_feed_notified(operator)
