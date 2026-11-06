"""Fork: announcements to a room or a device, and the devices' feed of them
(docs/fork/voice-assistant.md#announcements).

:func:`deliver` is the one way to announce: it keeps the announcement for the devices'
feed (``GET /api/chat/voice/v1/announcements``, ``fork/voice/satellite_routes.py``) and
speaks it through Home Assistant (``fork/voice/announce.py``'s ``send``) on the
satellites of its target:

* ``"all"``: the configured list (``FORK_VOICE_ANNOUNCE_ENTITIES``), as before;
* ``{"area": "Kitchen"}``: the ``assist_satellite.*`` entities Home Assistant has in that
  area (:func:`satellites`, one fixed template through ``POST /api/template`` with
  ``areas()`` and ``area_entities()``; nothing the owner said is ever put in a template);
* ``{"device_id": "..."}``: the satellite of that device (by its name, else its room, from
  the registry ``fork/voice/devices.py``).

When Home Assistant has no satellite for the area or device, it falls back to the
configured list. The same local-only rule as before: Home Assistant must be on the
owner's network (``announce.check_url``), and is only asked when it is set up.

The feed keeps each announcement :data:`KEEP_SECONDS` (24 h) and shows it until its
``expires_at`` (:data:`SHOW_SECONDS` after it was made); a device gets only those meant
for it: every device, its area (as it last said in its hello or turn), or its device id.
Owner-scoped: a device only ever sees its own owner's announcements.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from fork.voice import announce as voice_announce
from fork.voice import devices

logger = logging.getLogger(__name__)

KEEP_SECONDS = 24 * 3600
SHOW_SECONDS = 3600
MAX_FEED = 50
MAX_TITLE = 120
# "settings": the device's hub-managed settings changed (fork/voice/device_settings.py);
# the device refetches them and shows no card.
KINDS = frozenset({"reminder", "announcement", "settings"})
TEMPLATE_PATH = "/api/template"
# Fixed: every satellite Home Assistant has in an area, with the area's id and name and
# the entity's name. Never built from user input.
SATELLITES_TEMPLATE = (
    "{%- set ns = namespace(rows=[]) -%}"
    "{%- for a in areas() -%}"
    "{%- for e in area_entities(a) if e.startswith('assist_satellite.') -%}"
    "{%- set ns.rows = ns.rows + [{'entity_id': e, 'area_id': a, 'area': area_name(a),"
    " 'name': state_attr(e, 'friendly_name') or ''}] -%}"
    "{%- endfor -%}"
    "{%- endfor -%}"
    "{{ ns.rows | tojson }}"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    at REAL NOT NULL,
    expires_at REAL NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    target TEXT NOT NULL,
    ref TEXT
);
CREATE INDEX IF NOT EXISTS announcements_owner_at ON announcements (owner, at);
"""

_tasks: set[asyncio.Task] = set()


# ------------------------------------------------------------------------ targets
def normalize_target(target: Any) -> dict:
    """``"all"`` / None -> ``{"all": True}``; ``{"area": ...}`` / ``{"device_id": ...}``
    cleaned. The feed shows a target in this shape (always an object)."""
    if isinstance(target, dict):
        area = devices.clean(target.get("area"), devices.FIELD_LIMITS["area"])
        if area:
            return {"area": area}
        device_id = devices.clean(target.get("device_id"), devices.FIELD_LIMITS["device_id"])
        if device_id:
            return {"device_id": device_id}
    if target in (None, "", "all") or (isinstance(target, dict) and target.get("all")):
        return {"all": True}
    raise ValueError("target must be 'all', {'area': ...} or {'device_id': ...}")


# ----------------------------------------------------------------------- the feed
@contextmanager
def _connect(write: bool = False) -> Iterator[sqlite3.Connection]:
    from fork.voice.ledger import db_path

    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)
        if write:
            db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            if write:
                db.execute("ROLLBACK")
            raise
        if write:
            db.execute("COMMIT")
    finally:
        db.close()


def _row(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "at": int(round(row["at"] * 1000)), "kind": row["kind"],
            "title": row["title"], "text": row["text"], "target": json.loads(row["target"]),
            "ref": row["ref"], "expires_at": int(round(row["expires_at"] * 1000))}


