"""Date parsing and Discord timestamp formatting."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

INPUT_FORMAT = "%Y-%m-%d %H:%M"
INPUT_HINT = "YYYY-MM-DD HH:MM"


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone `{name}`. Use an IANA name such as `Europe/Paris`.") from exc


def parse_local(text: str, tz_name: str) -> datetime:
    """Parses `YYYY-MM-DD HH:MM` in the server's timezone and returns an aware UTC datetime."""
    try:
        naive = datetime.strptime(text.strip(), INPUT_FORMAT)
    except ValueError as exc:
        raise ValueError(f"Dates must be written as `{INPUT_HINT}`, e.g. `2026-10-17 18:00`.") from exc
    return naive.replace(tzinfo=zone(tz_name)).astimezone(timezone.utc)


def format_local(value: datetime, tz_name: str) -> str:
    return value.astimezone(zone(tz_name)).strftime(INPUT_FORMAT)


def discord_ts(value: datetime | None, style: str = "F") -> str:
    """Renders a date as a Discord timestamp, shown in each reader's own timezone."""
    if value is None:
        return "*not set*"
    return f"<t:{int(value.timestamp())}:{style}>"
