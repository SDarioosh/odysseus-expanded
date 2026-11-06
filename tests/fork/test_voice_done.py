"""Done buttons on a voice device (contract C5, ``fork/voice/done.py``). Fictional data:
the notes database is a temp SQLite file, Athena is a fake ``module_json``."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fork.voice import done as vd
from fork.voice import ledger
from fork.voice.routes import PREFIX
from tests.fork.brain_fakes import notes_db

DEVICE = "echo-kitchen-1"


@pytest.fixture
def db(monkeypatch, data_dir):
    return notes_db(monkeypatch)


def add_note(factory, note_id, *, owner="alice", title="Home", items=None,
             due="2026-11-06T09:00", archived=False, note_type="checklist"):
    from core.database import Note

    s = factory()
    try:
        s.add(Note(id=note_id, owner=owner, title=title, note_type=note_type,
                   items=json.dumps(items) if items is not None else None,
                   due_date=due, archived=archived))
        s.commit()
    finally:
        s.close()


def get_note(factory, note_id):
    from core.database import Note

    s = factory()
    try:
        n = s.query(Note).filter(Note.id == note_id).first()
        return {"items": json.loads(n.items) if n.items else None, "archived": n.archived}
    finally:
        s.close()


ITEMS = [{"text": "Buy bread", "done": False}, {"text": "Call the plumber", "done": False},
         {"text": "Water the ferns", "done": True}]


class FakeAthena:
    def __init__(self):
        self.rows = {17: {"id": 17, "title": "Send Sam the photos", "status": "open"},
                     18: {"id": 18, "text": "Book the hall", "status": "done"}}
        self.down = False
        self.calls = []

    async def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if self.down:
            from fork.athena.call_recorder import RecorderError
            raise RecorderError(503, "Athena isn't set up")
        if method == "GET":
            return 200, {"action_items": list(self.rows.values())}
        item_id = int(path.rsplit("/", 1)[1])
        if item_id not in self.rows:
            return 404, {"detail": "action item not found"}
        self.rows[item_id]["status"] = kw["json"]["status"]
        return 200, {"ok": True}


@pytest.fixture
def athena(monkeypatch):
    from fork.athena import call_recorder as rec

    fake = FakeAthena()
    monkeypatch.setattr(rec, "module_json", fake)
    return fake


def client(owner="alice"):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        request.state.api_token_owner = request.headers.get("x-test-owner", owner)
        request.state.api_token_scopes = scopes.split(",") if scopes else []
        return await call_next(request)

    app.include_router(vd.setup_voice_done_routes())
    return TestClient(app)


V = {"x-test-scopes": "voice"}


def post(c, path, body, **headers):
    return c.post(PREFIX + path, json=body, headers={**V, **headers})


# ------------------------------------------------------------------- to-dos
def test_todo_done_ticks_the_item_and_records_it(db):
    add_note(db, "n41", items=ITEMS)
    r = post(client(), "/todos/done", {"ref": "todo:n41:1", "device_id": DEVICE})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "already": False, "title": "Call the plumber"}
    assert [i["done"] for i in get_note(db, "n41")["items"]] == [False, True, True]
    acts = ledger.actions("alice")
    assert len(acts) == 1
    a = acts[0]
    assert (a["action"], a["kind"], a["title"], a["where"], a["source"], a["turn_id"]) == (
        "completed", "todo", "Call the plumber", "Home", "device", f"device:{DEVICE}")


def test_todo_done_is_idempotent(db):
    add_note(db, "n41", items=ITEMS)
    c = client()
    post(c, "/todos/done", {"ref": "todo:n41:1", "device_id": DEVICE})
    r = post(c, "/todos/done", {"ref": "todo:n41:1", "device_id": DEVICE})
    assert r.status_code == 200 and r.json()["already"] is True
    r = post(c, "/todos/done", {"ref": "todo:n41:2", "device_id": DEVICE})
    assert r.json() == {"ok": True, "already": True, "title": "Water the ferns"}
    assert len(ledger.actions("alice")) == 1


@pytest.mark.parametrize("body", [
    {"ref": "todo:n41", "device_id": DEVICE},
    {"ref": "note:n41:1", "device_id": DEVICE},
    {"ref": "todo:n41:x", "device_id": DEVICE},
    {"ref": 41, "device_id": DEVICE},
    {"device_id": DEVICE},
    {"ref": "todo:n41:1"},
    {"ref": "todo:n41:1", "device_id": "   "},
    {"ref": "todo:n41:1", "device_id": "x" * 200},
])
def test_todo_done_malformed_is_400(db, body):
    add_note(db, "n41", items=ITEMS)
    r = post(client(), "/todos/done", body)
    assert r.status_code == 400 and "error" in r.json()
    assert get_note(db, "n41")["items"] == ITEMS


def test_todo_done_body_must_be_a_json_object(db):
    c = client()
    r = c.post(PREFIX + "/todos/done", content=b"not json", headers=V)
    assert r.status_code == 400
    assert post(c, "/todos/done", ["todo:n41:1"]).status_code == 400


def test_todo_done_unknown_is_404(db):
    add_note(db, "n41", items=ITEMS)
    add_note(db, "plain", items=None, note_type="note")
    c = client()
    for ref in ("todo:nope:0", "todo:n41:3", "todo:plain:0"):
        r = post(c, "/todos/done", {"ref": ref, "device_id": DEVICE})
        assert r.status_code == 404 and r.json() == {"error": "not found"}


def test_owner_isolation(db):
    add_note(db, "n41", owner="bob", items=ITEMS)
    c = client()
    r = post(c, "/todos/done", {"ref": "todo:n41:0", "device_id": DEVICE})
    assert r.status_code == 404
    assert post(c, "/reminders/done", {"ref": "todo:n41", "device_id": DEVICE}).status_code == 404
    assert get_note(db, "n41") == {"items": ITEMS, "archived": False}
    # bob ticks it on his device; alice can't undo bob's done
    r = post(c, "/todos/done", {"ref": "todo:n41:0", "device_id": DEVICE}, **{"x-test-owner": "bob"})
    assert r.status_code == 200
    add_note(db, "a41", items=ITEMS)
    assert post(c, "/todos/undo", {"ref": "todo:a41:0"}).status_code == 409
    assert ledger.actions("alice") == [] and len(ledger.actions("bob")) == 1


def test_scope_voice_required(db):
    c = client()
    body = {"ref": "todo:n41:1", "device_id": DEVICE}
    for path in ("/todos/done", "/todos/undo", "/reminders/done", "/action-items/done",
                 "/action-items/undo"):
        assert c.post(PREFIX + path, json=body).status_code == 403
        assert c.post(PREFIX + path, json=body,
                      headers={"x-test-scopes": "todos:write"}).status_code == 403


def test_todo_undo_reopens_within_the_window(db):
    add_note(db, "n41", items=ITEMS)
    c = client()
    post(c, "/todos/done", {"ref": "todo:n41:1", "device_id": DEVICE})
    r = post(c, "/todos/undo", {"ref": "todo:n41:1"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "already": False, "title": "Call the plumber"}
    assert get_note(db, "n41")["items"][1]["done"] is False
    assert ledger.actions("alice") == []  # the completed action is gone
    r = post(c, "/todos/undo", {"ref": "todo:n41:1"})
    assert r.status_code == 200 and r.json()["already"] is True


def test_todo_undo_after_ten_minutes_is_409(db):
    add_note(db, "n41", items=ITEMS)
    assert vd.todo_done("alice", "todo:n41:1", DEVICE)["already"] is False
    later = time.time() + vd.UNDO_SECONDS + 1
    with pytest.raises(vd.Failure) as e:
        vd.todo_undo("alice", "todo:n41:1", now=later)
    assert e.value.status == 409
    assert get_note(db, "n41")["items"][1]["done"] is True
    assert vd.todo_undo("alice", "todo:n41:1",
                        now=time.time() + vd.UNDO_SECONDS - 5)["already"] is False


def test_todo_undo_of_a_tick_not_made_by_a_device_is_409(db):
    add_note(db, "n41", items=ITEMS)
    c = client()
    r = post(c, "/todos/undo", {"ref": "todo:n41:2"})  # ticked on the notes page
    assert r.status_code == 409 and r.json() == {"error": "too late to undo"}
    assert post(c, "/todos/undo", {"ref": "todo:nope:0"}).status_code == 404
    assert post(c, "/todos/undo", {"ref": "bad"}).status_code == 400


def test_todo_undo_follows_a_moved_line(db):
    from core.database import Note

    add_note(db, "n41", items=ITEMS)
    vd.todo_done("alice", "todo:n41:1", DEVICE)
    s = db()
    n = s.query(Note).filter(Note.id == "n41").first()
    items = json.loads(n.items)
    n.items = json.dumps([{"text": "New first line", "done": False}] + items)
    s.commit()
    s.close()
    assert vd.todo_undo("alice", "todo:n41:1")["already"] is False
    assert [i["done"] for i in get_note(db, "n41")["items"]] == [False, False, False, True]


def test_todo_undo_after_a_ticked_again_elsewhere_is_409(db):
    add_note(db, "n41", items=ITEMS)
    vd.todo_done("alice", "todo:n41:1", DEVICE)
    vd.todo_undo("alice", "todo:n41:1")
    from core.database import Note
    s = db()
    n = s.query(Note).filter(Note.id == "n41").first()
    items = json.loads(n.items)
    items[1]["done"] = True
    n.items = json.dumps(items)
    s.commit()
    s.close()
    with pytest.raises(vd.Failure) as e:
        vd.todo_undo("alice", "todo:n41:1")
    assert e.value.status == 409


# ------------------------------------------------------------------- reminders
def test_reminder_done_archives_the_note(db):
    add_note(db, "r7", title="Take the bins out", items=None, note_type="note")
    c = client()
    r = post(c, "/reminders/done", {"ref": "todo:r7", "device_id": DEVICE})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "already": False, "title": "Take the bins out"}
    assert get_note(db, "r7")["archived"] is True
    a = ledger.actions("alice")[0]
    assert (a["action"], a["kind"], a["source"]) == ("completed", "reminder", "device")
    r = post(c, "/reminders/done", {"ref": "todo:r7", "device_id": DEVICE})
    assert r.status_code == 200 and r.json()["already"] is True
    assert len(ledger.actions("alice")) == 1


def test_reminder_done_errors(db):
    c = client()
    assert post(c, "/reminders/done", {"ref": "todo:r7:0", "device_id": DEVICE}).status_code == 400
    assert post(c, "/reminders/done", {"ref": "todo:r7"}).status_code == 400
    r = post(c, "/reminders/done", {"ref": "todo:nope", "device_id": DEVICE})
    assert r.status_code == 404 and r.json() == {"error": "not found"}


# ------------------------------------------------------------------- action items
def test_action_item_done_through_athena(data_dir, athena):
    c = client()
    r = post(c, "/action-items/done", {"id": "17", "device_id": DEVICE})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "already": False, "title": "Send Sam the photos"}
    assert athena.rows[17]["status"] == "done"
    assert ("PATCH", "/action-items/17", {"json": {"status": "done"}}) in athena.calls
    a = ledger.actions("alice")[0]
    assert (a["action"], a["kind"], a["where"], a["source"]) == (
        "completed", "action_item", "Athena", "device")
    # idempotent; "ai_17" and a number are read too
    for item in ("ai_17", 17):
        r = post(c, "/action-items/done", {"id": item, "device_id": DEVICE})
        assert r.status_code == 200 and r.json()["already"] is True
    r = post(c, "/action-items/done", {"id": 18, "device_id": DEVICE})
    assert r.json() == {"ok": True, "already": True, "title": "Book the hall"}
    assert len(ledger.actions("alice")) == 1


def test_action_item_errors(data_dir, athena):
    c = client()
    for item in ("x17", "", True, -3, None, "0"):
        assert post(c, "/action-items/done", {"id": item, "device_id": DEVICE}).status_code == 400
    assert post(c, "/action-items/done", {"id": 17}).status_code == 400
    r = post(c, "/action-items/done", {"id": 99, "device_id": DEVICE})
    assert r.status_code == 404 and r.json() == {"error": "not found"}
    athena.down = True
    r = post(c, "/action-items/done", {"id": 17, "device_id": DEVICE})
    assert r.status_code == 503 and r.json() == {"error": "athena unavailable"}
    assert post(c, "/action-items/undo", {"id": 17}).status_code == 503


def test_action_item_slow_athena_is_503(data_dir, monkeypatch):
    from fork.athena import call_recorder as rec

    async def slow(method, path, **kw):
        await asyncio.sleep(1)
        return 200, {"action_items": []}

    monkeypatch.setattr(rec, "module_json", slow)
    monkeypatch.setattr(vd, "ATHENA_TIMEOUT", 0.05)
    r = post(client(), "/action-items/done", {"id": 17, "device_id": DEVICE})
    assert r.status_code == 503


def test_action_item_undo(data_dir, athena):
    c = client()
    assert post(c, "/action-items/undo", {"id": 18}).status_code == 409  # not by a device
    post(c, "/action-items/done", {"id": 17, "device_id": DEVICE})
    # another owner can't undo alice's done
    assert post(c, "/action-items/undo", {"id": 17}, **{"x-test-owner": "bob"}).status_code == 409
    r = post(c, "/action-items/undo", {"id": "ai_17"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "already": False, "title": "Send Sam the photos"}
    assert athena.rows[17]["status"] == "open"
    assert ledger.actions("alice") == []
    assert post(c, "/action-items/undo", {"id": 17}).json()["already"] is True
    assert post(c, "/action-items/undo", {"id": 99}).status_code == 404


def test_action_item_undo_after_ten_minutes_is_409(data_dir, athena):
    asyncio.run(vd.action_item_done("alice", 17, DEVICE))
    with pytest.raises(vd.Failure) as e:
        asyncio.run(vd.action_item_undo("alice", 17, now=time.time() + vd.UNDO_SECONDS + 1))
    assert e.value.status == 409
    assert athena.rows[17]["status"] == "done"


# ------------------------------------------------------------------- wiring
def test_voice_router_serves_done_routes(db):
    from fork.voice.routes import setup_voice_routes

    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.api_token = True
        request.state.api_token_owner = "alice"
        request.state.api_token_scopes = ["voice"]
        return await call_next(request)

    app.include_router(setup_voice_routes())
    add_note(db, "n41", items=ITEMS)
    r = TestClient(app).post(PREFIX + "/todos/done", json={"ref": "todo:n41:0",
                                                           "device_id": DEVICE})
    assert r.status_code == 200 and r.json()["title"] == "Buy bread"


def test_remove_action_is_owner_scoped(data_dir):
    a = ledger.add_action("t", "alice", action="completed", kind="todo", title="X",
                          where="To-dos")
    assert ledger.remove_action("bob", a["id"]) is False
    assert ledger.remove_action("alice", a["id"]) is True
    assert ledger.actions("alice") == []
