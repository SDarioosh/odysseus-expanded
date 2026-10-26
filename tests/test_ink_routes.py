"""Fork: handwritten notes, H1 routes (routes/ink_routes.py).

The routes run on a temporary notes database and upload store, with a stand-in for the
auth middleware (signed-in user or API token). Fictional strokes only.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.database as cdb
import routes.ink_routes as ir
from core.database import Note
from src import ink_notes as ink
from src import tool_utils
from src.upload_handler import UploadHandler
from tests.helpers.sqlite_db import make_temp_sqlite

PEN = {"color": "#1f5fbf", "width": 2.5, "highlighter": False}


@pytest.fixture
def env(tmp_path, monkeypatch):
    factory, engine, _tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(tool_utils, "_upload_handler", UploadHandler(str(tmp_path), str(uploads)))
    monkeypatch.setattr(ink, "_LOCKS", {})
    yield factory
    engine.dispose()


def client_for(user="alice", token=False):
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = user
        request.state.api_token = token
        if token:
            request.state.api_token_owner = user
            request.state.api_token_scopes = ["chat", "documents:write"]
        return await call_next(request)

    app.include_router(ir.setup_ink_routes())
    return TestClient(app)


def doc(edited_at=1000, sid="s1", title=None):
    out = {"ruled": True, "edited_at": edited_at,
           "pages": [{"id": "p0", "width": 1000, "height": 1414,
                      "strokes": [{"id": sid, "pen": PEN, "at": 0,
                                   "points": [[100, 100, 0], [180, 120, 16], [260, 110, 32]]}]}]}
    if title is not None:
        out["title"] = title
    return out


def nid():
    return str(uuid.uuid4())


def test_save_load_list_delete(env):
    c = client_for()
    note_id = nid()
    r = c.put(f"/api/ink/notes/{note_id}", json=doc(title="Pond walk"))
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Pond walk"
    assert r.json()["image_url"].startswith("/api/upload/")
    got = c.get(f"/api/ink/notes/{note_id}").json()
    assert got["pages"][0]["strokes"][0]["pen"] == PEN
    assert "render" not in got
    listed = c.get("/api/ink/notes").json()["notes"]
    assert [(n["id"], n["title"], n["pages"]) for n in listed] == [(note_id, "Pond walk", 1)]
    assert c.delete(f"/api/ink/notes/{note_id}").json() == {"ok": True}
    assert c.get(f"/api/ink/notes/{note_id}").status_code == 404
    assert c.get("/api/ink/notes").json()["notes"] == []


def test_stale_save_answers_409_with_the_stored_note(env):
    c = client_for()
    note_id = nid()
    c.put(f"/api/ink/notes/{note_id}", json=doc(edited_at=5000, sid="newer"))
    r = c.put(f"/api/ink/notes/{note_id}", json=doc(edited_at=10, sid="older"))
    assert r.status_code == 409
    assert r.json()["note"]["pages"][0]["strokes"][0]["id"] == "newer"


def test_save_after_delete_answers_410(env):
    c = client_for()
    note_id = nid()
    c.put(f"/api/ink/notes/{note_id}", json=doc())
    c.delete(f"/api/ink/notes/{note_id}")
    assert c.put(f"/api/ink/notes/{note_id}", json=doc(edited_at=99999)).status_code == 410


def test_bad_bodies_and_ids(env):
    c = client_for()
    assert c.put(f"/api/ink/notes/{nid()}", content=b"not json").status_code == 400
    assert c.put(f"/api/ink/notes/{nid()}", json={"pages": "x"}).status_code == 400
    assert c.get("/api/ink/notes/short").status_code == 404
    assert c.put("/api/ink/notes/bad_id_with_underscores", json=doc()).status_code == 404


def test_body_size_limit(env, monkeypatch):
    monkeypatch.setattr(ir, "MAX_BODY", 100)
    assert client_for().put(f"/api/ink/notes/{nid()}", json=doc()).status_code == 413


def test_other_users_get_404_and_cannot_overwrite(env):
    note_id = nid()
    client_for("alice").put(f"/api/ink/notes/{note_id}", json=doc(sid="mine"))
    bob = client_for("bob")
    assert bob.get(f"/api/ink/notes/{note_id}").status_code == 404
    assert bob.delete(f"/api/ink/notes/{note_id}").status_code == 404
    assert bob.put(f"/api/ink/notes/{note_id}", json=doc(edited_at=9e9)).status_code == 404
    assert bob.get("/api/ink/notes").json()["notes"] == []
    got = client_for("alice").get(f"/api/ink/notes/{note_id}").json()
    assert got["pages"][0]["strokes"][0]["id"] == "mine"


def test_api_tokens_are_refused(env):
    c = client_for("alice", token=True)
    assert c.get("/api/ink/notes").status_code == 403
    assert c.put(f"/api/ink/notes/{nid()}", json=doc()).status_code == 403


def test_signed_out_is_refused_when_auth_is_on(env, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("LOCALHOST_BYPASS", "false")
    c = client_for(None)
    assert c.get("/api/ink/notes").status_code in (401, 403)


def test_listing_sweeps_notes_deleted_in_the_notes_window(env):
    c = client_for()
    a, b = nid(), nid()
    c.put(f"/api/ink/notes/{a}", json=doc())
    c.put(f"/api/ink/notes/{b}", json=doc())
    s = env()
    s.delete(s.query(Note).filter(Note.id == a).first())
    s.commit()
    s.close()
    assert [n["id"] for n in c.get("/api/ink/notes").json()["notes"]] == [b]
    assert ink.sidecar_ids("alice") == [b]
    assert c.put(f"/api/ink/notes/{a}", json=doc(edited_at=99999)).status_code == 410


def test_other_notes_are_not_reachable_through_ink_routes(env):
    s = env()
    s.add(Note(id="11111111-2222-3333-4444-555555555555", owner="alice", title="Groceries",
               label="work", note_type="checklist"))
    s.commit()
    s.close()
    c = client_for()
    nid_ = "11111111-2222-3333-4444-555555555555"
    assert c.put(f"/api/ink/notes/{nid_}", json=doc(title="Hijacked")).status_code == 404
    assert c.get(f"/api/ink/notes/{nid_}").status_code == 404
    assert c.delete(f"/api/ink/notes/{nid_}").status_code == 404
    s = env()
    row = s.query(Note).filter(Note.id == nid_).first()
    assert row is not None and row.title == "Groceries"
    s.close()


def test_title_must_be_text_and_pages_the_write_size(env):
    c = client_for()
    assert c.put(f"/api/ink/notes/{nid()}", json=doc(title=["a", "b"])).status_code == 400
    big = doc()
    big["pages"][0].update(width=4000, height=20000)
    assert c.put(f"/api/ink/notes/{nid()}", json=big).status_code == 400
