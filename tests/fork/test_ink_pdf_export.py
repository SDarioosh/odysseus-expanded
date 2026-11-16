"""Fork: PDF datasheets, export with marks (fork/ink/pdf_export.py, the route
``GET /api/ink/notes/{id}/pdf?marks=1`` in fork/routes/ink_routes.py).

A fictional datasheet (tests/fork/pdf_fixtures.py) is imported and marked the way the
Write view saves strokes; the exported copy is reopened with pypdfium2 and its pages and
annotations (subtype, /Contents, ink paths, QuadPoints) are checked, and rendered to see
that a reader draws them. Temporary notes database and upload store.
"""
from __future__ import annotations

import ctypes
import hashlib
import uuid
from urllib.parse import unquote

import pypdfium2 as pdfium
import pypdfium2.raw as c
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.database as cdb
from fork.ink import mark_reading
from fork.ink import notes as ink
from fork.ink import pdf as ink_pdf
from fork.ink import pdf_export as px
from fork.ink import pdf_marks as pm
from fork.routes import ink_marks_routes as mr
from fork.routes import ink_routes as ir
from src import tool_utils
from src.upload_handler import UploadHandler
from tests.fork.pdf_fixtures import A4, LETTER, datasheet, make_pdf
from tests.helpers.sqlite_db import make_temp_sqlite

PEN = {"color": "#1f5fbf", "width": 2.5, "highlighter": False}
RED = {"color": "#d0021b", "width": 3.0, "highlighter": False}
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


def stroke(sid, pen, pts, at=1000):
    return {"id": sid, "pen": pen, "at": at,
            "points": [[x, y, i * 16] for i, (x, y) in enumerate(pts)]}


def span(words, *texts):
    seq = [w.text for w in words]
    for i in range(len(seq) - len(texts) + 1):
        if tuple(seq[i:i + len(texts)]) == texts:
            run = words[i:i + len(texts)]
            return (min(w.x0 for w in run), min(w.y0 for w in run),
                    max(w.x1 for w in run), max(w.y1 for w in run))
    raise AssertionError(f"{texts} not on the page: {seq}")


# ---------------------------------------------------------------- reading back
def annots(doc, index):
    """``[(subtype, contents, ink paths, quads, rect)]`` of a page of a pdfium doc."""
    page = doc[index]
    out = []
    try:
        for i in range(c.FPDFPage_GetAnnotCount(page.raw)):
            a = c.FPDFPage_GetAnnot(page.raw, i)
            try:
                n = c.FPDFAnnot_GetStringValue(a, b"Contents", None, 0)
                buf = ctypes.create_string_buffer(n)
                c.FPDFAnnot_GetStringValue(a, b"Contents",
                                           ctypes.cast(buf, ctypes.POINTER(c.FPDF_WCHAR)), n)
                text = buf.raw[:max(0, n - 2)].decode("utf-16-le")
                paths = []
                for k in range(c.FPDFAnnot_GetInkListCount(a)):
                    m = c.FPDFAnnot_GetInkListPath(a, k, None, 0)
                    arr = (c.FS_POINTF * m)()
                    c.FPDFAnnot_GetInkListPath(a, k, arr, m)
                    paths.append([(p.x, p.y) for p in arr])
                quads = []
                for k in range(c.FPDFAnnot_CountAttachmentPoints(a)):
                    q = c.FS_QUADPOINTSF()
                    c.FPDFAnnot_GetAttachmentPoints(a, k, q)
                    quads.append((q.x1, q.y1, q.x2, q.y2, q.x3, q.y3, q.x4, q.y4))
                r = c.FS_RECTF()
                c.FPDFAnnot_GetRect(a, r)
                out.append((c.FPDFAnnot_GetSubtype(a), text, paths, quads,
                            (r.left, r.bottom, r.right, r.top)))
            finally:
                c.FPDFPage_CloseAnnot(a)
    finally:
        page.close()
    return out


def page_text(doc, index):
    page = doc[index]
    try:
        tp = page.get_textpage()
        try:
            return tp.get_text_range()
        finally:
            tp.close()
    finally:
        page.close()


def pixel(doc, index, x_pt, y_top_pt):
    page = doc[index]
    try:
        img = page.render(scale=1).to_pil().convert("RGB")
    finally:
        page.close()
    return img.getpixel((int(x_pt), int(y_top_pt)))


