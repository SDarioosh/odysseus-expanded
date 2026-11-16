"""Fork: the alarms of each Echo, kept by the hub (docs/fork/voice-assistant.md#alarms).

"Set an alarm for 7" reaches Odysseus (Home Assistant has no alarm intent); the agent's
``alarm`` tool (``fork/voice/alarm_tool.py``) keeps it here, for the device it was said
to. The Echo rings it itself: it fetches its list
(``GET /api/chat/voice/v1/alarms?device_id=…``, ``fork/voice/alarm_routes.py``) at start,
when its feed delivers an item of ``kind: "alarms"`` (put there on every change by
:func:`_changed`, never a card), and when its turn shows an ``alarm`` action.

One row per alarm in the voice SQLite file (``<DATA_DIR>/fork_voice.db``), owner-scoped,
keyed by device id; a second table keeps when each device's list last changed. One-off
alarms whose time has passed (in the owner's zone) are never served and are deleted when
the list is read. At most :data:`MAX_PER_DEVICE` per device. The rules (times, days,
next ring) are ``fork/voice/alarm_rules.py``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
from typing import Any

from fork.common import sqlite as common_sqlite
from fork.voice import alarm_rules as rules
from fork.voice import devices

logger = logging.getLogger(__name__)

MAX_PER_DEVICE = 20
MAX_LABEL = 60
FEED_KIND = "alarms"
FEED_TEXT = "Alarms changed in Odysseus."
FEED_SHOW_SECONDS = 600  # the device polls every 30 s; at start it fetches anyway
NEVER = datetime.fromtimestamp(0, tz=timezone.utc).isoformat(timespec="seconds")

SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_alarms (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    device_id TEXT NOT NULL,
    time TEXT NOT NULL,
    days TEXT NOT NULL DEFAULT '[]',
    date TEXT,
    label TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS voice_alarms_device ON voice_alarms (owner, device_id);
CREATE TABLE IF NOT EXISTS voice_alarm_lists (
    owner TEXT NOT NULL,
    device_id TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (owner, device_id)
);
"""


class TooMany(ValueError):
    """The device has :data:`MAX_PER_DEVICE` alarms already."""


@dataclass(frozen=True)
class Alarm:
    id: str
    device_id: str
    time: str
    days: tuple[int, ...]
    date: str | None
    label: str | None

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "time": self.time, "days": list(self.days),
                "date": self.date, "label": self.label}

    def next_ring(self, now_local: datetime) -> datetime | None:
        return rules.next_ring(self.time, self.days, self.date, now_local)


def _connect(write: bool = False) -> AbstractContextManager[sqlite3.Connection]:
    from fork.voice.ledger import db_path

    return common_sqlite.connect(db_path(), SCHEMA, write)


def owner_tz(owner: Any) -> tzinfo:
    """The owner's zone (the Today page's rule), UTC when it can't be told."""
    try:
        from fork.insight import today as ti

        return ti.owner_timezone(owner)
    except Exception:  # noqa: BLE001
        logger.warning("voice alarms: owner timezone unavailable", exc_info=True)
        return timezone.utc


def now_local(owner: Any, now: float | None = None, tz: tzinfo | None = None) -> datetime:
    at = datetime.fromtimestamp(time.time() if now is None else float(now), tz=timezone.utc)
    return at.astimezone(tz or owner_tz(owner))


def _clean_id(device_id: Any) -> str:
    return devices.clean(device_id, devices.FIELD_LIMITS["device_id"])


def _alarm(row: sqlite3.Row) -> Alarm:
    try:
        days = tuple(int(d) for d in json.loads(row["days"] or "[]") if 1 <= int(d) <= 7)
    except (TypeError, ValueError):
        days = ()
    return Alarm(id=row["id"], device_id=row["device_id"], time=row["time"],
                 days=tuple(sorted(set(days))), date=None if days else row["date"],
                 label=row["label"] or None)


def _rows(db: sqlite3.Connection, key: str, device_id: str) -> list[Alarm]:
    return [_alarm(r) for r in db.execute(
        "SELECT * FROM voice_alarms WHERE owner = ? AND device_id = ? ORDER BY created_at, rowid",
        (key, device_id)).fetchall()]


def alarms(owner: Any, device_id: Any, *, now: float | None = None,
           tz: tzinfo | None = None) -> list[Alarm]:
    """This device's alarms, the next to ring first; one-offs that have passed are
    deleted (no feed item: the device knows they passed)."""
    key, device_id = devices.owner_key(owner), _clean_id(device_id)
    if not key or not device_id:
        return []
    local = now_local(owner, now, tz)
    with _connect(write=True) as db:
        found = _rows(db, key, device_id)
        gone = [a.id for a in found if a.next_ring(local) is None]
        for alarm_id in gone:
            db.execute("DELETE FROM voice_alarms WHERE id = ? AND owner = ?", (alarm_id, key))
    left = [a for a in found if a.id not in gone]
    return sorted(left, key=lambda a: a.next_ring(local))  # type: ignore[arg-type,return-value]


