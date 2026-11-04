"""Fork: calendar times in the owner's wall time, not UTC.

Two places let the model see an event at the wrong hour:

* ``manage_calendar``'s ``list_events`` answer showed the stored times as they are kept,
  ``2026-11-05T15:00:00Z``. A model reads that as the local time and says "3 pm" for a
  4 pm Berlin meeting. :func:`event_lines_when` gives each event's start and end in the
  user's zone, and :func:`with_local_times` adds them to the event rows the model sees.
* A voice turn (``fork/voice/routes.py``) has no browser to send ``X-Tz-Name``, so the
  user's zone fell back to the server's, UTC in Docker. :func:`use_owner_timezone` sets
  the owner's zone for such a turn: the owner's personal assistant's timezone, else any
  of their crew members' (Settings, the same zone their check-ins use).

Upstream's ``src/user_time.py`` holds the request's zone; nothing here is stored.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, tzinfo
from typing import Any

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ the owner's zone
def owner_timezone_name(owner: Any) -> str | None:
    """The owner's IANA zone from their crew: the personal assistant's, else the first
    crew member with one. None when none is set or valid."""
    if not owner:
        return None
    from zoneinfo import ZoneInfo

    try:
        from core.database import CrewMember, SessionLocal

        db = SessionLocal()
        try:
            rows = (db.query(CrewMember)
                    .filter(CrewMember.owner == str(owner), CrewMember.timezone.isnot(None))
                    .all())
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 -- no zone, the server's is used
        logger.debug("local time: owner timezone lookup failed: %s", e)
        return None
    rows.sort(key=lambda cm: not bool(getattr(cm, "is_default_assistant", False)))
    for cm in rows:
        name = str(cm.timezone or "").strip()
        if not name:
            continue
        try:
            ZoneInfo(name)
        except Exception:  # noqa: BLE001 -- an invalid name is skipped
            continue
        return name
    return None


def use_owner_timezone(owner: Any) -> str | None:
    """Give this request the owner's zone when no client sent one. Returns the zone
    set, or None (a client's zone stays, and without an owner zone nothing changes)."""
    from src.user_time import get_user_tz_name, get_user_tz_offset, set_user_tz_name

    if get_user_tz_name() or get_user_tz_offset() is not None:
        return None
    name = owner_timezone_name(owner)
    if name:
        set_user_tz_name(name)
    return name


# --------------------------------------------------------------- the event's times
def _local(value: str, all_day: bool, zone: tzinfo) -> datetime | None:
    """A serialized event time (``_event_to_dict``) as a naive wall time in ``zone``.
    A ``Z`` time is UTC and converted; a naive one is a wall time already."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if all_day or dt.tzinfo is None:
        return dt.replace(tzinfo=None)
    return dt.astimezone(zone).replace(tzinfo=None)


def _zone() -> tzinfo:
    from src.user_time import user_timezone

    return user_timezone()


def local_times(ev: dict, zone: tzinfo | None = None) -> tuple[str, str]:
    """``(start, end)`` of an event row as wall-time text, ``2026-11-05 Thu 16:00`` (the
    end without its date when on the same day); all-day rows give their dates."""
    zone = zone or _zone()
    all_day = bool(ev.get("all_day"))
    start = _local(ev.get("dtstart", ""), all_day, zone)
    end = _local(ev.get("dtend", ""), all_day, zone)
    if start is None:
        return str(ev.get("dtstart", "")), str(ev.get("dtend", ""))
    if all_day:
        return start.strftime("%Y-%m-%d %a"), end.strftime("%Y-%m-%d %a") if end else ""
    start_s = start.strftime("%Y-%m-%d %a %H:%M")
    if end is None:
        return start_s, ""
    end_s = end.strftime("%H:%M") if end.date() == start.date() else end.strftime("%Y-%m-%d %a %H:%M")
    return start_s, end_s


def event_line_when(ev: dict, zone: tzinfo | None = None) -> str:
    """The ``when`` part of a ``list_events`` line: ``2026-11-05 Thu 16:00 -> 17:00``,
    or ``2026-11-05 Thu (all day)``."""
    start, end = local_times(ev, zone)
    if ev.get("all_day"):
        return f"{start} (all day)"
    return f"{start} -> {end}" if end else start


def zone_note(zone: tzinfo | None = None) -> str:
    """The line that says which zone the times are in."""
    from src.user_time import timezone_label

    zone = zone or _zone()
    return f"Times are the user's local time ({timezone_label(datetime.now(timezone.utc).astimezone(zone))})."


def with_local_times(events: list[dict], zone: tzinfo | None = None) -> list[dict]:
    """The event rows with ``local_start`` and ``local_end`` first, so the model reads
    the wall time before the stored ``dtstart``/``dtend``."""
    zone = zone or _zone()
    out = []
    for ev in events:
        start, end = local_times(ev, zone)
        out.append({"local_start": start, "local_end": end, **ev})
    return out