# ------------------------------------------------------------------- the route
def marked(c_, owner="alice"):
    """A three-page datasheet: page 1 with a highlight over words, an underline and a
    highlight over no words; a blank page with ink after it; page 2 with a handwritten
    note in the margin (corrected to "check EN pull-down"). Answers the note id, the
    saved note and the words of PDF pages 1 and 2."""
    note_id = nid()
    r = c_.post(f"/api/ink/notes/{note_id}/pdf",
                files={"pdf": ("zx100-datasheet.pdf", datasheet(pages=3), "application/pdf")},
                data={"edited_at": "1000"})
    assert r.status_code == 200, r.text
    note = r.json()
    path = ink.pdf_file(owner, note_id)
    w1, w2 = ink_pdf.page_words(path, 1), ink_pdf.page_words(path, 2)
    hx0, hy0, hx1, hy1 = span(w1, "2.7", "V", "to", "5.5", "V")
    ux0, _, ux1, uy1 = span(w1, "Output", "current")
    _, ly0, lx1, ly1 = span(w2, "EN", "to", "GND", "-0.3", "V", "to", "VIN")
    mid = (hy0 + hy1) / 2
    x, y = lx1 + 60, ly0  # "EN" written in the margin: E in 4 strokes, N in 3
    writing = [stroke("e1", PEN, [(x, y), (x, y + 20)]),
               stroke("e2", PEN, [(x, y), (x + 12, y)]),
               stroke("e3", PEN, [(x, y + 10), (x + 9, y + 10)]),
               stroke("e4", PEN, [(x, y + 20), (x + 12, y + 20)]),
               stroke("n1", PEN, [(x + 18, y + 20), (x + 18, y)]),
               stroke("n2", PEN, [(x + 18, y), (x + 30, y + 20)]),
               stroke("n3", PEN, [(x + 30, y + 20), (x + 30, y)])]
    pages = [{"id": p["id"], "width": p["width"], "height": p["height"], "strokes": []}
             for p in note["pages"]]
    pages[0]["strokes"] = [
        stroke("hl1", YELLOW, [(hx0 + 2, mid), (hx1 - 2, mid)]),
        stroke("ul1", RED, [(ux0, uy1 + 4), ((ux0 + ux1) / 2, uy1 + 5), (ux1, uy1 + 4)]),
        stroke("hl2", YELLOW, [(500, 1300), (800, 1300)]),  # over no words
    ]
    pages[1]["strokes"] = writing
    blank = {"id": "blank1", "width": 1000, "height": 1414,
             "strokes": [stroke("b1", PEN, [(100, 100), (900, 100)]),
                         stroke("b2", YELLOW, [(100, 200), (900, 200)])]}
    pages.insert(1, blank)
    r = c_.put(f"/api/ink/notes/{note_id}",
               json={"paper": "blank", "edited_at": 2000, "pages": pages})
    assert r.status_code == 200, r.text
    marks = c_.get(f"/api/ink/notes/{note_id}/marks").json()["marks"]
    written = [m for m in marks if "e1" in m["strokes"]]
    assert len(written) == 1 and written[0]["kind"] == "ink"
    r = c_.put(f"/api/ink/notes/{note_id}/marks/{written[0]['id']}",
               json={"written": "check EN pull-down"})
    assert r.status_code == 200, r.text
    return note_id, r, w1


