"""Environment-backed configuration.

Everything the bot needs to run comes from the environment, loaded from a .env
file sitting next to the project root when one exists. Nothing here reaches out
to the network, so importing this module stays cheap and test friendly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or unusable."""


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}.") from exc


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    donation_url: str
    web_app_url: str
    database_path: Path
    session_path: Path
    supabase_url: str = ""
    supabase_service_key: str = ""

    # Tuning knobs. The defaults are deliberately gentle on data.gov.my, which
    # rate limits aggressively when a client gets greedy.
    static_refresh_hours: int = 24
    realtime_poll_seconds: int = 60
    scheduler_tick_seconds: int = 30
    http_timeout_seconds: int = 30
    nearby_radius_metres: int = 2000

    admin_ids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def has_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)


def _admin_ids() -> tuple[int, ...]:
    raw = _optional("ADMIN_USER_IDS")
    if not raw:
        return ()
    out: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            continue
    return tuple(out)


def load_settings() -> Settings:
    """Read and validate configuration, raising ConfigError on anything missing."""

    api_id_raw = _require("TELEGRAM_API_ID")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise ConfigError("TELEGRAM_API_ID must be the numeric id from my.telegram.org.") from exc

    data_dir = Path(_optional("DATA_DIR", str(PROJECT_ROOT / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        api_id=api_id,
        api_hash=_require("TELEGRAM_API_HASH"),
        bot_token=_require("TELEGRAM_BOT_TOKEN"),
        donation_url=_require("DONATION_URL"),
        web_app_url=_optional("WEB_APP_URL", "https://malaysia.uwuapps.org/trains/"),
        database_path=data_dir / _optional("DATABASE_NAME", "trains.sqlite3"),
        session_path=data_dir / _optional("SESSION_NAME", "trains-bot"),
        supabase_url=_optional("SUPABASE_URL"),
        supabase_service_key=_optional("SUPABASE_SERVICE_KEY"),
        static_refresh_hours=_int_env("STATIC_REFRESH_HOURS", 24),
        realtime_poll_seconds=_int_env("REALTIME_POLL_SECONDS", 60),
        scheduler_tick_seconds=_int_env("SCHEDULER_TICK_SECONDS", 30),
        http_timeout_seconds=_int_env("HTTP_TIMEOUT_SECONDS", 30),
        nearby_radius_metres=_int_env("NEARBY_RADIUS_METRES", 2000),
        admin_ids=_admin_ids(),
    )
