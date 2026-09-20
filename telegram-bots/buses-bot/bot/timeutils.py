"""Time handling for Malaysian schedules.

Everything user facing runs in Asia/Kuala_Lumpur. GTFS allows hours past 24 to
express trips that run after midnight but still belong to the previous service
day, so `26:15:00` is a real and valid value that has to be handled rather than
rejected.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

MYT = timezone(timedelta(hours=8), "MYT")

_TIME_RE = re.compile(r"^(\d{1,3}):(\d{2})(?::(\d{2}))?$")

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def now_myt() -> datetime:
    return datetime.now(MYT)


def today_myt() -> date:
    return now_myt().date()


def parse_gtfs_time(value: str) -> int | None:
    """Convert an HH:MM:SS GTFS time to seconds after midnight.

    Returns None when the value is missing or malformed. Values at or beyond
    24:00:00 are preserved rather than wrapped, because the caller needs to
    know the departure belongs to the following calendar day.
    """

    if not value:
        return None
    match = _TIME_RE.match(value.strip())
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = int(match.group(3) or 0)
    if minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


def parse_clock(value: str) -> dtime | None:
    """Parse a plain HH:MM setting value such as a quiet-hours boundary."""

    match = _TIME_RE.match((value or "").strip())
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return dtime(hour=hours, minute=minutes)


def format_time(value: str, fmt: str = "12h") -> str:
    """Render a GTFS time for display, marking next-day departures."""

    total = parse_gtfs_time(value)
    if total is None:
        return value or "Unknown"

    next_day = total >= 24 * 3600
    total %= 24 * 3600
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60

    if fmt == "24h":
        rendered = f"{hours:02d}:{minutes:02d}"
    else:
        period = "am" if hours < 12 else "pm"
        display_hour = hours % 12 or 12
        rendered = f"{display_hour}:{minutes:02d}{period}"

    return f"{rendered} (+1d)" if next_day else rendered


def seconds_since_midnight(moment: datetime | None = None) -> int:
    moment = moment or now_myt()
    return moment.hour * 3600 + moment.minute * 60 + moment.second


def next_occurrence_epoch(gtfs_time: str, reference: datetime | None = None) -> int | None:
    """Epoch seconds of the next time this GTFS departure comes round."""

    total = parse_gtfs_time(gtfs_time)
    if total is None:
        return None

    reference = reference or now_myt()
    midnight = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    candidate = midnight + timedelta(seconds=total)
    if candidate <= reference:
        candidate += timedelta(days=1)
    return int(candidate.timestamp())


def format_relative(seconds: float) -> str:
    """Human phrasing for a duration, used for live vehicle timestamps."""

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


def minutes_until(epoch: int, reference: datetime | None = None) -> int:
    reference = reference or now_myt()
    return int(round((epoch - reference.timestamp()) / 60))


def in_quiet_hours(user_row: Any, moment: datetime | None = None) -> bool:
    """True when the user's quiet hours are active.

    Accepts anything key-addressable (an sqlite3.Row or a plain dict). Quiet
    windows usually wrap midnight (23:00 to 06:00), so both the wrapping and
    non-wrapping cases are handled.
    """

    def field(name: str, default: Any = None) -> Any:
        try:
            value = user_row[name]
        except (KeyError, IndexError, TypeError):
            return default
        return default if value is None else value

    if not int(field("quiet_enabled", 0) or 0):
        return False

    start = parse_clock(str(field("quiet_from", "23:00"))) or dtime(23, 0)
    end = parse_clock(str(field("quiet_to", "06:00"))) or dtime(6, 0)
    current = (moment or now_myt()).time()

    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def parse_days(value: str) -> set[int]:
    """Parse a stored '0,1,2' day list into a set of weekday numbers."""

    out: set[int] = set()
    for chunk in (value or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            day = int(chunk)
        except ValueError:
            continue
        if 0 <= day <= 6:
            out.add(day)
    return out or set(range(7))


def describe_days(value: str) -> str:
    days = parse_days(value)
    if days == set(range(7)):
        return "every day"
    if days == {0, 1, 2, 3, 4}:
        return "weekdays"
    if days == {5, 6}:
        return "weekends"
    return ", ".join(DAY_SHORT[d] for d in sorted(days))


def within_window(window_from: str, window_to: str, moment: datetime | None = None) -> bool:
    start = parse_clock(window_from) or dtime(0, 0)
    end = parse_clock(window_to) or dtime(23, 59)
    current = (moment or now_myt()).time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end
