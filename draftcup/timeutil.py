"""Dates and times: day-only dates, the closing moment, Discord timestamps."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
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
    """Day-only dates are shown as plain text: a Discord timestamp would shift them across timezones."""
    return day.strftime("%A %d %B %Y") if day else "*not set*"


def short_day(day: date) -> str:
    return day.strftime("%a %d %b")


def week_starts(today: date, count: int = 25) -> list[date]:
    """Mondays of the current week and the following ones (25 is Discord's select menu limit)."""
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(weeks=i) for i in range(count)]


def discord_ts(value: datetime | None, style: str = "F") -> str:
    """Renders a moment as a Discord timestamp, shown in each reader's own timezone."""
    if value is None:
        return "*not set*"
    return f"<t:{int(value.timestamp())}:{style}>"
