"""Fork: Athena action items on Odysseus' to-do lists and notes (``fork/athena/action_items.py``).

Named to-do lists ("add milk to my shopping list") and named notes ("add to my hardware
notes"): finding them by a loose name, adding, ticking and removing one line, and
reading the notes and events items were filed as.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fork.athena.action_items_state import LABEL, VOICE_LIST, _when_label, item_title

# ------------------------------------------------------------------------ notes
_NOTES_WORDS = re.compile(r"^(?:my|the|our)\s+|\s+notes?$", re.IGNORECASE)


def notes_key(name: Any) -> str:
    """"My Hardware notes" and "hardware" are the same notes."""
    key = re.sub(r"\s+", " ", str(name or "").strip().lower())
    for _ in range(2):
        key = _NOTES_WORDS.sub("", key).strip()
    return "" if key in {"note", "notes", "none"} else key


def notes_title(name: Any) -> str | None:
    """The title new notes get ("hardware" -> "Hardware notes"); None for no usable name."""
    key = notes_key(name)
    if not key or len(key) > 60:
        return None
    text = re.sub(r"\s+", " ", str(name).strip())
    for _ in range(2):
        text = _NOTES_WORDS.sub("", text).strip()
    return f"{text[:1].upper()}{text[1:]} notes"


def find_notes(owner: str, name: Any):
    """The owner's open note (type note) called ``name``, with or without "notes"."""
    from core.database import Note, SessionLocal

    key = notes_key(name)
    if not key:
        return None
    db = SessionLocal()
    try:
        q = db.query(Note).filter(Note.archived == False,  # noqa: E712
                                  Note.note_type == "note")
        if owner:
            q = q.filter(Note.owner == owner)
        for n in q.order_by(Note.updated_at.desc()).limit(500).all():
            if notes_key(n.title) == key:
                db.expunge(n)
                return n
        return None
    finally:
        db.close()


def _block_index(content: Any, block: str) -> int | None:
    lines = str(content or "").split("\n")
    want = _norm_line(block)
    return next((i for i, ln in enumerate(lines) if _norm_line(ln) == want), None)


async def _add_to_notes(owner: str, item: dict, text: str) -> dict:
    """Add ``item`` as a line to its named notes (made if missing); the entry's ``ref``
    (note id), ``block`` (the line) and ``notes`` (title). The same line isn't added
    twice."""
    from src.tools.notes import do_manage_notes

    name = notes_title(item.get("list_name"))
    block = f"- {text}"
    note = find_notes(owner, name)
    if note is None:
        res = await do_manage_notes(json.dumps({
            "action": "add", "note_type": "note", "title": name, "label": LABEL,
            "content": block}), owner=owner or None)
        if res.get("error") or not res.get("note_id"):
            raise RuntimeError(f"notes: {res.get('error') or 'no note created'}")
        return {"ref": res["note_id"], "block": block, "notes": name}
    if _block_index(note.content, block) is None:
        content = str(note.content or "").rstrip()
        res = await do_manage_notes(json.dumps({
            "action": "update", "id": note.id,
            "content": f"{content}\n{block}" if content else block}), owner=owner or None)
        if res.get("error"):
            raise RuntimeError(f"notes: {res['error']}")
    return {"ref": note.id, "block": block, "notes": (note.title or name).strip()}


async def _remove_block(owner: str, ref: str, block: str) -> None:
    """Take one line out of a note, keeping the rest as it is."""
    from src.tools.notes import do_manage_notes

    note = _note(owner, ref)
    if note is None:
        return
    lines = str(note.content or "").split("\n")
    i = _block_index(note.content, block)
    if i is None:
        return
    lines.pop(i)
    res = await do_manage_notes(json.dumps({"action": "update", "id": ref,
                                            "content": "\n".join(lines)}), owner=owner or None)
    if res.get("error"):
        raise RuntimeError(f"notes: {res['error']}")


# ------------------------------------------------------------------ named to-do lists
LIST_TYPES = ("checklist", "todo")
_LIST_WORDS = re.compile(r"^(?:my|the|our)\s+|\s+(?:to-?do\s+|to\s+do\s+)?(?:list|to-?dos)$",
                         re.IGNORECASE)


def list_key(name: Any) -> str:
    """"My Shopping list" and "shopping" are the same list."""
    key = re.sub(r"\s+", " ", str(name or "").strip().lower())
    for _ in range(2):
        key = _LIST_WORDS.sub("", key).strip()
    return "" if key in {"list", "to-do", "todo", "to do", "to-dos", "todos"} else key


def list_title(name: Any) -> str | None:
    """The title a new list gets ("shopping" -> "Shopping"); None for no usable name."""
    text = re.sub(r"\s+", " ", str(name or "").strip())
    if not list_key(text) or len(text) > 60 or text.lower() == "none":
        return None
    return text[:1].upper() + text[1:]


def _lists(owner: str) -> list:
    """The owner's open checklists and to-do notes, most recently changed first."""
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(Note).filter(Note.archived == False,  # noqa: E712
                                  Note.note_type.in_(LIST_TYPES))
        if owner:
            q = q.filter(Note.owner == owner)
        notes = q.order_by(Note.updated_at.desc()).limit(500).all()
        for n in notes:
            db.expunge(n)
        return notes
    finally:
        db.close()


