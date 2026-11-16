"""Fork: handwritten notes, PDF datasheets (fork/ink/pdf.py, and the PDF pages of
fork/ink/notes.py, fork/ink/transcribe.py and fork/ink/athena.py).

Temporary notes database and upload store. Every PDF is made by tests/fork/pdf_fixtures.py
with fictional text.
"""
import asyncio
import io
import uuid

import pytest
from PIL import Image

import core.database as cdb
from fork.ink import athena
from fork.ink import notes as ink
from fork.ink import pdf
from fork.ink import transcribe as tr
from src import tool_utils
from src.upload_handler import UploadHandler
from tests.fork.pdf_fixtures import A4, LETTER, datasheet, make_pdf
from tests.helpers.sqlite_db import make_temp_sqlite

PEN = {"color": "#1f1f1f", "width": 2.0, "highlighter": False}
HL = {"color": "#ffd400", "width": 14.0, "highlighter": True}


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


def stroke(sid="s1", pen=PEN, y=120, x=100):
    return {"id": sid, "pen": pen, "at": 0,
            "points": [[x, y, 0], [x + 80, y + 10, 16], [x + 160, y, 32]]}


def imported(owner="alice", data=None, note_id=None, **kw):
    note_id = note_id or nid()
    return note_id, ink.add_pdf(owner, note_id, data or datasheet(), name="zx-100.pdf", **kw)


def as_doc(note, **overrides):
    """What the Write view would send back for a note it got from the hub."""
    out = {"paper": note["paper"], "edited_at": note["edited_at"] + 1,
           "pages": [{"id": p["id"], "width": p["width"], "height": p["height"],
                      "strokes": p["strokes"]} for p in note["pages"]]}
    out.update(overrides)
    return out


def write_pdf(tmp_path, data, name="sheet.pdf"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------- inspect
def test_inspect_lists_pages_as_displayed():
    data = make_pdf([{"size": A4}, {"size": LETTER}, {"size": A4, "rotate": 90},
                     {"size": A4, "rotate": 270}, {"size": A4, "rotate": 180}])
    got = pdf.inspect_pdf(data)
    assert len(got["hash"]) == 64
    sizes = [(p["page"], p["width_pt"], p["height_pt"]) for p in got["pages"]]
    assert sizes == [(1, 595, 842), (2, 612, 792), (3, 842, 595), (4, 842, 595),
                     (5, 595, 842)]
    assert pdf.page_height(595, 842) == 1415 and pdf.page_height(842, 595) == 707


def test_inspect_refuses_what_is_not_a_readable_pdf():
    for data in (b"", b"hello", b"%PDF-1.7\nbroken", Image.new("RGB", (9, 9)).tobytes()):
        with pytest.raises(ink.InkError) as exc:
            pdf.inspect_pdf(data)
        assert exc.value.status == 400


def test_inspect_refuses_a_password_protected_pdf():
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter(clone_from=io.BytesIO(datasheet()))
    writer.encrypt("fictional-secret")
    out = io.BytesIO()
    writer.write(out)
    with pytest.raises(ink.InkError) as exc:
        pdf.inspect_pdf(out.getvalue())
    assert exc.value.status == 400 and "password" in str(exc.value)


def test_inspect_limits_size_pages_and_shape(monkeypatch):
    with pytest.raises(ink.InkError) as exc:
        pdf.inspect_pdf(make_pdf([{"size": (1000, 200)}]))   # 200 units tall
    assert exc.value.status == 400
    with pytest.raises(ink.InkError) as exc:
        pdf.inspect_pdf(make_pdf([{"size": (200, 900)}]))    # 4500 units tall
    assert exc.value.status == 400
    assert pdf.inspect_pdf(make_pdf([{"size": (1000, 250)}, {"size": (250, 1000)}]))
    monkeypatch.setattr(pdf, "MAX_PDF_PAGES", 2)
    with pytest.raises(ink.InkError) as exc:
        pdf.inspect_pdf(datasheet(pages=3))
    assert exc.value.status == 413
    monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 100)
    with pytest.raises(ink.InkError) as exc:
        pdf.inspect_pdf(datasheet())
    assert exc.value.status == 413