def store(owner: Any, text: str, target: Any = "all", *, kind: str = "announcement",
          title: str | None = None, ref: str | None = None, now: float | None = None,
          show_seconds: float = SHOW_SECONDS) -> dict:
    """Keep an announcement for the feed; returns it as the feed shows it."""
    key = devices.owner_key(owner)
    if not key:
        raise ValueError("an announcement needs an owner")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    text = " ".join(str(text or "").split())[:voice_announce.MAX_MESSAGE]
    if not text:
        raise ValueError("nothing to announce")
    title = " ".join(str(title or text).split())[:MAX_TITLE]
    now = time.time() if now is None else float(now)
    item_id = "an_" + uuid.uuid4().hex[:16]
    with _connect(write=True) as db:
        db.execute("DELETE FROM announcements WHERE at < ?", (now - KEEP_SECONDS,))
        db.execute(
            "INSERT INTO announcements (id, owner, at, expires_at, kind, title, text, target,"
            " ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, key, now, now + max(0.0, float(show_seconds)), kind, title, text,
             json.dumps(normalize_target(target)), (str(ref)[:120] if ref else None)))
        row = db.execute("SELECT * FROM announcements WHERE id = ?", (item_id,)).fetchone()
    return _row(row)


def _for_device(target: dict, device_id: str, area: str) -> bool:
    if target.get("all"):
        return True
    if target.get("device_id"):
        return target["device_id"] == device_id
    if target.get("area"):
        return bool(area) and devices.slug(target["area"]) == devices.slug(area)
    return False


def feed(owner: Any, device_id: Any, *, since_ms: Any = 0,
         now: float | None = None) -> list[dict]:
    """The announcements meant for this device, made after ``since_ms`` (epoch ms) and
    not yet expired, oldest first."""
    now = time.time() if now is None else float(now)
    try:
        since = max(0, int(since_ms or 0))
    except (TypeError, ValueError):
        since = 0
    device_id = str(device_id or "")
    device = devices.get(owner, device_id) if device_id else None
    area = device.area if device is not None else ""
    with _connect() as db:
        rows = db.execute(
            # compared in the feed's own epoch ms, so `since` = an item's `at` skips it
            "SELECT * FROM announcements WHERE owner = ?"
            " AND CAST(ROUND(at * 1000) AS INTEGER) > ? AND expires_at > ?"
            " AND at >= ? ORDER BY at, rowid",
            (devices.owner_key(owner), since, now, now - KEEP_SECONDS)).fetchall()
    out = [_row(r) for r in rows]
    return [a for a in out if _for_device(a["target"], device_id, area)][-MAX_FEED:]


# ----------------------------------------------------- Home Assistant's satellites
@dataclass(frozen=True)
class Satellite:
    entity_id: str
    area_id: str
    area: str
    name: str


