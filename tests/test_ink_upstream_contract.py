"""Fork: the handwritten notes adapter against the real upstream notes code.

Each adapter function in src/ink_notes.py is run against upstream's own code on a
temporary database and upload store, without fakes: notes written by the adapter are
read back through upstream's notes routes, notes changed or deleted by upstream's
routes are seen by the adapter, the page image goes through upstream's upload store
and is served by its upload route and kept by its cleanup scan. When an upstream sync
changes the notes code, this file fails in the sync PR; the fix belongs in the adapter.
"""
import io
import re
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import core.database as cdb
import routes.note.note_routes as note_routes
import routes.upload_routes as upload_routes
from src import ink_notes as ink
from src import tool_utils
from src.upload_handler import UploadHandler
from tests.helpers.sqlite_db import make_temp_sqlite

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    factory, engine, _tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(note_routes, "SessionLocal", factory)
    monkeypatch.setattr(upload_routes, "SessionLocal", factory)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    handler = UploadHandler(str(tmp_path), str(uploads))
    monkeypatch.setattr(tool_utils, "_upload_handler", handler)
    yield handler
    engine.dispose()


def upstream_client(handler, user="alice"):
    """Upstream's notes and upload routes behind a signed-in session."""
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = user
        request.state.api_token = False
        return await call_next(request)

    app.include_router(note_routes.setup_note_routes(None, upload_handler=handler))
    # setup_upload_routes adds to one module-level router on every call; take only the
    # routes this call added (as tests/test_upload_routes_owner_scope.py does).
    before = len(upload_routes.router.routes)
    upload_routes.setup_upload_routes(handler)
    for route in upload_routes.router.routes[before:]:
        app.router.routes.append(route)
    return TestClient(app)


def png():
    page = ink.clean_document({"edited_at": 1, "pages": [{"id": "p0", "strokes": [
        {"id": "s", "pen": {"color": "#c62828", "width": 3, "highlighter": False},
         "points": [[50, 50, 0], [400, 80, 40]]}]}]})["pages"][0]
    return ink.render_page_png(page)


def test_create_note_is_an_ordinary_upstream_note(env):
    note_id = str(uuid.uuid4())
    assert ink.create_note("alice", "Garden plan", ink.LABEL, None, note_id=note_id) == note_id
    assert ink.create_note("alice", "again", ink.LABEL, None, note_id=note_id) is None
    c = upstream_client(env)
    got = c.get(f"/api/notes/{note_id}").json()
    assert (got["title"], got["label"], got["note_type"], got["owner"]) == (
        "Garden plan", "handwritten", "note", "alice")
    assert got["archived"] is False and got["source"] == "user"
    listed = c.get("/api/notes", params={"label": "handwritten"}).json()["notes"]
    assert [n["id"] for n in listed] == [note_id]
    assert upstream_client(env, "bob").get(f"/api/notes/{note_id}").status_code == 404
    made = ink.create_note("alice", "No id given", ink.LABEL, None)
    assert c.get(f"/api/notes/{made}").json()["title"] == "No id given"


def test_update_note_and_upstream_edits_are_seen_both_ways(env):
    note_id = ink.create_note("alice", "Draft", ink.LABEL, None)
    url = ink.save_page_image("alice", png())
    assert ink.update_note("alice", note_id, content="milk, eggs", image_url=url, title="List")
    c = upstream_client(env)
    got = c.get(f"/api/notes/{note_id}").json()
    assert (got["content"], got["image_url"], got["title"]) == ("milk, eggs", url, "List")
    assert ink.update_note("bob", note_id, content="x") is False
    c.put(f"/api/notes/{note_id}", json={"title": "Renamed in Notes"})
    assert ink.note_title("alice", note_id) == "Renamed in Notes"
    c.put(f"/api/notes/{note_id}", json={"archived": True})
    assert ink.existing_note_ids("alice", [note_id]) == {note_id}
    assert ink.handwritten_notes("alice") == []  # archived notes leave the Write list
    assert ink.update_note("alice", note_id, image_url="")
    assert c.get(f"/api/notes/{note_id}").json()["image_url"] is None


def test_existing_note_ids_follows_upstream_deletes(env):
    a = ink.create_note("alice", "A", ink.LABEL, None)
    b = ink.create_note("alice", "B", ink.LABEL, None)
    c_id = ink.create_note("carol", "C", ink.LABEL, None)
    assert ink.existing_note_ids("alice", [a, b, c_id, "missing-id-123"]) == {a, b}
    assert ink.existing_note_ids(None, [a, c_id]) == {a, c_id}  # single-user mode
    assert upstream_client(env).delete(f"/api/notes/{a}").json() == {"ok": True}
    assert ink.existing_note_ids("alice", [a, b]) == {b}
    assert ink.delete_note("alice", b) is True
    assert upstream_client(env).get(f"/api/notes/{b}").status_code == 404
    assert ink.delete_note("alice", c_id) is False


def test_save_page_image_goes_through_upstream_upload_store(env, monkeypatch):
    data = png()
    url = ink.save_page_image("alice", data)
    assert re.fullmatch(r"/api/upload/[^/]+", url)
    upload_id = url.rsplit("/", 1)[1]
    info = env.get_upload_info(upload_id)
    assert info["owner"] == "alice" and info["mime"] == "image/png"
    # Upstream's own upload route serves it (auth off here: no auth manager in tests).
    monkeypatch.setenv("AUTH_ENABLED", "false")
    r = upstream_client(env).get(url)
    assert r.status_code == 200 and r.content == data
    assert Image.open(io.BytesIO(r.content)).size == (1000, 1414)
    # Referenced from a note, upstream's upload cleanup keeps it.
    note_id = ink.create_note("alice", "Kept", ink.LABEL, url)
    assert note_id
    referenced, _hashes = upload_routes._collect_persisted_upload_references()
    assert upload_id in referenced


def test_note_link_opens_the_note_in_the_app(env):
    note_id = str(uuid.uuid4())
    link = ink.note_link(note_id)
    assert link == f"/#open=notes&note={note_id}"
    hash_part = link[1:]
    entity = (REPO / "static/js/forkEntityHash.js").read_text()
    js_regex = re.search(r"const ENTITY_HASH = /(.+)/;", entity).group(1)
    assert re.match(js_regex, hash_part)
    renderer = (REPO / "static/js/chatRenderer.js").read_text()
    assert "#open=notes&note=" in renderer  # upstream's click handler for these links
