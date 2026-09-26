from datetime import date, datetime, timezone

import pytest

from draftcup import timeutil


@pytest.mark.parametrize("raw, expected", [("23:59", "23:59"), ("9", "09:00"), ("20h30", "20:30"), (" 7.05 ", "07:05")])
def test_parse_time(raw, expected):
    assert timeutil.parse_time(raw) == expected


@pytest.mark.parametrize("raw", ["", "24:00", "12:60", "noon", "1234"])
def test_parse_time_rejects(raw):
    with pytest.raises(ValueError):
        timeutil.parse_time(raw)


def test_closing_moment_follows_daylight_saving():
    # CEST (UTC+2) in October, CET (UTC+1) in November.
    assert timeutil.closing_moment(date(2026, 10, 18), "23:59", "Europe/Paris") == datetime(2026, 10, 18, 21, 59, tzinfo=timezone.utc)
    assert timeutil.closing_moment(date(2026, 11, 8), "23:59", "Europe/Paris") == datetime(2026, 11, 8, 22, 59, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "raw, expected",
    [("17/10/2026", date(2026, 10, 17)), ("1/2/2027", date(2027, 2, 1)), (" 05.11.2026 ", date(2026, 11, 5)), ("5-11-2026", date(2026, 11, 5))],
)
def test_parse_day(raw, expected):
    assert timeutil.parse_day(raw) == expected


@pytest.mark.parametrize("raw", ["", "17/10", "31/02/2026", "2026-10-17", "17/10/26", "tomorrow"])
def test_parse_day_rejects(raw):
    with pytest.raises(ValueError, match="DD/MM/YYYY"):
        timeutil.parse_day(raw)


def test_input_day():
    assert timeutil.input_day(date(2026, 10, 5)) == "05/10/2026"
    assert timeutil.input_day(None) == ""


def test_local_today_uses_the_server_timezone():
    late_utc = datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)
    assert timeutil.local_today("Europe/Paris", late_utc) == date(2026, 10, 2)
    assert timeutil.local_today("UTC", late_utc) == date(2026, 10, 1)


def test_format_day():
    assert timeutil.format_day(date(2026, 10, 25)) == "Sunday 25 October 2026"
    assert timeutil.format_day(None) == "*not set*"


def test_unknown_timezone():
    with pytest.raises(ValueError, match="Unknown timezone"):
        timeutil.zone("Mars/Olympus")