def find_list(owner: str, name: Any):
    """The to-do list called ``name`` (any case, with or without "list"), or None."""
    key = list_key(name)
    return next((n for n in _lists(owner) if key and list_key(n.title) == key), None)


def todo_lists(owner: str) -> list[str]:
    """Titles of the owner's to-do lists, for the page's picker. A to-do of its own (its
    only line is its title, as filed for an item without a list) isn't a list."""
    out, seen = [], set()
    for n in _lists(owner):
        key, items = list_key(n.title), _note_items(n)
        # a filed dated to-do's title is the short title, its line the full text
        single = len(items) <= 1 and (
            all(_norm_line(i.get("text")) == _norm_line(n.title) for i in items)
            or (n.label == LABEL and len(items) == 1 and (
                bool(n.due_date)
                or _norm_line(n.title) == _norm_line(item_title(items[0])))))
        if key and key not in seen and not single:
            seen.add(key)
            out.append(n.title.strip())
    return sorted(out, key=str.lower)


def _norm_line(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def _raw_items(note) -> list:
    """A list's lines exactly as stored (also lines that aren't objects), so changing one
    line writes the others back unchanged."""
    try:
        items = json.loads(note.items or "[]")
    except (TypeError, ValueError):
        return []
    return items if isinstance(items, list) else []


def _ticked(item: Any) -> bool:
    return isinstance(item, dict) and bool(item.get("done") or item.get("checked"))


def _line_index(items: list, line: str) -> int | None:
    """The line's index: an open one with that text first, else a ticked one."""
    want = _norm_line(line)
    same = [i for i, it in enumerate(items)
            if isinstance(it, dict) and _norm_line(it.get("text")) == want]
    return next((i for i in same if not _ticked(items[i])), same[0] if same else None)


def _line_text(item: dict, text: str) -> str:
    when = _when_label(item)
    return f"{text} (due {when})" if when else text


async def _add_to_list(owner: str, item: dict, text: str) -> dict:
    """Add ``item`` as a line to its named list (made if missing); the entry's
    ``ref`` (note id), ``line`` and ``list`` (title). An open line with the same text
    is reused, not repeated."""
    from src.tools.notes import do_manage_notes

    name = list_title(item.get("list_name"))
    line = _line_text(item, text)
    note = find_list(owner, name)
    if note is None:
        res = await do_manage_notes(json.dumps({
            "action": "add", "note_type": "todo", "title": name, "label": LABEL,
            "checklist_items": [{"text": line, "done": False}]}), owner=owner or None)
        if res.get("error") or not res.get("note_id"):
            raise RuntimeError(f"to-dos: {res.get('error') or 'no list created'}")
        return {"ref": res["note_id"], "line": line, "list": name}
    items = _raw_items(note)
    i = _line_index(items, line)
    if i is None or _ticked(items[i]):
        res = await do_manage_notes(json.dumps({
            "action": "update", "id": note.id,
            "checklist_items": items + [{"text": line, "done": False}]}), owner=owner or None)
        if res.get("error"):
            raise RuntimeError(f"to-dos: {res['error']}")
    return {"ref": note.id, "line": line, "list": (note.title or name).strip()}


async def _change_line(owner: str, ref: str, line: str, *, done: bool | None = None,
                       remove: bool = False) -> bool:
    """Tick / untick or remove one line of a list, keeping the others as they are."""
    from src.tools.notes import do_manage_notes

    note = _note(owner, ref)
    if note is None:
        return False
    items = _raw_items(note)
    i = _line_index(items, line)
    if i is None:
        return False
    if remove:
        items.pop(i)
    else:
        items[i] = {**items[i], "done": bool(done)}
        items[i].pop("checked", None)
    res = await do_manage_notes(json.dumps({"action": "update", "id": ref,
                                            "checklist_items": items}), owner=owner or None)
    if res.get("error"):
        raise RuntimeError(f"to-dos: {res['error']}")
    if remove and not items and list_key(note.title) == list_key(VOICE_LIST):
        await _archive_if_empty(owner, ref)
    return True


async def _archive_if_empty(owner: str, ref: str) -> None:
    """The "Voice notes" list whose last line was taken off is archived (unless pinned);
    the next undated task starts a new one. Finished lists wait for
    src/checklist_auto_archive.py; an empty one would stay forever."""
    from fork.hub.checklist_auto_archive import is_empty
    from src.tools.notes import do_manage_notes

    note = _note(owner, ref)
    if note is None or not is_empty(note):
        return
    res = await do_manage_notes(json.dumps({"action": "update", "id": ref, "archived": True}),
                                owner=owner or None)
    if res.get("error"):
        raise RuntimeError(f"to-dos: {res['error']}")


def _note(owner: str, ref: str):
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(Note).filter(Note.id == ref)
        if owner:
            q = q.filter(Note.owner == owner)
        note = q.first()
        if note is not None:
            db.expunge(note)
        return note
    finally:
        db.close()


def _event(owner: str, ref: str):
    from core.database import CalendarCal, CalendarEvent, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(CalendarEvent).join(CalendarCal).filter(CalendarEvent.uid == ref)
        if owner:
            q = q.filter(CalendarCal.owner == owner)
        ev = q.first()
        if ev is not None:
            db.expunge(ev)
        return ev
    finally:
        db.close()


def _note_items(note) -> list[dict]:
    try:
        items = json.loads(note.items or "[]")
    except (TypeError, ValueError):
        return []
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
