"""Fork: marks on PDF datasheets over HTTP (fork/routes/ink_marks_routes.py,
fork/ink/pdf_marks.py ``note_marks``/``search``).

A fictional datasheet (tests/fork/pdf_fixtures.py) is imported through
``POST /api/ink/notes/{id}/pdf``, then strokes are saved the way the Write view saves
them, placed over words found with ``fork.ink.pdf.page_words``. Temporary notes database
and upload store, a stand-in for the auth middleware.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.database as cdb
from fork.ink import mark_reading
from fork.ink import notes as ink
from fork.ink import pdf_marks as pm
from fork.routes import ink_marks_routes as mr
from fork.routes import ink_routes as ir
from src import tool_utils
from src.upload_handler import UploadHandler
from tests.fork.pdf_fixtures import datasheet
from tests.helpers.sqlite_db import make_temp_sqlite

PEN = {"color": "#1f5fbf", "width": 2.5, "highlighter": False}
RED = {"color": "#d0021b", "width": 2.5, "highlighter": False}
YELLOW = {"color": "#ffd400", "width": 14.0, "highlighter": True}


@pytest.fixture
def env(data_dir, monkeypatch):
    factory, engine, _tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    uploads = data_dir / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(tool_utils, "_upload_handler", UploadHandler(str(data_dir), str(uploads)))
    monkeypatch.setattr(ink, "_LOCKS", {})
    monkeypatch.setattr(pm, "_CACHE", type(pm._CACHE)())
    # No reading runs here (tests/fork/test_ink_mark_reading.py has them).
    monkeypatch.setattr(mark_reading, "schedule", lambda *a, **k: None)
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
    app.include_router(mr.setup_ink_marks_routes())
    return TestClient(app)


def nid():
    return str(uuid.uuid4())


def import_pdf(c, note_id=None, name="zx100-datasheet.pdf", **fields):
    note_id = note_id or nid()
    r = c.post(f"/api/ink/notes/{note_id}/pdf", files={"pdf": (name, datasheet(), "application/pdf")},
               data={"edited_at": "1000", **fields})
    assert r.status_code == 200, r.text
    return note_id, r.json()


def word_boxes(owner, note_id, page_no):
    from fork.ink import pdf as ink_pdf

    path = ink.pdf_file(owner, note_id)
    return ink_pdf.page_words(path, page_no)


def span(words, *texts):
    """The box ``(x0, y0, x1, y1)`` around the first run of words reading ``texts``."""
    seq = [w.text for w in words]
    for i in range(len(seq) - len(texts) + 1):
        if tuple(seq[i:i + len(texts)]) == texts:
            run = words[i:i + len(texts)]
            return (min(w.x0 for w in run), min(w.y0 for w in run),
                    max(w.x1 for w in run), max(w.y1 for w in run))
    raise AssertionError(f"{texts} not on the page: {seq}")


def stroke(sid, pen, pts, at):
    return {"id": sid, "pen": pen, "at": at, "points": [[x, y, i * 16] for i, (x, y) in enumerate(pts)]}


def save(c, note, strokes_by_page, edited_at=None):
    """Save ``strokes_by_page`` ({page index: [stroke]}) as the Write view does."""
    body = {"paper": note["paper"], "edited_at": edited_at or note["edited_at"] + 1,
            "pages": [{"id": p["id"], "width": p["width"], "height": p["height"],
                       "strokes": p["strokes"] + strokes_by_page.get(i, [])}
                      for i, p in enumerate(note["pages"])]}
    r = c.put(f"/api/ink/notes/{note['note_id']}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def marked_note(c, owner="alice"):
    """A datasheet with a highlight, an underline and a circled value on page 1 and a
    handwritten note beside a line on page 2 (fork/ink/pdf_marks.py kinds highlight,
    underline, circle, ink)."""
    note_id, note = import_pdf(c)
    w1 = word_boxes(owner, note_id, 1)
    w2 = word_boxes(owner, note_id, 2)
    hx0, hy0, hx1, hy1 = span(w1, "2.7", "V", "to", "5.5", "V")
    ux0, _, ux1, uy1 = span(w1, "Output", "current")
    cx0, cy0, cx1, cy1 = span(w1, "18", "uA")
    _, ly0, lx1, ly1 = span(w2, "VIN", "to", "GND", "-0.3", "V", "to", "6.0", "V")
    mid = (hy0 + hy1) / 2
    strokes = {
        0: [stroke("hl1", YELLOW, [(hx0 + 2, mid), (hx1 - 2, mid)], at=2000),
            stroke("ul1", RED, [(ux0, uy1 + 4), ((ux0 + ux1) / 2, uy1 + 5), (ux1, uy1 + 4)], at=3000),
            stroke("ci1", PEN, [(cx0 - 6, cy0 - 6), (cx1 + 6, cy0 - 6), (cx1 + 6, cy1 + 6),
                                (cx0 - 6, cy1 + 6), (cx0 - 6, cy0 - 5)], at=4000)],
        1: [stroke("hw1", PEN, [(lx1 + 40, (ly0 + ly1) / 2), (lx1 + 60, ly1),
                                (lx1 + 80, ly0)], at=5000),
            stroke("hw2", PEN, [(lx1 + 90, ly0), (lx1 + 110, ly1)], at=5100)],
    }
    return note_id, save(c, note, strokes)


# ------------------------------------------------------------------- /marks
def test_note_marks_find_the_text_under_each_mark(env):
    c = client_for()
    note_id, note = marked_note(c)
    r = c.get(f"/api/ink/notes/{note_id}/marks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["note_id"] == note_id and body["title"] == note["title"]
    got = [(m["kind"], m["page_no"], m["text"], m["near"], m["strokes"]) for m in body["marks"]]
    assert got == [
        ("highlight", 1, "2.7 V to 5.5 V", "", ["hl1"]),
        ("underline", 1, "Output current", "", ["ul1"]),
        ("circle", 1, "18 uA", "", ["ci1"]),
        ("ink", 2, "", "VIN to GND -0.3 V to 6.0 V", ["hw1", "hw2"]),
    ]
    pages = [p["id"] for p in note["pages"]]
    assert [m["page_id"] for m in body["marks"]] == [pages[0]] * 3 + [pages[1]]
    first = body["marks"][0]
    assert first["id"] == "m-hl1" and first["color"] == "#ffd400" and first["at"] == 2000
    assert len(first["box"]) == 4


def test_marks_of_a_note_without_pdf_pages_and_of_others_notes(env):
    c = client_for()
    plain = nid()
    r = c.put(f"/api/ink/notes/{plain}", json={"paper": "lined", "edited_at": 1, "pages": [
        {"id": "p0", "width": 1000, "height": 1414,
         "strokes": [stroke("s1", YELLOW, [(100, 100), (300, 100)], at=1)]}]})
    assert r.status_code == 200, r.text
    assert c.get(f"/api/ink/notes/{plain}/marks").json()["marks"] == []
    note_id, _ = marked_note(c)
    bob = client_for("bob")
    assert bob.get(f"/api/ink/notes/{note_id}/marks").status_code == 404
    assert bob.get("/api/ink/marks", params={"q": "5.5"}).json()["results"] == []
    assert c.get("/api/ink/notes/not-a-note!/marks").status_code == 404
    assert c.get(f"/api/ink/notes/{nid()}/marks").status_code == 404


def test_marks_are_cached_until_the_strokes_change(env, monkeypatch):
    from fork.ink import pdf as ink_pdf

    c = client_for()
    note_id, note = marked_note(c)
    w1 = word_boxes("alice", note_id, 1)
    calls = []
    real = ink_pdf.page_words
    monkeypatch.setattr(ink_pdf, "page_words", lambda *a: calls.append(a) or real(*a))
    first = c.get(f"/api/ink/notes/{note_id}/marks").json()["marks"]
    assert len(calls) == 2                    # one per page with strokes
    assert c.get(f"/api/ink/notes/{note_id}/marks").json()["marks"] == first
    assert len(calls) == 2                    # cached
    x0, y0, x1, y1 = span(w1, "ZX-100")
    save(c, note, {0: [stroke("hl2", YELLOW, [(x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2)], at=9000)]})
    again = c.get(f"/api/ink/notes/{note_id}/marks").json()["marks"]
    assert len(calls) == 3                    # only page 1 changed
    assert again[0]["text"] == "ZX-100" and again[0]["id"] == "m-hl2"


# ------------------------------------------------------------------- search
def test_search_matches_all_words_in_text_near_title_and_pdf_name(env):
    c = client_for()
    note_id, _ = marked_note(c)
    other, _ = import_pdf(c, name="ab-200.pdf", title="Bench notes")  # no marks
    r = c.get("/api/ink/marks", params={"q": "  5.5  "})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["q"] == "5.5"
    assert [(x["note_id"], x["mark"]["id"]) for x in body["results"]] == [(note_id, "m-hl1")]
    hit = body["results"][0]
    assert hit["pdf_name"] == "zx100-datasheet.pdf" and hit["title"]
    # all words, any case, in near text too
    got = c.get("/api/ink/marks", params={"q": "vin GND"}).json()["results"]
    assert [x["mark"]["id"] for x in got] == ["m-hw1"]
    assert c.get("/api/ink/marks", params={"q": "vin current"}).json()["results"] == []
    # the PDF's name matches every mark of that note, newest first
    got = c.get("/api/ink/marks", params={"q": "ZX100"}).json()["results"]
    assert [x["mark"]["id"] for x in got] == ["m-hw1", "m-ci1", "m-ul1", "m-hl1"]
    got = c.get("/api/ink/marks", params={"q": "zx100", "limit": 2}).json()["results"]
    assert [x["mark"]["id"] for x in got] == ["m-hw1", "m-ci1"]
    assert c.get("/api/ink/marks", params={"q": "bench"}).json()["results"] == []
    assert other


def test_search_needs_two_characters_and_keeps_limit_in_range(env):
    c = client_for()
    marked_note(c)
    assert c.get("/api/ink/marks", params={"q": "a"}).status_code == 400
    assert c.get("/api/ink/marks", params={"q": "   "}).status_code == 400
    assert c.get("/api/ink/marks").status_code == 400
    assert len(c.get("/api/ink/marks", params={"q": "zx100", "limit": 0}).json()["results"]) == 1
    assert len(c.get("/api/ink/marks", params={"q": "zx100", "limit": 500}).json()["results"]) == 4


# ------------------------------------------------------------------- arrows
def test_an_arrow_and_a_note_with_an_arrow(env):
    c = client_for()
    note_id, note = import_pdf(c)
    w2 = word_boxes("alice", note_id, 2)
    _, ey0, ex1, ey1 = span(w2, "EN", "to", "GND", "-0.3", "V", "to", "VIN")
    tip = (ex1 + 6, (ey0 + ey1) / 2)
    tail = (tip[0] + 120, tip[1] + 60)
    back = ((tail[0] - tip[0]) / 134.16, (tail[1] - tip[1]) / 134.16)
    barb = (tip[0] + 10 * back[0] - 5 * back[1], tip[1] + 10 * back[1] + 5 * back[0])
    arrow = stroke("ar1", RED, [tail, tip, barb], at=6000)
    # Writing at the arrow's tail, 60 below the line: a note with an arrow.
    letters = [stroke(f"lt{i}", PEN, [(tail[0] + 5 + 15 * i, tail[1] - 5),
                                      (tail[0] + 12 + 15 * i, tail[1] + 8)], at=6100 + i)
               for i in range(3)]
    save(c, note, {1: [arrow, *letters]})
    (m,) = c.get(f"/api/ink/notes/{note_id}/marks").json()["marks"]
    assert m["kind"] == "ink" and m["strokes"] == ["ar1", "lt0", "lt1", "lt2"]
    assert m["near"] == "EN to GND -0.3 V to VIN" and m["points_to"] is True
    assert not any(k.startswith("_") for k in m)   # the writing's strokes stay inside
    assert m["reading"] == "waiting"
    # The arrow alone: an arrow mark, never read.
    note = c.get(f"/api/ink/notes/{note_id}").json()
    body = {"paper": note["paper"], "edited_at": note["edited_at"] + 1,
            "pages": [{"id": p["id"], "width": p["width"], "height": p["height"],
                       "strokes": [arrow] if i == 1 else []}
                      for i, p in enumerate(note["pages"])]}
    assert c.put(f"/api/ink/notes/{note_id}", json=body).status_code == 200
    (m,) = c.get(f"/api/ink/notes/{note_id}/marks").json()["marks"]
    assert m["kind"] == "arrow" and m["text"] == "EN to GND -0.3 V to VIN"
    assert m["reading"] == "none" and m["near"] == ""
    r = c.put(f"/api/ink/notes/{note_id}/marks/m-ar1", json={"written": "x"})
    assert r.status_code == 400


# ------------------------------------------------------------------- parts
def test_parts_are_guessed_on_import_kept_and_set(env):
    c = client_for()
    note_id, note = import_pdf(c, name="zx100-datasheet.pdf")
    assert note["parts"] == ["ZX-100"]          # the title line, the name's ZX100 is the same
    listed = next(n for n in c.get("/api/ink/notes").json()["notes"] if n["id"] == note_id)
    assert listed["parts"] == ["ZX-100"]
    r = c.put(f"/api/ink/notes/{note_id}/parts", json={"parts": [" ZX-100A ", "zx-100a", "AB-200"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"parts": ["ZX-100A", "AB-200"]}
    # A save from the Write view keeps them; the note answers them.
    saved = save(c, note, {0: [stroke("w1", PEN, [(700, 700), (720, 720)], at=7000)]})
    assert saved["parts"] == ["ZX-100A", "AB-200"]
    assert c.get(f"/api/ink/notes/{note_id}").json()["parts"] == ["ZX-100A", "AB-200"]
    assert c.put(f"/api/ink/notes/{note_id}/parts", json={"parts": []}).json() == {"parts": []}
    assert c.get(f"/api/ink/notes/{note_id}").json()["parts"] == []


def test_set_parts_refuses_bad_bodies_other_notes_and_non_datasheets(env):
    c = client_for()
    note_id, _ = import_pdf(c)
    url = f"/api/ink/notes/{note_id}/parts"
    for bad in ({"parts": "ZX-100"}, {"parts": [1]}, {}, {"parts": ["x" * 41]},
                {"parts": [f"P{i}" for i in range(21)]}):
        assert c.put(url, json=bad).status_code == 400, bad
    assert c.put(url, content=b"[1]", headers={"content-type": "application/json"}).status_code == 400
    assert client_for("bob").put(url, json={"parts": ["X"]}).status_code == 404
    assert c.put(f"/api/ink/notes/{nid()}/parts", json={"parts": []}).status_code == 404
    assert c.put("/api/ink/notes/bad!/parts", json={"parts": []}).status_code == 404
    plain = nid()
    r = c.put(f"/api/ink/notes/{plain}", json={"paper": "lined", "edited_at": 1, "pages": [
        {"id": "p0", "width": 1000, "height": 1414, "strokes": []}]})
    assert r.status_code == 200 and r.json()["parts"] == []
    assert c.put(f"/api/ink/notes/{plain}/parts", json={"parts": ["X1"]}).status_code == 400
    listed = next(n for n in c.get("/api/ink/notes").json()["notes"] if n["id"] == plain)
    assert listed["parts"] == []


def test_search_finds_a_datasheet_by_its_part_number(env):
    c = client_for()
    note_id, _ = marked_note(c)
    assert c.get("/api/ink/marks", params={"q": "QX7720"}).json()["results"] == []
    c.put(f"/api/ink/notes/{note_id}/parts", json={"parts": ["ZX-100", "QX7720"]})
    got = c.get("/api/ink/marks", params={"q": "qx7720 vin"}).json()["results"]
    assert [x["mark"]["id"] for x in got] == ["m-hw1"]
    assert got[0]["parts"] == ["ZX-100", "QX7720"]
