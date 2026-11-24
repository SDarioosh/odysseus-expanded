"""A note's checklist, for fork code: the one place the fork reads or writes it.

Upstream keeps a note as one markdown document (``Note.content``, from upstream
odysseus-dev/odysseus#7323): checklist items are task lines (``- [ ] milk``,
``- [x] eggs``) and images are ``![](url)`` lines, parsed by
``core/notes_markdown.py``. The ``items`` JSON column is legacy: the boot migration
folds it into ``content``, and a row it skipped is folded on read
(``effective_content``). Fork code never touches ``note.items`` itself; it goes
through these helpers, so the next change to notes is a change here.

Items are ``{"text", "done"}`` dicts (plus ``"indent"`` when a line is nested), in
document order: index ``n`` is the ``n``-th task line, the index upstream's
``toggle_item`` and the notes UI use.

A note counts as a checklist when its stored ``note_type`` says so (``checklist``,
``todo``, ``goal``: what the fork and ``manage_notes`` have always written), or when
upstream's ``derive_note_type`` calls its content a checklist (a pure task list: what
the notes window makes now, stored as ``note``).
"""

from __future__ import annotations

from typing import Any, Iterable

# Stored note types whose task lines are a to-do list (manage_notes, the notes UI and
# the Module Action Items task write these).
CHECKLIST_TYPES = frozenset({"checklist", "todo", "goal"})


def content_of(note: Any) -> str:
    """The note's markdown, with any legacy ``items`` the migration skipped folded in."""
    from core.notes_markdown import effective_content

    return effective_content(getattr(note, "content", None), getattr(note, "items", None), None)


def note_items(note: Any) -> list[dict]:
    """The note's checklist items in document order, ``[{"text", "done"[, "indent"]}]``."""
    from core.notes_markdown import parse_task_lines

    out = []
    for t in parse_task_lines(content_of(note)):
        item = {"text": t["text"], "done": t["done"]}
        if t["indent"]:
            item["indent"] = t["indent"]
        out.append(item)
    return out


def note_type(note: Any) -> str:
    """The note's type: its stored checklist type if it has one, else upstream's
    derived type (``checklist`` for a pure task list, ``note``, ``goal``, ``draw``)."""
    from core.notes_markdown import derive_note_type

    stored = getattr(note, "note_type", None) or "note"
    if stored in CHECKLIST_TYPES:
        return stored
    return derive_note_type(stored, content_of(note))


def is_checklist(note: Any, types: Iterable[str] = CHECKLIST_TYPES) -> bool:
    """True when the note is a to-do list (see the module docstring)."""
    return note_type(note) in set(types)


def prose_of(note: Any) -> str:
    """The note's text without its task lines and its standalone image lines (what
    ``content`` held before checklists and images moved into it). Inline images and
    images in fenced code stay: they are part of the text."""
    from core.notes_markdown import image_urls, remove_image_line, strip_task_lines

    text = strip_task_lines(content_of(note))
    for url in image_urls(text):
        text = remove_image_line(text, url)
    return text.strip()


def set_note_items(note: Any, items: list[dict]) -> None:
    """Write ``items`` as the note's task lines (the caller commits). Line ``n`` is
    rewritten from item ``n``; unchanged lines and all other text stay as they were;
    extra items are added after the last task line, lines past the list are dropped."""
    from core.notes_markdown import apply_items_to_content, fold_legacy_columns

    fold_legacy_columns(note)
    note.content = apply_items_to_content(note.content, items)
    note.items = None