def test_export_has_the_marks_as_annotations(env):
    c_ = client_for()
    note_id, _, w1 = marked(c_)
    stored = ink.pdf_file("alice", note_id)
    before = hashlib.sha256(stored.read_bytes()).hexdigest()

    r = c_.get(f"/api/ink/notes/{note_id}/pdf?marks=1")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert unquote(cd.split("filename*=utf-8''", 1)[1]) == "zx100-datasheet (marked).pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in r.headers["cache-control"]

    doc = pdfium.PdfDocument(r.content)
    try:
        assert len(doc) == 4  # PDF page 1, the blank page, PDF pages 2 and 3
        assert "2.7 V to 5.5 V" in page_text(doc, 0)
        assert page_text(doc, 1).strip() == ""
        assert doc[1].get_size() == pytest.approx(A4)
        assert "Absolute maximum" in page_text(doc, 2)

        first = annots(doc, 0)
        kinds = sorted((a[0], a[1]) for a in first)
        assert kinds == sorted([(c.FPDF_ANNOT_HIGHLIGHT, "2.7 V to 5.5 V"),
                                (c.FPDF_ANNOT_INK, "Output current"),
                                (c.FPDF_ANNOT_INK, "")])
        hl = next(a for a in first if a[0] == c.FPDF_ANNOT_HIGHLIGHT)
        assert len(hl[3]) == 1  # one run of words on one line
        x1, y1, x2, y2, x3, y3, x4, y4 = hl[3][0]
        k = A4[0] / 1000
        hx0, hy0, hx1, hy1 = span(w1, "2.7", "V", "to", "5.5", "V")
        assert (x1, x2) == pytest.approx((hx0 * k, hx1 * k), abs=0.2)
        assert (y1, y3) == pytest.approx((A4[1] - hy0 * k, A4[1] - hy1 * k), abs=0.2)
        assert (x3, x4, y2, y4) == pytest.approx((x1, x2, y1, y3), abs=0.01)
        ul = next(a for a in first if a[1] == "Output current")
        assert len(ul[2]) == 1 and len(ul[2][0]) == 3  # one stroke, its three points

        blank = annots(doc, 1)
        assert sorted(a[0] for a in blank) == [c.FPDF_ANNOT_INK, c.FPDF_ANNOT_INK]
        assert all(a[1] == "" for a in blank)

        note = annots(doc, 2)
        assert [(a[0], a[1], len(a[2])) for a in note] == [
            (c.FPDF_ANNOT_INK, "check EN pull-down", 7)]
        assert annots(doc, 3) == []

        # A reader draws them: the ink and the highlight are on the rendered page.
        ux0, _, _, uy1 = span(w1, "Output", "current")
        assert pixel(doc, 0, (ux0 + 20) * k, (uy1 + 4.5) * k) != (255, 255, 255)
        r_, g_, b_ = pixel(doc, 0, (hx0 + 5) * k, (hy0 + 2) * k)
        assert (r_, g_, b_) != (255, 255, 255) and b_ < 150  # yellow over the paper
    finally:
        doc.close()

    # The stored PDF never changes, and the plain GET is still the original.
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == before
    plain = c_.get(f"/api/ink/notes/{note_id}/pdf")
    assert hashlib.sha256(plain.content).hexdigest() == before


def test_export_is_only_for_the_owners_pdf_notes(env):
    c_ = client_for()
    note_id, _, _ = marked(c_)
    assert client_for("bob").get(f"/api/ink/notes/{note_id}/pdf?marks=1").status_code == 404
    plain = nid()
    c_.put(f"/api/ink/notes/{plain}", json={"edited_at": 1, "pages": [
        {"id": "p0", "width": 1000, "height": 1414, "strokes": []}]})
    assert c_.get(f"/api/ink/notes/{plain}/pdf?marks=1").status_code == 404
    assert c_.get(f"/api/ink/notes/{nid()}/pdf?marks=1").status_code == 404
    ink.pdf_file("alice", note_id).unlink()
    assert c_.get(f"/api/ink/notes/{note_id}/pdf?marks=1").status_code == 404


def test_export_of_an_unmarked_datasheet_is_the_pdf_without_annotations(env):
    c_ = client_for()
    note_id = nid()
    c_.post(f"/api/ink/notes/{note_id}/pdf",
            files={"pdf": ("a/b.pdf", datasheet(pages=2), "application/pdf")})
    r = c_.get(f"/api/ink/notes/{note_id}/pdf?marks=true")
    assert r.status_code == 200
    assert 'filename="b (marked).pdf"' in r.headers["content-disposition"]
    doc = pdfium.PdfDocument(r.content)
    try:
        assert len(doc) == 2 and annots(doc, 0) == [] and annots(doc, 1) == []
    finally:
        doc.close()


# --------------------------------------------------------------------- build()
def _pdf_file(tmp_path, pages):
    path = tmp_path / "sheet.pdf"
    path.write_bytes(make_pdf(pages))
    return path


