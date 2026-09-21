"""SQLite persistence.

Everything the bot remembers lives here: users and their preferences,
favourites, subscriptions, the scheduled job queue, inline button payloads and
the upstream response cache. There is no external database and no in-memory
state that matters, so the bot can be stopped and restarted at any moment
without losing a favourite, a pending notification or a live keyboard.

WAL is enabled so the scheduler writing a job never blocks a handler reading a
user row.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Sequence

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,
    chat_id         INTEGER NOT NULL,
    username        TEXT,
    first_name      TEXT,
    time_format     TEXT    NOT NULL DEFAULT '12h',
    quiet_from      TEXT    NOT NULL DEFAULT '23:00',
    quiet_to        TEXT    NOT NULL DEFAULT '06:00',
    quiet_enabled   INTEGER NOT NULL DEFAULT 1,
    digest_time     TEXT    NOT NULL DEFAULT '07:00',
    -- Minimum magnitude before an earthquake is worth a message.
    quake_threshold REAL    NOT NULL DEFAULT 5.0,
    -- Flood indicator at or above which an alert fires: ALERT, WARNING, DANGER.
    flood_threshold TEXT    NOT NULL DEFAULT 'ALERT',
    home_location   TEXT    NOT NULL DEFAULT '',
    -- The forecast area looked up most recently, so /weather can answer
    -- without asking again.
    last_location   TEXT    NOT NULL DEFAULT '',
    last_lat        REAL,
    last_lon        REAL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

-- A favourite is either a forecast town or a river gauge. `kind` says which,
-- and `ref_id` points into the matching upstream feed.
CREATE TABLE IF NOT EXISTS favourites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    ref_id      TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    detail      TEXT    NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    UNIQUE(user_id, kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_fav_user ON favourites(user_id);

CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    kind          TEXT    NOT NULL,
    favourite_id  INTEGER,
    days          TEXT    NOT NULL DEFAULT '0,1,2,3,4,5,6',
    window_from   TEXT    NOT NULL DEFAULT '00:00',
    window_to     TEXT    NOT NULL DEFAULT '23:59',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL,
    UNIQUE(user_id, kind, favourite_id)
);
CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_sub_kind ON subscriptions(kind, active);
-- The UNIQUE constraint above does not catch rows where favourite_id is NULL,
-- because SQL treats NULLs as distinct from one another. Account-wide
-- subscriptions (digest, national quake and flood watches) are exactly that
-- case, so without this index toggling one repeatedly would stack duplicate
-- rows and send duplicate messages.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_account_wide
    ON subscriptions(user_id, kind) WHERE favourite_id IS NULL;

-- Scheduling lives in SQLite rather than in an in-process timer wheel, so a
-- restart never drops a pending notification.
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type     TEXT    NOT NULL,
    run_at       INTEGER NOT NULL,
    user_id      INTEGER,
    payload      TEXT    NOT NULL DEFAULT '{}',
    dedupe_key   TEXT    UNIQUE,
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_run_at ON scheduled_jobs(run_at);

-- Inline button payloads. Rows are never dropped on restart, which is what
-- keeps old keyboards alive: a button pressed weeks later still resolves.
CREATE TABLE IF NOT EXISTS callbacks (
    token       TEXT PRIMARY KEY,
    action      TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    user_id     INTEGER,
    created_at  INTEGER NOT NULL,
    used_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_callbacks_action ON callbacks(action);
CREATE UNIQUE INDEX IF NOT EXISTS idx_callbacks_lookup
    ON callbacks(action, payload, IFNULL(user_id, -1));

CREATE TABLE IF NOT EXISTS feed_cache (
    key         TEXT PRIMARY KEY,
    body        BLOB NOT NULL,
    fetched_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feed_health (
    feed         TEXT PRIMARY KEY,
    last_ok_at   INTEGER,
    last_fail_at INTEGER,
    last_error   TEXT NOT NULL DEFAULT '',
    notified     INTEGER NOT NULL DEFAULT 0
);

-- Remembers which event a user was already told about, so one storm or one
-- rising river produces one message rather than one per poll.
CREATE TABLE IF NOT EXISTS alert_state (
    user_id     INTEGER NOT NULL,
    event_key   TEXT    NOT NULL,
    notified_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, event_key)
);
"""

# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so each is applied only when missing.
MIGRATIONS: tuple[tuple[str, str], ...] = (
    # /weather with no argument answers with whatever was looked up last, so
    # the forecast area is remembered alongside the coordinates.
    ("users", "last_location TEXT NOT NULL DEFAULT ''"),
)


