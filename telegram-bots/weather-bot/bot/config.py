"""Configuration, read once from the environment at startup.

Everything the bot needs comes from environment variables, loaded from a .env
file when one is present. The required values fail loudly and immediately, so a
missing token is a clear error at boot rather than a confusing failure later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _clean(value: str | None) -> str:
    """Strip whitespace and surrounding quotes from a raw env value.

    Editing .env by hand tends to leave quotes behind, and a token wrapped in
    quotes fails authentication with an unhelpful message.
    """

    if value is None:
        return ""
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in "\"'":
        trimmed = trimmed[1:-1].strip()
    return trimmed


def _env(name: str, default: str = "") -> str:
    return _clean(os.getenv(name)) or default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_ids(name: str) -> tuple[int, ...]:
    raw = _env(name)
    if not raw:
        return ()
    ids: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            continue
    return tuple(ids)


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or unusable."""


@dataclass(frozen=True)
class Settings:
    # -- required ---------------------------------------------------------
    api_id: int
    api_hash: str
    bot_token: str

    # -- links shown to users ---------------------------------------------
    donation_url: str
    web_app_url: str

    # -- optional Supabase ------------------------------------------------
    # The bot stores everything in SQLite. These exist only because the wider
    # project passes them around, and nothing here requires them.
    supabase_url: str = ""
    supabase_service_key: str = ""

    # -- storage ----------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    database_name: str = "weather.sqlite3"
    session_name: str = "weather-bot"

    # -- upstream ---------------------------------------------------------
    http_timeout_seconds: int = 30
    # data.gov.my publishes on its own cadence. These are how long a cached
    # response is served before the bot asks upstream again.
    forecast_cache_seconds: int = 1800
    warning_cache_seconds: int = 300
    quake_cache_seconds: int = 300
    flood_cache_seconds: int = 300

    # -- scheduling -------------------------------------------------------
    scheduler_tick_seconds: int = 30
    alert_poll_seconds: int = 300

    # -- behaviour --------------------------------------------------------
    nearby_radius_metres: int = 60000
    admin_user_ids: tuple[int, ...] = ()

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_name

    @property
    def session_path(self) -> Path:
        return self.data_dir / self.session_name

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids


def load_settings(env_file: str | os.PathLike[str] | None = ".env") -> Settings:
    """Read settings from the environment, loading a .env file when present."""

    if env_file:
        path = Path(env_file)
        if path.exists():
            load_dotenv(path, override=False)

    missing: list[str] = []

    raw_api_id = _env("TELEGRAM_API_ID")
    if not raw_api_id:
        missing.append("TELEGRAM_API_ID")

    api_hash = _env("TELEGRAM_API_HASH")
    if not api_hash:
        missing.append("TELEGRAM_API_HASH")

    bot_token = _env("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")

    if missing:
        raise ConfigError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill it in."
        )

    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise ConfigError("TELEGRAM_API_ID must be a number.") from exc

    data_dir = Path(_env("DATA_DIR", "./data")).expanduser()

    return Settings(
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        donation_url=_env("DONATION_URL"),
        web_app_url=_env("WEB_APP_URL", "https://malaysia.uwuapps.org/weather/"),
        supabase_url=_env("SUPABASE_URL"),
        supabase_service_key=_env("SUPABASE_SERVICE_KEY"),
        data_dir=data_dir,
        database_name=_env("DATABASE_NAME", "weather.sqlite3"),
        session_name=_env("SESSION_NAME", "weather-bot"),
        http_timeout_seconds=_env_int("HTTP_TIMEOUT_SECONDS", 30),
        forecast_cache_seconds=_env_int("FORECAST_CACHE_SECONDS", 1800),
        warning_cache_seconds=_env_int("WARNING_CACHE_SECONDS", 300),
        quake_cache_seconds=_env_int("QUAKE_CACHE_SECONDS", 300),
        flood_cache_seconds=_env_int("FLOOD_CACHE_SECONDS", 300),
        scheduler_tick_seconds=_env_int("SCHEDULER_TICK_SECONDS", 30),
        alert_poll_seconds=_env_int("ALERT_POLL_SECONDS", 300),
        nearby_radius_metres=_env_int("NEARBY_RADIUS_METRES", 60000),
        admin_user_ids=_env_ids("ADMIN_USER_IDS"),
    )


__all__ = ["Settings", "ConfigError", "load_settings"]
