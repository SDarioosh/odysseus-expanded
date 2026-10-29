"""Fork: notebooks for handwritten notes (the Write view). Plan: docs/fork/handwritten-notes.md
("Notebooks").

A notebook is a named, ordered group of handwritten notes. Each note in it is one of its
**pages** (a note can still hold more than one sheet, as before); a handwritten note in
no notebook is a **quick note**. Every page stays an ordinary handwritten note
(``src/ink_notes.py``), so transcription, review and Send to Athena work per page, and a
page written on another day reaches Athena with its own date.

Notebooks live in one file per owner, ``<ink folder>/_notebooks.json`` (the leading underscore keeps it
out of the sidecar sweep: no note id starts with one), written under the
owner's ink lock::

    {"version": 1,
     "notebooks": [{"id": str, "name": str, "created_at": str, "updated_at": str,
                    "pages": [note id, ...]}]}   # in page order

A note is in at most one notebook. Ids of notes that no longer exist are dropped when
the notebooks are read (:func:`prune`). Deleting a notebook never deletes its pages:
they become quick notes.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src import ink_notes as ink

VERSION = 1
MAX_NAME = 120
MAX_NOTEBOOKS = 200
MAX_PAGES = 2000  # notes per notebook (each note has its own page limit)
_NB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,63}$")


def valid_notebook_id(notebook_id: Any) -> bool:
    return isinstance(notebook_id, str) and bool(_NB_ID_RE.match(notebook_id))


def _path(owner: str | None) -> Path:
    return ink.ink_dir(owner) / "_notebooks.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read(owner: str | None) -> list[dict[str, Any]]:
    try:
        data = json.loads(_path(owner).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    books = data.get("notebooks") if isinstance(data, dict) else None
    out = []
    seen_notes: set[str] = set()
    for b in books if isinstance(books, list) else []:
        if not isinstance(b, dict) or not valid_notebook_id(b.get("id")):
            continue
        pages = []
        for p in b.get("pages") or []:
            if ink.valid_note_id(p) and p not in seen_notes:  # one notebook per note
                seen_notes.add(p)
                pages.append(p)
        out.append({"id": b["id"], "name": str(b.get("name") or "")[:MAX_NAME],
                    "created_at": b.get("created_at"), "updated_at": b.get("updated_at"),
                    "pages": pages})
    return out


def _write(owner: str | None, books: list[dict[str, Any]]) -> None:
    ink._write_json(_path(owner), {"version": VERSION, "notebooks": books})


def _clean_name(name: Any) -> str:
    if not isinstance(name, str):
        raise ink.InkError("name must be a string")
    name = " ".join(name.split())[:MAX_NAME]
    if not name:
        raise ink.InkError("A notebook needs a name")
    return name


def _find(books: list[dict[str, Any]], notebook_id: str) -> dict[str, Any]:
    for b in books:
        if b["id"] == notebook_id:
            return b
    raise ink.InkError("Notebook not found", 404)


def _detach(books: list[dict[str, Any]], note_id: str) -> bool:
    changed = False
    for b in books:
        if note_id in b["pages"]:
            b["pages"].remove(note_id)
            b["updated_at"] = _now_iso()
            changed = True
    return changed


def _insert(book: dict[str, Any], note_id: str, after: Any) -> None:
    if len(book["pages"]) >= MAX_PAGES:
        raise ink.InkError(f"A notebook has at most {MAX_PAGES} pages", 413)
    pages = book["pages"]
    if after == "":  # at the front
        pages.insert(0, note_id)
    elif isinstance(after, str) and after in pages:
        pages.insert(pages.index(after) + 1, note_id)
    else:
        pages.append(note_id)
    book["updated_at"] = _now_iso()


def _public(book: dict[str, Any]) -> dict[str, Any]:
    return {"id": book["id"], "name": book["name"], "created_at": book["created_at"],
            "updated_at": book["updated_at"], "pages": list(book["pages"])}


# ------------------------------------------------------------------- reading
def prune(owner: str | None, alive: Iterable[str]) -> list[dict[str, Any]]:
    """The owner's notebooks, oldest first, keeping only pages in ``alive`` (the owner's
    handwritten notes); dropped ids are written back."""
    alive = set(alive)
    with ink._lock(owner):
        books = _read(owner)
        changed = False
        for b in books:
            kept = [p for p in b["pages"] if p in alive]
            if kept != b["pages"]:
                b["pages"] = kept
                changed = True
        if changed:
            _write(owner, books)
        return [_public(b) for b in books]


def notebook_of(owner: str | None, note_id: str) -> str | None:
    """The id of the notebook that holds this note, or ``None`` (a quick note)."""
    with ink._lock(owner):
        for b in _read(owner):
            if note_id in b["pages"]:
                return b["id"]
    return None


def page_map(owner: str | None) -> dict[str, str]:
    """note id -> notebook id, for every page of the owner's notebooks."""
    with ink._lock(owner):
        return {p: b["id"] for b in _read(owner) for p in b["pages"]}


