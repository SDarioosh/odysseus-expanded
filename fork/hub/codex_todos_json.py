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

import json
from datetime import datetime
from typing import Any, Iterable, Optional


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _items(raw: Any) -> list[dict[str, Any]]:
    """The note's checklist items as ``[{"text", "done"}]``, in stored order.

    The order matters: ``toggle_item`` addresses items by index. Items that
    can't be read are kept as empty text, so the indexes still line up.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict):
            text = item.get("text")
            done = bool(item.get("done") or item.get("checked") or item.get("complete"))
        else:
            text, done = item, False
        out.append({"text": text if isinstance(text, str) else "", "done": done})
    return out


# Note types whose items are a checklist. ``manage_notes`` (and the notes UI) treat all
# three the same; "todo" is what the Module Action Items task files.
CHECKLIST_TYPES = frozenset({"checklist", "todo", "goal"})


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
        checklist = (getattr(n, "note_type", None) or "note") in CHECKLIST_TYPES
        out.append({
            "id": n.id,
            "title": getattr(n, "title", None) or "",
            "type": "checklist" if checklist else "note",
            "label": getattr(n, "label", None) or None,
            "pinned": bool(getattr(n, "pinned", False)),
            "due_date": getattr(n, "due_date", None) or None,
            "updated_at": _iso(getattr(n, "updated_at", None)),
            "items": _items(getattr(n, "items", None)) if checklist else [],
            "content": None if checklist else (getattr(n, "content", None) or ""),
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
