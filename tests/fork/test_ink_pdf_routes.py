"""Fork: handwritten notes, PDF datasheets: the routes in fork/routes/ink_routes.py
(``POST``/``GET /api/ink/notes/{id}/pdf``, ``GET /api/ink/notes/{id}/pdf/{page}.png``).

Temporary notes database and upload store, a stand-in auth middleware. Every PDF is made
by tests/fork/pdf_fixtures.py with fictional text.
"""
import io
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import core.database as cdb
from fork.ink import notes as ink
from fork.ink import pdf
from fork.ink import transcribe as tr
from fork.routes import ink_routes as ir
from src import tool_utils
from src.upload_handler import UploadHandler
from tests.fork.pdf_fixtures import A4, datasheet, make_pdf
from tests.helpers.sqlite_db import make_temp_sqlite

PEN = {"color": "#1f1f1f", "width": 2.0, "highlighter": False}


@pytest.fixture
def env(data_dir, monkeypatch):
    factory, engine, _tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    uploads = data_dir / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(tool_utils, "_upload_handler", UploadHandler(str(data_dir), str(uploads)))
    monkeypatch.setattr(ink, "_LOCKS", {})
    monkeypatch.setattr(tr, "_RUNNING", set())
    yield factory
    engine.dispose()


def nid():
    return str(uuid.uuid4())


def client_for(user="alice"):
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = user
        request.state.api_token = False
        return await call_next(request)

    app.include_router(ir.setup_ink_routes())
    return TestClient(app)


def upload(c, note_id, data=None, name="ZX-100 datasheet.pdf", **fields):
    files = [("pdf", (name, data if data is not None else datasheet(), "application/pdf"))]
    return c.post(f"/api/ink/notes/{note_id}/pdf", files=files, data=fields)


def test_import_serve_and_open(env):
    c = client_for()
    note_id = nid()
    data = datasheet(pages=3)
    r = upload(c, note_id, data, edited_at="1234", paper="lined")
    assert r.status_code == 200, r.text
    note = r.json()
    assert note["title"] == "ZX-100 datasheet" and note["edited_at"] == 1234
    assert note["paper"] == "lined" and note["notebook"] is None
    assert note["pdf"]["name"] == "ZX-100 datasheet.pdf" and note["pdf"]["pages"] == 3
    assert [p["pdf"]["page"] for p in note["pages"]] == [1, 2, 3]
    assert c.get(f"/api/ink/notes/{note_id}").json()["pages"] == note["pages"]
    listed = c.get("/api/ink/notes").json()["notes"][0]
    assert listed["pdf"] == {"name": "ZX-100 datasheet.pdf", "pages": 3}
    # The original file, as an attachment with its name.
    got = c.get(f"/api/ink/notes/{note_id}/pdf")
    assert got.status_code == 200 and got.content == data
    assert got.headers["content-type"] == "application/pdf"
    assert got.headers["content-disposition"].startswith("attachment")
    assert "ZX-100" in got.headers["content-disposition"]
    assert got.headers["x-content-type-options"] == "nosniff"
    # A page at the tier for the asked width.
    pid = note["pages"][0]["id"]
    img = c.get(f"/api/ink/notes/{note_id}/pdf/{pid}.png?w=900")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert img.headers["cache-control"] == "private, max-age=604800, immutable"
    assert img.headers["x-content-type-options"] == "nosniff"
    assert Image.open(io.BytesIO(img.content)).size == (1200, round(1200 * 842 / 595))
    big = c.get(f"/api/ink/notes/{note_id}/pdf/{pid}.png?w=99999")
    assert Image.open(io.BytesIO(big.content)).width == 3000
    small = c.get(f"/api/ink/notes/{note_id}/pdf/{pid}.png?w=0")
    assert Image.open(io.BytesIO(small.content)).width == 600
    assert c.get(f"/api/ink/notes/{note_id}/pdf/{pid}.png").status_code == 200


