"""Configuration, read once from the environment at startup.

Only four variables exist, and only the token is required. Everything else that
might be tuned lives here as a named constant, so the .env file stays short and
there is exactly one place to look when a number needs changing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# -- links shown to users -----------------------------------------------------

SITE_URL = "https://malaysia.uwuapps.org"
WEATHER_PAGE = f"{SITE_URL}/weather/"
QUAKE_PAGE = f"{SITE_URL}/quake/"
FLOOD_PAGE = f"{SITE_URL}/flood-alerts/"
TRAINS_PAGE = f"{SITE_URL}/trains/"
BUS_PAGE = f"{SITE_URL}/bus/"
SOURCE_URL = "https://github.com/augystudios/malaysia"

USER_AGENT = "malaysia-discord-bot/1.0 (+https://malaysia.uwuapps.org)"

# -- upstream -----------------------------------------------------------------

HTTP_TIMEOUT_SECONDS = 30

# How long a cached upstream response is served before asking again. MET
# refreshes forecasts twice a day, the alert feeds far more often.
FORECAST_CACHE_SECONDS = 30 * 60
WARNING_CACHE_SECONDS = 5 * 60
QUAKE_CACHE_SECONDS = 5 * 60
FLOOD_CACHE_SECONDS = 5 * 60
FUEL_CACHE_SECONDS = 60 * 60
FOREX_CACHE_SECONDS = 30 * 60
PRAYER_CACHE_SECONDS = 12 * 3600
# GTFS bundles are large and change rarely.
STATIC_REFRESH_HOURS = 24
# Live positions are cached briefly so several people asking at once cost one
# request.
REALTIME_CACHE_SECONDS = 20

# -- scheduling ---------------------------------------------------------------

SCHEDULER_TICK_SECONDS = 30
# Below 120 risks the data.gov.my rate limit.
ALERT_POLL_SECONDS = 300
LIVE_POLL_SECONDS = 60


def _clean(value: str | None) -> str:
    """Strip whitespace and surrounding quotes from a raw env value.

    Editing .env by hand tends to leave quotes behind, and a token wrapped in
    quotes fails to log in with an unhelpful message.
    """

    if value is None:
        return ""
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in "\"'":
        trimmed = trimmed[1:-1].strip()
    return trimmed


def _env(name: str, default: str = "") -> str:
    return _clean(os.getenv(name)) or default


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or unusable."""


@dataclass(frozen=True)
class Settings:
    discord_token: str
    donation_url: str = ""

    # The bot keeps everything in SQLite. These are accepted because the wider
    # project passes them around, and nothing here reads them.
    supabase_url: str = ""
    supabase_service_key: str = ""

    data_dir: Path = field(default_factory=lambda: Path("./data"))
    database_name: str = "malaysia.sqlite3"

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_name


def load_settings(env_file: str | os.PathLike[str] | None = ".env") -> Settings:
    """Read settings from the environment, loading a .env file when present."""

    if env_file:
        path = Path(env_file)
        if path.exists():
            load_dotenv(path, override=False)

    token = _env("DISCORD_TOKEN")
    if not token:
        raise ConfigError(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
        )

    return Settings(
        discord_token=token,
        donation_url=_env("DONATION_URL"),
        supabase_url=_env("SUPABASE_URL"),
        supabase_service_key=_env("SUPABASE_SERVICE_KEY"),
    )


__all__ = [
    "Settings",
    "ConfigError",
    "load_settings",
    "SITE_URL",
    "WEATHER_PAGE",
    "QUAKE_PAGE",
    "FLOOD_PAGE",
    "TRAINS_PAGE",
    "BUS_PAGE",
    "SOURCE_URL",
    "USER_AGENT",
]