# ------------------------------------------------------------------- changes
def create(owner: str | None, name: Any, notebook_id: Any = None) -> dict[str, Any]:
    """A new, empty notebook. ``notebook_id`` (optional, made by the client) makes a
    repeated request answer the same notebook instead of a second one."""
    name = _clean_name(name)
    if notebook_id is not None and not valid_notebook_id(notebook_id):
        raise ink.InkError("Invalid notebook id")
    with ink._lock(owner):
        books = _read(owner)
        if notebook_id is not None:
            for b in books:
                if b["id"] == notebook_id:
                    return _public(b)
        if len(books) >= MAX_NOTEBOOKS:
            raise ink.InkError(f"At most {MAX_NOTEBOOKS} notebooks", 413)
        now = _now_iso()
        book = {"id": notebook_id or str(uuid.uuid4()), "name": name, "created_at": now,
                "updated_at": now, "pages": []}
        books.append(book)
        _write(owner, books)
        return _public(book)


def rename(owner: str | None, notebook_id: str, name: Any) -> dict[str, Any]:
    name = _clean_name(name)
    with ink._lock(owner):
        books = _read(owner)
        book = _find(books, notebook_id)
        book["name"] = name
        book["updated_at"] = _now_iso()
        _write(owner, books)
        return _public(book)


def delete(owner: str | None, notebook_id: str) -> list[str]:
    """Remove the notebook; its pages become quick notes. Returns their ids."""
    with ink._lock(owner):
        books = _read(owner)
        book = _find(books, notebook_id)
        books.remove(book)
        _write(owner, books)
        return list(book["pages"])


def move(owner: str | None, note_id: str, notebook_id: str | None, after: Any = None
         ) -> str | None:
    """Put the note in ``notebook_id`` (after the page ``after``, ``""`` at the front,
    else at the end), or make it a quick note (``None``). The caller checks the note is
    the owner's. Returns the note's notebook id."""
    if notebook_id is not None and not valid_notebook_id(notebook_id):
        raise ink.InkError("Notebook not found", 404)
    with ink._lock(owner):
        books = _read(owner)
        target = _find(books, notebook_id) if notebook_id is not None else None
        if after == note_id:
            after = None
        _detach(books, note_id)
        if target is not None:
            _insert(target, note_id, after)
        _write(owner, books)
        return notebook_id


def place_new(owner: str | None, note_id: str, notebook_id: Any, after: Any = None) -> str | None:
    """A note's first save asked to be a page of ``notebook_id`` (made on the device,
    maybe offline). A notebook that is gone meanwhile leaves it a quick note; a note
    already in a notebook stays where it is. Never raises for a bad request: the note
    is saved either way."""
    if not valid_notebook_id(notebook_id):
        return None
    with ink._lock(owner):
        books = _read(owner)
        for b in books:
            if note_id in b["pages"]:
                return b["id"]
        try:
            target = _find(books, notebook_id)
            _insert(target, note_id, after)
        except ink.InkError:
            return None
        _write(owner, books)
        return notebook_id


def forget(owner: str | None, note_id: str) -> None:
    """The note was deleted: drop it from its notebook."""
    with ink._lock(owner):
        books = _read(owner)
        if _detach(books, note_id):
            _write(owner, books)


def list_notebooks(owner: str | None) -> list[dict[str, Any]]:
    """The owner's notebooks, oldest first, without pages whose note is gone (archived
    notes stay: they come back when unarchived)."""
    with ink._lock(owner):
        ids = [p for b in _read(owner) for p in b["pages"]]
    return prune(owner, ink.existing_note_ids(owner, ids))
