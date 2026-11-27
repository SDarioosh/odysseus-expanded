"""A note's checklist through the fork's one seam (fork/common/note_items.py)."""

import json
from types import SimpleNamespace

from fork.common.note_items import (
    content_of, is_checklist, note_items, note_type, prose_of, set_note_items,
)


def _note(**kw):
    base = dict(id="n1", content=None, items=None, image_url=None, note_type="note")
    base.update(kw)
    return SimpleNamespace(**base)


def test_items_are_the_task_lines_in_order():
    n = _note(content="Shop\n\n- [ ] Milk\n* [x] Bread\n  - [ ] Rye\n")
    assert note_items(n) == [{"text": "Milk", "done": False}, {"text": "Bread", "done": True},
                             {"text": "Rye", "done": False, "indent": 1}]


def test_task_lines_in_fenced_code_are_not_items():
    n = _note(content="```\n- [ ] sample\n```\n- [ ] real\n")
    assert note_items(n) == [{"text": "real", "done": False}]


def test_a_row_the_migration_skipped_is_read_from_its_legacy_items():
    n = _note(note_type="checklist",
              items=json.dumps([{"text": "Milk", "done": False}, {"text": "Bread", "checked": True}]))
    assert note_items(n) == [{"text": "Milk", "done": False}, {"text": "Bread", "done": True}]
    assert "- [ ] Milk" in content_of(n)


def test_checklist_by_stored_type_or_by_content():
    assert is_checklist(_note(note_type="todo"))            # what the fork files
    assert is_checklist(_note(note_type="goal"))
    made_in_notes_window = _note(note_type="note", content="- [ ] Milk\n- [ ] Eggs\n")
    assert is_checklist(made_in_notes_window) and note_type(made_in_notes_window) == "checklist"
    mixed = _note(note_type="note", content="Weekend\n\n- [ ] Milk\n")
    assert not is_checklist(mixed) and note_type(mixed) == "note"
    assert not is_checklist(_note(note_type="todo"), ("checklist",))


def test_prose_leaves_out_task_and_image_lines():
    n = _note(content="Ideas for the shed\n\n- [ ] paint\n\n![](/api/upload/abc.png)\n")
    assert prose_of(n) == "Ideas for the shed"
    inline = _note(content="See ![](/api/upload/abc.png) here")
    assert prose_of(inline) == "See ![](/api/upload/abc.png) here"


def test_set_items_rewrites_only_task_lines_and_clears_legacy_items():
    n = _note(content="Shop\n\n- [ ] Milk\n- [ ] Bread\n")
    set_note_items(n, [{"text": "Milk", "done": True}, {"text": "Bread", "done": False},
                       {"text": "Eggs", "done": False}])
    assert n.content == "Shop\n\n- [x] Milk\n- [ ] Bread\n- [ ] Eggs\n"
    legacy = _note(content="Shop", items=json.dumps([{"text": "Milk", "done": False}]))
    set_note_items(legacy, [{"text": "Milk", "done": True}])
    assert legacy.items is None and note_items(legacy) == [{"text": "Milk", "done": True}]
