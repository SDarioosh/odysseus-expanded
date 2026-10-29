import json
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

from src import codex_todos_json
from src.codex_todos_json import notes_to_json


def _note(id, **kw):
    base = dict(title="", content=None, items=None, note_type="note", label=None, pinned=False,
                archived=False, due_date=None, source="user", owner="alice",
                updated_at=datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc))
    base.update(kw)
    return SimpleNamespace(id=id, **base)


def test_checklist_items_keep_their_order_and_done_flags():
    n = _note("n1", title="Groceries", note_type="checklist",
              items=json.dumps([{"text": "Milk", "done": False}, {"text": "Bread", "checked": True},
                                {"text": "Eggs"}]))
    out = notes_to_json([n])["notes"][0]
    assert out["type"] == "checklist"
    assert out["items"] == [{"text": "Milk", "done": False}, {"text": "Bread", "done": True},
                            {"text": "Eggs", "done": False}]
    assert out["content"] is None


def test_todo_and_goal_notes_are_checklists():
    for note_type in ("todo", "goal"):
        n = _note("t", note_type=note_type, content="From a voice note",
                  items=json.dumps([{"text": "Call Jason", "done": False}]))
        out = notes_to_json([n])["notes"][0]
        assert out["type"] == "checklist", note_type
        assert out["items"] == [{"text": "Call Jason", "done": False}]


def test_plain_note_has_content_and_no_items():
    out = notes_to_json([_note("n2", title="Idea", content="Call the bank")])["notes"][0]
    assert out["type"] == "note"
    assert out["items"] == []
    assert out["content"] == "Call the bank"


def test_pinned_first_then_most_recently_updated():
    old = _note("old", updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    new = _note("new", updated_at=datetime(2026, 10, 19, tzinfo=timezone.utc))
    pinned = _note("pin", pinned=True, updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    ids = [n["id"] for n in notes_to_json([old, new, pinned])["notes"]]
    assert ids == ["pin", "new", "old"]


def test_calendar_reminder_notes_are_left_out():
    reminder = _note("r", source="calendar", label="calendar")
    calendar_label_only = _note("c", label="calendar")
    ids = [n["id"] for n in notes_to_json([reminder, calendar_label_only])["notes"]]
    assert ids == ["c"]


def test_unreadable_items_keep_indexes_aligned():
    n = _note("n3", note_type="checklist", items=json.dumps(["Plain string", {"text": 5}, {"text": "Ok"}]))
    assert notes_to_json([n])["notes"][0]["items"] == [
        {"text": "Plain string", "done": False}, {"text": "", "done": False}, {"text": "Ok", "done": False}]
    broken = _note("n4", note_type="checklist", items="{not json")
    assert notes_to_json([broken])["notes"][0]["items"] == []


def test_fields_and_empty_values():
    n = _note("n5", title=None, label="", due_date="2026-10-23", note_type="checklist", items="[]")
    out = notes_to_json([n])["notes"][0]
    assert out["title"] == ""
    assert out["label"] is None
    assert out["due_date"] == "2026-10-23"
    assert out["updated_at"].startswith("2026-10-20T09:00:00")


class _Col:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return (self.name, other)


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []

    def filter(self, cond):
        self.filters.append(cond)
        return self

    def all(self):
        rows = self.rows
        for name, value in self.filters:
            rows = [r for r in rows if getattr(r, name) == value]
        return rows


def _install_db(monkeypatch, rows):
    query = _Query(rows)
    db = SimpleNamespace(query=lambda model: query, close=lambda: None)
    fake = types.ModuleType("core.database")
    fake.SessionLocal = lambda: db
    fake.Note = SimpleNamespace(owner=_Col("owner"), label=_Col("label"), archived=_Col("archived"))
    monkeypatch.setitem(sys.modules, "core.database", fake)
    return query


def test_list_is_scoped_to_the_owner_and_not_archived(monkeypatch):
    rows = [_note("mine"), _note("theirs", owner="bob"), _note("archived", archived=True)]
    _install_db(monkeypatch, rows)
    ids = [n["id"] for n in codex_todos_json.list_notes_json("alice")["notes"]]
    assert ids == ["mine"]


def test_list_label_filter_and_archived(monkeypatch):
    rows = [_note("a", label="T5"), _note("b", label="home"), _note("c", label="T5", archived=True)]
    _install_db(monkeypatch, rows)
    assert [n["id"] for n in codex_todos_json.list_notes_json("alice", label="T5")["notes"]] == ["a"]
    _install_db(monkeypatch, rows)
    assert [n["id"] for n in codex_todos_json.list_notes_json("alice", archived=True)["notes"]] == ["c"]
    _install_db(monkeypatch, rows)
    assert len(codex_todos_json.list_notes_json("alice", label="default")["notes"]) == 2


def _client(monkeypatch, scopes, rows):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.base import BaseHTTPMiddleware

    from routes.codex_routes import setup_codex_routes

    _install_db(monkeypatch, rows)
    app = FastAPI()

    async def as_token(request, call_next):
        request.state.api_token = True
        request.state.api_token_scopes = scopes
        request.state.api_token_owner = "alice"
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=as_token)
    app.include_router(setup_codex_routes())
    return TestClient(app)


def test_route_returns_structured_notes_for_a_todos_read_token(monkeypatch):
    rows = [_note("n1", title="Groceries", note_type="checklist", items=json.dumps([{"text": "Milk", "done": True}])),
            _note("bob", owner="bob")]
    r = _client(monkeypatch, ["calendar:read", "todos:read"], rows).get("/api/codex/todos?format=json")
    assert r.status_code == 200
    assert r.json() == {"notes": [{
        "id": "n1", "title": "Groceries", "type": "checklist", "label": None, "pinned": False,
        "due_date": None, "updated_at": "2026-10-20T09:00:00+00:00",
        "items": [{"text": "Milk", "done": True}], "content": None}]}


def test_route_needs_a_todos_scope(monkeypatch):
    r = _client(monkeypatch, ["calendar:read", "calendar:write"], []).get("/api/codex/todos?format=json")
    assert r.status_code == 403
