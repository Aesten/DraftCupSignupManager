"""Dates and times: the signup close day, the closing moment, Discord timestamps."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Draft Cup signups usually close at 23:59 CET/CEST.
DEFAULT_TIMEZONE = "Europe/Paris"
DEFAULT_CLOSE_TIME = "23:59"

_TIME_RE = re.compile(r"^\s*(\d{1,2})(?:[:h.](\d{2}))?\s*$", re.IGNORECASE)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone `{name}`. Use a name such as `Europe/Paris` or `Europe/London`.") from exc


def parse_time(text: str) -> str:
    """Accepts `23:59`, `23h59`, `23.59` or `23`; returns `HH:MM`."""
    match = _TIME_RE.match(text)
    if match:
        hour, minute = int(match[1]), int(match[2] or 0)
        if hour < 24 and minute < 60:
            return f"{hour:02d}:{minute:02d}"
    raise ValueError("Write the time as `HH:MM`, e.g. `23:59`.")


def closing_moment(day: date, close_time: str, tz_name: str) -> datetime:
    """The UTC moment signups close: `close_time` on `day`, in the server's timezone."""
    hour, minute = (int(part) for part in close_time.split(":"))
    local = datetime.combine(day, time(hour, minute), tzinfo=zone(tz_name))
    return local.astimezone(timezone.utc)


def local_today(tz_name: str, now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(zone(tz_name)).date()


def format_day(day: date | None) -> str:
    return day.strftime("%A %d %B %Y") if day else "*not set*"


def input_day(day: date | None) -> str:
    """A day as typed in forms: DD/MM/YYYY."""
    return day.strftime("%d/%m/%Y") if day else ""


_DAY_RE = re.compile(r"^\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4})\s*$")


def parse_day(text: str) -> date:
    """Parses DD/MM/YYYY (also with `.` or `-`, and single-digit day or month)."""
    match = _DAY_RE.match(text)
    if match:
        try:
            return date(int(match[3]), int(match[2]), int(match[1]))
        except ValueError:
            pass
    raise ValueError(f"`{text.strip()}` isn't a date: write it as DD/MM/YYYY, e.g. `17/10/2026`.")


def discord_ts(value: datetime | None, style: str = "F") -> str:
    """Renders a moment as a Discord timestamp, shown in each reader's own timezone."""
    if value is None:
        return "*not set*"
    return f"<t:{int(value.timestamp())}:{style}>"