def _note_pages(info):
    return [{"id": f"pd{i}", "width": 1000,
             "height": ink_pdf.page_height(p["width_pt"], p["height_pt"]),
             "strokes": [], "pdf": p} for i, p in enumerate(info["pages"])]


def test_pages_removed_or_reordered_and_blank_pages_sizes(tmp_path):
    data = make_pdf([
        {"size": A4, "lines": [(72, 80, 14, "First page")]},
        {"size": LETTER, "lines": [(72, 80, 14, "Second page")]},
        {"size": A4, "lines": [(72, 80, 14, "Third page")]},
    ])
    path = tmp_path / "s.pdf"
    path.write_bytes(data)
    pages = _note_pages(ink_pdf.inspect_pdf(data))
    lead = {"id": "lead", "width": 1000, "height": 1414,
            "strokes": [stroke("s1", PEN, [(10, 10), (990, 1400)])]}
    tail = {"id": "tail", "width": 1000, "height": 1414, "strokes": []}
    photo = {"id": "ph", "width": 1000, "height": 1414, "strokes": [],
             "photo": {"file": "x.jpg"}}
    # A blank page first, page 2 before page 1, a photo page, page 3 removed.
    note = [lead, pages[1], photo, pages[0], tail]
    out, made = px.build(path, note, [], lambda n: [])
    assert made == 1
    doc = pdfium.PdfDocument(out)
    try:
        assert len(doc) == 4  # the photo page is left out
        assert doc[0].get_size() == pytest.approx(LETTER)  # as the PDF page after it
        assert "Second page" in page_text(doc, 1)
        assert "First page" in page_text(doc, 2)
        assert doc[3].get_size() == pytest.approx(A4)       # as the PDF page before it
        (sub, _, paths, _, _), = annots(doc, 0)
        k = min(LETTER[0] / 1000, LETTER[1] / 1414)
        x0 = (LETTER[0] - 1000 * k) / 2
        assert paths[0][0] == pytest.approx((x0 + 10 * k, LETTER[1] - 10 * k), abs=0.01)
        assert paths[0][1] == pytest.approx((x0 + 990 * k, LETTER[1] - 1400 * k), abs=0.01)
    finally:
        doc.close()
    assert path.read_bytes() == data


def test_a_note_of_blank_pages_only_is_exported_at_a4(tmp_path):
    path = _pdf_file(tmp_path, [{"size": LETTER, "lines": []}])
    out, _ = px.build(path, [{"id": "b", "width": 1000, "height": 1414, "strokes": []}],
                      [], lambda n: [])
    doc = pdfium.PdfDocument(out)
    try:
        assert len(doc) == 1 and doc[0].get_size() == pytest.approx(A4)
    finally:
        doc.close()
    with pytest.raises(ink.InkError):
        px.build(path, [{"id": "ph", "width": 1000, "height": 1414, "strokes": [],
                         "photo": {}}], [], lambda n: [])


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_rotated_pages_map_back_to_user_space(tmp_path, rotate):
    data = make_pdf([{"size": A4, "rotate": rotate,
                      "lines": [(72, 140, 11, "Input voltage range 2.7 V to 5.5 V")]}])
    path = tmp_path / "r.pdf"
    path.write_bytes(data)
    words = ink_pdf.page_words(path, 1)
    hx0, hy0, hx1, hy1 = span(words, "2.7", "V")
    pages = _note_pages(ink_pdf.inspect_pdf(data))
    mid = (hy0 + hy1) / 2
    pages[0]["strokes"] = [stroke("h", YELLOW, [(hx0 + 1, mid), (hx1 - 1, mid)]),
                           stroke("p", PEN, [(100, 50), (200, 60)])]
    marks = pm.page_marks(pages[0], 1, words)
    out, made = px.build(path, pages, marks, lambda n: words)
    assert made == 2
    doc = pdfium.PdfDocument(out)
    try:
        page = doc[0]
        try:
            to_display, scale = ink_pdf._display_map(page)
        finally:
            page.close()
        found = annots(doc, 0)
        hl = next(a for a in found if a[0] == c.FPDF_ANNOT_HIGHLIGHT)
        corners = []
        for x1, y1, x2, y2, x3, y3, x4, y4 in hl[3]:  # one per run of words
            shown = [tuple(v * scale for v in to_display(x, y))
                     for x, y in ((x1, y1), (x2, y2), (x3, y3), (x4, y4))]
            (l, t), (r, _), (_, b), _ = shown
            # top-left, top-right, bottom-left, bottom-right, as the page is shown
            assert [v for pt in shown for v in pt] == pytest.approx(
                [l, t, r, t, l, b, r, b], abs=0.01)
            assert l < r and t < b
            corners += shown
        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
        assert (min(xs), min(ys), max(xs), max(ys)) == pytest.approx(
            (hx0, hy0, hx1, hy1), abs=0.5)  # the words' boxes, wherever the page turns
        assert hl[1] == "2.7 V"
        ink_annot = next(a for a in found if a[0] == c.FPDF_ANNOT_INK)
        back = [tuple(v * scale for v in to_display(x, y)) for x, y in ink_annot[2][0]]
        assert [v for pt in back for v in pt] == pytest.approx([100, 50, 200, 60], abs=0.5)
    finally:
        doc.close()