def test_tiers():
    assert [pdf.tier(w) for w in (1, 600, 601, 1200, 1999, 2000, 2500, 9000)] == \
        [600, 600, 1200, 1200, 2000, 2000, 3000, 3000]


# ---------------------------------------------------------------- render, words
def _dark(img, box):
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    crop = img.crop((x0, y0, x1 + 1, y1 + 1)).convert("L")
    return min(crop.getextrema()[0], 255)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_render_and_words_line_up_as_displayed(tmp_path, rotate):
    data = make_pdf([{"size": A4, "rotate": rotate, "lines": [
        (72, 80, 20, "ZX-100 Buck Regulator"), (72, 140, 11, "Input voltage 2.7 V")]}])
    path = write_pdf(tmp_path, data)
    words = pdf.page_words(path, 1)
    assert [w.text for w in words if w.line == words[0].line]  # grouped into lines
    texts = {w.text: w for w in words}
    assert {"ZX-100", "Buck", "Regulator", "Input", "voltage", "2.7", "V"} <= set(texts)
    assert len({w.line for w in words}) == 2
    assert [w.line for w in words] == sorted(w.line for w in words)
    png = pdf.render_page(path, 1, 1000)
    img = Image.open(io.BytesIO(png))
    tall = rotate in (0, 180)
    assert img.format == "PNG" and img.size == ((1000, 1415) if tall else (1000, 707))
    # Each word box (page units = pixels at 1000 wide) has ink in it; the page corner
    # far from any text is white.
    for w in words:
        assert 0 <= w.x0 < w.x1 <= 1000 and 0 <= w.y0 < w.y1 <= img.height
        assert _dark(img, (w.x0, w.y0, w.x1, w.y1)) < 100, w
    assert img.getpixel((500, img.height // 2))[0] > 245


def test_words_are_in_page_units_top_left():
    # Upright A4: "Input" starts 72 pt from the left, its baseline 140 pt from the top.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = write_pdf(Path(tmp), datasheet())
        words = pdf.page_words(path, 1)
        first = next(w for w in words if w.text == "Input")
        k = 1000 / 595
        assert abs(first.x0 - 72 * k) < 3
        assert first.y0 < 140 * k < first.y1          # the baseline is inside the box
        title = next(w for w in words if w.text == "ZX-100")
        assert title.line == 0 and first.line == 1 and title.y1 < first.y0
        assert pdf.page_words(path, 2)[0].text == "Absolute"
        with pytest.raises(ValueError):
            pdf.page_words(path, 3)


def test_render_is_exactly_the_tier_wide(tmp_path):
    path = write_pdf(tmp_path, make_pdf([{"size": (612.3, 791.7)}]))
    for width in pdf.WIDTH_TIERS:
        img = Image.open(io.BytesIO(pdf.render_page(path, 1, width)))
        assert img.width == width and img.height == round(width * 791.7 / 612.3)
    with pytest.raises(ValueError):
        pdf.render_page(path, 2, 600)
    (tmp_path / "broken.pdf").write_bytes(b"not a pdf")
    with pytest.raises(ValueError):
        pdf.render_page(tmp_path / "broken.pdf", 1, 600)
    with pytest.raises(OSError):
        pdf.render_page(tmp_path / "missing.pdf", 1, 600)


def test_words_are_cached_by_file_and_mtime(tmp_path, monkeypatch):
    path = write_pdf(tmp_path, datasheet())
    calls = []
    real = pdf._read_words

    def counting(p, n):
        calls.append(n)
        return real(p, n)

    monkeypatch.setattr(pdf, "_read_words", counting)
    monkeypatch.setattr(pdf, "_WORDS", type(pdf._WORDS)())
    first = pdf.page_words(path, 1)
    assert pdf.page_words(path, 1) == first and calls == [1]
    first.clear()                                   # callers get their own list
    assert pdf.page_words(path, 1)
    import os

    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    pdf.page_words(path, 1)
    assert calls == [1, 1]
    monkeypatch.setattr(pdf, "WORDS_CACHE_SIZE", 1)
    pdf.page_words(path, 2)
    assert len(pdf._WORDS) == 1


# ------------------------------------------------------------------ the note
def test_add_pdf_makes_one_note_of_pdf_pages(env, tmp_path):
    data = make_pdf([{"size": A4}, {"size": A4, "rotate": 90}])
    note_id, note = imported("alice", data, edited_at=777)
    assert note["title"] == "zx-100" and note["paper"] == "blank" and note["edited_at"] == 777
    assert note["pdf"]["name"] == "zx-100.pdf" and note["pdf"]["pages"] == 2
    assert note["pdf"]["file"] == f"pdfs/{note_id}.pdf"
    assert note["pdf"]["hash"] == pdf.inspect_pdf(data)["hash"]
    first, second = note["pages"]
    assert first["id"].startswith("pd") and len(first["id"]) == 14
    assert (first["width"], first["height"]) == (1000, 1415)
    assert (second["width"], second["height"]) == (1000, 707)
    assert first["pdf"] == {"page": 1, "width_pt": 595.0, "height_pt": 842.0}
    assert second["pdf"] == {"page": 2, "width_pt": 842.0, "height_pt": 595.0}
    assert first["strokes"] == [] and first["transcript"]["status"] == "none"
    path = tmp_path / "ink" / "alice" / "pdfs" / f"{note_id}.pdf"
    assert path.read_bytes() == data
    assert ink.pdf_file("alice", note_id) == path
    assert ink.pdf_file("bob", note_id) is None
    assert ink.load("alice", note_id)["pdf"] == note["pdf"]
    listed = ink.list_notes("alice")[0]
    assert listed["pdf"] == {"name": "zx-100.pdf", "pages": 2} and listed["pages"] == 2
    assert note["image_url"].startswith("/api/upload/")


def test_add_pdf_titles_names_and_paper(env):
    _, note = imported(title="  My sheet ", paper="dotted")
    assert note["title"] == "My sheet" and note["paper"] == "dotted"
    note_id = nid()
    note = ink.add_pdf("alice", note_id, datasheet(), name="C:\\data\\a\x07b.PDF")
    assert note["pdf"]["name"] == "a b.PDF" and note["title"] == "a b"
    assert ink.clean_pdf_name("") == "datasheet.pdf"
    long = ink.clean_pdf_name("x" * 300 + ".pdf")
    assert len(long) == 200 and long.endswith(".pdf")


def test_a_pdf_starts_a_new_note(env):
    note_id, _ = imported()
    with pytest.raises(ink.InkError) as exc:
        ink.add_pdf("alice", note_id, datasheet())
    assert exc.value.status == 409
    with pytest.raises(ink.InkError) as exc:
        ink.add_pdf("bob", note_id, datasheet())       # another owner's id
    assert exc.value.status == 404
    written = nid()
    ink.save("alice", written, {"edited_at": 1, "pages": [{"id": "p", "strokes": []}]})
    with pytest.raises(ink.InkError) as exc:
        ink.add_pdf("alice", written, datasheet())
    assert exc.value.status == 409
    ink.delete("alice", note_id)
    with pytest.raises(ink.InkError) as exc:
        ink.add_pdf("alice", note_id, datasheet())
    assert exc.value.status in (404, 410)


def test_a_refused_pdf_leaves_nothing(env, tmp_path):
    note_id = nid()
    with pytest.raises(ink.InkError):
        ink.add_pdf("alice", note_id, b"%PDF-1.7 nothing here")
    assert ink.load("alice", note_id) is None
    assert not ink.note_id_taken(note_id)
    assert not (tmp_path / "ink" / "alice" / "pdfs").exists()


def test_page_one_image_is_the_pdf_page_with_its_ink(env, monkeypatch):
    sent = []
    real = ink.save_page_image

    def capture(owner, data, filename="handwritten.png"):
        sent.append((data, filename))
        return real(owner, data, filename)

    monkeypatch.setattr(ink, "save_page_image", capture)
    note_id, note = imported()
    data, filename = sent[0]
    img = Image.open(io.BytesIO(data))
    assert filename == "handwritten.jpg" and img.format == "JPEG" and img.size == (1000, 1415)
    title = next(w for w in pdf.page_words(ink.pdf_file("alice", note_id), 1)
                 if w.text == "ZX-100")
    assert _dark(img, (title.x0, title.y0, title.x1, title.y1)) < 100
    body = as_doc(note)
    body["pages"][0]["strokes"] = [stroke("ink1", y=900)]
    ink.save("alice", note_id, body, final=True)
    img = Image.open(io.BytesIO(sent[-1][0]))
    assert len(sent) == 2 and _dark(img, (100, 895, 260, 915)) < 100   # the ink is on it


def test_a_save_keeps_pdf_pages_and_cannot_set_or_change_them(env):
    note_id, note = imported()
    body = as_doc(note)
    body["pages"][0]["strokes"] = [stroke("ink1"), stroke("h1", HL, y=300)]  # pen and highlighter
    body["pages"][0]["pdf"] = {"page": 2, "width_pt": 1, "height_pt": 1}
    body["pages"].insert(1, {"id": "blank1", "width": 1000, "height": 1414, "strokes": [],
                             "pdf": {"page": 1}})
    body["pdf"] = {"file": "../../etc/passwd"}
    got = ink.save("alice", note_id, body)
    assert got["pdf"] == note["pdf"]
    assert got["pages"][0]["pdf"] == note["pages"][0]["pdf"]
    assert [s["id"] for s in got["pages"][0]["strokes"]] == ["ink1", "h1"]
    assert got["pages"][1]["id"] == "blank1" and "pdf" not in got["pages"][1]
    assert got["pages"][2]["pdf"]["page"] == 2


@pytest.mark.parametrize("which,height", [(0, 1414), (0, 1416), ("new", 1415)])
def test_page_heights_are_held_to_the_stored_size(env, which, height):
    note_id, note = imported()
    body = as_doc(note)
    if which == "new":   # a blank page must be the Write view's size
        body["pages"].append({"id": "x", "width": 1000, "height": height, "strokes": []})
    else:                # a PDF page keeps its PDF page's height
        body["pages"][which]["height"] = height
    with pytest.raises(ink.InkError) as exc:
        ink.save("alice", note_id, body)
    assert exc.value.status == 400


def test_ink_may_reach_the_bottom_of_a_tall_pdf_page(env):
    note_id, note = imported(data=make_pdf([{"size": (300, 1000)}]))   # 3333 units tall
    body = as_doc(note)
    body["pages"][0]["strokes"] = [stroke("low", y=3300)]
    assert ink.save("alice", note_id, body)["pages"][0]["strokes"][0]["id"] == "low"


def test_blank_pages_count_toward_max_pages_pdf_pages_do_not(env, monkeypatch):
    monkeypatch.setattr(ink, "MAX_PAGES", 2)
    note_id, note = imported(data=datasheet(pages=4))
    body = as_doc(note)
    blank = {"width": 1000, "height": 1414, "strokes": []}
    body["pages"][1:1] = [{"id": "b1", **blank}, {"id": "b2", **blank}]
    got = ink.save("alice", note_id, body)
    assert len(got["pages"]) == 6
    body = as_doc(got)
    body["pages"].append({"id": "b3", **blank})
    with pytest.raises(ink.InkError):
        ink.save("alice", note_id, body)
    with pytest.raises(ink.InkError) as exc:   # photos count like blank pages
        ink.add_photos("alice", note_id, [{"jpeg": b"x", "width": 9, "height": 9, "hash": "h"}])
    assert exc.value.status == 413


def test_page_images_are_cached_and_follow_the_note(env, tmp_path):
    note_id, note = imported()
    first, second = (p["id"] for p in note["pages"])
    path = ink.pdf_page_image("alice", note_id, first, 600)
    assert path == tmp_path / "ink" / "alice" / "pdfs" / "cache" / note_id / f"{first}-600.png"
    assert Image.open(path).size == (600, round(600 * 842 / 595))
    stamp = path.stat().st_ino  # a new render is a new file (atomic write)
    assert ink.pdf_page_image("alice", note_id, first, 600) == path
    assert path.stat().st_ino == stamp                    # served from the cache
    assert ink.pdf_page_image("bob", note_id, first, 600) is None
    assert ink.pdf_page_image("alice", note_id, "nope", 600) is None
    assert ink.pdf_page_image("alice", note_id, "../x", 600) is None
    other = ink.pdf_page_image("alice", note_id, second, 600)
    # A save that drops page 2 drops its images, not the PDF.
    body = as_doc(note)
    body["pages"] = body["pages"][:1]
    ink.save("alice", note_id, body)
    assert not other.exists() and path.exists()
    assert ink.pdf_file("alice", note_id).is_file()
    # Deleting the note takes the file and the cache.
    assert ink.delete("alice", note_id)
    assert not ink.pdf_path("alice", note_id).exists()
    assert not ink.pdf_cache_dir("alice", note_id).exists()


def test_a_written_page_has_no_pdf_image(env):
    note_id = nid()
    ink.save("alice", note_id, {"edited_at": 1, "pages": [{"id": "p0", "strokes": []}]})
    assert ink.pdf_page_image("alice", note_id, "p0", 600) is None
    assert ink.pdf_file("alice", note_id) is None
    assert ink.list_notes("alice")[0]["pdf"] is None


def test_the_sweep_removes_pdfs_of_gone_notes(env):
    note_id, note = imported()
    ink.pdf_page_image("alice", note_id, note["pages"][0]["id"], 600)
    ink.delete_note("alice", note_id)        # deleted in the Notes window
    ink.sweep("alice")
    assert not ink.pdf_path("alice", note_id).exists()
    assert not ink.pdf_cache_dir("alice", note_id).exists()
    # Leftovers of an import or render cut short are swept too.
    stray = nid()
    ink.pdf_path("alice", stray).parent.mkdir(parents=True, exist_ok=True)
    ink.pdf_path("alice", stray).write_bytes(b"%PDF")
    ink.pdf_cache_dir("alice", stray).mkdir(parents=True)
    ink.sweep("alice")
    assert not ink.pdf_path("alice", stray).exists()
    assert not ink.pdf_cache_dir("alice", stray).exists()


def test_photos_added_to_a_datasheet_keep_it_a_datasheet(env):
    photo = io.BytesIO()
    Image.new("RGB", (800, 1100), (250, 248, 240)).save(photo, format="JPEG")
    note_id, note = imported()
    got = ink.add_photos("alice", note_id, [ink.prepare_photo(photo.getvalue())],
                         after=note["pages"][0]["id"])
    assert got["pdf"] == note["pdf"] and ink.is_photo(got["pages"][1])


# ------------------------------------------------------------- transcription
def test_a_pdf_page_is_read_as_ink_only(env):
    note_id, note = imported()
    page = ink.load_sidecar("alice", note_id)["pages"][0]
    assert not tr.has_ink(page) and tr.page_bands(page) == []
    assert not tr.needs_transcription(page)
    page["strokes"] = [stroke("h1", HL)]
    assert not tr.has_ink(page)              # the highlighter is never read
    page["strokes"].append(stroke("ink1", y=500))
    assert tr.has_ink(page)
    bands = tr.page_bands(page)
    assert len(bands) == 1
    img = Image.open(io.BytesIO(bands[0]["png"]))
    # Only the pen stroke: no PDF text, no highlighter, white around it.
    lo, hi = img.convert("L").getextrema()
    assert lo < 100 and hi == 255
    assert bands[0]["top"] > 400
    view = tr.page_view(page, 0)
    assert view["pdf"] is True and view["photo"] is False


def test_writing_hash_tells_pdf_pages_apart(env):
    _, note = imported()
    a, b = note["pages"]
    plain = {k: v for k, v in a.items() if k != "pdf"}
    assert tr.writing_hash(a) != tr.writing_hash(plain)
    assert tr.writing_hash(a) != tr.writing_hash({**b, "height": a["height"]})
    assert tr.writing_hash({**a, "strokes": [stroke("h", HL)]}) == tr.writing_hash(a)


def test_a_pdf_page_without_ink_never_waits_for_review(env):
    note_id, note = imported()
    stored = ink.load_sidecar("alice", note_id)
    assert athena.waiting_pages(stored) == []
    body = as_doc(note)
    body["pages"][1]["strokes"] = [stroke("ink1")]
    ink.save("alice", note_id, body)
    stored = ink.load_sidecar("alice", note_id)
    assert athena.waiting_pages(stored) == [{"page": 2, "why": "not transcribed"}]


# --------------------------------------------------------------------- Athena
def test_a_datasheet_is_never_sent_to_athena(env, monkeypatch):
    note_id, note = imported()
    body = as_doc(note)
    body["pages"][0]["strokes"] = [stroke("ink1")]
    ink.save("alice", note_id, body)
    stored = ink.load_sidecar("alice", note_id)
    page = stored["pages"][0]
    page["transcript"].update(text="check the input range", status="reviewed",
                              strokes_hash=tr.writing_hash(page))
    ink._write_json(ink.sidecar_path("alice", note_id), stored)
    with pytest.raises(ink.InkError) as exc:
        asyncio.run(athena.send("alice", note_id))
    assert exc.value.status == 400 and str(exc.value) == "Datasheets stay in Odysseus"
    monkeypatch.setattr(tr, "auto_send_enabled", lambda: True)
    assert asyncio.run(athena.auto_send("alice", note_id)) is None
    stored = ink.load_sidecar("alice", note_id)
    assert athena.state(stored) == "not_sent" and athena.view(stored)["datasheet"] is True
    assert ink.list_notes("alice")[0]["athena"] == "not_sent"
    with pytest.raises(ink.InkError) as exc:   # not bob's note
        asyncio.run(athena.send("bob", note_id))
    assert exc.value.status == 404


def test_tall_pages_are_rendered_narrower_to_cap_pixels():
    assert pdf.capped_width(3000, 1414) == 3000          # A4 at the widest tier: as asked
    assert pdf.capped_width(3000, 1294) == 3000          # Letter
    w = pdf.capped_width(3000, 4000)                      # the tallest page allowed
    assert w < 3000 and w * w * 4 <= pdf.MAX_RENDER_PIXELS
    assert pdf.capped_width(600, 4000) == 600            # small tiers untouched


def test_page_cache_drops_least_recently_used(tmp_path):
    import os
    cache = tmp_path / "cache"
    cache.mkdir()
    paths = []
    for i in range(5):
        p = cache / f"p{i}-600.png"
        p.write_bytes(b"x" * 100)
        os.utime(p, (1000 + i, 1000 + i))
        paths.append(p)
    ink._trim_pdf_cache(cache, keep=paths[0], limit=350)  # 500 bytes: trim to <= 280
    left = sorted(p.name for p in cache.iterdir())
    assert "p0-600.png" in left                            # just rendered: kept
    assert "p4-600.png" in left and "p1-600.png" not in left
    assert sum(p.stat().st_size for p in cache.iterdir()) <= 280
    ink._trim_pdf_cache(cache, keep=paths[0], limit=10_000)  # under the limit: nothing goes
    assert len(list(cache.iterdir())) == len(left)
