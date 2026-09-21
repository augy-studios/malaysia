"""SQLite persistence.

One file holds everything: users and their preferences, favourites,
subscriptions, the scheduler queue, the GTFS feed cache, and the callback
button registry.

The button registry is the reason inline keyboards keep working forever. A
Telegram callback payload is capped at 64 bytes, which is not enough to carry a
station id, a line id and an operator. Instead every button stores its real
payload as a row here and puts only a short token in the callback data. Because
the row outlives the process, a button tapped weeks after a restart still
resolves. Nothing is held in memory, so there is no state to lose.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any, Sequence

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    chat_id        INTEGER NOT NULL,
    username       TEXT,
    first_name     TEXT,
    language       TEXT    NOT NULL DEFAULT 'en',
    operator       TEXT    NOT NULL DEFAULT 'rapid-rail-kl',
    time_format    TEXT    NOT NULL DEFAULT '12h',
    quiet_from     TEXT    NOT NULL DEFAULT '23:00',
    quiet_to       TEXT    NOT NULL DEFAULT '06:00',
    quiet_enabled  INTEGER NOT NULL DEFAULT 1,
    lead_minutes   INTEGER NOT NULL DEFAULT 10,
    digest_time    TEXT    NOT NULL DEFAULT '07:00',
    home_operator  TEXT    NOT NULL DEFAULT '',
    home_stop_id   TEXT    NOT NULL DEFAULT '',
    home_stop_name TEXT    NOT NULL DEFAULT '',
    last_lat       REAL,
    last_lon       REAL,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS favourites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    operator    TEXT    NOT NULL,
    stop_id     TEXT    NOT NULL,
    stop_name   TEXT    NOT NULL,
    route_id    TEXT    NOT NULL DEFAULT '',
    route_name  TEXT    NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    UNIQUE(user_id, operator, stop_id, route_id)
);
CREATE INDEX IF NOT EXISTS idx_fav_user ON favourites(user_id);

CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    kind          TEXT    NOT NULL,
    favourite_id  INTEGER,
    days          TEXT    NOT NULL DEFAULT '0,1,2,3,4,5,6',
    window_from   TEXT    NOT NULL DEFAULT '05:00',
    window_to     TEXT    NOT NULL DEFAULT '23:59',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL,
    UNIQUE(user_id, kind, favourite_id)
);
CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_sub_kind ON subscriptions(kind, active);
-- The UNIQUE constraint above does not catch rows where favourite_id is NULL,
-- because SQL treats NULLs as distinct from one another. Account-wide
-- subscriptions (digest, health) are exactly that case, so without this index
-- toggling one repeatedly would stack duplicate rows and send duplicate
-- messages.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_account_wide
    ON subscriptions(user_id, kind) WHERE favourite_id IS NULL;

-- Scheduling lives in SQLite rather than in an in-process timer wheel so a
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
-- keeps old keyboards alive.
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
    operator     TEXT PRIMARY KEY,
    last_ok_at   INTEGER,
    last_fail_at INTEGER,
    last_error   TEXT NOT NULL DEFAULT '',
    notified     INTEGER NOT NULL DEFAULT 0
);

-- Remembers which train we already alerted on, so one creeping toward a
-- station produces one message instead of one per poll.
CREATE TABLE IF NOT EXISTS live_alert_state (
    user_id     INTEGER NOT NULL,
    vehicle_key TEXT    NOT NULL,
    notified_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, vehicle_key)
);
"""

# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so each is applied only when missing.
MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("users", "home_operator TEXT NOT NULL DEFAULT ''"),
    ("users", "home_stop_id TEXT NOT NULL DEFAULT ''"),
    ("users", "home_stop_name TEXT NOT NULL DEFAULT ''"),
)


# How many button tokens to memoise. A few thousand covers every menu a busy
# session draws while staying negligible in memory.
_TOKEN_CACHE_MAX = 4096


def _now() -> int:
    return int(time.time())


