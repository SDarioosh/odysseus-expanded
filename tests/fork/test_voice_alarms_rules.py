"""Fork: the Echo alarms' rules (fork/voice/alarm_rules.py): times, days, dates, the
next ring time and how it is said. No storage."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from fork.voice import alarm_rules as rules

TZ = ZoneInfo("America/New_York")
# Monday 16 November 2026, 08:00 in New York
NOW = datetime(2026, 11, 16, 8, 0, tzinfo=TZ)


@pytest.mark.parametrize("said, hhmm", [
    ("07:30", "07:30"), ("7:30", "07:30"), ("7", "07:00"), ("7am", "07:00"),
    ("7:30 pm", "19:30"), ("7:30 p.m.", "19:30"), ("12am", "00:00"), ("12 pm", "12:00"),
    ("19.30", "19:30"), ("0730", "07:30"), ("730", "07:30"), ("noon", "12:00"),
    ("midnight", "00:00"), (" 23:59 ", "23:59"),
])
def test_times(said, hhmm):
    assert rules.parse_time(said) == hhmm


@pytest.mark.parametrize("bad", ["", None, "24:00", "7:60", "13pm", "soon", "7:3"])
def test_bad_times(bad):
    with pytest.raises(rules.BadAlarm):
        rules.parse_time(bad)


@pytest.mark.parametrize("said, days", [
    (None, []), ("", []), ([], []), ("weekdays", [1, 2, 3, 4, 5]), (["weekends"], [6, 7]),
    ("every day", [1, 2, 3, 4, 5, 6, 7]), ("daily", [1, 2, 3, 4, 5, 6, 7]),
    (["Monday", "wed"], [1, 3]), ("mondays and fridays", [1, 5]), ("Tue, Thu", [2, 4]),
    ([7, 1], [1, 7]), (["on Sundays"], [7]), ("every monday", [1]),
])
def test_days(said, days):
    assert rules.parse_days(said) == days


@pytest.mark.parametrize("bad", ["someday", [8], [True], ["monday", "funday"]])
def test_bad_days(bad):
    with pytest.raises(rules.BadAlarm):
        rules.parse_days(bad)


def test_dates():
    assert rules.parse_date("2026-11-20") == date(2026, 11, 20)
    assert rules.parse_date("") is None
    with pytest.raises(rules.BadAlarm):
        rules.parse_date("next friday")


def test_a_one_off_defaults_to_today_or_tomorrow():
    assert rules.default_date("09:00", NOW) == date(2026, 11, 16)
    assert rules.default_date("08:00", NOW) == date(2026, 11, 17)  # now is not "to come"
    assert rules.default_date("07:30", NOW) == date(2026, 11, 17)


def test_next_ring_of_a_one_off():
    at = rules.next_ring("07:30", (), date(2026, 11, 17), NOW)
    assert at == datetime(2026, 11, 17, 7, 30, tzinfo=TZ)
    assert at.isoformat() == "2026-11-17T07:30:00-05:00"
    assert rules.next_ring("07:30", (), "2026-11-16", NOW) is None  # passed
    assert rules.next_ring("07:30", (), None, NOW) is None


def test_next_ring_of_a_repeating_alarm():
    # Monday 08:00: a weekday 07:30 alarm next rings Tuesday; 09:00 still today
    assert rules.next_ring("07:30", [1, 2, 3, 4, 5], None, NOW).date() == date(2026, 11, 17)
    assert rules.next_ring("09:00", [1, 2, 3, 4, 5], None, NOW).date() == date(2026, 11, 16)
    # Mondays only, past today's: next Monday
    assert rules.next_ring("07:00", [1], None, NOW).date() == date(2026, 11, 23)
    assert rules.next_ring("10:00", [6, 7], None, NOW).date() == date(2026, 11, 21)


def test_how_it_is_said():
    assert rules.days_detail([1, 2, 3, 4, 5]) == "Weekdays"
    assert rules.days_detail([6, 7]) == "Weekends"
    assert rules.days_detail(range(1, 8)) == "Every day"
    assert rules.days_detail([1, 3]) == "Mon, Wed"
    assert rules.days_detail([]) is None
    assert rules.days_phrase([1, 3]) == "on Mondays and Wednesdays"
    assert rules.days_phrase([1, 2, 3, 4, 5]) == "on weekdays"
    assert rules.spoken_time("07:30") == "7:30 AM"
    assert rules.spoken_time("19:00") == "7 PM"
    assert rules.spoken_time("00:05") == "12:05 AM"
    at = datetime(2026, 11, 17, 7, 30, tzinfo=TZ)
    assert rules.spoken_when(at, NOW) == "7:30 AM tomorrow"
    assert rules.spoken_when(NOW.replace(hour=9), NOW) == "9 AM today"
    assert rules.spoken_when(datetime(2026, 11, 20, 6, 0, tzinfo=TZ), NOW) == "6 AM on Friday"
    assert rules.spoken_when(datetime(2026, 12, 4, 6, 0, tzinfo=TZ), NOW) == \
        "6 AM on Friday, 4 December"
