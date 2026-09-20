"""Time handling for Malaysian weather, earthquake and flood data.

Everything user facing runs in Asia/Kuala_Lumpur. The upstream feeds are not
consistent about timestamps: data.gov.my forecast dates are plain YYYY-MM-DD,
warning validity is ISO with an offset, MET earthquake records carry a naive
UTC field alongside a naive local one, and flood stations use a space-separated
local datetime. Each of those is parsed here so the rest of the bot only ever
deals with aware datetimes.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

MYT = timezone(timedelta(hours=8), "MYT")

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def now_myt() -> datetime:
    return datetime.now(MYT)


def today_myt() -> date:
    return now_myt().date()


# ---------------------------------------------------------------------------
# Parsing upstream timestamps
# ---------------------------------------------------------------------------


def parse_iso(value: Any, assume: timezone = MYT) -> datetime | None:
    """Parse an upstream timestamp into an aware datetime.

    Handles the several shapes data.gov.my returns: full ISO with an offset,
    ISO with a trailing Z, a space separator instead of a T, and a bare date.
    A value with no offset is assumed to be in `assume`, which is Malaysian
    time for every feed here except the earthquake `utcdatetime` field.
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
    # "2026-09-21 14:30:00" is common in the flood feed.
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # A bare date, or something unparseable.
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=assume)


def parse_date(value: Any) -> date | None:
    """Parse a plain YYYY-MM-DD forecast date."""

    moment = parse_iso(value)
    return moment.date() if moment else None


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


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_clock(moment: dtime | datetime, fmt: str = "12h") -> str:
    hours = moment.hour
    minutes = moment.minute
    if fmt == "24h":
        return f"{hours:02d}:{minutes:02d}"
    period = "am" if hours < 12 else "pm"
    return f"{hours % 12 or 12}:{minutes:02d}{period}"


def format_datetime(value: Any, fmt: str = "12h") -> str:
    """Render an upstream timestamp in Malaysian time."""

    moment = parse_iso(value)
    if moment is None:
        return "Unknown"
    local = moment.astimezone(MYT)
    return f"{local.day} {local.strftime('%b')} {local.year}, {format_clock(local, fmt)}"


def format_day(value: Any) -> str:
    """Render a forecast date as 'Today', 'Tomorrow' or a short weekday."""

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
    """Human phrasing for how long ago something happened."""

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


def ago(value: Any) -> str:
    """Relative phrasing for an upstream timestamp."""

    moment = parse_iso(value)
    if moment is None:
        return "unknown time"
    delta = now_myt().timestamp() - moment.timestamp()
    if delta < 0:
        return "just now"
    return format_relative(delta)


def minutes_until(epoch: int, reference: datetime | None = None) -> int:
    reference = reference or now_myt()
    return int(round((epoch - reference.timestamp()) / 60))


# ---------------------------------------------------------------------------
# User preference helpers
# ---------------------------------------------------------------------------


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


def is_active_window(valid_from: Any, valid_to: Any, moment: datetime | None = None) -> bool:
    """True when a warning's validity period covers `moment`.

    A missing bound is treated as open ended, which matches how MET issues
    warnings that have started but carry no stated end.
    """

    reference = moment or now_myt()
    start = parse_iso(valid_from)
    end = parse_iso(valid_to)
    if start and reference < start:
        return False
    if end and reference > end:
        return False
    return True


__all__ = [
    "MYT",
    "DAY_NAMES",
    "DAY_SHORT",
    "now_myt",
    "today_myt",
    "parse_iso",
    "parse_date",
    "parse_clock",
    "format_clock",
    "format_datetime",
    "format_day",
    "format_relative",
    "ago",
    "minutes_until",
    "in_quiet_hours",
    "parse_days",
    "describe_days",
    "within_window",
    "is_active_window",
]
