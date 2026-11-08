"""Fork: the voice satellites the hub has heard from (docs/fork/voice-assistant.md#rooms-and-devices).

Each Echo says who it is with ``POST /api/chat/voice/v1/satellite/hello`` (at start and
hourly) and with every ``POST …/satellite/turn`` (``fork/voice/satellite_routes.py``):
its device id, name, room (area) and, in the hello, its app's version code. One row per
(owner, device id) in the voice SQLite file (``<DATA_DIR>/fork_voice.db``, beside the turn
ledger), with when it was last seen. A device unheard for :data:`KEEP_DAYS` is dropped.

Used to send an announcement to a room or a device (``fork/voice/announcements.py``): the
feed gives a device what was meant for its area, and the agent's ``announce`` tool matches
"the kitchen" or "the bedroom Echo" against these names (:func:`match`).
"""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from fork.common import sqlite as common_sqlite
from fork.common.owner import owner_key

KEEP_DAYS = 90
MAX_DEVICES = 64  # per owner: the oldest are dropped
FIELD_LIMITS = {"device_id": 128, "device_name": 100, "area": 100}

SCHEMA = """
CREATE TABLE IF NOT EXISTS satellite_devices (
    owner TEXT NOT NULL,
    device_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    app_version_code INTEGER,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (owner, device_id)
);
"""


@dataclass(frozen=True)
class Device:
    device_id: str
    name: str
    area: str
    app_version_code: int | None
    last_seen: float

    def as_json(self) -> dict[str, Any]:
        return {"device_id": self.device_id, "name": self.name, "area": self.area,
                "app_version_code": self.app_version_code,
                "last_seen": int(self.last_seen * 1000)}


def _connect(write: bool = False) -> AbstractContextManager[sqlite3.Connection]:
    from fork.voice.ledger import db_path

    return common_sqlite.connect(db_path(), SCHEMA, write)


def clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def slug(text: Any) -> str:
    """``"Living Room"`` -> ``"living_room"``: how Home Assistant makes an area id, so a
    room name and its id compare equal."""
    return re.sub(r"[^0-9a-z]+", "_", str(text or "").casefold()).strip("_")


def _device(row: sqlite3.Row) -> Device:
    return Device(device_id=row["device_id"], name=row["name"], area=row["area"],
                  app_version_code=row["app_version_code"], last_seen=row["last_seen"])


def seen(owner: Any, device_id: Any, *, name: Any = None, area: Any = None,
         app_version_code: Any = None, now: float | None = None) -> None:
    """Record that this device was heard from. A field left None (or blank) keeps what
    was stored: a turn without a name doesn't erase the hello's."""
    device_id = clean(device_id, FIELD_LIMITS["device_id"])
    key = owner_key(owner)
    if not device_id or not key:
        return
    now = time.time() if now is None else float(now)
    name = clean(name, FIELD_LIMITS["device_name"]) or None
    area = clean(area, FIELD_LIMITS["area"]) or None
    try:
        version = int(app_version_code) if app_version_code not in (None, "") else None
    except (TypeError, ValueError):
        version = None
    with _connect(write=True) as db:
        db.execute("DELETE FROM satellite_devices WHERE last_seen < ?",
                   (now - KEEP_DAYS * 86400,))
        db.execute(
            "INSERT INTO satellite_devices (owner, device_id, name, area, app_version_code,"
            " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (owner, device_id) DO UPDATE SET"
            " name = COALESCE(?, name), area = COALESCE(?, area),"
            " app_version_code = COALESCE(?, app_version_code), last_seen = ?",
            (key, device_id, name or "", area or "", version, now, now,
             name, area, version, now))
        db.execute(
            "DELETE FROM satellite_devices WHERE owner = ? AND device_id NOT IN ("
            " SELECT device_id FROM satellite_devices WHERE owner = ?"
            " ORDER BY last_seen DESC LIMIT ?)", (key, key, MAX_DEVICES))


def get(owner: Any, device_id: Any) -> Device | None:
    with _connect() as db:
        row = db.execute("SELECT * FROM satellite_devices WHERE owner = ? AND device_id = ?",
                         (owner_key(owner), str(device_id or ""))).fetchone()
    return _device(row) if row is not None else None


def devices(owner: Any) -> list[Device]:
    """This owner's devices, most recently seen first."""
    with _connect() as db:
        rows = db.execute("SELECT * FROM satellite_devices WHERE owner = ?"
                          " ORDER BY last_seen DESC", (owner_key(owner),)).fetchall()
    return [_device(r) for r in rows]


# --------------------------------------------------------------------- matching words
ALL_WORDS = frozenset({"", "all", "everywhere", "every room", "all rooms", "the house",
                       "the whole house", "whole house", "house", "home", "everyone",
                       "everybody", "all devices", "every device"})
_LEAD = re.compile(r"^(?:in|on|to|at|the)\s+")
_TAIL = re.compile(r"\s+(?:room|echo|speaker|device)$")


def _said(text: Any) -> str:
    """Lower case, punctuation gone, a leading "in the" / "to the" dropped."""
    out = str(text or "").casefold().replace("’", "'")
    out = " ".join(re.sub(r"[^\w\s']+", " ", out).split())
    previous = None
    while previous != out:
        previous, out = out, _LEAD.sub("", out).strip()
    return out


def _room(text: Any) -> str:
    """:func:`_said` without a trailing "room" / "echo" ("the kitchen echo" -> kitchen)."""
    out = _said(text)
    previous = None
    while previous != out:
        previous, out = out, _TAIL.sub("", out).strip()
    return out


def is_everywhere(text: Any) -> bool:
    return _said(text) in ALL_WORDS


def match(owner: Any, text: Any, *, areas: tuple[str, ...] = ()) -> dict | str | None:
    """What ``text`` (a room or device name the owner said) names: ``"all"``,
    ``{"device_id": ...}``, ``{"area": ...}``, or None when nothing matches.

    A device's own name wins over a room ("the kitchen Echo" is that device when one is
    called so; else its room). ``areas``: more room names that count (Home Assistant's
    rooms that have a satellite), so a room with no Echo app can still be named.
    """
    if is_everywhere(text):
        return "all"
    said, room = slug(_said(text)), slug(_room(text))
    if not said:
        return None
    known = devices(owner)
    for d in known:
        if d.name and slug(_said(d.name)) == said:
            return {"device_id": d.device_id}
    for area in [d.area for d in known if d.area] + [a for a in areas if a]:
        if room and slug(area) in (said, room):
            return {"area": area}
    return None


def label(owner: Any, target: Any) -> str:
    """How a target is said: "everywhere", "the Kitchen", "Kitchen Echo"."""
    if not isinstance(target, dict):
        return "everywhere"
    if target.get("area"):
        return f"the {target['area']}"
    device = get(owner, target.get("device_id"))
    if device is not None and device.name:
        return device.name
    return "that device"
