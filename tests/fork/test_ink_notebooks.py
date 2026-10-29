"""Fork: notebooks for handwritten notes (src/ink_notebooks.py, routes/ink_routes.py).

A notebook is a named, ordered group of handwritten notes (its pages); a note in no
notebook is a quick note. Runs on a temporary notes database and upload store, like
tests/fork/test_ink_routes.py. Fictional strokes only.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.database as cdb
import routes.ink_routes as ir
from core.database import Note
from src import ink_notebooks as nb
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


def client_for(user="alice"):
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = user
        request.state.api_token = False
        return await call_next(request)

    app.include_router(ir.setup_ink_routes())
    return TestClient(app)


def doc(edited_at=1000, **extra):
    out = {"paper": "lined", "edited_at": edited_at,
           "pages": [{"id": "p0", "width": 1000, "height": 1414,
                      "strokes": [{"id": "s1", "pen": PEN, "at": 0,
                                   "points": [[100, 100, 0], [180, 120, 16]]}]}]}
    out.update(extra)
    return out


def nid():
    return str(uuid.uuid4())


def listing(c):
    body = c.get("/api/ink/notes").json()
    return body["notes"], body["notebooks"]


def test_create_page_in_notebook_and_list(env):
    c = client_for()
    r = c.post("/api/ink/notebooks", json={"name": "  Garden   journal "})
    assert r.status_code == 200, r.text
    book = r.json()
    assert book["name"] == "Garden journal" and book["pages"] == []

    quick, first, second, between = nid(), nid(), nid(), nid()
    assert c.put(f"/api/ink/notes/{quick}", json=doc()).json()["notebook"] is None
    r = c.put(f"/api/ink/notes/{first}", json=doc(notebook=book["id"]))
    assert r.json()["notebook"] == book["id"]
    c.put(f"/api/ink/notes/{second}", json=doc(notebook=book["id"]))
    # After the first page: in between.
    c.put(f"/api/ink/notes/{between}", json=doc(notebook=book["id"], notebook_after=first))

    notes, books = listing(c)
    assert [b["pages"] for b in books] == [[first, between, second]]
    by_id = {n["id"]: n["notebook"] for n in notes}
    assert by_id == {quick: None, first: book["id"], second: book["id"], between: book["id"]}
    assert c.get(f"/api/ink/notes/{between}").json()["notebook"] == book["id"]


def test_notebook_only_applies_on_the_creating_save(env):
    c = client_for()
    book = c.post("/api/ink/notebooks", json={"name": "Work"}).json()
    note = nid()
    c.put(f"/api/ink/notes/{note}", json=doc(edited_at=1000))
    # A later save never files a quick note (a moved-out page stays out).
    r = c.put(f"/api/ink/notes/{note}", json=doc(edited_at=2000, notebook=book["id"]))
    assert r.json()["notebook"] is None
    # A notebook that is gone leaves the new note a quick note; the note is still saved.
    other = nid()
    r = c.put(f"/api/ink/notes/{other}", json=doc(notebook=str(uuid.uuid4())))
    assert r.status_code == 200 and r.json()["notebook"] is None
    r = c.put(f"/api/ink/notes/{nid()}", json=doc(notebook="../../etc"))
    assert r.status_code == 200 and r.json()["notebook"] is None


def test_move_rename_delete(env):
    c = client_for()
    a = c.post("/api/ink/notebooks", json={"name": "A"}).json()
    b = c.post("/api/ink/notebooks", json={"name": "B"}).json()
    n1, n2 = nid(), nid()
    c.put(f"/api/ink/notes/{n1}", json=doc(notebook=a["id"]))
    c.put(f"/api/ink/notes/{n2}", json=doc())

    r = c.put(f"/api/ink/notes/{n2}/notebook", json={"notebook": a["id"], "after": ""})
    assert r.status_code == 200 and r.json()["notebook"] == a["id"]
    assert listing(c)[1][0]["pages"] == [n2, n1]
    # Into another notebook: out of the first.
    c.put(f"/api/ink/notes/{n2}/notebook", json={"notebook": b["id"]})
    books = {x["id"]: x["pages"] for x in listing(c)[1]}
    assert books == {a["id"]: [n1], b["id"]: [n2]}
    # Out of any notebook: a quick note.
    c.put(f"/api/ink/notes/{n2}/notebook", json={"notebook": None})
    assert {x["id"]: x["pages"] for x in listing(c)[1]}[b["id"]] == []

    r = c.patch(f"/api/ink/notebooks/{a['id']}", json={"name": "Renamed"})
    assert r.json()["name"] == "Renamed"
    assert c.patch(f"/api/ink/notebooks/{a['id']}", json={"name": "  "}).status_code == 400

    # Deleting a notebook keeps its pages as quick notes.
    r = c.delete(f"/api/ink/notebooks/{a['id']}")
    assert r.json() == {"ok": True, "pages": [n1]}
    notes, books = listing(c)
    assert [x["id"] for x in books] == [b["id"]]
    assert {n["id"]: n["notebook"] for n in notes} == {n1: None, n2: None}
    assert c.delete(f"/api/ink/notebooks/{a['id']}").status_code == 404


def test_errors_and_other_users(env):
    c = client_for()
    assert c.post("/api/ink/notebooks", json={}).status_code == 400
    assert c.post("/api/ink/notebooks", json={"name": 5}).status_code == 400
    assert c.post("/api/ink/notebooks", json={"name": "x", "id": "bad id"}).status_code == 400
    book = c.post("/api/ink/notebooks", json={"name": "Mine"}).json()
    # A repeated create with the same client id answers the same notebook.
    again = c.post("/api/ink/notebooks", json={"name": "Mine 2", "id": book["id"]}).json()
    assert again == book
    note = nid()
    c.put(f"/api/ink/notes/{note}", json=doc())
    assert c.put(f"/api/ink/notes/{note}/notebook", json={"notebook": str(uuid.uuid4())}).status_code == 404
    assert c.put(f"/api/ink/notes/{note}/notebook", json={"notebook": 3}).status_code == 400
    assert c.put(f"/api/ink/notes/{nid()}/notebook", json={"notebook": book["id"]}).status_code == 404
    assert c.patch("/api/ink/notebooks/x", json={"name": "y"}).status_code == 404

    bob = client_for("bob")
    assert bob.get("/api/ink/notes").json() == {"notes": [], "notebooks": []}
    assert bob.put(f"/api/ink/notes/{note}/notebook", json={"notebook": None}).status_code == 404
    assert bob.patch(f"/api/ink/notebooks/{book['id']}", json={"name": "z"}).status_code == 404
    assert bob.delete(f"/api/ink/notebooks/{book['id']}").status_code == 404
    # Bob's page can't be filed in Alice's notebook.
    r = bob.put(f"/api/ink/notes/{nid()}", json=doc(notebook=book["id"]))
    assert r.json()["notebook"] is None
    assert [x["pages"] for x in listing(c)[1]] == [[]]


def test_deleted_notes_leave_their_notebook(env):
    c = client_for()
    book = c.post("/api/ink/notebooks", json={"name": "Trip"}).json()
    kept, gone_here, gone_elsewhere, archived = nid(), nid(), nid(), nid()
    for n in (kept, gone_here, gone_elsewhere, archived):
        c.put(f"/api/ink/notes/{n}", json=doc(notebook=book["id"]))
    assert c.delete(f"/api/ink/notes/{gone_here}").status_code == 200
    db = env()
    try:
        db.query(Note).filter(Note.id == gone_elsewhere).delete()  # the Notes window
        db.query(Note).filter(Note.id == archived).update({"archived": True})
        db.commit()
    finally:
        db.close()
    notes, books = listing(c)
    # An archived note leaves the list but keeps its place: it comes back unarchived.
    assert books[0]["pages"] == [kept, archived]
    assert [n["id"] for n in notes] == [kept]
    assert nb.notebook_of("alice", gone_here) is None


def test_notebooks_file_survives_junk(env, tmp_path):
    folder = ink.ink_dir("alice")
    folder.mkdir(parents=True)
    (folder / "_notebooks.json").write_text("not json")
    assert nb.list_notebooks("alice") == []
    (folder / "_notebooks.json").write_text(
        '{"notebooks": [{"id": "bad"}, {"id": "abcdefgh-1", "name": "Ok", "pages": ["x", 3]}]}')
    assert [(b["id"], b["pages"]) for b in nb.list_notebooks("alice")] == [("abcdefgh-1", [])]


# ------------------------------------------------------------------ in a browser
def _playwright_ready() -> bool:
    import shutil
    import subprocess

    if not shutil.which("node"):
        return False
    probe = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True)
    return probe.returncode == 0


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/fork-modules.css">
<style>:root{--bg:#fff;--fg:#222;--panel:#f6f6f6;--border:#ddd;--red:#c33}
html,body{margin:0;height:100%}#host{width:900px;height:760px;display:flex;flex-direction:column}</style>
</head><body><div id="host"></div>
<script type="module">
  import { mountInk } from '/static/js/forkInk.js';
  window.view = mountInk(document.getElementById('host'));
  window.ready = true;
</script></body></html>"""