class Database:
    """Thin async wrapper over the SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        # (action, payload, user_id) -> token. Callback rows are never deleted,
        # so a token held here stays valid for the life of the process.
        self._callback_tokens: dict[tuple[str, str, int | None], str] = {}

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        # Rendering one card writes a callback row per button, and every write
        # commits. On the default rollback journal each of those is a full
        # fsync, which is what made replies lag behind the other bots. WAL with
        # NORMAL sync keeps the data safe across a crash and drops the fsync.
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA synchronous = NORMAL")
        # Duplicates left by an older build have to go before the schema runs,
        # because the schema creates the unique index that they violate.
        await self._dedupe_before_schema()
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._migrate()

    async def _dedupe_before_schema(self) -> None:
        """Collapse rows an older build allowed that the schema now forbids.

        Account-wide subscriptions were only constrained by
        UNIQUE(user_id, kind, favourite_id), which SQL does not enforce when
        favourite_id is NULL. A database written then can hold duplicates, and
        creating the partial index over them would fail and stop the bot from
        starting at all.
        """

        table = await self._fetchone(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'subscriptions'"
        )
        if table is None:
            return

        await self._execute(
            """
            DELETE FROM subscriptions
             WHERE favourite_id IS NULL
               AND id NOT IN (
                   SELECT MIN(id) FROM subscriptions
                    WHERE favourite_id IS NULL
                    GROUP BY user_id, kind
               )
            """
        )

    async def _migrate(self) -> None:
        """Apply additive column migrations to a database from an older build."""

        for table, definition in MIGRATIONS:
            column = definition.split()[0]
            existing = {
                row["name"] for row in await self._fetchall(f"PRAGMA table_info({table})")
            }
            if column in existing:
                continue
            await self._execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database.connect() must be awaited first.")
        return self._db

    async def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        await self.db.execute(sql, params)
        await self.db.commit()

    async def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self.db.execute(sql, params) as cur:
            return list(await cur.fetchall())

    # -- users ------------------------------------------------------------

    async def ensure_user(
        self,
        user_id: int,
        chat_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> aiosqlite.Row:
        now = _now()
        await self._execute(
            """
            INSERT INTO users (user_id, chat_id, username, first_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id    = excluded.chat_id,
                username   = excluded.username,
                first_name = excluded.first_name,
                updated_at = excluded.updated_at
            """,
            (user_id, chat_id, username, first_name, now, now),
        )
        row = await self._fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
        assert row is not None
        return row

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))

    async def set_pref(self, user_id: int, field: str, value: Any) -> None:
        allowed = {
            "operator",
            "time_format",
            "quiet_from",
            "quiet_to",
            "quiet_enabled",
            "lead_minutes",
            "digest_time",
            "language",
            "home_operator",
            "home_stop_id",
            "home_stop_name",
            "last_lat",
            "last_lon",
        }
        if field not in allowed:
            raise ValueError(f"Refusing to update unknown preference {field!r}.")
        await self._execute(
            f"UPDATE users SET {field} = ?, updated_at = ? WHERE user_id = ?",
            (value, _now(), user_id),
        )

    async def set_home_station(
        self, user_id: int, operator: str, stop_id: str, stop_name: str
    ) -> None:
        await self._execute(
            """
            UPDATE users
               SET home_operator = ?, home_stop_id = ?, home_stop_name = ?, updated_at = ?
             WHERE user_id = ?
            """,
            (operator, stop_id, stop_name, _now(), user_id),
        )

    async def set_last_location(self, user_id: int, lat: float, lon: float) -> None:
        await self._execute(
            "UPDATE users SET last_lat = ?, last_lon = ?, updated_at = ? WHERE user_id = ?",
            (lat, lon, _now(), user_id),
        )

    async def all_users(self) -> list[aiosqlite.Row]:
        return await self._fetchall("SELECT * FROM users")

    # -- favourites -------------------------------------------------------

    async def add_favourite(
        self,
        user_id: int,
        operator: str,
        stop_id: str,
        stop_name: str,
        route_id: str = "",
        route_name: str = "",
    ) -> bool:
        """Returns True when a new favourite was stored, False when duplicate."""

        cur = await self.db.execute(
            """
            INSERT OR IGNORE INTO favourites
                (user_id, operator, stop_id, stop_name, route_id, route_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, operator, stop_id, stop_name, route_id, route_name, _now()),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def remove_favourite(self, user_id: int, favourite_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM favourites WHERE id = ? AND user_id = ?",
            (favourite_id, user_id),
        )
        await self.db.commit()
        if cur.rowcount:
            await self._execute(
                "DELETE FROM subscriptions WHERE user_id = ? AND favourite_id = ?",
                (user_id, favourite_id),
            )
        return cur.rowcount > 0

    async def list_favourites(self, user_id: int) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT * FROM favourites WHERE user_id = ? ORDER BY stop_name, route_name",
            (user_id,),
        )

    async def get_favourite(self, user_id: int, favourite_id: int) -> aiosqlite.Row | None:
        return await self._fetchone(
            "SELECT * FROM favourites WHERE id = ? AND user_id = ?",
            (favourite_id, user_id),
        )

    async def find_favourite(
        self, user_id: int, operator: str, stop_id: str, route_id: str = ""
    ) -> aiosqlite.Row | None:
        return await self._fetchone(
            """
            SELECT * FROM favourites
             WHERE user_id = ? AND operator = ? AND stop_id = ? AND route_id = ?
            """,
            (user_id, operator, stop_id, route_id),
        )

    # -- subscriptions ----------------------------------------------------

    async def add_subscription(
        self,
        user_id: int,
        kind: str,
        favourite_id: int | None = None,
        days: str = "0,1,2,3,4,5,6",
        window_from: str = "05:00",
        window_to: str = "23:59",
    ) -> bool:
        # Account-wide rows (favourite_id IS NULL) are guarded by a partial
        # index, which ON CONFLICT cannot name, so that case updates in place
        # after an ignored insert instead.
        if favourite_id is None:
            cur = await self.db.execute(
                """
                INSERT OR IGNORE INTO subscriptions
                    (user_id, kind, favourite_id, days, window_from, window_to,
                     active, created_at)
                VALUES (?, ?, NULL, ?, ?, ?, 1, ?)
                """,
                (user_id, kind, days, window_from, window_to, _now()),
            )
            inserted = cur.rowcount > 0
            await self.db.execute(
                """
                UPDATE subscriptions
                   SET active = 1, days = ?, window_from = ?, window_to = ?
                 WHERE user_id = ? AND kind = ? AND favourite_id IS NULL
                """,
                (days, window_from, window_to, user_id, kind),
            )
            await self.db.commit()
            return inserted

        cur = await self.db.execute(
            """
            INSERT INTO subscriptions
                (user_id, kind, favourite_id, days, window_from, window_to, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(user_id, kind, favourite_id) DO UPDATE SET
                active      = 1,
                days        = excluded.days,
                window_from = excluded.window_from,
                window_to   = excluded.window_to
            """,
            (user_id, kind, favourite_id, days, window_from, window_to, _now()),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def remove_subscription(
        self, user_id: int, kind: str, favourite_id: int | None = None
    ) -> bool:
        if favourite_id is None:
            cur = await self.db.execute(
                "DELETE FROM subscriptions WHERE user_id = ? AND kind = ? AND favourite_id IS NULL",
                (user_id, kind),
            )
        else:
            cur = await self.db.execute(
                "DELETE FROM subscriptions WHERE user_id = ? AND kind = ? AND favourite_id = ?",
                (user_id, kind, favourite_id),
            )
        await self.db.commit()
        return cur.rowcount > 0

    async def remove_subscriptions_of_kind(self, user_id: int, kind: str) -> int:
        cur = await self.db.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND kind = ?", (user_id, kind)
        )
        await self.db.commit()
        return cur.rowcount

    async def remove_all_subscriptions(self, user_id: int) -> int:
        cur = await self.db.execute(
            "DELETE FROM subscriptions WHERE user_id = ?", (user_id,)
        )
        await self.db.commit()
        return cur.rowcount

    async def list_subscriptions(self, user_id: int) -> list[aiosqlite.Row]:
        return await self._fetchall(
            """
            SELECT s.*, f.stop_name, f.route_name, f.stop_id, f.route_id, f.operator
            FROM subscriptions s
            LEFT JOIN favourites f ON f.id = s.favourite_id
            WHERE s.user_id = ?
            ORDER BY s.kind
            """,
            (user_id,),
        )

    async def subscriptions_of_kind(self, kind: str) -> list[aiosqlite.Row]:
        return await self._fetchall(
            """
            SELECT s.*, f.stop_name, f.route_name, f.stop_id, f.route_id, f.operator,
                   u.chat_id, u.time_format, u.lead_minutes, u.quiet_enabled,
                   u.quiet_from, u.quiet_to, u.digest_time
            FROM subscriptions s
            LEFT JOIN favourites f ON f.id = s.favourite_id
            JOIN users u ON u.user_id = s.user_id
            WHERE s.kind = ? AND s.active = 1
            """,
            (kind,),
        )

    async def has_subscription(self, user_id: int, kind: str) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM subscriptions WHERE user_id = ? AND kind = ? AND active = 1 LIMIT 1",
            (user_id, kind),
        )
        return row is not None

    # -- scheduler --------------------------------------------------------

    async def schedule_job(
        self,
        job_type: str,
        run_at: int,
        user_id: int | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> bool:
        cur = await self.db.execute(
            """
            INSERT OR IGNORE INTO scheduled_jobs
                (job_type, run_at, user_id, payload, dedupe_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_type, run_at, user_id, json.dumps(payload or {}), dedupe_key, _now()),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def due_jobs(self, now: int | None = None, limit: int = 100) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT * FROM scheduled_jobs WHERE run_at <= ? ORDER BY run_at LIMIT ?",
            (now if now is not None else _now(), limit),
        )

    async def delete_job(self, job_id: int) -> None:
        await self._execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,))

    async def defer_job(self, job_id: int, run_at: int) -> None:
        await self._execute(
            "UPDATE scheduled_jobs SET run_at = ?, attempts = attempts + 1 WHERE id = ?",
            (run_at, job_id),
        )

    async def purge_stale_jobs(self, older_than: int) -> int:
        cur = await self.db.execute(
            "DELETE FROM scheduled_jobs WHERE run_at < ?", (older_than,)
        )
        await self.db.commit()
        return cur.rowcount

    # -- callbacks --------------------------------------------------------

    async def make_callback(
        self, action: str, payload: dict[str, Any] | None = None, user_id: int | None = None
    ) -> str:
        """Store a button payload and return the short token for callback_data.

        Identical (action, payload, user) triples reuse their existing token so
        repeatedly rendering the same menu does not grow the table without
        bound.
        """

        body = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))

        # Tokens are stable once stored, so a render of a menu the bot has
        # already drawn can answer from memory. Without this every button on
        # every card costs a query, and a station list has dozens.
        memo_key = (action, body, user_id)
        cached = self._callback_tokens.get(memo_key)
        if cached is not None:
            return cached

        existing = await self._fetchone(
            "SELECT token FROM callbacks WHERE action = ? AND payload = ? AND user_id IS ?",
            (action, body, user_id),
        )
        if existing:
            self._remember_token(memo_key, existing["token"])
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
        stored = row["token"] if row else token
        self._remember_token(memo_key, stored)
        return stored

    def _remember_token(self, key: tuple[str, str, int | None], token: str) -> None:
        """Memoise a token, discarding the oldest once the cache is full."""

        if len(self._callback_tokens) >= _TOKEN_CACHE_MAX:
            for stale_key in list(self._callback_tokens)[: _TOKEN_CACHE_MAX // 4]:
                del self._callback_tokens[stale_key]
        self._callback_tokens[key] = token

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
        """Return cached bytes regardless of age, for use when upstream fails."""

        row = await self._fetchone("SELECT * FROM feed_cache WHERE key = ?", (key,))
        if row is None:
            return None
        return row["body"], row["fetched_at"]

    # -- feed health ------------------------------------------------------

    async def record_feed_ok(self, operator: str) -> None:
        await self._execute(
            "INSERT INTO feed_health (operator, last_ok_at, notified) VALUES (?, ?, 0) "
            "ON CONFLICT(operator) DO UPDATE SET last_ok_at = excluded.last_ok_at, notified = 0",
            (operator, _now()),
        )

    async def record_feed_fail(self, operator: str, error: str) -> None:
        await self._execute(
            "INSERT INTO feed_health (operator, last_fail_at, last_error) VALUES (?, ?, ?) "
            "ON CONFLICT(operator) DO UPDATE SET last_fail_at = excluded.last_fail_at, "
            "last_error = excluded.last_error",
            (operator, _now(), error[:400]),
        )

    async def feed_health(self) -> list[aiosqlite.Row]:
        return await self._fetchall("SELECT * FROM feed_health")

    async def mark_feed_notified(self, operator: str) -> None:
        await self._execute(
            "UPDATE feed_health SET notified = 1 WHERE operator = ?", (operator,)
        )

    # -- live alert dedupe ------------------------------------------------

    async def should_alert_vehicle(self, user_id: int, vehicle_key: str, cooldown: int) -> bool:
        row = await self._fetchone(
            "SELECT notified_at FROM live_alert_state WHERE user_id = ? AND vehicle_key = ?",
            (user_id, vehicle_key),
        )
        now = _now()
        if row and now - row["notified_at"] < cooldown:
            return False
        await self._execute(
            "INSERT INTO live_alert_state (user_id, vehicle_key, notified_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, vehicle_key) DO UPDATE SET notified_at = excluded.notified_at",
            (user_id, vehicle_key, now),
        )
        return True

    async def prune_live_alert_state(self, older_than: int) -> None:
        await self._execute(
            "DELETE FROM live_alert_state WHERE notified_at < ?", (older_than,)
        )

    # -- stats ------------------------------------------------------------

    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for label, table in (
            ("users", "users"),
            ("favourites", "favourites"),
            ("subscriptions", "subscriptions"),
            ("jobs", "scheduled_jobs"),
            ("callbacks", "callbacks"),
        ):
            row = await self._fetchone(f"SELECT COUNT(*) AS n FROM {table}")
            out[label] = int(row["n"]) if row else 0
        return out
