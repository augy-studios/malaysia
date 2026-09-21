"""Background work: the morning digest and the three alert watchers.

There is no in-process timer wheel. Pending work lives in `scheduled_jobs`, and
a single loop wakes on a fixed tick, claims whatever is due and runs it. If the
bot is restarted or the VPS reboots, nothing is lost: the rows are still there
and the next tick picks them up.

The watchers are different in kind. They poll the feeds on an interval and
compare what they see against each user's thresholds, using `alert_state` to
make sure one storm produces one message rather than one per poll.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from typing import Any

from .feeds import FLOOD_RANK, FeedError, FloodStation, Quake, Warning
from .timeutils import in_quiet_hours, now_myt, parse_clock, parse_days
from .views import (
    digest_doc,
    flood_alert_doc,
    quake_alert_doc,
    warning_alert_doc,
)

log = logging.getLogger(__name__)

# A given warning, gauge or quake is mentioned to a user at most once per
# window. Weather warnings are reissued with the same wording, and a river sits
# at one level for hours, so without this the bot would repeat itself.
WARNING_COOLDOWN = 6 * 3600
FLOOD_COOLDOWN = 3 * 3600
QUAKE_COOLDOWN = 24 * 3600

# A feed silent for longer than this is treated as stale.
FEED_STALE_AFTER = 3 * 3600

# A digest whose moment passed while the bot was down is dropped rather than
# sent late, since a morning briefing at midnight helps nobody.
DIGEST_GRACE = 2 * 3600


class Scheduler:
    """Owns the background loops."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.db = bot.db
        self.feeds = bot.feeds
        self.settings = bot.settings
        self._tasks: list[asyncio.Task[Any]] = []
        self._stopping = asyncio.Event()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._job_loop(), name="job-loop"),
            asyncio.create_task(self._planner_loop(), name="planner-loop"),
            asyncio.create_task(self._alert_loop(), name="alert-loop"),
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
        for job in await self.db.due_jobs():
            try:
                payload = json.loads(job["payload"] or "{}")
            except json.JSONDecodeError:
                payload = {}

            if job["job_type"] != "digest":
                await self.db.delete_job(job["id"])
                continue

            try:
                await self._run_digest(job, payload)
            except Exception as exc:  # noqa: BLE001
                log.warning("Job %s failed: %s", job["id"], exc)
            finally:
                await self.db.delete_job(job["id"])

    async def _run_digest(self, job: Any, payload: dict[str, Any]) -> None:
        user = await self.db.get_user(job["user_id"])
        if user is None:
            return

        user_id = int(user["user_id"])

        # The subscription may have been switched off after the job was queued.
        if not await self.db.has_subscription(user_id, "digest"):
            return

        # A digest that is badly overdue is skipped, but tomorrow's is still
        # queued below so the chain does not break.
        if time.time() - job["run_at"] > DIGEST_GRACE:
            log.info("Skipping stale digest for %s", user_id)
            await self.queue_digest(user)
            return

        favourites = await self.db.list_favourites(user_id)
        forecasts: list[tuple[str, Any]] = []
        elevated: list[FloodStation] = []
        warnings: list[Warning] = []

        try:
            forecast_snapshot = await self.feeds.forecast()
            flood_snapshot = await self.feeds.flood()
            warning_snapshot = await self.feeds.warnings()
        except FeedError as exc:
            log.warning("Digest for %s skipped, feeds unavailable: %s", user_id, exc)
            await self.queue_digest(user)
            return

        for fav in favourites:
            if fav["kind"] == "location":
                location = forecast_snapshot.locations.get(
                    _normalise_key(str(fav["ref_id"]))
                )
                if location is not None and location.today is not None:
                    forecasts.append((location.name, location.today))
                    warnings.extend(
                        w for w in warning_snapshot.warnings if w.mentions(location.name)
                    )
            elif fav["kind"] == "station":
                station = next(
                    (
                        st
                        for st in flood_snapshot.stations
                        if st.station_id == str(fav["ref_id"])
                    ),
                    None,
                )
                if station is not None and station.is_elevated:
                    elevated.append(station)

        # Deduplicate warnings that named several of the user's areas.
        seen: set[str] = set()
        unique_warnings = []
        for warning in warnings:
            if warning.warning_id not in seen:
                seen.add(warning.warning_id)
                unique_warnings.append(warning)

        await self.bot.send(
            user["chat_id"], digest_doc(forecasts, unique_warnings, elevated)
        )

        # Queue tomorrow's so the chain continues.
        await self.queue_digest(user)

    # -- planning ---------------------------------------------------------

    async def _planner_loop(self) -> None:
        # Give the feeds a moment to warm before the first planning pass.
        if not await self._sleep(20):
            return
        while not self._stopping.is_set():
            try:
                await self._plan_digests()
                await self.db.purge_stale_jobs(int(time.time()) - 86400)
                await self.db.prune_alert_state(int(time.time()) - 7 * 86400)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Planner error: %s", exc)
            if not await self._sleep(30 * 60):
                return

    async def _plan_digests(self) -> None:
        for sub in await self.db.subscriptions_of_kind("digest"):
            user = await self.db.get_user(sub["user_id"])
            if user is not None:
                await self.queue_digest(user)

    async def queue_digest(self, user: Any) -> None:
        """Queue the next digest for one user, deduplicated by date."""

        target = parse_clock(str(user["digest_time"])) or parse_clock("07:00")
        assert target is not None

        now = now_myt()
        run_at = now.replace(
            hour=target.hour, minute=target.minute, second=0, microsecond=0
        )
        if run_at <= now:
            run_at += timedelta(days=1)

        await self.db.schedule_job(
            "digest",
            int(run_at.timestamp()),
            user_id=int(user["user_id"]),
            dedupe_key=f"digest:{user['user_id']}:{run_at.date().isoformat()}",
        )

    # -- alert watchers ---------------------------------------------------

    async def _alert_loop(self) -> None:
        if not await self._sleep(45):
            return
        while not self._stopping.is_set():
            try:
                await self.check_alerts()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Alert check error: %s", exc)
            if not await self._sleep(self.settings.alert_poll_seconds):
                return

    async def check_alerts(self) -> None:
        await self._check_warnings()
        await self._check_floods()
        await self._check_quakes()

    async def _deliver(
        self, user: Any, doc: Any, urgent: bool = False
    ) -> None:
        """Send an alert, honouring quiet hours unless it is urgent.

        Quiet hours exist so routine notices do not wake anyone. A gauge at
        danger level or a major quake overrides them, because that is exactly
        the situation someone would want to be woken for.
        """

        if not urgent and in_quiet_hours(user):
            return
        await self.bot.send(user["chat_id"], doc)

    async def _check_warnings(self) -> None:
        subs = await self.db.subscriptions_of_kind("warning")
        if not subs:
            return

        try:
            snapshot = await self.feeds.warnings()
        except FeedError:
            return
        if not snapshot.warnings:
            return

        weekday = now_myt().weekday()

        for sub in subs:
            if weekday not in parse_days(sub["days"]):
                continue
            user = await self.db.get_user(sub["user_id"])
            if user is None:
                continue

            # A warning-kind subscription is account wide, so it covers every
            # forecast area the user has saved.
            favourites = await self.db.list_favourites(int(user["user_id"]), "location")
            if not favourites:
                continue

            for fav in favourites:
                label = str(fav["label"])
                for warning in snapshot.warnings:
                    if not warning.mentions(label):
                        continue
                    key = f"warn:{warning.warning_id}:{fav['id']}"
                    if not await self.db.should_alert(
                        int(user["user_id"]), key, WARNING_COOLDOWN
                    ):
                        continue
                    await self._deliver(
                        user,
                        warning_alert_doc(warning, label),
                        urgent=warning.severity == "danger",
                    )

    async def _check_floods(self) -> None:
        favourite_subs = await self.db.subscriptions_of_kind("flood")
        national_subs = await self.db.subscriptions_of_kind("national_flood")
        if not favourite_subs and not national_subs:
            return

        try:
            snapshot = await self.feeds.flood()
        except FeedError:
            return

        by_id = {st.station_id: st for st in snapshot.stations}

        # Favourite-scoped watchers, each against their own threshold.
        for sub in favourite_subs:
            user = await self.db.get_user(sub["user_id"])
            if user is None:
                continue
            threshold = FLOOD_RANK.get(str(user["flood_threshold"]).upper(), 1)

            for fav in await self.db.list_favourites(int(user["user_id"]), "station"):
                station = by_id.get(str(fav["ref_id"]))
                if station is None or station.rank < threshold:
                    continue
                key = f"flood:{station.station_id}:{station.indicator}"
                if not await self.db.should_alert(
                    int(user["user_id"]), key, FLOOD_COOLDOWN
                ):
                    continue
                await self._deliver(
                    user,
                    flood_alert_doc(station),
                    urgent=station.severity == "danger",
                )

        # National watchers only ever hear about danger level, which is rare
        # enough not to be noise.
        danger = [st for st in snapshot.stations if st.severity == "danger"]
        if not danger:
            return
        for sub in national_subs:
            user = await self.db.get_user(sub["user_id"])
            if user is None:
                continue
            for station in danger:
                key = f"natflood:{station.station_id}"
                if not await self.db.should_alert(
                    int(user["user_id"]), key, FLOOD_COOLDOWN
                ):
                    continue
                await self._deliver(user, flood_alert_doc(station), urgent=True)

    async def _check_quakes(self) -> None:
        subs = await self.db.subscriptions_of_kind("quake")
        national = await self.db.subscriptions_of_kind("national_quake")
        if not subs and not national:
            return

        try:
            snapshot = await self.feeds.quakes()
        except FeedError:
            return
        if not snapshot.quakes:
            return

        # Only bulletins from the last day are worth pushing. Older ones are
        # still visible through /quake.
        cutoff = time.time() - 86400
        recent = [q for q in snapshot.quakes if _epoch_of(q) >= cutoff]
        if not recent:
            return

        for sub in subs:
            user = await self.db.get_user(sub["user_id"])
            if user is None:
                continue
            threshold = float(user["quake_threshold"])
            for quake in recent:
                if quake.magnitude is None or quake.magnitude < threshold:
                    continue
                key = f"quake:{quake.quake_id}"
                if not await self.db.should_alert(
                    int(user["user_id"]), key, QUAKE_COOLDOWN
                ):
                    continue
                await self._deliver(
                    user, quake_alert_doc(quake), urgent=quake.severity == "high"
                )

        strong = [q for q in recent if (q.magnitude or 0) >= 6.0]
        for sub in national:
            user = await self.db.get_user(sub["user_id"])
            if user is None:
                continue
            for quake in strong:
                key = f"natquake:{quake.quake_id}"
                if not await self.db.should_alert(
                    int(user["user_id"]), key, QUAKE_COOLDOWN
                ):
                    continue
                await self._deliver(user, quake_alert_doc(quake), urgent=True)

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
        """Record in the log when a feed has gone quiet.

        Nobody is messaged about this, admins included. A feed outage is not
        something the reader can act on, the bot already marks a stale reading
        where it is shown, and the notice went out worded in a way that left
        people thinking their own alerts had broken.
        """

        now = int(time.time())
        stale: list[tuple[str, str]] = []
        for row in await self.db.feed_health():
            last_ok = row["last_ok_at"] or 0
            if last_ok and now - last_ok > FEED_STALE_AFTER:
                stale.append((row["feed"], row["last_error"] or "no recent success"))

        if stale:
            log.warning(
                "Stale feeds: %s",
                ", ".join(f"{feed} ({reason})" for feed, reason in stale),
            )


def _epoch_of(quake: Quake) -> float:
    from .timeutils import parse_iso

    moment = parse_iso(quake.when)
    return moment.timestamp() if moment else 0.0


def _normalise_key(value: str) -> str:
    from .feeds import normalise

    return normalise(value)


__all__ = ["Scheduler"]
