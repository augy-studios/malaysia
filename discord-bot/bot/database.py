"""SQLite persistence.

One file holds everything: people and their preferences, favourites, alert
subscriptions, server alert channels, the scheduler queue, the upstream cache
and the button registry.

The button registry is what keeps every button working forever. A Discord
button carries a custom id of at most 100 characters, and the bot forgets any
view it built in memory the moment it restarts. So each button's real payload
is stored as a row here and only a short token travels in the custom id. When a
button is pressed, including one sent months ago and several restarts back,
the token is looked up and the action runs. Nothing lives only in memory.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any, Sequence

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id          INTEGER PRIMARY KEY,
    quiet_enabled    INTEGER NOT NULL DEFAULT 1,
    quiet_from       TEXT    NOT NULL DEFAULT '23:00',
    quiet_to         TEXT    NOT NULL DEFAULT '06:00',
    digest_time      TEXT    NOT NULL DEFAULT '07:00',
    lead_minutes     INTEGER NOT NULL DEFAULT 10,
    flood_threshold  TEXT    NOT NULL DEFAULT 'WARNING',
    quake_threshold  REAL    NOT NULL DEFAULT 5.0,
    home_operator    TEXT    NOT NULL DEFAULT '',
    home_stop_id     TEXT    NOT NULL DEFAULT '',
    home_stop_name   TEXT    NOT NULL DEFAULT '',
    prayer_zone      TEXT    NOT NULL DEFAULT '',
    last_town        TEXT    NOT NULL DEFAULT '',
    dm_ok            INTEGER NOT NULL DEFAULT 1,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);

-- kind is one of town, gauge, station (rail) or stop (bus). ref_id is the
-- upstream id and operator the feed it came from, empty for weather.
CREATE TABLE IF NOT EXISTS favourites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    operator    TEXT    NOT NULL DEFAULT '',
    ref_id      TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    created_at  INTEGER NOT NULL,
    UNIQUE(user_id, kind, operator, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_fav_user ON favourites(user_id);

-- Personal alerts, delivered by DM. Every kind is account wide and follows the
-- person's favourites, so one row per kind is enough.
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id     INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_sub_kind ON subscriptions(kind);

-- Server alerts, posted to one channel per server.
CREATE TABLE IF NOT EXISTS guild_alerts (
    guild_id    INTEGER PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    warnings    INTEGER NOT NULL DEFAULT 1,
    floods      INTEGER NOT NULL DEFAULT 1,
    quakes      INTEGER NOT NULL DEFAULT 1,
    quake_min   REAL    NOT NULL DEFAULT 5.0,
    fuel        INTEGER NOT NULL DEFAULT 0,
    state       TEXT    NOT NULL DEFAULT '',
    updated_by  INTEGER,
    updated_at  INTEGER NOT NULL
);

-- Scheduling lives here rather than in an in-process timer, so a restart
-- never drops a pending reminder. target is 'u:<user id>' or 'g:<guild id>'.
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type    TEXT    NOT NULL,
    run_at      INTEGER NOT NULL,
    target      TEXT    NOT NULL DEFAULT '',
    payload     TEXT    NOT NULL DEFAULT '{}',
    dedupe_key  TEXT    UNIQUE,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_run_at ON scheduled_jobs(run_at);
CREATE INDEX IF NOT EXISTS idx_jobs_target ON scheduled_jobs(target, job_type);

-- Button payloads. Rows are never dropped, which is what keeps old buttons
-- alive. owner_id is who the menu belongs to, NULL when anyone may use it.
CREATE TABLE IF NOT EXISTS buttons (
    token        TEXT PRIMARY KEY,
    action       TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    owner_id     INTEGER,
    created_at   INTEGER NOT NULL,
    used_count   INTEGER NOT NULL DEFAULT 0,
    last_used_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_buttons_lookup
    ON buttons(action, payload, IFNULL(owner_id, -1));

CREATE TABLE IF NOT EXISTS feed_cache (
    key         TEXT PRIMARY KEY,
    body        BLOB NOT NULL,
    fetched_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feed_health (
    feed         TEXT PRIMARY KEY,
    last_ok_at   INTEGER,
    last_fail_at INTEGER,
    last_error   TEXT NOT NULL DEFAULT ''
);

-- Remembers what each person or server has already been told, so one storm
-- produces one message rather than one per poll.
CREATE TABLE IF NOT EXISTS alert_state (
    target       TEXT    NOT NULL,
    key          TEXT    NOT NULL,
    notified_at  INTEGER NOT NULL,
    PRIMARY KEY (target, key)
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

# Columns added after the first release. SQLite has no ADD COLUMN IF NOT
# EXISTS, so each is applied only when missing.
MIGRATIONS: tuple[tuple[str, str], ...] = ()

USER_PREFS = frozenset(
    {
        "quiet_enabled",
        "quiet_from",
        "quiet_to",
        "digest_time",
        "lead_minutes",
        "flood_threshold",
        "quake_threshold",
        "home_operator",
        "home_stop_id",
        "home_stop_name",
        "prayer_zone",
        "last_town",
        "dm_ok",
    }
)

GUILD_FLAGS = frozenset({"warnings", "floods", "quakes", "quake_min", "fuel", "state"})

# How many button tokens to memoise. Tokens never change once stored, so a
# menu drawn again answers from memory instead of costing a query per button.
_TOKEN_CACHE_MAX = 4096


def _now() -> int:
    return int(time.time())


def user_target(user_id: int) -> str:
    return f"u:{user_id}"


def guild_target(guild_id: int) -> str:
    return f"g:{guild_id}"


class Database:
    """Thin async wrapper over the SQLite file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._tokens: dict[tuple[str, str, int | None], str] = {}

    async def connect(self) -> None:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        # Drawing one menu writes a row per new button, and every write
        # commits. WAL with NORMAL sync keeps that cheap and still survives a
        # crash.
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA synchronous = NORMAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._migrate()

    async def _migrate(self) -> None:
        for table, definition in MIGRATIONS:
            column = definition.split()[0]
            existing = {
                row["name"] for row in await self._fetchall(f"PRAGMA table_info({table})")
            }
            if column not in existing:
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

    async def _execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = await self.db.execute(sql, params)
        await self.db.commit()
        return cur.rowcount

    async def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self.db.execute(sql, params) as cur:
            return list(await cur.fetchall())

    # -- users ------------------------------------------------------------

    async def ensure_user(self, user_id: int) -> aiosqlite.Row:
        now = _now()
        await self._execute(
            "INSERT OR IGNORE INTO users (user_id, created_at, updated_at) VALUES (?, ?, ?)",
            (user_id, now, now),
        )
        row = await self.get_user(user_id)
        assert row is not None
        return row

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))

    async def set_pref(self, user_id: int, field: str, value: Any) -> None:
        if field not in USER_PREFS:
            raise ValueError(f"Refusing to update unknown preference {field!r}.")
        await self.ensure_user(user_id)
        await self._execute(
            f"UPDATE users SET {field} = ?, updated_at = ? WHERE user_id = ?",
            (value, _now(), user_id),
        )

    async def delete_user_data(self, user_id: int) -> None:
        """Remove every trace of one person, for the settings delete button."""

        target = user_target(user_id)
        for sql, params in (
            ("DELETE FROM favourites WHERE user_id = ?", (user_id,)),
            ("DELETE FROM subscriptions WHERE user_id = ?", (user_id,)),
            ("DELETE FROM scheduled_jobs WHERE target = ?", (target,)),
            ("DELETE FROM alert_state WHERE target = ?", (target,)),
            ("DELETE FROM users WHERE user_id = ?", (user_id,)),
        ):
            await self.db.execute(sql, params)
        await self.db.commit()

    # -- favourites -------------------------------------------------------

    async def add_favourite(
        self, user_id: int, kind: str, ref_id: str, label: str, operator: str = ""
    ) -> bool:
        """True when a new favourite was stored, False when it already existed."""

        await self.ensure_user(user_id)
        count = await self._execute(
            """
            INSERT OR IGNORE INTO favourites (user_id, kind, operator, ref_id, label, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, kind, operator, ref_id, label, _now()),
        )
        return count > 0

    async def remove_favourite(self, user_id: int, favourite_id: int) -> bool:
        count = await self._execute(
            "DELETE FROM favourites WHERE id = ? AND user_id = ?", (favourite_id, user_id)
        )
        return count > 0

    async def remove_favourite_ref(
        self, user_id: int, kind: str, ref_id: str, operator: str = ""
    ) -> bool:
        count = await self._execute(
            "DELETE FROM favourites WHERE user_id = ? AND kind = ? AND operator = ? AND ref_id = ?",
            (user_id, kind, operator, ref_id),
        )
        return count > 0

    async def list_favourites(self, user_id: int, kind: str | None = None) -> list[aiosqlite.Row]:
        if kind is None:
            return await self._fetchall(
                "SELECT * FROM favourites WHERE user_id = ? ORDER BY kind, label", (user_id,)
            )
        return await self._fetchall(
            "SELECT * FROM favourites WHERE user_id = ? AND kind = ? ORDER BY label",
            (user_id, kind),
        )

    async def get_favourite(self, user_id: int, favourite_id: int) -> aiosqlite.Row | None:
        return await self._fetchone(
            "SELECT * FROM favourites WHERE id = ? AND user_id = ?", (favourite_id, user_id)
        )

    async def is_favourite(self, user_id: int, kind: str, ref_id: str, operator: str = "") -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM favourites WHERE user_id = ? AND kind = ? AND operator = ? AND ref_id = ?",
            (user_id, kind, operator, ref_id),
        )
        return row is not None

    async def favourites_of_kind(self, kind: str) -> list[aiosqlite.Row]:
        return await self._fetchall("SELECT * FROM favourites WHERE kind = ?", (kind,))

    # -- subscriptions ----------------------------------------------------

    async def set_subscription(self, user_id: int, kind: str, on: bool) -> None:
        await self.ensure_user(user_id)
        if on:
            await self._execute(
                "INSERT OR IGNORE INTO subscriptions (user_id, kind, created_at) VALUES (?, ?, ?)",
                (user_id, kind, _now()),
            )
        else:
            await self._execute(
                "DELETE FROM subscriptions WHERE user_id = ? AND kind = ?", (user_id, kind)
            )
            await self._execute(
                "DELETE FROM scheduled_jobs WHERE target = ? AND job_type = ?",
                (user_target(user_id), kind),
            )

    async def user_subscriptions(self, user_id: int) -> set[str]:
        rows = await self._fetchall(
            "SELECT kind FROM subscriptions WHERE user_id = ?", (user_id,)
        )
        return {row["kind"] for row in rows}

    async def clear_subscriptions(self, user_id: int) -> int:
        count = await self._execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        await self._execute(
            "DELETE FROM scheduled_jobs WHERE target = ?", (user_target(user_id),)
        )
        return count

    async def subscribers(self, kind: str) -> list[aiosqlite.Row]:
        """Everyone with this alert on whose DMs are still open."""

        return await self._fetchall(
            """
            SELECT u.* FROM subscriptions s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.kind = ? AND u.dm_ok = 1
            """,
            (kind,),
        )

    # -- server alert channels ---------------------------------------------

    async def get_guild_alerts(self, guild_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM guild_alerts WHERE guild_id = ?", (guild_id,))

    async def set_guild_channel(self, guild_id: int, channel_id: int, by: int) -> None:
        await self._execute(
            """
            INSERT INTO guild_alerts (guild_id, channel_id, updated_by, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (guild_id, channel_id, by, _now()),
        )

    async def set_guild_flag(self, guild_id: int, field: str, value: Any) -> None:
        if field not in GUILD_FLAGS:
            raise ValueError(f"Refusing to update unknown server setting {field!r}.")
        await self._execute(
            f"UPDATE guild_alerts SET {field} = ?, updated_at = ? WHERE guild_id = ?",
            (value, _now(), guild_id),
        )

    async def delete_guild_alerts(self, guild_id: int) -> None:
        await self._execute("DELETE FROM guild_alerts WHERE guild_id = ?", (guild_id,))
        await self._execute(
            "DELETE FROM alert_state WHERE target = ?", (guild_target(guild_id),)
        )

    async def guild_alert_targets(self, flag: str) -> list[aiosqlite.Row]:
        if flag not in GUILD_FLAGS:
            raise ValueError(f"Unknown server alert {flag!r}.")
        return await self._fetchall(f"SELECT * FROM guild_alerts WHERE {flag} = 1")

    # -- scheduler --------------------------------------------------------

    async def schedule_job(
        self,
        job_type: str,
        run_at: int,
        target: str = "",
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> bool:
        count = await self._execute(
            """
            INSERT OR IGNORE INTO scheduled_jobs
                (job_type, run_at, target, payload, dedupe_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_type, int(run_at), target, json.dumps(payload or {}), dedupe_key, _now()),
        )
        return count > 0

    async def due_jobs(self, now: int | None = None, limit: int = 200) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT * FROM scheduled_jobs WHERE run_at <= ? ORDER BY run_at LIMIT ?",
            (now if now is not None else _now(), limit),
        )

    async def delete_job(self, job_id: int) -> None:
        await self._execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,))

    async def delete_jobs(self, target: str, job_type: str) -> None:
        await self._execute(
            "DELETE FROM scheduled_jobs WHERE target = ? AND job_type = ?", (target, job_type)
        )

    async def purge_stale_jobs(self, older_than: int) -> int:
        return await self._execute(
            "DELETE FROM scheduled_jobs WHERE run_at < ?", (older_than,)
        )

    async def count_jobs(self, job_type: str | None = None) -> int:
        if job_type is None:
            row = await self._fetchone("SELECT COUNT(*) AS n FROM scheduled_jobs")
        else:
            row = await self._fetchone(
                "SELECT COUNT(*) AS n FROM scheduled_jobs WHERE job_type = ?", (job_type,)
            )
        return int(row["n"]) if row else 0

    # -- buttons ----------------------------------------------------------

    async def make_token(
        self, action: str, payload: dict[str, Any] | None = None, owner_id: int | None = None
    ) -> str:
        """Store a button payload and return the short token for its custom id.

        An identical (action, payload, owner) triple reuses its token, so
        drawing the same menu again does not grow the table.
        """

        body = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
        memo_key = (action, body, owner_id)
        cached = self._tokens.get(memo_key)
        if cached is not None:
            return cached

        sql = "SELECT token FROM buttons WHERE action = ? AND payload = ? AND owner_id IS ?"
        existing = await self._fetchone(sql, (action, body, owner_id))
        if existing is None:
            # Two concurrent draws of one menu can both miss the SELECT, so the
            # insert ignores a duplicate and the stored row is read back.
            await self._execute(
                "INSERT OR IGNORE INTO buttons (token, action, payload, owner_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (secrets.token_urlsafe(9), action, body, owner_id, _now()),
            )
            existing = await self._fetchone(sql, (action, body, owner_id))
        assert existing is not None
        token = existing["token"]

        if len(self._tokens) >= _TOKEN_CACHE_MAX:
            for stale in list(self._tokens)[: _TOKEN_CACHE_MAX // 4]:
                del self._tokens[stale]
        self._tokens[memo_key] = token
        return token

    async def resolve_token(self, token: str) -> tuple[str, dict[str, Any], int | None] | None:
        row = await self._fetchone("SELECT * FROM buttons WHERE token = ?", (token,))
        if row is None:
            return None
        await self._execute(
            "UPDATE buttons SET used_count = used_count + 1, last_used_at = ? WHERE token = ?",
            (_now(), token),
        )
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            payload = {}
        return row["action"], payload, row["owner_id"]

    # -- upstream cache ---------------------------------------------------

    async def cache_put(self, key: str, body: bytes) -> None:
        await self._execute(
            "INSERT INTO feed_cache (key, body, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET body = excluded.body, fetched_at = excluded.fetched_at",
            (key, body, _now()),
        )

    async def cache_get(self, key: str, max_age: int) -> tuple[bytes, int] | None:
        """Cached bytes and when they were fetched, if younger than max_age."""

        row = await self._fetchone("SELECT * FROM feed_cache WHERE key = ?", (key,))
        # `>=` so that max_age=0 always means expired.
        if row is None or _now() - row["fetched_at"] >= max_age:
            return None
        return row["body"], row["fetched_at"]

    async def cache_get_any_age(self, key: str) -> tuple[bytes, int] | None:
        """Cached bytes regardless of age, for when upstream fails."""

        row = await self._fetchone("SELECT * FROM feed_cache WHERE key = ?", (key,))
        if row is None:
            return None
        return row["body"], row["fetched_at"]

    # -- feed health ------------------------------------------------------

    async def record_feed_ok(self, feed: str) -> None:
        await self._execute(
            "INSERT INTO feed_health (feed, last_ok_at) VALUES (?, ?) "
            "ON CONFLICT(feed) DO UPDATE SET last_ok_at = excluded.last_ok_at",
            (feed, _now()),
        )

    async def record_feed_fail(self, feed: str, error: str) -> None:
        await self._execute(
            "INSERT INTO feed_health (feed, last_fail_at, last_error) VALUES (?, ?, ?) "
            "ON CONFLICT(feed) DO UPDATE SET last_fail_at = excluded.last_fail_at, "
            "last_error = excluded.last_error",
            (feed, _now(), error[:400]),
        )

    async def feed_health(self) -> list[aiosqlite.Row]:
        return await self._fetchall("SELECT * FROM feed_health ORDER BY feed")

    # -- alert dedupe -----------------------------------------------------

    async def should_alert(self, target: str, key: str, cooldown: int) -> bool:
        """True, and remembered, unless this target heard about `key` recently."""

        now = _now()
        row = await self._fetchone(
            "SELECT notified_at FROM alert_state WHERE target = ? AND key = ?", (target, key)
        )
        if row is not None and now - row["notified_at"] < cooldown:
            return False
        await self._execute(
            "INSERT INTO alert_state (target, key, notified_at) VALUES (?, ?, ?) "
            "ON CONFLICT(target, key) DO UPDATE SET notified_at = excluded.notified_at",
            (target, key, now),
        )
        return True

    async def prune_alert_state(self, older_than: int) -> None:
        await self._execute("DELETE FROM alert_state WHERE notified_at < ?", (older_than,))

    # -- meta -------------------------------------------------------------

    async def meta_get(self, key: str, default: str = "") -> str:
        row = await self._fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    async def meta_set(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- stats ------------------------------------------------------------

    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for label, table in (
            ("people", "users"),
            ("favourites", "favourites"),
            ("subscriptions", "subscriptions"),
            ("server channels", "guild_alerts"),
            ("queued jobs", "scheduled_jobs"),
            ("buttons", "buttons"),
        ):
            row = await self._fetchone(f"SELECT COUNT(*) AS n FROM {table}")
            out[label] = int(row["n"]) if row else 0
        return out
