"""Fork: Done buttons on a voice device (docs/fork/voice-assistant.md#done-from-a-device).

Contract C5. A tap on the device's own screen (the Echo's Today page or a reminder card)
is the owner's confirmation, so these need no spoken "yes". Scope ``voice``, the same
token check as the other voice routes (:func:`fork.voice.routes.voice_owner`)::

    POST …/todos/done         {"ref": "todo:<note id>:<item index>", "device_id": "…",
                               "title"?: "<the row's text>"}
    POST …/reminders/done     {"ref": "todo:<note id>[:<item index>]", "device_id": "…",
                               "title"?: "…"}
    POST …/action-items/done  {"id": "17", "device_id": "…"}
    POST …/todos/undo         {"ref": "todo:<note id>:<item index>", "device_id"?}
    POST …/action-items/undo  {"id": "17", "device_id"?}

* A to-do item is ticked: the same change as the notes page's tick (the item's ``done``).
* A reminder (a whole note with a due date; the ref its announcement carries) is archived: what the notes page's ✓ Done does
  to a note (``PATCH archived: true``). The reminder scanner and the notes page skip
  archived notes, so it doesn't fire again, and it can be restored from the archive.
  A reminder ref with an item index ticks that item instead, as ``todos/done``.
* ``title`` (optional, the row's text the device showed) guards against a stale index:
  lines added, removed or moved since the device fetched Today. When the line at the
  index doesn't read ``title`` (compared as ``/today`` titles a line: whitespace
  collapsed, its first 200 characters, any case), the line with that text is ticked
  instead (an unticked one first); when no line reads it, 409 ``{"error": "changed"}``
  and nothing changes. Without ``title`` (older devices) the index alone counts, and so it
  does for a reminder card whose ``title`` is its note's title (the card shows that).
* An Athena action item gets ``status: done`` through the hub's Athena link
  (``PATCH /action-items/{id}``, the relay's own write, ``fork/athena/call_recorder.py``).

All are idempotent (already done: 200 with ``"already": true``). Each change is a
``completed`` action in the voice ledger (``fork/voice/ledger.py``) with ``source:
"device"`` and ``turn_id: "device:<device id>"`` (no voice turn made it). Undo reopens a
to-do item or an action item within :data:`UNDO_SECONDS` of a done by a device (its
ledger action is removed); later, or for one a device didn't close, 409. A device's dones
are kept in ``<DATA_DIR>/fork_voice.db`` (table ``device_done``) for a day, owner-scoped.

Errors: 400 for a malformed body, ref, id or title; 404 ``{"error": "not found"}``; 409
``{"error": "changed"}`` (the line the device showed is gone) or ``{"error": "too late to
undo"}``; 503 ``{"error": "athena unavailable"}`` when Athena can't be reached, 503
``{"error": "busy"}`` on a database error (locked, …; only its type is logged).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fork.common import sqlite as common_sqlite
from fork.common.owner import owner_key as _owner
from fork.voice import ledger

logger = logging.getLogger(__name__)

UNDO_SECONDS = 10 * 60
KEEP_SECONDS = 24 * 3600
ATHENA_TIMEOUT = 5.0  # seconds, per call to Athena
MAX_DEVICE_ID = 128
SOURCE = "device"
TODOS_WHERE = "To-dos"
ATHENA_WHERE = "Athena"

_ID = r"[A-Za-z0-9_-]{1,64}"
TODO_REF = re.compile(rf"^todo:({_ID}):(\d{{1,4}})$")
REMINDER_REF = re.compile(rf"^todo:({_ID})$")
ITEM_ID = re.compile(r"^(?:ai_)?(\d{1,12})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS device_done (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    target TEXT NOT NULL,
    device_id TEXT NOT NULL,
    at REAL NOT NULL,
    action_id TEXT,
    item_text TEXT,
    undone_at REAL
);
CREATE INDEX IF NOT EXISTS device_done_target ON device_done (owner, target, at);
"""


class Failure(Exception):
    """An answer other than 200: ``status`` and the ``error`` text."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


NOT_FOUND = Failure(404, "not found")
UNAVAILABLE = Failure(503, "athena unavailable")
TOO_LATE = Failure(409, "too late to undo")
CHANGED = Failure(409, "changed")
BUSY = Failure(503, "busy")
TITLE_CHARS = 200  # /today titles a line by its first 200 characters
MAX_TITLE_IN = 2000


# ------------------------------------------------------------------- device dones
def _connect(write: bool = False) -> AbstractContextManager[sqlite3.Connection]:
    return common_sqlite.connect(ledger.db_path(), SCHEMA, write)


def remember(owner: Any, target: str, device_id: str, *, action_id: str | None,
             item_text: str | None = None, now: float | None = None) -> None:
    """Keep a device's done of ``target`` (``todo:41:2``, ``action_item:17``) for undo."""
    now = time.time() if now is None else float(now)
    with _connect(write=True) as db:
        db.execute("DELETE FROM device_done WHERE at < ?", (now - KEEP_SECONDS,))
        db.execute("INSERT INTO device_done (id, owner, target, device_id, at, action_id,"
                   " item_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (uuid.uuid4().hex, _owner(owner), target, device_id, now, action_id,
                    item_text))


