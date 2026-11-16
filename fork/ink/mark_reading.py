"""Fork: read what the owner wrote by hand on a PDF datasheet (each **ink** mark,
``fork/ink/pdf_marks.py``) with the hub's local vision model, so the Marks panel and
Search marks show and find it. Plan: ``docs/fork/datasheets.md``.

Rules:

* Only ink marks are read (handwriting on a PDF page); highlights, underlines and marks
  that are only a circle or an arrow (kinds ``circle``, ``arrow``) have no writing. A
  mark is read from a **synthetic page** (:func:`mark_page`): only its writing's pen
  strokes (:func:`mark_strokes`: not an arrow drawn with it), never the highlighter or
  the PDF, moved so its box (padded
  ``MARK_PAD``) starts at 0 and scaled up (at most ``MARK_MAX_SCALE``) to fill a page
  ``PAGE_WIDTH`` wide, through ``fork/ink/transcribe.py`` (``read_page`` with
  ``MARK_HINT``: a mark may be only a shape).
* A mark is **settled** when its latest stroke is ``MARK_SETTLE_MS`` old, or when the
  note had no save for that long (a tablet whose clock is ahead): the owner may still be
  writing it until then.
* Readings are kept in the sidecar, note level::

      "mark_readings": {<mark id>: {"hash", "text", "status", "model", "at", "error"?}}

  ``hash`` is :func:`mark_hash` of the strokes read, ``status`` ``read`` | ``failed`` |
  ``reviewed`` (the owner's correction, :func:`correct`), ``at`` ms. A reading is
  **current** when its hash is the mark's hash now; a mark without a current reading is
  read again (also a reviewed one whose strokes changed). A failed reading is tried again
  at most every ``FAILED_RETRY_MS``. Entries of marks that are gone are dropped at the
  next run. ``fork/ink/notes.py`` keeps the field across saves.
* :func:`schedule` (after each save of a PDF note, and from ``GET …/marks`` while
  something is unread) starts, or restarts, one run per note: it sleeps until the newest
  unread mark is settled (at most ``MARK_SETTLE_MS`` + 1 s), then reads each settled mark
  without a current reading, one at a time, writing each result as it comes. A save
  during a run makes the run look again when it ends; never two runs of one note.
* What a mark shows (:func:`view`): ``written`` (the current read or reviewed text, else
  ``""``), ``reading`` (``none`` for highlight/underline, ``waiting``, ``pending`` while
  the model reads it, ``read``, ``failed`` (with ``reading_error``), ``reviewed``).

Writes hold the owner's lock of ``fork/ink/notes.py``; nothing leaves the hub (the model
is the local one ``transcribe.default_reader`` picks).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

from fork.ink import notes as ink
from fork.ink import transcribe as tr

logger = logging.getLogger(__name__)

MARK_SETTLE_MS = 20_000      # the owner may still be writing a mark this recent
MARK_PAD = 16                # page units of paper around a mark on its synthetic page
MARK_MAX_SCALE = 4.0         # a small mark is drawn at most this much larger
MARK_MIN_HEIGHT = 120        # the synthetic page's height, page units
MARK_MAX_HEIGHT = 4000
MAX_WRITTEN = 2000           # characters kept of a mark's text (model or owner)
FAILED_RETRY_MS = 10 * 60 * 1000
NO_MODEL = "No vision model is set"
MARK_HINT = ("This is a short note written on a printed page. If it is only a shape (a "
             'circle, an arrow, a line, a tick) and no words, answer {"text": ""}.')
STATUSES = ("pending", "read", "failed", "reviewed")


def now_ms() -> int:
    """The clock (tests replace it)."""
    return int(time.time() * 1000)


# ----------------------------------------------------------------------- pure
def mark_hash(strokes: list[dict[str, Any]]) -> str:
    """Stable hash of a mark's strokes: their ids and points, in order."""
    raw = json.dumps([[s.get("id"), s.get("points") or []] for s in strokes],
                     separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mark_strokes(page: dict[str, Any], mark: dict[str, Any]) -> list[dict[str, Any]]:
    """The mark's pen strokes the model reads on ``page``, in the page's order: its
    writing (``_writing``, without an arrow's strokes, ``fork/ink/pdf_marks.py``), else
    all its strokes; never the highlighter."""
    writing = mark.get("_writing")
    ids = set(writing if isinstance(writing, list) else mark.get("strokes") or [])
    return [s for s in page.get("strokes") or []
            if isinstance(s, dict) and s.get("id") in ids
            and not (s.get("pen") or {}).get("highlighter")]


def mark_page(mark: dict[str, Any], strokes: list[dict[str, Any]]) -> dict[str, Any]:
    """The synthetic page a mark is read from: ``strokes`` moved so the mark's box,
    padded by ``MARK_PAD``, starts at 0, and scaled by ``s = min(MARK_MAX_SCALE,
    PAGE_WIDTH / padded width, MARK_MAX_HEIGHT / padded height)`` (points and pen width),
    on a page ``PAGE_WIDTH`` wide and as tall as the scaled box (at least
    ``MARK_MIN_HEIGHT``). The box is the writing's (``_writing_box``, without an arrow)
    when the mark has one. Pure."""
    x, y, w, h = (float(v) for v in mark.get("_writing_box") or mark["box"])
    x0, y0 = x - MARK_PAD, y - MARK_PAD
    pw, ph = max(1.0, w + 2 * MARK_PAD), max(1.0, h + 2 * MARK_PAD)
    s = min(MARK_MAX_SCALE, ink.PAGE_WIDTH / pw, MARK_MAX_HEIGHT / ph)
    height = int(min(MARK_MAX_HEIGHT, max(MARK_MIN_HEIGHT, round(ph * s))))
    out = []
    for st in strokes:
        pen = dict(st.get("pen") or {})
        pen["width"] = round(float(pen.get("width") or 1) * s, 2)
        pen["highlighter"] = False
        out.append({"id": st.get("id"), "pen": pen, "at": st.get("at", 0),
                    "points": [[round((float(p[0]) - x0) * s, 1), round((float(p[1]) - y0) * s, 1),
                                p[2] if len(p) > 2 else 0] for p in st.get("points") or []]})
    return {"id": str(mark.get("id") or "mark"), "width": ink.PAGE_WIDTH, "height": height,
            "strokes": out}


def settled(mark_at: int, now: int, last_save: int | None = None) -> bool:
    """Whether the owner is done with a mark (see the module docstring)."""
    if now - int(mark_at or 0) >= MARK_SETTLE_MS:
        return True
    return last_save is not None and now - last_save >= MARK_SETTLE_MS


def current(mark: dict[str, Any], reading: Any) -> bool:
    """Whether ``reading`` is for the mark's strokes as they are now."""
    return (isinstance(reading, dict) and bool(mark.get("_hash"))
            and reading.get("hash") == mark.get("_hash"))


def needs_reading(mark: dict[str, Any], reading: Any, now: int) -> bool:
    """An ink mark the model should read (now or once settled): no current reading, or a
    current failed one older than ``FAILED_RETRY_MS``."""
    if mark.get("kind") != "ink":
        return False
    if not current(mark, reading):
        return True
    return (reading.get("status") == "failed"
            and now - int(reading.get("at") or 0) >= FAILED_RETRY_MS)


def view(mark: dict[str, Any], reading: Any, *, reading_now: bool = False) -> dict[str, Any]:
    """The mark as answered: a copy with ``written`` and ``reading`` (and
    ``reading_error``, the stored error, for a failed reading), without the internal ``_hash``. ``reading_now``: the model is
    reading this mark at this moment."""
    out = {k: v for k, v in mark.items() if not k.startswith("_")}
    out["written"], out["reading"] = "", "none"
    if mark.get("kind") != "ink":
        return out
    status = reading.get("status") if current(mark, reading) else None
    if status == "reviewed" or (status == "read" and not reading_now):
        out["reading"], out["written"] = status, str(reading.get("text") or "")
    elif reading_now:
        out["reading"] = "pending"
    elif status == "failed":
        out["reading"], out["reading_error"] = "failed", str(reading.get("error") or "")
    else:
        out["reading"] = "waiting"
    return out


def readings_of(stored: dict[str, Any] | None) -> dict[str, Any]:
    r = (stored or {}).get("mark_readings")
    return r if isinstance(r, dict) else {}


# ------------------------------------------------------------- run state
# Per note id (note ids are unique across owners): the run's task, whether it is reading,
# whether a save came during the run, the mark being read, and the last save (ms).
_TASKS: dict[str, asyncio.Task] = {}
_BUSY: set[str] = set()
_AGAIN: set[str] = set()
_NOW_READING: dict[str, str] = {}
_LAST_SAVE: dict[str, int] = {}


def is_active(note_id: str) -> bool:
    """A run of the note is scheduled or reading."""
    task = _TASKS.get(note_id)
    return task is not None and not task.done()


def reading_now(note_id: str) -> str | None:
    return _NOW_READING.get(note_id)


def merged(mark: dict[str, Any], stored: dict[str, Any] | None, note_id: str) -> dict[str, Any]:
    """:func:`view` of a mark with the note's stored reading."""
    return view(mark, readings_of(stored).get(mark.get("id")),
                reading_now=_NOW_READING.get(note_id) == mark.get("id"))


def schedule(owner: str | None, note_id: str, *, saved: bool = False) -> None:
    """Start (or restart) the note's reading run on this event loop (debounced: a run
    still waiting for its marks to settle starts over; a run that is reading looks again
    when it ends). ``saved``: called after a save (marks the note's last save)."""
    now = now_ms()
    if saved or note_id not in _LAST_SAVE:
        _LAST_SAVE[note_id] = now
    task = _TASKS.get(note_id)
    if task is not None and not task.done():
        if note_id in _BUSY:
            _AGAIN.add(note_id)
            return
        task.cancel()
    loop = asyncio.get_running_loop()
    task = loop.create_task(_run(owner, note_id))
    _TASKS[note_id] = task

    def done(t: asyncio.Task, note_id: str = note_id) -> None:
        if _TASKS.get(note_id) is t:
            _TASKS.pop(note_id, None)

    task.add_done_callback(done)


def _snapshot(owner: str | None, note_id: str) -> tuple[str | None, list[dict[str, Any]],
                                                       dict[str, Any]] | None:
    """``(store, ink marks with their strokes, readings)`` of the owner's PDF note, after
    dropping readings of marks that are gone. ``None`` when it isn't one."""
    from fork.ink import pdf_marks

    if not ink.valid_note_id(note_id) or not ink.existing_note_ids(owner, [note_id]):
        return None
    store = ink._store_owner(owner, note_id)
    with ink._lock(store):
        stored = ink.load_sidecar(store, note_id)
        if not ink.is_pdf_note(stored):
            return None
        marks = pdf_marks._stored_marks(owner, note_id, stored)
        readings = readings_of(stored)
        alive = {m["id"] for m in marks if m.get("kind") == "ink"}
        gone = [k for k in readings if k not in alive]
        if gone:
            for k in gone:
                readings.pop(k, None)
            stored["mark_readings"] = readings
            ink._write_json(ink.sidecar_path(store, note_id), stored)
        pages = {p.get("id"): p for p in stored.get("pages") or [] if isinstance(p, dict)}
        out = []
        for m in marks:
            if m.get("kind") == "ink":
                page = pages.get(m["page_id"]) or {}
                out.append({**m, "_strokes": mark_strokes(page, m)})
    return store, out, dict(readings)


async def _run(owner: str | None, note_id: str) -> None:
    reader: tuple[str, Any] | None = None
    no_model = False
    try:
        while True:
            snap = await asyncio.to_thread(_snapshot, owner, note_id)
            if snap is None:
                return
            store, marks, readings = snap
            now = now_ms()
            todo = [m for m in marks if needs_reading(m, readings.get(m["id"]), now)]
            if not todo:
                return
            last_save = _LAST_SAVE.get(note_id)
            ready = [m for m in todo if settled(m["at"], now, last_save)]
            if not ready:
                # Sleep until the newest unread mark (or the last save) is settled.
                newest = max(int(m["at"] or 0) for m in todo)
                wait = min(MARK_SETTLE_MS - (now - newest),
                           MARK_SETTLE_MS - (now - (last_save or now)))
                await asyncio.sleep(max(0.05, min(MARK_SETTLE_MS + 1000, wait + 50) / 1000))
                continue
            _BUSY.add(note_id)
            _AGAIN.discard(note_id)
            try:
                for mark in ready:
                    if reader is None and not no_model:
                        try:
                            reader = tr.default_reader(owner)
                        except tr.NoModel:
                            no_model = True
                    _NOW_READING[note_id] = mark["id"]
                    try:
                        if no_model:
                            text, error, model = None, NO_MODEL, None
                        else:
                            text, error, model = await _read(mark, reader)
                    finally:
                        _NOW_READING.pop(note_id, None)
                    await asyncio.to_thread(_store, store, note_id, mark, text, error, model)
            finally:
                _BUSY.discard(note_id)
            if note_id not in _AGAIN and len(ready) == len(todo):
                return  # everything read; a later save schedules again
            _AGAIN.discard(note_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- the next save or GET schedules again
        logger.warning("ink marks: reading run failed (%s)", type(exc).__name__)


async def _read(mark: dict[str, Any], reader: tuple[str, Any]
                ) -> tuple[str | None, str | None, str | None]:
    model, read = reader
    page = mark_page(mark, mark["_strokes"])
    try:
        text = await tr.read_page(page, read, hint=MARK_HINT)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- stored as failed, shown to the owner
        logger.warning("ink marks: a mark could not be read (%s)", type(exc).__name__)
        detail = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
        return None, f"The model could not read this note: {str(detail)[:200]}", model
    model = getattr(read, "answered_by", None) or model
    return tr.clean_text(text).strip()[:MAX_WRITTEN], None, model


def _store(store: str | None, note_id: str, mark: dict[str, Any], text: str | None,
           error: str | None, model: str | None) -> None:
    """Write one mark's reading, unless the owner corrected the same strokes meanwhile."""
    with ink._lock(store):
        stored = ink.load_sidecar(store, note_id)
        if not ink.is_pdf_note(stored):
            return
        readings = readings_of(stored)
        old = readings.get(mark["id"])
        if (isinstance(old, dict) and old.get("status") == "reviewed"
                and old.get("hash") == mark["_hash"]):
            return
        entry: dict[str, Any] = {"hash": mark["_hash"], "text": text or "",
                                 "status": "failed" if text is None else "read",
                                 "model": model, "at": now_ms()}
        if text is None:
            entry["error"] = error or "The model could not read this note"
        readings[mark["id"]] = entry
        stored["mark_readings"] = readings
        ink._write_json(ink.sidecar_path(store, note_id), stored)


# ------------------------------------------------------------- the owner
def correct(owner: str | None, note_id: str, mark_id: str, written: Any) -> dict[str, Any]:
    """The owner's text for an ink mark: stored ``reviewed`` for its strokes as they are
    now. Returns the merged mark. Raises ``InkError``: 404 not the owner's note or no
    such mark, 400 not an ink mark or ``written`` not text."""
    from fork.ink import pdf_marks

    if not isinstance(written, str):
        raise ink.InkError("written must be text")
    if len(written) > MAX_WRITTEN:
        raise ink.InkError(f"at most {MAX_WRITTEN} characters")
    if not ink.valid_note_id(note_id) or not ink.existing_note_ids(owner, [note_id]):
        raise ink.InkError("Note not found", 404)
    store = ink._store_owner(owner, note_id)
    with ink._lock(store):
        stored = ink.load_sidecar(store, note_id)
        if stored is None:
            raise ink.InkError("Note not found", 404)
        mark = next((m for m in pdf_marks._stored_marks(owner, note_id, stored)
                     if m["id"] == mark_id), None)
        if mark is None:
            raise ink.InkError("Mark not found", 404)
        if mark.get("kind") != "ink":
            raise ink.InkError("Only a handwritten mark has writing to correct")
        readings = readings_of(stored)
        readings[mark_id] = {"hash": mark["_hash"], "text": tr.clean_text(written).strip()[:MAX_WRITTEN],
                             "status": "reviewed", "model": None, "at": now_ms()}
        stored["mark_readings"] = readings
        ink._write_json(ink.sidecar_path(store, note_id), stored)
    return view(mark, readings[mark_id])


async def wait_idle(timeout: float = 10.0) -> None:
    """Wait for this loop's runs to finish (tests)."""
    loop = asyncio.get_running_loop()
    mine = [t for t in list(_TASKS.values()) if t.get_loop() is loop]
    if mine:
        await asyncio.wait(mine, timeout=timeout)