async def satellites(*, transport: httpx.AsyncBaseTransport | None = None
                     ) -> list[Satellite]:
    """Every ``assist_satellite`` entity Home Assistant has in an area. Raises
    ``announce.Refused`` when Home Assistant isn't set up, isn't local, or can't say."""
    from fork.common import settings
    from fork.voice.ha_settings import home_assistant_url

    ha_url = home_assistant_url()
    if not ha_url:
        raise voice_announce.Refused("No Home Assistant address")
    token = settings.ha_token()
    if not token:
        raise voice_announce.Refused("FORK_HA_TOKEN is not set")
    base = await voice_announce.check_url(ha_url)
    try:
        async with httpx.AsyncClient(timeout=voice_announce.TIMEOUT, transport=transport,
                                     follow_redirects=False) as client:
            res = await client.post(base + TEMPLATE_PATH,
                                    json={"template": SATELLITES_TEMPLATE},
                                    headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise voice_announce.Refused(
            f"Home Assistant is not reachable ({type(exc).__name__})") from None
    if not res.is_success:
        raise voice_announce.Refused(f"Home Assistant answered HTTP {res.status_code}")
    try:
        rows = json.loads(res.text)
    except ValueError:
        raise voice_announce.Refused("Home Assistant's satellite list can't be read") from None
    out = []
    for r in rows if isinstance(rows, list) else ():
        if not isinstance(r, dict):
            continue
        entity = str(r.get("entity_id") or "")
        if not entity.startswith("assist_satellite."):
            continue
        out.append(Satellite(entity, str(r.get("area_id") or ""), str(r.get("area") or ""),
                             str(r.get("name") or "")))
    return out


def _in_area(sats: list[Satellite], area: str) -> list[str]:
    want = devices.slug(area)
    return [s.entity_id for s in sats
            if want and (devices.slug(s.area_id) == want or devices.slug(s.area) == want)]


def pick_entities(owner: Any, target: dict, sats: list[Satellite]) -> list[str]:
    """The satellites of ``target`` among Home Assistant's; [] when none match."""
    if target.get("area"):
        return _in_area(sats, target["area"])
    if target.get("device_id"):
        device = devices.get(owner, target["device_id"])
        if device is None:
            return []
        name = devices.slug(device.name)
        if name:
            named = [s.entity_id for s in sats
                     if devices.slug(s.name).startswith(name)
                     or devices.slug(s.entity_id.split(".", 1)[1]).startswith(name)]
            if len(named) == 1:
                return named
        if device.area:
            return _in_area(sats, device.area)
    return []


async def entities_for(owner: Any, target: Any, *,
                       transport: httpx.AsyncBaseTransport | None = None) -> list[str] | None:
    """The entities to speak on: those Home Assistant has for the target, else None (the
    configured list)."""
    target = normalize_target(target)
    if target.get("all"):
        return None
    try:
        sats = await satellites(transport=transport)
    except voice_announce.Refused as exc:
        logger.info("voice announce: satellites unknown (%s); the configured list is used", exc)
        return None
    found = pick_entities(owner, target, sats)
    return found or None


# ---------------------------------------------------------------------- delivering
@dataclass
class Delivery:
    item: dict | None = None  # as the feed shows it; None when it couldn't be kept
    spoken: bool = False
    entities: list[str] = field(default_factory=list)
    problem: str | None = None  # why it wasn't spoken


def can_speak() -> bool:
    """Home Assistant is set up for speaking (its address and token)."""
    from fork.common import settings
    from fork.voice.ha_settings import home_assistant_url

    return bool(home_assistant_url() and settings.ha_token())


async def deliver(owner: Any, message: str, target: Any = "all", *,
                  kind: str = "announcement", title: str | None = None,
                  ref: str | None = None,
                  transport: httpx.AsyncBaseTransport | None = None) -> Delivery:
    """Keep it for the feed and speak it on the target's satellites. Never raises."""
    out = Delivery()
    try:
        norm = normalize_target(target)
    except ValueError as exc:
        out.problem = str(exc)
        return out
    try:
        out.item = await asyncio.to_thread(store, owner, message, norm, kind=kind,
                                           title=title, ref=ref)
    except Exception as exc:  # noqa: BLE001 -- the feed is extra; speaking still goes on
        logger.warning("voice announce: not kept for the feed: %s", type(exc).__name__)
    if not can_speak():
        out.problem = "Home Assistant is not set up for announcements"
        return out
    try:
        entities = await entities_for(owner, norm, transport=transport)
        out.entities = list(entities or ())
        await voice_announce.send(message, entities, transport=transport)
        out.spoken = True
    except voice_announce.Refused as exc:
        out.problem = str(exc)
        logger.info("voice announce: not spoken: %s", exc)
    except Exception as exc:  # noqa: BLE001
        out.problem = "failed"
        logger.warning("voice announce: failed: %s", type(exc).__name__)
    return out


def deliver_soon(owner: Any, message: str, target: Any = "all", **kwargs: Any) -> None:
    """:func:`deliver` in the background on the running loop; never raises, never waits."""
    try:
        task = asyncio.get_running_loop().create_task(
            deliver(owner, message, target, **kwargs))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception as exc:  # noqa: BLE001
        logger.debug("voice announce: background delivery skipped: %s", type(exc).__name__)
