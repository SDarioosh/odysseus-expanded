"""Fork: the rules of the Echo alarms, no storage (docs/fork/voice-assistant.md#alarms).

Plain functions, unit-tested in ``tests/fork/test_voice_alarms_rules.py``: reading a
clock time, weekdays and a date the way the model passes them, the next time an alarm
rings in the owner's zone, and how that is said. ``fork/voice/alarms.py`` stores alarms;
``fork/voice/alarm_tool.py`` is the agent's tool.

An alarm is a wall-clock ``time`` (``"07:30"``, 24 h) plus either ``days`` (ISO weekdays,
Monday 1 … Sunday 7: it repeats) or a ``date`` (``YYYY-MM-DD``: it rings once that day).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
FULL_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
                  "Sunday")
WEEKDAYS = (1, 2, 3, 4, 5)
WEEKENDS = (6, 7)
EVERY_DAY = (1, 2, 3, 4, 5, 6, 7)
_DAY_WORDS: dict[str, tuple[int, ...]] = {
    "weekday": WEEKDAYS, "weekdays": WEEKDAYS, "workday": WEEKDAYS, "workdays": WEEKDAYS,
    "weekend": WEEKENDS, "weekends": WEEKENDS,
    "every day": EVERY_DAY, "everyday": EVERY_DAY, "daily": EVERY_DAY, "all": EVERY_DAY,
    "every": EVERY_DAY, "all days": EVERY_DAY,
}
for _i, _name in enumerate(FULL_DAY_NAMES, start=1):
    _low = _name.lower()
    _DAY_WORDS[_low] = (_i,)
    _DAY_WORDS[_low + "s"] = (_i,)
    _DAY_WORDS[_low[:3]] = (_i,)
for _short, _i in (("tues", 2), ("weds", 3), ("thur", 4), ("thurs", 4)):
    _DAY_WORDS[_short] = (_i,)

_TIME = re.compile(r"^(\d{1,2})(?:[:.h](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?$")
_COMPACT = re.compile(r"^(\d{1,2})(\d{2})$")  # "0730", "730"


class BadAlarm(ValueError):
    """A time, day or date that can't be read (the message is said to the model)."""