def test_contents_of_each_kind():
    ink_mark = {"kind": "ink", "text": "", "near": "EN pin", "written": "check EN",
                "reading": "read"}
    assert px._contents(ink_mark) == "check EN"
    assert px._contents({**ink_mark, "reading": "reviewed"}) == "check EN"
    assert px._contents({**ink_mark, "written": "", "reading": "waiting"}) == ""
    assert px._contents({**ink_mark, "written": "", "reading": "failed"}) == ""
    assert px._contents({"kind": "highlight", "text": "2.7 V"}) == "2.7 V"
    assert px._contents({"kind": "underline", "text": "Output current"}) == "Output current"
    assert px._contents({"kind": "circle", "text": "18 uA"}) == "18 uA"  # other kinds: text


def test_strokes_of_one_mark_with_two_pens_and_a_pen_width_in_points(tmp_path):
    path = _pdf_file(tmp_path, [{"size": A4, "lines": []}])
    pages = _note_pages(ink_pdf.inspect_pdf(path.read_bytes()))
    pages[0]["strokes"] = [stroke("a", PEN, [(500, 500), (520, 520)]),
                           stroke("b", RED, [(530, 500), (540, 520)]),
                           stroke("c", PEN, [(550, 500)])]  # a dot
    mark = {"id": "m-a", "page_id": "pd0", "kind": "ink", "strokes": ["a", "b", "c"],
            "written": "ok", "reading": "reviewed", "text": ""}
    out, made = px.build(path, pages, [mark], lambda n: [])
    assert made == 2  # one annotation per pen; what was written on the first
    doc = pdfium.PdfDocument(out)
    try:
        found = annots(doc, 0)
        assert sorted(a[1] for a in found) == ["", "ok"]
        assert sorted(len(a[2]) for a in found) == [1, 2]
        page = doc[0]
        try:
            widths = []
            for i in range(c.FPDFPage_GetAnnotCount(page.raw)):
                a = c.FPDFPage_GetAnnot(page.raw, i)
                h, v, w = ctypes.c_float(), ctypes.c_float(), ctypes.c_float()
                c.FPDFAnnot_GetBorder(a, h, v, w)
                widths.append(round(w.value, 3))
                c.FPDFPage_CloseAnnot(a)
        finally:
            page.close()
        k = A4[0] / 1000
        assert sorted(widths) == pytest.approx(sorted([2.5 * k, 3.0 * k]), abs=0.01)
    finally:
        doc.close()


def test_word_runs_join_words_of_a_line():
    W = ink_pdf.Word
    words = [W("Input", 100, 100, 140, 112, 0), W("voltage", 145, 100, 200, 112, 0),
             W("far", 600, 100, 630, 112, 0), W("next", 100, 130, 140, 142, 1)]
    runs = px.word_runs(words, [(90, 95, 700, 145)], 0)
    assert runs == [(100, 100, 200, 112), (600, 100, 630, 112), (100, 130, 140, 142)]
    assert px.word_runs(words, [(0, 0, 10, 10)], 0) == []


def test_marked_name():
    assert px.marked_name("ZX-100 datasheet.pdf") == "ZX-100 datasheet (marked).pdf"
    assert px.marked_name("notes.PDF") == "notes (marked).pdf"
    assert px.marked_name(None) == "datasheet (marked).pdf"
