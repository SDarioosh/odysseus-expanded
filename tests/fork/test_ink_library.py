"""Fork: PDF datasheets, open a Library PDF in Write (fork/ink/library.py, the adapter
to upstream's documents, and the routes ``GET /api/ink/library-pdfs`` and
``POST /api/ink/notes/{id}/pdf`` with JSON in fork/routes/ink_routes.py).

Also the adapter's contract test: Library documents are made by upstream's own
``POST /api/documents/import-pdf`` and ``POST /api/document`` (routes/document/), with
upstream's upload store, on a temporary database, without fakes. When an upstream sync
changes how the Library keeps PDFs or checks owners, this file fails in the sync PR; the
fix belongs in fork/ink/library.py. Fictional PDFs only (tests/fork/pdf_fixtures.py).
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.database as cdb
import routes.document.document_routes as document_routes
import src.database as sdb
from fork.ink import library
from fork.ink import notes as ink
from fork.ink import pdf as ink_pdf
from fork.routes import ink_routes as ir
from src import tool_utils
from src.pdf_form_doc import find_source_upload_id
from src.upload_handler import UploadHandler
from tests.fork.pdf_fixtures import datasheet
from tests.helpers.sqlite_db import make_temp_sqlite


@pytest.fixture
def env(data_dir, monkeypatch):
    factory, engine, _tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(sdb, "SessionLocal", factory)
    monkeypatch.setattr(document_routes, "SessionLocal", factory)
    uploads = data_dir / "uploads"
    uploads.mkdir()
    handler = UploadHandler(str(data_dir), str(uploads))
    monkeypatch.setattr(tool_utils, "_upload_handler", handler)
    monkeypatch.setattr(ink, "_LOCKS", {})
    yield handler
    engine.dispose()


def client_for(handler, user="alice"):
    """Upstream's document routes and the ink routes behind a signed-in session."""
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = user
        request.state.api_token = False
        return await call_next(request)

    app.include_router(document_routes.setup_document_routes(None, upload_handler=handler))
    app.include_router(ir.setup_ink_routes())
    return TestClient(app)


def nid():
    return str(uuid.uuid4())