def parse_time(value: Any) -> str:
    """``"7:30"``, ``"07:30"``, ``"7"``, ``"7am"``, ``"7:30 pm"``, ``"19.30"``,
    ``"0730"`` -> ``"HH:MM"`` (24 h). Raises :class:`BadAlarm`."""
    text = " ".join(str(value if value is not None else "").lower().split())
    text = {"noon": "12:00", "midday": "12:00", "midnight": "00:00"}.get(text, text)
    m = _TIME.match(text)
    if m is None:
        c = _COMPACT.match(text)
        if c is None:
            raise BadAlarm(f"Can't read the time {value!r}; give it as HH:MM (24 h).")
        hour, minute, half = int(c.group(1)), int(c.group(2)), None
    else:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        half = (m.group(3) or "").replace(".", "") or None
    if half is not None:
        if not 1 <= hour <= 12:
            raise BadAlarm(f"Can't read the time {value!r}.")
        hour = hour % 12 + (12 if half == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise BadAlarm(f"Can't read the time {value!r}.")
    return f"{hour:02d}:{minute:02d}"


def parse_days(value: Any) -> list[int]:
    """Weekday names, ``"weekdays"``, ``"weekends"``, ``"every day"``, ISO numbers, or a
    list of those -> sorted ISO weekdays; ``[]`` (a one-off) for nothing. Raises
    :class:`BadAlarm` for a word it doesn't know."""
    if value is None or value == "" or value == []:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: set[int] = set()
    for item in items:
        if isinstance(item, bool):
            raise BadAlarm(f"Can't read the day {item!r}.")
        if isinstance(item, int):
            if not 1 <= item <= 7:
                raise BadAlarm(f"Can't read the day {item!r} (Monday is 1, Sunday 7).")
            out.add(item)
            continue
        text = str(item or "").lower().replace(" and ", ",").replace("&", ",")
        for part in re.split(r"[,;/]+", text):
            word = " ".join(re.sub(r"[^a-z0-9 ]+", " ", part).split())
            word = re.sub(r"^(?:on|every|each)\s+(?=\w)", "", word) \
                if word not in _DAY_WORDS else word
            if not word or word in ("none", "once", "one off", "one time"):
                continue
            if word.isdigit() and 1 <= int(word) <= 7:
                out.add(int(word))
            elif word in _DAY_WORDS:
                out.update(_DAY_WORDS[word])
            else:
                raise BadAlarm(f"Can't read the day {part.strip()!r}; use weekday names, "
                               "\"weekdays\", \"weekends\" or \"every day\".")
    return sorted(out)


def parse_date(value: Any) -> date | None:
    """``"YYYY-MM-DD"`` -> a date; None for nothing. Raises :class:`BadAlarm`."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise BadAlarm(f"Can't read the date {value!r}; give it as YYYY-MM-DD.") from None


def _at(day: date, hhmm: str, now_local: datetime) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=now_local.tzinfo)


def default_date(hhmm: str, now_local: datetime) -> date:
    """A one-off's day when none is said: today while that time is still to come, else
    tomorrow."""
    today = now_local.date()
    return today if _at(today, hhmm, now_local) > now_local else today + timedelta(days=1)


def next_ring(hhmm: str, days: list[int] | tuple[int, ...], day: date | str | None,
              now_local: datetime) -> datetime | None:
    """When the alarm rings next, strictly after ``now_local`` (aware, in the owner's
    zone); None for a one-off whose time has passed (or one without a date)."""
    if days:
        today = now_local.date()
        for ahead in range(8):
            d = today + timedelta(days=ahead)
            if d.isoweekday() in days:
                at = _at(d, hhmm, now_local)
                if at > now_local:
                    return at
        return None
    if isinstance(day, str):
        try:
            day = date.fromisoformat(day)
        except ValueError:
            return None
    if day is None:
        return None
    at = _at(day, hhmm, now_local)
    return at if at > now_local else None


# ------------------------------------------------------------------- how it's said
def days_detail(days: list[int] | tuple[int, ...]) -> str | None:
    """``"Weekdays"``, ``"Weekends"``, ``"Every day"``, ``"Mon, Wed"``; None for a one-off."""
    days = tuple(sorted(set(days or ())))
    if not days:
        return None
    if days == EVERY_DAY:
        return "Every day"
    if days == WEEKDAYS:
        return "Weekdays"
    if days == WEEKENDS:
        return "Weekends"
    return ", ".join(DAY_NAMES[d - 1] for d in days)


def days_phrase(days: list[int] | tuple[int, ...]) -> str:
    """How a repeat is said after the time: "every day", "on weekdays", "on Mondays and
    Wednesdays"; "" for a one-off."""
    detail = days_detail(days)
    if detail is None:
        return ""
    if detail == "Every day":
        return "every day"
    if detail in ("Weekdays", "Weekends"):
        return "on " + detail.lower()
    names = [FULL_DAY_NAMES[d - 1] + "s" for d in sorted(set(days))]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return "on " + joined


def spoken_time(at: datetime | str) -> str:
    """``7:30 AM``, ``7 AM``, ``12:15 PM``."""
    if isinstance(at, str):
        hour, minute = (int(x) for x in at.split(":"))
    else:
        hour, minute = at.hour, at.minute
    half = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12} {half}" if minute == 0 else f"{h12}:{minute:02d} {half}"


def spoken_when(at: datetime, now_local: datetime) -> str:
    """``7:30 AM today``, ``7:30 AM tomorrow``, ``7:30 AM on Friday`` (within a week),
    ``7:30 AM on Friday, 4 December``."""
    ahead = (at.date() - now_local.date()).days
    clock = spoken_time(at)
    if ahead == 0:
        return f"{clock} today"
    if ahead == 1:
        return f"{clock} tomorrow"
    name = FULL_DAY_NAMES[at.isoweekday() - 1]
    if 1 < ahead < 7:
        return f"{clock} on {name}"
    return f"{clock} on {name}, {at.day} {at.strftime('%B')}"