def last_done(owner: Any, target: str, now: float | None = None) -> dict | None:
    """This owner's newest device done of ``target`` within :data:`UNDO_SECONDS`."""
    now = time.time() if now is None else float(now)
    with _connect() as db:
        row = db.execute("SELECT * FROM device_done WHERE owner = ? AND target = ? AND at >= ?"
                         " ORDER BY at DESC, rowid DESC LIMIT 1",
                         (_owner(owner), target, now - UNDO_SECONDS)).fetchone()
    return dict(row) if row is not None else None


def mark_undone(row_id: str, now: float | None = None) -> None:
    now = time.time() if now is None else float(now)
    with _connect(write=True) as db:
        db.execute("UPDATE device_done SET undone_at = ? WHERE id = ? AND undone_at IS NULL",
                   (now, row_id))


def _record(owner: str, device_id: str, *, kind: str, title: str, where: str) -> str | None:
    """The ledger's ``completed`` action; its id (None when it couldn't be written: the
    change itself is done)."""
    try:
        return ledger.add_action(f"device:{device_id}", owner, action="completed", kind=kind,
                                 title=title, where=where, source=SOURCE)["id"]
    except Exception:  # noqa: BLE001
        logger.exception("voice done: recording the action failed")
        return None


def _unrecord(owner: str, action_id: str | None) -> None:
    if not action_id:
        return
    try:
        ledger.remove_action(owner, action_id)
    except Exception:  # noqa: BLE001
        logger.exception("voice done: removing the action failed")


# ------------------------------------------------------------------------ parsing
def parse_todo_ref(value: Any) -> tuple[str, int]:
    m = TODO_REF.match(str(value or "").strip()) if isinstance(value, str) else None
    if not m:
        raise Failure(400, "ref must be todo:<note id>:<item index>")
    return m.group(1), int(m.group(2))


def parse_reminder_ref(value: Any) -> str:
    m = REMINDER_REF.match(str(value or "").strip()) if isinstance(value, str) else None
    if not m:
        raise Failure(400, "ref must be todo:<note id> or todo:<note id>:<item index>")
    return m.group(1)


def parse_item_id(value: Any) -> int:
    if isinstance(value, bool):
        value = None
    if isinstance(value, int):
        value = str(value)
    m = ITEM_ID.match(value.strip()) if isinstance(value, str) else None
    if not m or int(m.group(1)) <= 0:
        raise Failure(400, "id must be an action item's id")
    return int(m.group(1))


def parse_title(value: Any) -> str:
    """The optional ``title`` of a done, as a line key; "" when not given."""
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > MAX_TITLE_IN:
        raise Failure(400, "title must be the row's text")
    return _line_key(value)


def parse_device_id(value: Any) -> str:
    device_id = " ".join(str(value).split()) if isinstance(value, str) else ""
    if not device_id or len(device_id) > MAX_DEVICE_ID:
        raise Failure(400, "device_id is required")
    return device_id


# ------------------------------------------------------------------------ notes
def _items(note: Any) -> list:
    try:
        items = json.loads(note.items) if note.items else []
    except (TypeError, ValueError):
        return []
    return items if isinstance(items, list) else []


def _item_text(item: Any) -> str:
    return str(item.get("text") or "").strip() if isinstance(item, dict) else ""


def _item_done(item: Any) -> bool:
    return isinstance(item, dict) and bool(item.get("done") or item.get("checked"))


def _line_key(text: Any) -> str:
    """A line's text as ``/today`` titles it (``fork/insight/today_candidates.py``):
    whitespace collapsed, its first :data:`TITLE_CHARS` characters; any case."""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:TITLE_CHARS].strip().casefold()


def _shown_line(items: list, index: int, key: str) -> int:
    """The line a device's done means: at ``index`` when it reads ``key`` (or no ``key``
    was given), else the line that reads it, an unticked one first, the nearest to
    ``index``. :data:`NOT_FOUND` / :data:`CHANGED` when there is none."""
    here = index < len(items) and isinstance(items[index], dict)
    if not key:
        if not here:
            raise NOT_FOUND
        return index
    if here and _line_key(_item_text(items[index])) == key:
        return index
    same = [i for i, it in enumerate(items)
            if isinstance(it, dict) and _line_key(_item_text(it)) == key]
    if not same:
        raise CHANGED
    same.sort(key=lambda i: (_item_done(items[i]), abs(i - index), i))
    return same[0]