@pytest.mark.skipif(not _playwright_ready(), reason="node or playwright not available")
def test_notebooks_in_the_write_view(env):
    """The whole flow in Chromium against the real ink routes: a notebook, pages made
    and flipped in the editor, a quick note moved in, rename, delete."""
    import json
    import socket
    import subprocess
    import threading
    from pathlib import Path

    import uvicorn
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    repo = Path(__file__).resolve().parents[2]
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = "alice"
        request.state.api_token = False
        return await call_next(request)

    app.include_router(ir.setup_ink_routes())

    @app.get("/")
    async def index():
        return HTMLResponse(PAGE)

    @app.get("/api/auth/status")
    async def auth():
        return {"username": "alice"}

    app.mount("/static", StaticFiles(directory=str(repo / "static")), name="static")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            _time_sleep(0.05)
        proc = subprocess.run(["node", str(repo / "tests/helpers/ink_notebooks_e2e.mjs"),
                               f"http://127.0.0.1:{port}"],
                              capture_output=True, text=True, cwd=str(repo), timeout=240)
    finally:
        server.should_exit = True
        thread.join(10)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip().splitlines()[-1]
    if out.startswith("SKIP"):
        pytest.skip(out)
    got = json.loads(out)
    failed = {name: r["detail"] for name, r in got.items() if not r["ok"]}
    assert not failed, failed
    assert len(got) == 13


def _time_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