def body(owner: Any, device_id: Any, *, now: float | None = None,
         tz: tzinfo | None = None) -> dict[str, Any]:
    """``GET …/alarms``'s answer: ``{"alarms": [...], "updated_at": iso}`` (the epoch
    when this device's list never changed)."""
    found = alarms(owner, device_id, now=now, tz=tz)
    with _connect() as db:
        row = db.execute("SELECT updated_at FROM voice_alarm_lists WHERE owner = ? AND"
                         " device_id = ?", (devices.owner_key(owner), _clean_id(device_id))
                         ).fetchone()
    updated = (datetime.fromtimestamp(row["updated_at"], tz=timezone.utc)
               .isoformat(timespec="seconds") if row is not None else NEVER)
    return {"alarms": [a.as_json() for a in found], "updated_at": updated}


def add(owner: Any, device_id: Any, hhmm: str, *, days: list[int] | tuple[int, ...] = (),
        day: date | None = None, label: str | None = None, now: float | None = None,
        tz: tzinfo | None = None) -> Alarm:
    """Keep a new alarm (already checked by the rules: ``hhmm`` from ``parse_time``,
    ``days`` from ``parse_days``, ``day`` the one-off's date); raises :class:`TooMany`."""
    key, device_id = devices.owner_key(owner), _clean_id(device_id)
    if not key or not device_id:
        raise ValueError("an owner and a device id are needed")
    days = tuple(sorted(set(days or ())))
    if not days and day is None:
        raise ValueError("a one-off alarm needs its date")
    label = " ".join(str(label or "").split())[:MAX_LABEL] or None
    stamp = time.time() if now is None else float(now)
    alarms(owner, device_id, now=now, tz=tz)  # prune passed one-offs before counting
    alarm = Alarm(id="al_" + uuid.uuid4().hex[:16], device_id=device_id, time=hhmm,
                  days=days, date=None if days else day.isoformat(),  # type: ignore[union-attr]
                  label=label)
    with _connect(write=True) as db:
        count = db.execute("SELECT COUNT(*) FROM voice_alarms WHERE owner = ? AND"
                           " device_id = ?", (key, device_id)).fetchone()[0]
        if count >= MAX_PER_DEVICE:
            raise TooMany(f"This device has {MAX_PER_DEVICE} alarms already; cancel one "
                          "first.")
        db.execute("INSERT INTO voice_alarms (id, owner, device_id, time, days, date, label,"
                   " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (alarm.id, key, device_id, alarm.time, json.dumps(list(days)),
                    alarm.date, label, stamp))
    _changed(key, device_id, stamp)
    return alarm


def remove(owner: Any, device_id: Any, alarm_ids: list[str], *,
           now: float | None = None) -> list[Alarm]:
    """Delete these alarms of this device; returns those that were there. The device's
    feed gets an item when any was."""
    key, device_id = devices.owner_key(owner), _clean_id(device_id)
    ids = [str(i) for i in alarm_ids if i]
    if not key or not device_id or not ids:
        return []
    stamp = time.time() if now is None else float(now)
    with _connect(write=True) as db:
        found = [a for a in _rows(db, key, device_id) if a.id in ids]
        for a in found:
            db.execute("DELETE FROM voice_alarms WHERE id = ? AND owner = ? AND device_id = ?",
                       (a.id, key, device_id))
    if found:
        _changed(key, device_id, stamp)
    return found


def _changed(owner: str, device_id: str, now: float) -> None:
    """The list changed: its ``updated_at``, and a ``kind: "alarms"`` item in that
    device's feed (no card; the device fetches its alarms). A feed failure is logged:
    the device still fetches at start and after its turn."""
    from fork.voice import announcements

    with _connect(write=True) as db:
        db.execute("INSERT INTO voice_alarm_lists (owner, device_id, updated_at) VALUES"
                   " (?, ?, ?) ON CONFLICT (owner, device_id) DO UPDATE SET"
                   " updated_at = excluded.updated_at", (owner, device_id, now))
    logger.info("voice: alarms changed (owner %s, device %s)", owner, device_id)
    try:
        announcements.store(owner, FEED_TEXT, {"device_id": device_id}, kind=FEED_KIND,
                            title="Alarms", ref=f"alarms:{int(round(now * 1000))}", now=now,
                            show_seconds=FEED_SHOW_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("voice: the alarms feed item was not kept", exc_info=True)
