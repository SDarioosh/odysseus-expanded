"""Structured notes for API-token clients (``GET /api/codex/todos?format=json``).

The Codex todo route answers with the chat-model text of ``manage_notes``:
one line per note, without checklist items. That suits an LLM, but a device
client (Athena's T5 PDA, through its phone relay) needs the same data
the notes UI shows: ids, titles, items with their done flags, due dates. The
UI's own ``/api/notes`` routes refuse API tokens on purpose, so this gives
token clients a read-only, structured view behind the existing
``todos:read`` scope instead. Writes still go through ``toggle_item`` and the
other ``manage_notes`` actions of ``POST /api/codex/todos``.

Same selection as ``manage_notes`` ``list``: the owner's notes (every note
when there is no owner, single-user mode), not archived unless asked, an
optional label, calendar-reminder notes left out, pinned first, then the most
recently updated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Optional

from fork.common.note_items import CHECKLIST_TYPES, is_checklist


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _items(note: Any) -> list[dict[str, Any]]:
    """The note's checklist items as ``[{"text", "done"}]``, in document order.

    The order matters: ``toggle_item`` addresses items by index (the n-th task line of
    the note's markdown, fork/common/note_items.py).
    """
    from fork.common.note_items import note_items

    return [{"text": i["text"], "done": i["done"]} for i in note_items(note)]


# Note types whose items are a checklist. ``manage_notes`` (and the notes UI) treat all
# three the same; "todo" is what the Module Action Items task files. A checklist made
# in the notes window is stored as "note" (fork/common/note_items.py). CHECKLIST_TYPES
# is imported from there above and kept as this module's name for its importers.
__all__ = ["CHECKLIST_TYPES", "list_notes_json", "notes_to_json"]


def _revision(note: Any) -> Optional[str]:
    """The note's body revision (upstream's ``core/note_writes.py``), which
    ``toggle_item`` and ``update`` check: send it back to tick an item."""
    try:
        from core.note_writes import note_body_revision

        return note_body_revision(note)
    except (AttributeError, TypeError, ValueError):
        return None


def fill_toggle_revision(args: dict[str, Any], owner: Optional[str]) -> None:
    """Hook for ``POST /api/codex/todos`` (``routes/codex_routes.py``).

    Upstream's ``toggle_item`` now refuses a call without the note's current
    ``body_revision``. Athena's T5 PDA relays (phone and PC) toggle by note id and item
    index with an explicit ``done`` and no revision, as the hub asked before. For such
    a call the current revision is filled in, so the PDA keeps working: the same
    index-based tick it always made. A call that sends a revision, or no explicit
    ``done`` (a flip, which a stale index could turn the wrong way), is left as it is.
    The relays can send the ``revision`` this feed now gives each note.
    """
    if args.get("action") != "toggle_item" or "body_revision" in args:
        return
    if not isinstance(args.get("done"), bool):
        return
    note_id = str(args.get("id") or args.get("note_id") or args.get("noteId") or "").strip()
    if not note_id:
        return
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(Note).filter(Note.id == note_id)
        if owner:
            q = q.filter(Note.owner == owner)
        note = q.first()
        if note is not None:
            args["body_revision"] = _revision(note)
    finally:
        db.close()


def _is_calendar_reminder(note: Any) -> bool:
    return getattr(note, "source", None) == "calendar" and getattr(note, "label", None) == "calendar"


def _sort_key(note: Any) -> tuple:
    updated = getattr(note, "updated_at", None)
    stamp = updated.timestamp() if isinstance(updated, datetime) else 0.0
    return (0 if getattr(note, "pinned", False) else 1, -stamp)


def notes_to_json(notes: Iterable[Any]) -> dict[str, Any]:
    """Serialize notes (already selected) in the documented shape and order."""
    out = []
    for n in sorted((n for n in notes if not _is_calendar_reminder(n)), key=_sort_key):
        checklist = is_checklist(n)
        out.append({
            "id": n.id,
            "title": getattr(n, "title", None) or "",
            "type": "checklist" if checklist else "note",
            "label": getattr(n, "label", None) or None,
            "pinned": bool(getattr(n, "pinned", False)),
            "due_date": getattr(n, "due_date", None) or None,
            "updated_at": _iso(getattr(n, "updated_at", None)),
            "items": _items(n) if checklist else [],
            "content": None if checklist else (getattr(n, "content", None) or ""),
            "revision": _revision(n),
        })
    return {"notes": out}


def list_notes_json(owner: Optional[str], archived: bool = False, label: Optional[str] = None) -> dict[str, Any]:
    """The owner's notes as structured data (see the module docstring)."""
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(Note)
        if owner is not None:
            q = q.filter(Note.owner == owner)
        label_filter = str(label or "").strip()
        if label_filter and label_filter.lower() != "default":
            q = q.filter(Note.label == label_filter)
        q = q.filter(Note.archived == bool(archived))
        return notes_to_json(q.all())
    finally:
        db.close()