# A device's read-change-write of a note's items is one at a time, so two Echos (or two
# quick taps) ticking two lines of one note don't overwrite each other's tick.
_NOTES_LOCK = threading.Lock()


@contextmanager
def _note(owner: str, note_id: str) -> Iterator[tuple[Any, Any]]:
    """``(db session, the owner's note)``; :data:`NOT_FOUND` when there is none."""
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        note = db.query(Note).filter(Note.id == note_id, Note.owner == owner).first()
        if note is None:
            raise NOT_FOUND
        yield db, note
    finally:
        db.close()


def _save_items(db: Any, note: Any, items: list) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    note.items = json.dumps(items)
    flag_modified(note, "items")
    db.commit()


def todo_done(owner: str, ref: Any, device_id: Any, *, kind: str = "todo",
              title: Any = None) -> dict:
    note_id, index = parse_todo_ref(ref)
    device_id = parse_device_id(device_id)
    key = parse_title(title)
    target = f"todo:{note_id}:{index}"
    with _NOTES_LOCK, _note(owner, note_id) as (db, note):
        items = _items(note)
        item = items[_shown_line(items, index, key)]
        title = _item_text(item)
        if _item_done(item):
            return {"ok": True, "already": True, "title": title}
        item["done"] = True
        _save_items(db, note, items)
        where = str(note.title or "").strip()[:80] or TODOS_WHERE
    action_id = _record(owner, device_id, kind=kind, title=title, where=where)
    remember(owner, target, device_id, action_id=action_id, item_text=title)
    return {"ok": True, "already": False, "title": title}


def _ticked_line(items: list, index: int, text: str) -> int:
    """The line a device ticked: at its index, else by its text (lines moved or were
    removed since); of equal lines, a ticked one. :data:`NOT_FOUND` when it's gone."""
    here = index < len(items) and isinstance(items[index], dict)
    if not text:
        if not here:
            raise NOT_FOUND
        return index
    same = [i for i, it in enumerate(items) if isinstance(it, dict) and _item_text(it) == text]
    if not same:
        raise NOT_FOUND
    ticked = [i for i in same if _item_done(items[i])]
    if ticked:
        return index if index in ticked else ticked[0]
    return index if index in same else same[0]


def todo_undo(owner: str, ref: Any, now: float | None = None) -> dict:
    note_id, index = parse_todo_ref(ref)
    target = f"todo:{note_id}:{index}"
    with _NOTES_LOCK, _note(owner, note_id) as (db, note):
        items = _items(note)
        done = last_done(owner, target, now)
        if done is None:
            if index >= len(items) or not isinstance(items[index], dict):
                raise NOT_FOUND
            raise TOO_LATE
        at = _ticked_line(items, index, done.get("item_text") or "")
        item = items[at]
        if not _item_done(item):
            mark_undone(done["id"], now)
            return {"ok": True, "already": True, "title": _item_text(item)}
        if done.get("undone_at") is not None:
            raise TOO_LATE  # ticked again since, not by a device
        item["done"] = False
        item.pop("checked", None)
        _save_items(db, note, items)
    mark_undone(done["id"], now)
    _unrecord(owner, done.get("action_id"))
    return {"ok": True, "already": False, "title": _item_text(item)}


def _note_title_key(owner: str, ref: str) -> str:
    note_id, _index = parse_todo_ref(ref)
    with _note(owner, note_id) as (_db, note):
        return _line_key(note.title)


def reminder_done(owner: str, ref: Any, device_id: Any, title: Any = None) -> dict:
    """``todo:<note id>`` archives the reminder's note; ``todo:<note id>:<index>`` (a ref
    naming one line) ticks that line, as :func:`todo_done` (``title`` guards its index)."""
    if isinstance(ref, str) and TODO_REF.match(ref.strip()):
        if parse_title(title) and parse_title(title) == _note_title_key(owner, ref):
            title = None  # a reminder card titled by its note, not the line: the index counts
        return todo_done(owner, ref, device_id, kind="reminder", title=title)
    note_id = parse_reminder_ref(ref)
    device_id = parse_device_id(device_id)
    with _NOTES_LOCK, _note(owner, note_id) as (db, note):
        title = str(note.title or "").strip() or "Reminder"
        if note.archived:
            return {"ok": True, "already": True, "title": title}
        note.archived = True
        db.commit()
    _record(owner, device_id, kind="reminder", title=title, where=TODOS_WHERE)
    return {"ok": True, "already": False, "title": title}


