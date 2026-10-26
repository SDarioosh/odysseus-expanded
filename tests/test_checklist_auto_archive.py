import asyncio
import json
from datetime import timedelta

import pytest

import core.database as cdb
from core.database import Note, utcnow_naive
from src import checklist_auto_archive as caa
from tests.helpers.sqlite_db import make_temp_sqlite


@pytest.fixture
def db(monkeypatch):
    factory, engine, tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.delenv(caa.ENV, raising=False)
    yield factory
    engine.dispose()


def _items(*done):
    return json.dumps([{"text": f"Item {i}", "done": d} for i, d in enumerate(done)])


def _add(factory, id, minutes_ago=30, **kw):
    base = dict(id=id, owner="alice", title=id, note_type="checklist", items=_items(True, True),
                updated_at=utcnow_naive() - timedelta(minutes=minutes_ago))
    base.update(kw)
    s = factory()
    s.add(Note(**base))
    s.commit()
    s.close()


def _archived(factory):
    s = factory()
    try:
        return sorted(n.id for n in s.query(Note).filter(Note.archived == True))  # noqa: E712
    finally:
        s.close()


def test_finished_checklist_is_archived_after_the_wait(db):
    _add(db, "done-long-ago", minutes_ago=11)
    _add(db, "done-just-now", minutes_ago=2)
    assert caa.sweep() == 1
    assert _archived(db) == ["done-long-ago"]


def test_open_item_pinned_empty_and_plain_notes_stay(db):
    _add(db, "open", items=_items(True, False))
    _add(db, "pinned", pinned=True)
    _add(db, "pinned-empty", pinned=True, items=json.dumps([]))
    _add(db, "empty", items=json.dumps([]))
    _add(db, "no-items", items=None)
    _add(db, "plain", note_type="note", items=None, content="text")
    _add(db, "reminder", source="calendar", label="calendar")
    _add(db, "repeating", repeat="weekly")
    assert caa.sweep() == 0
    assert _archived(db) == []


def test_todo_and_goal_notes_count_as_checklists(db):
    _add(db, "todo", note_type="todo")
    _add(db, "goal", note_type="goal", items=json.dumps([{"text": "Run", "checked": True}]))
    assert caa.sweep() == 2


def test_unarchived_finished_list_is_archived_again_after_the_wait(db):
    _add(db, "groceries")
    caa.sweep()
    s = db()
    note = s.get(Note, "groceries")
    note.archived = False  # the owner unarchives it: updated_at moves to now
    s.commit()
    s.close()
    assert caa.sweep() == 0
    assert caa.sweep(now=utcnow_naive() + timedelta(minutes=11)) == 1
    assert _archived(db) == ["groceries"]


def test_unarchived_list_with_an_open_item_stays(db):
    _add(db, "groceries", archived=True, items=_items(True, False), minutes_ago=60)
    s = db()
    s.get(Note, "groceries").archived = False
    s.commit()
    s.close()
    assert caa.sweep(now=utcnow_naive() + timedelta(hours=1)) == 0


def test_setting(monkeypatch, db):
    _add(db, "done", minutes_ago=3)
    monkeypatch.setenv(caa.ENV, "2")
    assert caa.archive_minutes() == 2
    assert caa.sweep() == 1
    for off in ("0", "off"):
        monkeypatch.setenv(caa.ENV, off)
        assert caa.archive_minutes() is None
        assert caa.sweep() == 0
    monkeypatch.setenv(caa.ENV, "soon")
    assert caa.archive_minutes() == caa.DEFAULT_MINUTES


def test_start_is_off_when_turned_off(monkeypatch):
    monkeypatch.setenv(caa.ENV, "off")
    assert caa.start() is None


def test_loop_survives_a_failed_pass(monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("db locked")

    async def stop_after_first(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(caa, "sweep", boom)
    monkeypatch.setattr(caa.asyncio, "sleep", stop_after_first)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(caa._loop())
    assert calls == [1]


def test_is_empty_is_for_unpinned_checklists_without_items():
    def note(**kw):
        base = dict(note_type="todo", items="[]", pinned=False, archived=False,
                    repeat="none", source="agent", label="odysseus-sound")
        return Note(**{**base, **kw})

    assert caa.is_empty(note()) and caa.is_empty(note(items=None))
    for other in (note(items=_items(True)), note(pinned=True), note(archived=True),
                  note(note_type="note"), note(repeat="daily"),
                  note(source="calendar", label="calendar")):
        assert not caa.is_empty(other)
