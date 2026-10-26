"""Fork: finished checklists archive themselves.

A checklist whose items are all done stays on the notes page, and on Athena's
T5 PDA (whose Tasks tab shows the owner's checklists through
``GET /api/codex/todos?format=json``), until someone archives it by hand. Now, about
once a minute, every checklist that

* is not pinned (a pinned list stays, finished or empty),
* has at least one item, and every item is done, and
* hasn't changed for ``FORK_CHECKLIST_ARCHIVE_MINUTES`` (default 10)

is archived. Archived notes leave the T5 feed, so the list goes from the T5 at its next
sync. The wait leaves time to undo: unticking an item makes the list unfinished again.
After that, the list is in the notes archive and can be unarchived; unarchiving is a
change, so the list then waits another full period and is archived again unless it has
an open item by then.

Checklist means note types ``checklist``, ``todo`` and ``goal`` (the same as
``src/codex_todos_json.py``). Calendar-reminder notes and repeating notes are left
alone. ``FORK_CHECKLIST_ARCHIVE_MINUTES=0`` (or ``off``) turns it off.

Hook: ``app.py`` startup, next to the background-job monitor.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

ENV = "FORK_CHECKLIST_ARCHIVE_MINUTES"
DEFAULT_MINUTES = 10.0
POLL_SECONDS = 60

_task: Optional[asyncio.Task] = None


def archive_minutes() -> Optional[float]:
    """Minutes a finished checklist waits, or ``None`` when turned off."""
    raw = os.environ.get(ENV, "").strip().lower()
    if not raw:
        return DEFAULT_MINUTES
    if raw in {"off", "no", "false", "none"}:
        return None
    try:
        minutes = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", ENV, raw, DEFAULT_MINUTES)
        return DEFAULT_MINUTES
    return minutes if minutes > 0 else None


def is_finished(note: Any) -> bool:
    """True for an unpinned checklist with at least one item, all of them done."""
    from src.codex_todos_json import CHECKLIST_TYPES, _is_calendar_reminder, _items

    if getattr(note, "pinned", False) or getattr(note, "archived", False):
        return False
    if (getattr(note, "note_type", None) or "note") not in CHECKLIST_TYPES:
        return False
    if _is_calendar_reminder(note):
        return False
    if (getattr(note, "repeat", None) or "none") != "none":
        return False
    items = _items(getattr(note, "items", None))
    return bool(items) and all(item["done"] for item in items)


def is_empty(note: Any) -> bool:
    """True for an unpinned checklist with no items left (``is_finished``'s other half).
    The sweep leaves empty checklists alone (a new, still empty list is fine); callers
    that just took a list's last line off use this to archive it then
    (src/module_action_items.py, the "Voice notes" list)."""
    from src.codex_todos_json import CHECKLIST_TYPES, _is_calendar_reminder, _items

    if getattr(note, "pinned", False) or getattr(note, "archived", False):
        return False
    if (getattr(note, "note_type", None) or "note") not in CHECKLIST_TYPES:
        return False
    if _is_calendar_reminder(note):
        return False
    if (getattr(note, "repeat", None) or "none") != "none":
        return False
    return not _items(getattr(note, "items", None))


def sweep(now: Optional[datetime] = None, minutes: Optional[float] = None) -> int:
    """Archive the finished checklists unchanged for ``minutes``. Returns how many."""
    from core.database import Note, SessionLocal, utcnow_naive
    from src.codex_todos_json import CHECKLIST_TYPES

    if minutes is None:
        minutes = archive_minutes()
        if minutes is None:
            return 0
    cutoff = (now or utcnow_naive()) - timedelta(minutes=minutes)
    db = SessionLocal()
    try:
        candidates = (
            db.query(Note)
            .filter(Note.archived == False)  # noqa: E712 (SQL comparison)
            .filter(Note.pinned != True)  # noqa: E712
            .filter(Note.note_type.in_(sorted(CHECKLIST_TYPES)))
            .filter(Note.updated_at <= cutoff)
            .all()
        )
        done = [n for n in candidates if is_finished(n)]
        for note in done:
            note.archived = True
        if done:
            db.commit()
            logger.info("Archived %d finished checklist(s)", len(done))
        return len(done)
    finally:
        db.close()


async def _loop() -> None:
    while True:
        try:
            await asyncio.to_thread(sweep)
        except Exception as exc:  # keep going: the next pass retries
            logger.warning("Checklist auto-archive pass failed: %s", exc)
        await asyncio.sleep(POLL_SECONDS)


def start() -> Optional[asyncio.Task]:
    """Start the loop (idempotent). ``None`` when turned off."""
    global _task
    if archive_minutes() is None:
        logger.info("Checklist auto-archive is off (%s)", ENV)
        return None
    if _task and not _task.done():
        return _task
    _task = asyncio.create_task(_loop())
    return _task