# ------------------------------------------------------------------- Athena
async def _athena(method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
    from fork.athena import call_recorder as rec

    try:
        return await asyncio.wait_for(rec.module_json(method, path, **kwargs), ATHENA_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 -- not configured, down, slow, refused
        logger.warning("voice done: Athena unavailable: %s", type(exc).__name__)
        raise UNAVAILABLE from None


async def _action_item(item_id: int) -> dict:
    """Athena's row of ``item_id`` (any status); :data:`NOT_FOUND` when it has none."""
    status, body = await _athena("GET", "/action-items",
                                 params={"status": "all", "include_notes": "true"})
    rows = body.get("action_items") if status == 200 and isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise UNAVAILABLE
    for row in rows:
        if isinstance(row, dict) and str(row.get("id")) == str(item_id):
            return row
    raise NOT_FOUND


async def _set_status(item_id: int, status: str) -> None:
    code, _body = await _athena("PATCH", f"/action-items/{item_id}", json={"status": status})
    if code == 404:
        raise NOT_FOUND
    if code != 200:
        raise UNAVAILABLE


def _item_title(row: dict) -> str:
    text = " ".join(str(row.get("title") or row.get("text") or "").split())
    return text[:200]


async def action_item_done(owner: str, item: Any, device_id: Any) -> dict:
    item_id = parse_item_id(item)
    device_id = parse_device_id(device_id)
    row = await _action_item(item_id)
    title = _item_title(row)
    if str(row.get("status") or "open") == "done":
        return {"ok": True, "already": True, "title": title}
    await _set_status(item_id, "done")
    action_id = await asyncio.to_thread(_record, owner, device_id, kind="action_item",
                                        title=title, where=ATHENA_WHERE)
    await asyncio.to_thread(remember, owner, f"action_item:{item_id}", device_id,
                            action_id=action_id, item_text=title)
    return {"ok": True, "already": False, "title": title}


async def action_item_undo(owner: str, item: Any, now: float | None = None) -> dict:
    item_id = parse_item_id(item)
    row = await _action_item(item_id)
    title = _item_title(row)
    done = await asyncio.to_thread(last_done, owner, f"action_item:{item_id}", now)
    if done is None:
        raise TOO_LATE
    if str(row.get("status") or "open") != "done":
        await asyncio.to_thread(mark_undone, done["id"], now)
        return {"ok": True, "already": True, "title": title}
    if done.get("undone_at") is not None:
        raise TOO_LATE  # done again since, not by a device
    await _set_status(item_id, "open")
    await asyncio.to_thread(mark_undone, done["id"], now)
    await asyncio.to_thread(_unrecord, owner, done.get("action_id"))
    return {"ok": True, "already": False, "title": title}


# --------------------------------------------------------------------------- routes
async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise Failure(400, "the body must be JSON") from None
    if not isinstance(body, dict):
        raise Failure(400, "the body must be a JSON object")
    return body


def _db_errors() -> tuple[type[BaseException], ...]:
    try:
        from sqlalchemy.exc import SQLAlchemyError
    except ImportError:  # pragma: no cover -- the hub always has SQLAlchemy
        return (sqlite3.Error,)
    return (sqlite3.Error, SQLAlchemyError)


_DB_ERRORS = _db_errors()


def setup_voice_done_routes() -> APIRouter:
    from fork.voice.routes import PREFIX, voice_owner

    router = APIRouter(tags=["fork-voice"])

    async def run(request: Request, work: Any) -> Any:
        owner = voice_owner(request)
        try:
            body = await _body(request)
            return await work(owner, body)
        except Failure as f:
            return JSONResponse(status_code=f.status, content={"error": f.error})
        except _DB_ERRORS as exc:  # locked, disk I/O, …: try again later
            logger.warning("voice done: database error %s", type(exc).__name__)
            return JSONResponse(status_code=BUSY.status, content={"error": BUSY.error})

    @router.post(PREFIX + "/todos/done")
    async def todos_done(request: Request) -> Any:
        return await run(request, lambda o, b: asyncio.to_thread(
            todo_done, o, b.get("ref"), b.get("device_id"), title=b.get("title")))

    @router.post(PREFIX + "/todos/undo")
    async def todos_undo(request: Request) -> Any:
        return await run(request, lambda o, b: asyncio.to_thread(todo_undo, o, b.get("ref")))

    @router.post(PREFIX + "/reminders/done")
    async def reminders_done(request: Request) -> Any:
        return await run(request, lambda o, b: asyncio.to_thread(
            reminder_done, o, b.get("ref"), b.get("device_id"), b.get("title")))

    @router.post(PREFIX + "/action-items/done")
    async def action_items_done(request: Request) -> Any:
        return await run(request, lambda o, b: action_item_done(o, b.get("id"),
                                                                b.get("device_id")))

    @router.post(PREFIX + "/action-items/undo")
    async def action_items_undo(request: Request) -> Any:
        return await run(request, lambda o, b: action_item_undo(o, b.get("id")))

    return router