def test_other_users_and_other_pages_get_404(env):
    c, bob = client_for("alice"), client_for("bob")
    note_id = nid()
    pid = upload(c, note_id).json()["pages"][0]["id"]
    assert bob.get(f"/api/ink/notes/{note_id}/pdf").status_code == 404
    assert bob.get(f"/api/ink/notes/{note_id}/pdf/{pid}.png?w=600").status_code == 404
    assert upload(bob, note_id).status_code == 404
    assert c.get(f"/api/ink/notes/{note_id}/pdf/nope.png?w=600").status_code == 404
    assert c.get(f"/api/ink/notes/{note_id}/pdf/{pid}.png?w=wide").status_code == 422
    written = nid()
    c.put(f"/api/ink/notes/{written}", json={"edited_at": 1, "pages": [
        {"id": "p0", "width": 1000, "height": 1414, "strokes": []}]})
    assert c.get(f"/api/ink/notes/{written}/pdf").status_code == 404
    assert c.get(f"/api/ink/notes/{written}/pdf/p0.png?w=600").status_code == 404


def test_a_pdf_starts_a_new_note(env):
    c = client_for()
    note_id = nid()
    assert upload(c, note_id).status_code == 200
    r = upload(c, note_id)
    assert r.status_code == 409 and r.json()["detail"] == "A PDF starts a new note"
    written = nid()
    c.put(f"/api/ink/notes/{written}", json={"edited_at": 1, "pages": [
        {"id": "p0", "width": 1000, "height": 1414, "strokes": []}]})
    assert upload(c, written).status_code == 409
    # Deleted: files gone, the id isn't reused.
    assert c.delete(f"/api/ink/notes/{note_id}").status_code == 200
    assert not ink.pdf_path("alice", note_id).exists()
    assert c.get(f"/api/ink/notes/{note_id}/pdf").status_code == 404
    assert upload(c, note_id).status_code == 410


def test_refused_uploads_create_nothing(env, monkeypatch):
    c = client_for()
    note_id = nid()
    assert upload(c, note_id, b"just text", name="notes.pdf").status_code == 400
    assert upload(c, note_id, make_pdf([{"size": (1000, 100)}])).status_code == 400
    assert c.post(f"/api/ink/notes/{note_id}/pdf", data={"title": "x"}).status_code == 400
    assert upload(c, note_id, edited_at="later").status_code == 400
    files = [("pdf", ("a.pdf", datasheet(), "application/pdf")),
             ("pdf", ("b.pdf", datasheet(), "application/pdf"))]
    assert c.post(f"/api/ink/notes/{note_id}/pdf", files=files).status_code == 400
    monkeypatch.setattr(pdf, "MAX_PDF_PAGES", 1)
    assert upload(c, note_id).status_code == 413
    monkeypatch.setattr(ir, "MAX_PDF_BODY", 1000)
    assert upload(c, note_id).status_code == 413
    assert c.get(f"/api/ink/notes/{note_id}").status_code == 404
    assert c.get("/api/ink/notes").json()["notes"] == []
    assert not ink.note_id_taken(note_id)


def test_write_on_a_pdf_page_through_a_save(env):
    c = client_for()
    note_id = nid()
    note = upload(c, note_id, make_pdf([{"size": A4}, {"size": (842, 595)}])).json()
    pages = [{"id": p["id"], "width": p["width"], "height": p["height"], "strokes": []}
             for p in note["pages"]]
    pages[1]["strokes"] = [{"id": "s1", "pen": PEN, "at": 0,
                            "points": [[100, 690, 0], [200, 700, 16]]}]  # near the bottom
    r = c.put(f"/api/ink/notes/{note_id}", json={"edited_at": note["edited_at"] + 1,
                                                  "paper": "blank", "pages": pages})
    assert r.status_code == 200, r.text
    assert r.json()["pages"][1]["pdf"] == note["pages"][1]["pdf"]
    pages[1]["height"] = 1414   # a PDF page can't take the Write view's page size
    r = c.put(f"/api/ink/notes/{note_id}", json={"edited_at": note["edited_at"] + 2,
                                                  "paper": "blank", "pages": pages})
    assert r.status_code == 400
    # Send is refused for a datasheet.
    r = c.post(f"/api/ink/notes/{note_id}/send", json={})
    assert r.status_code == 400 and r.json()["detail"] == "Datasheets stay in Odysseus"
    t = c.get(f"/api/ink/notes/{note_id}/transcripts").json()
    assert t["athena"]["datasheet"] is True and t["athena"]["state"] == "not_sent"
    assert [p["pdf"] for p in t["pages"]] == [True, True]
