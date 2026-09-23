"""Time handling.

Everything is reasoned about in Malaysian time (UTC+8, no daylight saving).
What the reader sees is different: wherever an exact moment is known, the bot
sends a Discord timestamp such as <t:1790000000:t>, which every client renders
in the reader's own clock format and time zone. That is why there is no 12 or
24 hour setting here, unlike the Telegram bots.

Two awkward inputs are handled in this module so nothing else has to:

  * The data.gov.my weather feeds disagree about timestamps: plain dates, ISO
    with an offset, naive UTC, and space separated local times all appear.
  * GTFS allows hours past 24, so 26:15:00 is a real departure that belongs to
    the previous service day. KTMB intercity and ETS trips use it regularly.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

MYT = timezone(timedelta(hours=8), "MYT")

DAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# GTFS calendar.txt column order matches Python's Monday-based weekday().
CALENDAR_DAY_COLUMNS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

# Hours may run to three digits so GTFS "25:10:00" style values parse.
_TIME_RE = re.compile(r"^(\d{1,3}):(\d{2})(?::(\d{2}))?$")


def now_myt() -> datetime:
    return datetime.now(MYT)


def today_myt() -> date:
    return now_myt().date()


def midnight_myt(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=MYT)


# ---------------------------------------------------------------------------
# Discord timestamps
# ---------------------------------------------------------------------------


def ts(epoch: float | int, style: str = "t") -> str:
    """A Discord timestamp tag, rendered in the reader's own time zone.

    Styles used here: t (4:30 pm), f (date and time), D (date), R (in 5
    minutes).
    """

    return f"<t:{int(epoch)}:{style}>"


def ts_of(value: Any, style: str = "f") -> str:
    """A Discord timestamp for an upstream timestamp, or a plain fallback."""

    moment = parse_iso(value)
    if moment is None:
        return "unknown time"
    return ts(moment.timestamp(), style)


# ---------------------------------------------------------------------------
# Upstream timestamps
# ---------------------------------------------------------------------------


def parse_iso(value: Any, assume: timezone = MYT) -> datetime | None:
    """Parse an upstream timestamp into an aware datetime.

    Handles full ISO with an offset, a trailing Z, a space instead of a T, and
    a bare date. A value with no offset is taken to be in `assume`, which is
    Malaysian time for every feed except the earthquake `utcdatetime` field.
    """

    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=assume)

    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=assume)


def parse_date(value: Any) -> date | None:
    moment = parse_iso(value)
    return moment.date() if moment else None


def parse_clock(value: str) -> dtime | None:
    """Parse a plain HH:MM setting such as a quiet hours boundary."""

    match = _TIME_RE.match((value or "").strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return dtime(hour=hours, minute=minutes)


def is_active_window(valid_from: Any, valid_to: Any, moment: datetime | None = None) -> bool:
    """True when a warning's validity period covers `moment`.

    A missing bound is open ended, which matches how MET issues warnings that
    have started but carry no stated end.
    """

    reference = moment or now_myt()
    start = parse_iso(valid_from)
    end = parse_iso(valid_to)
    if start and reference < start:
        return False
    if end and reference > end:
        return False
    return True


def format_day(value: Any) -> str:
    """A forecast date as Today, Tomorrow or a short weekday."""

    day = parse_date(value)
    if day is None:
        return "Unknown"
    delta = (day - today_myt()).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    return f"{DAY_SHORT[day.weekday()]} {day.day} {day.strftime('%b')}"


def format_relative(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = round(minutes / 60)
    if hours < 24:
        return f"{hours}h ago"
    return f"{round(hours / 24)}d ago"


# ---------------------------------------------------------------------------
# GTFS times
# ---------------------------------------------------------------------------


def parse_gtfs_time(value: str) -> int | None:
    """An HH:MM:SS GTFS time as seconds after the service day's midnight.

    Values at or past 24:00:00 are kept rather than wrapped, because they
    belong to the following calendar day.
    """

    if not value:
        return None
    match = _TIME_RE.match(value.strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    seconds = int(match.group(3) or 0)
    if minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


def seconds_to_gtfs(total: int) -> str:
    hours, rest = divmod(int(total), 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_gtfs_date(value: str) -> date | None:
    """A GTFS YYYYMMDD date from calendar.txt or calendar_dates.txt."""

    value = (value or "").strip()
    if len(value) != 8 or not value.isdigit():
        return None
    try:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None


def service_epoch(day: date, seconds: int) -> int:
    """Epoch of a GTFS time on a given service day."""

    return int((midnight_myt(day) + timedelta(seconds=seconds)).timestamp())


def format_wait(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes <= 0:
        return "now"
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest}m" if rest else f"{hours}h"


# ---------------------------------------------------------------------------
# User preferences
# ---------------------------------------------------------------------------


def in_quiet_hours(user_row: Any, moment: datetime | None = None) -> bool:
    """True when the user's quiet hours are in force.

    Accepts anything key addressable. Quiet windows usually wrap midnight, so
    both the wrapping and non-wrapping cases are handled.
    """

    def get(name: str, default: Any) -> Any:
        try:
            value = user_row[name]
        except (KeyError, IndexError, TypeError):
            return default
        return default if value is None else value

    if not int(get("quiet_enabled", 0) or 0):
        return False
    start = parse_clock(str(get("quiet_from", "23:00"))) or dtime(23, 0)
    end = parse_clock(str(get("quiet_to", "06:00"))) or dtime(6, 0)
    current = (moment or now_myt()).time()
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def next_clock_epoch(clock: str, reference: datetime | None = None) -> int:
    """Epoch of the next time the wall clock in Malaysia reads `clock`."""

    target = parse_clock(clock) or dtime(7, 0)
    now = reference or now_myt()
    run_at = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if run_at <= now:
        run_at += timedelta(days=1)
    return int(run_at.timestamp())