def _now() -> int:
    return int(time.time())


class Database:
    """Async SQLite wrapper. One connection, serialised by aiosqlite."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.db: aiosqlite.Connection | None = None

    # -- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self._migrate()
        await self.db.commit()
        log.info("Database ready at %s", self.path)

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    async def _migrate(self) -> None:
        assert self.db is not None
        for table, definition in MIGRATIONS:
            column = definition.split()[0]
            cursor = await self.db.execute(f"PRAGMA table_info({table})")
            existing = {row["name"] for row in await cursor.fetchall()}
            await cursor.close()
            if column not in existing:
                log.info("Adding column %s.%s", table, column)
                await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    # -- low level --------------------------------------------------------

    async def _execute(self, sql: str, args: Sequence[Any] = ()) -> aiosqlite.Cursor:
        assert self.db is not None, "connect() must run before any query"
        cursor = await self.db.execute(sql, args)
        await self.db.commit()
        return cursor

    async def _fetchone(self, sql: str, args: Sequence[Any] = ()) -> aiosqlite.Row | None:
        assert self.db is not None
        cursor = await self.db.execute(sql, args)
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _fetchall(self, sql: str, args: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        assert self.db is not None
        cursor = await self.db.execute(sql, args)
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)

    # -- users ------------------------------------------------------------

    async def ensure_user(
        self,
        user_id: int,
        chat_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> aiosqlite.Row:
        """Insert the user if new, refresh their display details if not."""

        await self._execute(
            "INSERT INTO users (user_id, chat_id, username, first_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  chat_id = excluded.chat_id,"
            "  username = excluded.username,"
            "  first_name = excluded.first_name,"
            "  updated_at = excluded.updated_at",
            (user_id, chat_id, username, first_name, _now(), _now()),
        )
        row = await self.get_user(user_id)
        assert row is not None
        return row

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))

    async def set_user_field(self, user_id: int, field: str, value: Any) -> None:
        """Update one preference column.

        The column name is checked against a fixed allowlist because it is
        interpolated into the statement, and a callback payload must never be
        able to name an arbitrary column.
        """

        allowed = {
            "time_format",
            "quiet_from",
            "quiet_to",
            "quiet_enabled",
            "digest_time",
            "quake_threshold",
            "flood_threshold",
            "home_location",
            "last_location",
            "last_lat",
            "last_lon",
        }
        if field not in allowed:
            raise ValueError(f"Refusing to update unknown column {field!r}")
        await self._execute(
            f"UPDATE users SET {field} = ?, updated_at = ? WHERE user_id = ?",
            (value, _now(), user_id),
        )

    async def set_last_position(self, user_id: int, lat: float, lon: float) -> None:
        await self._execute(
            "UPDATE users SET last_lat = ?, last_lon = ?, updated_at = ? WHERE user_id = ?",
            (lat, lon, _now(), user_id),
        )

    async def count_users(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    async def all_users(self) -> list[aiosqlite.Row]:
        return await self._fetchall("SELECT * FROM users")

    # -- favourites -------------------------------------------------------

    async def add_favourite(
        self, user_id: int, kind: str, ref_id: str, label: str, detail: str = ""
    ) -> bool:
        """Add a favourite. Returns False when it was already saved."""

        cursor = await self._execute(
            "INSERT OR IGNORE INTO favourites (user_id, kind, ref_id, label, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, kind, ref_id, label, detail, _now()),
        )
        return cursor.rowcount > 0

    async def remove_favourite(self, user_id: int, favourite_id: int) -> bool:
        # Subscriptions hang off favourites, so they go first. There is no
        # foreign key cascade here because subscriptions may also be
        # account-wide, with a NULL favourite_id.
        await self._execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND favourite_id = ?",
            (user_id, favourite_id),
        )
        cursor = await self._execute(
            "DELETE FROM favourites WHERE user_id = ? AND id = ?", (user_id, favourite_id)
        )
        return cursor.rowcount > 0

    async def list_favourites(
        self, user_id: int, kind: str | None = None
    ) -> list[aiosqlite.Row]:
        if kind:
            return await self._fetchall(
                "SELECT * FROM favourites WHERE user_id = ? AND kind = ? ORDER BY label",
                (user_id, kind),
            )
        return await self._fetchall(
            "SELECT * FROM favourites WHERE user_id = ? ORDER BY kind, label", (user_id,)
        )

    async def get_favourite(self, user_id: int, favourite_id: int) -> aiosqlite.Row | None:
        return await self._fetchone(
            "SELECT * FROM favourites WHERE user_id = ? AND id = ?", (user_id, favourite_id)
        )

    async def find_favourite(
        self, user_id: int, kind: str, ref_id: str
    ) -> aiosqlite.Row | None:
        return await self._fetchone(
            "SELECT * FROM favourites WHERE user_id = ? AND kind = ? AND ref_id = ?",
            (user_id, kind, ref_id),
        )

    async def count_favourites(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS n FROM favourites")
        return int(row["n"]) if row else 0

    # -- subscriptions ----------------------------------------------------

    async def add_subscription(
        self, user_id: int, kind: str, favourite_id: int | None = None
    ) -> bool:
        cursor = await self._execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, kind, favourite_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, kind, favourite_id, _now()),
        )
        if cursor.rowcount > 0:
            return True
        # Re-enabling one that was switched off counts as a change.
        cursor = await self._execute(
            "UPDATE subscriptions SET active = 1 WHERE user_id = ? AND kind = ? "
            "AND favourite_id IS ? AND active = 0",
            (user_id, kind, favourite_id),
        )
        return cursor.rowcount > 0

    async def remove_subscription(
        self, user_id: int, kind: str, favourite_id: int | None = None
    ) -> bool:
        cursor = await self._execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND kind = ? AND favourite_id IS ?",
            (user_id, kind, favourite_id),
        )
        return cursor.rowcount > 0

    async def has_subscription(
        self, user_id: int, kind: str, favourite_id: int | None = None
    ) -> bool:
        row = await self._fetchone(
            "SELECT 1 AS present FROM subscriptions WHERE user_id = ? AND kind = ? "
            "AND favourite_id IS ? AND active = 1",
            (user_id, kind, favourite_id),
        )
        return row is not None

    async def list_subscriptions(self, user_id: int) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT s.*, f.label AS label, f.kind AS fav_kind, f.ref_id AS ref_id, "
            "       f.detail AS detail "
            "FROM subscriptions s "
            "LEFT JOIN favourites f ON f.id = s.favourite_id "
            "WHERE s.user_id = ? AND s.active = 1 "
            "ORDER BY s.kind",
            (user_id,),
        )

    async def subscriptions_of_kind(self, kind: str) -> list[aiosqlite.Row]:
        """Every active subscription of one kind, across all users."""

        return await self._fetchall(
            "SELECT s.*, f.label AS label, f.ref_id AS ref_id, f.kind AS fav_kind, "
            "       f.detail AS detail, u.chat_id AS chat_id "
            "FROM subscriptions s "
            "LEFT JOIN favourites f ON f.id = s.favourite_id "
            "JOIN users u ON u.user_id = s.user_id "
            "WHERE s.kind = ? AND s.active = 1",
            (kind,),
        )

    async def count_subscriptions(self) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM subscriptions WHERE active = 1"
        )
        return int(row["n"]) if row else 0

    # -- scheduled jobs ---------------------------------------------------

    async def schedule_job(
        self,
        job_type: str,
        run_at: int,
        user_id: int | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> None:
        """Queue a job. A repeated dedupe_key is silently ignored."""

        await self._execute(
            "INSERT OR IGNORE INTO scheduled_jobs "
            "(job_type, run_at, user_id, payload, dedupe_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                job_type,
                run_at,
                user_id,
                json.dumps(payload or {}, separators=(",", ":")),
                dedupe_key,
                _now(),
            ),
        )

    async def due_jobs(self, limit: int = 50) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT * FROM scheduled_jobs WHERE run_at <= ? ORDER BY run_at LIMIT ?",
            (_now(), limit),
        )

    async def delete_job(self, job_id: int) -> None:
        await self._execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,))

    async def purge_stale_jobs(self, before: int) -> int:
        """Drop jobs whose moment passed while the bot was down.

        Sending a digest for a morning that is already over would be noise, so
        anything well past its time is discarded rather than fired late.
        """

        cursor = await self._execute(
            "DELETE FROM scheduled_jobs WHERE run_at < ?", (before,)
        )
        return cursor.rowcount

    async def count_jobs(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS n FROM scheduled_jobs")
        return int(row["n"]) if row else 0

    # -- callbacks --------------------------------------------------------

    async def make_callback(
        self, action: str, payload: dict[str, Any] | None = None, user_id: int | None = None
    ) -> str:
        """Store a button payload and return the short token for callback_data.

        Identical (action, payload, user) triples reuse their existing token so
        repeatedly rendering the same menu does not grow the table without
        bound. Nothing is ever deleted, which is what lets a button in an old
        message keep working after a restart.
        """

        body = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
        existing = await self._fetchone(
            "SELECT token FROM callbacks WHERE action = ? AND payload = ? AND user_id IS ?",
            (action, body, user_id),
        )
        if existing:
            return existing["token"]

        # Two concurrent renders of the same menu would both miss the SELECT
        # above, so the insert ignores a duplicate and the existing row is read
        # back. A unique index over (action, payload, user_id) makes that safe.
        token = secrets.token_urlsafe(9)
        await self._execute(
            "INSERT OR IGNORE INTO callbacks (token, action, payload, user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, action, body, user_id, _now()),
        )
        row = await self._fetchone(
            "SELECT token FROM callbacks WHERE action = ? AND payload = ? AND user_id IS ?",
            (action, body, user_id),
        )
        return row["token"] if row else token

    async def resolve_callback(self, token: str) -> tuple[str, dict[str, Any], int | None] | None:
        row = await self._fetchone("SELECT * FROM callbacks WHERE token = ?", (token,))
        if row is None:
            return None
        await self._execute(
            "UPDATE callbacks SET used_count = used_count + 1 WHERE token = ?", (token,)
        )
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            payload = {}
        return row["action"], payload, row["user_id"]

    # -- feed cache -------------------------------------------------------

    async def cache_put(self, key: str, body: bytes) -> None:
        await self._execute(
            "INSERT INTO feed_cache (key, body, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET body = excluded.body, fetched_at = excluded.fetched_at",
            (key, body, _now()),
        )

    async def cache_get(self, key: str, max_age: int) -> bytes | None:
        row = await self._fetchone("SELECT * FROM feed_cache WHERE key = ?", (key,))
        if row is None:
            return None
        # `>=` so that max_age=0 always means "treat as expired and refetch".
        if _now() - row["fetched_at"] >= max_age:
            return None
        return row["body"]

    async def cache_get_any_age(self, key: str) -> tuple[bytes, int] | None:
        """Return cached bytes regardless of age, for when upstream fails."""

        row = await self._fetchone("SELECT * FROM feed_cache WHERE key = ?", (key,))
        if row is None:
            return None
        return row["body"], row["fetched_at"]

    async def cache_age(self, key: str) -> int:
        row = await self._fetchone(
            "SELECT fetched_at FROM feed_cache WHERE key = ?", (key,)
        )
        return int(row["fetched_at"]) if row else _now()

    # -- feed health ------------------------------------------------------

    async def record_feed_ok(self, feed: str) -> None:
        await self._execute(
            "INSERT INTO feed_health (feed, last_ok_at, notified) VALUES (?, ?, 0) "
            "ON CONFLICT(feed) DO UPDATE SET last_ok_at = excluded.last_ok_at, notified = 0",
            (feed, _now()),
        )

    async def record_feed_fail(self, feed: str, error: str) -> None:
        await self._execute(
            "INSERT INTO feed_health (feed, last_fail_at, last_error) VALUES (?, ?, ?) "
            "ON CONFLICT(feed) DO UPDATE SET "
            "  last_fail_at = excluded.last_fail_at, last_error = excluded.last_error",
            (feed, _now(), error[:500]),
        )

    async def feed_health(self) -> list[aiosqlite.Row]:
        return await self._fetchall("SELECT * FROM feed_health")

    async def mark_feed_notified(self, feeds: Sequence[str]) -> None:
        """Note that a feed's outage has been reported.

        `record_feed_ok` clears the flag again, so one outage produces one
        message rather than one per health check.
        """

        for feed in feeds:
            await self._execute(
                "UPDATE feed_health SET notified = 1 WHERE feed = ?", (feed,)
            )

    # -- alert dedupe -----------------------------------------------------

    async def should_alert(self, user_id: int, event_key: str, cooldown: int) -> bool:
        """True when this user has not been told about `event_key` recently.

        Claiming the slot and reporting it are one step, so two polls running
        close together cannot both decide to send.
        """

        row = await self._fetchone(
            "SELECT notified_at FROM alert_state WHERE user_id = ? AND event_key = ?",
            (user_id, event_key),
        )
        now = _now()
        if row is not None and now - row["notified_at"] < cooldown:
            return False
        await self._execute(
            "INSERT INTO alert_state (user_id, event_key, notified_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, event_key) DO UPDATE SET notified_at = excluded.notified_at",
            (user_id, event_key, now),
        )
        return True

    async def prune_alert_state(self, before: int) -> int:
        cursor = await self._execute(
            "DELETE FROM alert_state WHERE notified_at < ?", (before,)
        )
        return cursor.rowcount


__all__ = ["Database", "SCHEMA"]