def library_pdf(c, name="ZX-100 datasheet.pdf", data=None):
    """A PDF imported into the Library by upstream's own route; answers its document."""
    r = c.post("/api/documents/import-pdf",
               files={"file": (name, data or datasheet(pages=3), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def from_library(c, note_id, document_id, **extra):
    return c.post(f"/api/ink/notes/{note_id}/pdf",
                  json={"document_id": document_id, "edited_at": 1234, **extra})


def test_library_pdfs_lists_only_the_owners_pdf_documents(env):
    alice, bob = client_for(env), client_for(env, "bob")
    first = library_pdf(alice, "ZX-100 datasheet.pdf")
    second = library_pdf(alice, "AB-200 notes.pdf", datasheet(pages=1))
    text = alice.post("/api/document", json={"title": "Shopping", "content": "Milk"})
    assert text.status_code == 200, text.text
    library_pdf(bob, "Bob's sheet.pdf", datasheet(pages=2))

    r = alice.get("/api/ink/library-pdfs")
    assert r.status_code == 200, r.text
    docs = r.json()["documents"]
    assert {d["id"] for d in docs} == {first["id"], second["id"]}  # no text doc, no Bob's
    assert {d["title"] for d in docs} == {"ZX-100 datasheet", "AB-200 notes"}
    assert all(set(d) == {"id", "title", "updated_at"} and d["updated_at"].endswith("Z")
               for d in docs)
    # The same documents as upstream's own Library, filtered to PDFs.
    upstream = alice.get("/api/documents/library", params={"language": "pdf"}).json()
    assert {d["id"] for d in upstream["documents"]} == {d["id"] for d in docs}
    assert [d["title"] for d in bob.get("/api/ink/library-pdfs").json()["documents"]] == [
        "Bob's sheet"]

    # Archived documents leave the Library, and this list.
    db = cdb.SessionLocal()
    try:
        db.query(cdb.Document).filter(cdb.Document.id == second["id"]).update(
            {"archived": True})
        db.commit()
    finally:
        db.close()
    assert [d["id"] for d in alice.get("/api/ink/library-pdfs").json()["documents"]] == [
        first["id"]]


def test_a_library_pdf_opens_as_a_new_datasheet_note(env):
    c = client_for(env)
    data = datasheet(pages=3)
    doc = library_pdf(c, "ZX-100 datasheet.pdf", data)
    note_id = nid()
    r = from_library(c, note_id, doc["id"])
    assert r.status_code == 200, r.text
    note = r.json()
    assert note["title"] == "ZX-100 datasheet" and note["edited_at"] == 1234
    assert note["pdf"]["name"] == "ZX-100 datasheet.pdf" and note["pdf"]["pages"] == 3
    assert [p["pdf"]["page"] for p in note["pages"]] == [1, 2, 3]
    assert note["paper"] == "blank"
    # The same file, served by the ink routes; the Library document is unchanged.
    assert c.get(f"/api/ink/notes/{note_id}/pdf").content == data
    after = c.get(f"/api/document/{doc['id']}").json()
    assert after["current_content"] == doc["current_content"]
    assert after["version_count"] == doc["version_count"]
    assert note_id not in after["current_content"]
    # A title of its own, and the paper of blank pages.
    other = from_library(c, nid(), doc["id"], title="  My ZX-100  ", paper="lined").json()
    assert other["title"] == "My ZX-100" and other["paper"] == "lined"
    # Like an upload: a PDF starts a new note.
    again = from_library(c, note_id, doc["id"])
    assert again.status_code == 409 and again.json()["detail"] == "A PDF starts a new note"


def test_only_the_owners_library_pdfs_open(env):
    alice, bob = client_for(env), client_for(env, "bob")
    doc = library_pdf(alice)
    assert from_library(bob, nid(), doc["id"]).status_code == 404
    assert from_library(alice, nid(), "no-such-document").status_code == 404
    text = alice.post("/api/document", json={"title": "Shopping", "content": "Milk"}).json()
    r = from_library(alice, nid(), text["id"])
    assert r.status_code == 404 and r.json()["detail"] == "That document is not a PDF"
    # Bob's note id can't be taken over with Alice's document.
    bobs = nid()
    assert from_library(bob, bobs, library_pdf(bob, "b.pdf")["id"]).status_code == 200
    assert from_library(alice, bobs, doc["id"]).status_code == 404


def test_a_library_pdf_whose_file_is_gone(env):
    c = client_for(env)
    doc = library_pdf(c)
    upload_id = find_source_upload_id(doc["current_content"])
    os.unlink(env.resolve_upload(upload_id, owner="alice")["path"])
    r = from_library(c, nid(), doc["id"])
    assert r.status_code == 404 and "no longer available" in r.json()["detail"]


def test_bad_bodies(env):
    c = client_for(env)
    doc = library_pdf(c)
    url = f"/api/ink/notes/{nid()}/pdf"
    assert c.post(url, json={"edited_at": 1}).status_code == 400
    assert c.post(url, json={"document_id": 5}).status_code == 400
    assert c.post(url, json={"document_id": doc["id"], "edited_at": "soon"}).status_code == 400
    assert c.post(url, json={"document_id": doc["id"], "title": 3}).status_code == 400
    assert c.post(url, json=[doc["id"]]).status_code == 400
    assert c.post(url, content=b"{", headers={"content-type": "application/json"}
                  ).status_code == 400
    assert c.post(url, content=b"x" * 9000, headers={"content-type": "application/json"}
                  ).status_code == 413
    assert c.post("/api/ink/notes/bad!/pdf", json={"document_id": doc["id"]}
                  ).status_code == 404


def test_the_pdf_limits_apply(env, monkeypatch):
    c = client_for(env)
    doc = library_pdf(c)
    monkeypatch.setattr(ink_pdf, "MAX_PDF_BYTES", 100)
    r = from_library(c, nid(), doc["id"])
    assert r.status_code == 413


def test_adapter_directly(env):
    c = client_for(env)
    doc = library_pdf(c, "Folder/Part.pdf")
    data, name, title = library.document_pdf("alice", doc["id"])
    assert data.startswith(b"%PDF") and title == doc["title"]
    assert "/" not in name and name.endswith(".pdf")
    with pytest.raises(ink.InkError) as err:
        library.document_pdf("bob", doc["id"])
    assert err.value.status == 404
    with pytest.raises(ink.InkError):
        library.document_pdf("alice", "x" * 65)
    assert [d["id"] for d in library.pdf_documents("alice")] == [doc["id"]]
    assert library.pdf_documents("bob") == []
