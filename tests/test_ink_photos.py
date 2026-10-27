"""Fork: handwritten notes, stage H6: photos of paper pages (src/ink_notes.py
``prepare_photo``/``add_photos``, src/ink_transcribe.py ``photo_bands``, the photo routes
in routes/ink_routes.py).

Temporary notes database and upload store, a stand-in auth middleware, and a fake reader
in place of the vision model. Every image is drawn here with Pillow: no real photos.
"""
import asyncio
import io
import itertools
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import core.database as cdb
import routes.ink_routes as ir
from src import ink_notes as ink
from src import ink_transcribe as tr
from src import tool_utils
from src.upload_handler import UploadHandler
from tests.helpers.sqlite_db import make_temp_sqlite

PEN = {"color": "#1f1f1f", "width": 2.0, "highlighter": False}
HL = {"color": "#ffd400", "width": 14.0, "highlighter": True}


@pytest.fixture
def env(tmp_path, monkeypatch):
    factory, engine, _tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(tool_utils, "_upload_handler", UploadHandler(str(tmp_path), str(uploads)))
    monkeypatch.setattr(ink, "_LOCKS", {})
    monkeypatch.setattr(tr, "_RUNNING", set())
    yield factory
    engine.dispose()


def paper(w=800, h=1100, lines=(), fmt="JPEG", color=(250, 248, 240), **save):
    """A fictional 'photo' of a page: off-white paper with dark bars for written lines."""
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    for y in lines:
        d.rectangle((60, y, w - 80, y + 18), fill=(30, 30, 40))
    buf = io.BytesIO()
    img.save(buf, format=fmt, **save)
    return buf.getvalue()


def stroke(sid="s1", pen=PEN, y=120):
    return {"id": sid, "pen": pen, "at": 0, "points": [[100, y, 0], [180, y + 10, 16], [260, y, 32]]}


def nid():
    return str(uuid.uuid4())


def with_photos(owner="alice", *images, note_id=None, **kw):
    note_id = note_id or nid()
    prepared = [ink.prepare_photo(i) for i in (images or (paper(),))]
    return note_id, ink.add_photos(owner, note_id, prepared, **kw)


def as_doc(note, **overrides):
    """What the Write view would send back for a note it got from the hub."""
    out = {"paper": note["paper"], "edited_at": note["edited_at"] + 1,
           "pages": [{"id": p["id"], "width": p["width"], "height": p["height"],
                      "strokes": p["strokes"]} for p in note["pages"]]}
    out.update(overrides)
    return out


# ------------------------------------------------------------------ validation
def test_prepare_photo_accepts_jpeg_png_webp_and_stores_jpeg():
    for fmt in ("JPEG", "PNG", "WEBP"):
        got = ink.prepare_photo(paper(fmt=fmt))
        assert Image.open(io.BytesIO(got["jpeg"])).format == "JPEG"
        assert (got["width"], got["height"]) == (800, 1100)
        assert len(got["hash"]) == 64


def test_prepare_photo_refuses_other_types_and_garbage():
    gif = io.BytesIO()
    Image.new("RGB", (300, 300), "white").save(gif, format="GIF")
    for data in (gif.getvalue(), b"%PDF-1.4 not an image", b"\x89PNG\r\n\x1a\n broken", b""):
        with pytest.raises(ink.InkError) as exc:
            ink.prepare_photo(data)
        assert exc.value.status == 400


def test_prepare_photo_size_limits(monkeypatch):
    monkeypatch.setattr(ink, "MAX_PHOTO_BYTES", 1000)
    with pytest.raises(ink.InkError) as exc:
        ink.prepare_photo(paper())
    assert exc.value.status == 413


def test_prepare_photo_refuses_a_decompression_bomb_before_decoding():
    # A tiny PNG whose header says 20000 x 20000 (400 MP): refused from the header.
    big = io.BytesIO()
    Image.new("1", (20000, 20000), 1).save(big, format="PNG", optimize=True)
    data = big.getvalue()
    assert len(data) < 1024 * 1024
    with pytest.raises(ink.InkError) as exc:
        ink.prepare_photo(data)
    assert exc.value.status == 413


def test_prepare_photo_refuses_tiny_or_strip_images():
    for w, h in ((40, 40), (100, 2000)):
        with pytest.raises(ink.InkError):
            ink.prepare_photo(paper(w, h))


def test_prepare_photo_applies_exif_orientation_and_strips_metadata():
    img = Image.new("RGB", (1200, 600), (250, 250, 250))
    ImageDraw.Draw(img).rectangle((0, 0, 100, 600), fill=(0, 0, 0))  # dark at the left
    exif = Image.Exif()
    exif[0x0112] = 6        # orientation: rotate 90 degrees clockwise to view
    exif[0x010F] = "FictionalCam"   # Make
    exif[0x8825] = {2: (1.0, 2.0, 3.0)}  # a GPS IFD
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes(), comment=b"fictional comment",
             icc_profile=b"\0" * 128)
    got = ink.prepare_photo(buf.getvalue())
    assert (got["width"], got["height"]) == (600, 1200)  # turned upright
    out = Image.open(io.BytesIO(got["jpeg"]))
    assert out.getpixel((300, 30))[0] < 60       # the dark bar is now at the top
    assert out.getpixel((300, 900))[0] > 200
    assert not out.getexif()
    assert not ({"exif", "icc_profile", "comment"} & set(out.info))
    assert b"FictionalCam" not in got["jpeg"]


def test_prepare_photo_scales_down_and_flattens_transparency():
    img = Image.new("RGBA", (4800, 3000), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    got = ink.prepare_photo(buf.getvalue())
    assert (got["width"], got["height"]) == (2400, 1500)
    assert Image.open(io.BytesIO(got["jpeg"])).getpixel((10, 10))[0] > 240  # white, not black


# ------------------------------------------------------------------ the sidecar
def test_add_photos_creates_the_note_and_stores_files_per_owner(env, tmp_path):
    note_id, note = with_photos("alice", paper(), paper(lines=(100,)), title="Paper notes",
                                edited_at=5000)
    assert note["title"] == "Paper notes" and note["paper"] == "blank"
    assert note["edited_at"] == 5000
    assert len(note["pages"]) == 2
    for page in note["pages"]:
        assert (page["width"], page["height"]) == (1000, 1414) and page["strokes"] == []
        assert page["photo"]["file"] == f"photos/{note_id}/{page['id']}.jpg"
        path = tmp_path / "ink" / "alice" / "photos" / note_id / f"{page['id']}.jpg"
        assert path.is_file()
        assert ink.photo_file("alice", note_id, page["id"]) == path
        assert ink.photo_file("bob", note_id, page["id"]) is None   # not bob's note
    assert note["image_url"].startswith("/api/upload/")             # page 1 shows the photo
    assert ink.list_notes("alice")[0]["pages"] == 2


def test_photos_go_after_a_page_and_respect_max_pages(env, monkeypatch):
    note_id = nid()
    ink.save("alice", note_id, {"edited_at": 10, "pages": [
        {"id": "a", "strokes": [stroke()]}, {"id": "b", "strokes": []}]})
    note = ink.add_photos("alice", note_id, [ink.prepare_photo(paper())], after="a",
                          edited_at=1)
    ids = [p["id"] for p in note["pages"]]
    assert ids[0] == "a" and ids[2] == "b" and ink.is_photo(note["pages"][1])
    assert note["edited_at"] == 11          # newer than the stored edit, whatever was sent
    assert note["paper"] == "lined"         # an existing note keeps its paper
    monkeypatch.setattr(ink, "MAX_PAGES", 4)
    with pytest.raises(ink.InkError) as exc:
        ink.add_photos("alice", note_id, [ink.prepare_photo(paper())] * 2)
    assert exc.value.status == 413
    assert len(ink.load("alice", note_id)["pages"]) == 3


def test_a_save_keeps_the_photo_and_cannot_set_one(env):
    note_id, note = with_photos()
    pid = note["pages"][0]["id"]
    body = as_doc(note)
    body["pages"][0]["photo"] = {"file": "../../etc/passwd", "hash": "x"}
    body["pages"].append({"id": "p2", "strokes": [], "photo": {"file": "x", "hash": "y"}})
    got = ink.save("alice", note_id, body)
    assert got["pages"][0]["photo"] == note["pages"][0]["photo"]
    assert got["pages"][0]["id"] == pid
    assert "photo" not in got["pages"][1]


def test_photo_pages_take_only_the_highlighter(env):
    note_id, note = with_photos()
    body = as_doc(note)
    body["pages"][0]["strokes"] = [stroke("h1", HL)]
    assert ink.save("alice", note_id, body)["pages"][0]["strokes"][0]["id"] == "h1"
    body = as_doc(ink.load("alice", note_id))
    body["pages"][0]["strokes"].append(stroke("ink1"))
    with pytest.raises(ink.InkError) as exc:
        ink.save("alice", note_id, body)
    assert exc.value.status == 400


def test_removing_a_photo_page_deletes_its_file(env):
    note_id, note = with_photos("alice", paper(), paper())
    first, second = (p["id"] for p in note["pages"])
    path = ink.photo_path("alice", note_id, second)
    assert path.is_file()
    body = as_doc(note)
    body["pages"] = body["pages"][:1]
    ink.save("alice", note_id, body)
    assert not path.exists()
    assert ink.photo_path("alice", note_id, first).is_file()


def test_deleting_the_note_or_sweeping_it_deletes_its_photos(env, tmp_path):
    note_id, _ = with_photos()
    folder = ink.photo_dir("alice", note_id)
    assert folder.is_dir()
    assert ink.delete("alice", note_id)
    assert not folder.exists()
    # Deleted elsewhere (the Notes window): the sweep removes sidecar and photos.
    other, _ = with_photos()
    ink.delete_note("alice", other)
    ink.sweep("alice")
    assert not ink.photo_dir("alice", other).exists()
    # A photo folder left by a write cut short is swept too.
    stray = ink.photo_dir("alice", nid())
    stray.mkdir(parents=True)
    ink.sweep("alice")
    assert not stray.exists()


def test_a_sent_note_keeps_its_photos_until_athena_confirms(env):
    note_id, _ = with_photos()
    stored = ink.load_sidecar("alice", note_id)
    stored["athena"]["sent_hash"] = "abc"
    ink._write_json(ink.sidecar_path("alice", note_id), stored)
    ink.delete("alice", note_id)
    assert ink.photo_dir("alice", note_id).is_dir()   # kept for the Athena sweep
    ink._remove_sidecar("alice", note_id)            # what ink_athena._forget does
    assert not ink.photo_dir("alice", note_id).exists()


def test_writing_hash_follows_the_photo(env):
    _, note = with_photos()
    page = note["pages"][0]
    plain = {k: v for k, v in page.items() if k != "photo"}
    assert tr.writing_hash(page) != tr.writing_hash(plain)
    other = {**page, "photo": {**page["photo"], "hash": "0" * 64}}
    assert tr.writing_hash(page) != tr.writing_hash(other)
    # Highlighting a photo page doesn't ask for a new transcription.
    assert tr.writing_hash({**page, "strokes": [stroke("h", HL)]}) == tr.writing_hash(page)
    # A page without a photo hashes as before H6.
    assert ink.strokes_hash(plain) == ink.strokes_hash({**plain})
    assert tr.has_ink(page) and not tr.has_ink(plain)


def test_page_one_image_is_the_photo(env, monkeypatch):
    sent = []
    real = ink.save_page_image

    def capture(owner, data, filename="handwritten.png"):
        sent.append((data, filename))
        return real(owner, data, filename)

    monkeypatch.setattr(ink, "save_page_image", capture)
    note_id, note = with_photos("alice", paper(800, 1100, lines=(0,)))
    url = note["image_url"]
    assert url.startswith("/api/upload/") and len(sent) == 1
    data, filename = sent[0]
    img = Image.open(io.BytesIO(data))
    assert filename == "handwritten.jpg" and img.format == "JPEG" and img.size == (1000, 1414)
    _, y, w, h = ink.photo_box(800, 1100, 1000, 1414)
    assert y == 0 and round(w) == 1000 and round(h) == 1375
    assert img.getpixel((500, 10))[0] < 80          # the photo's top line
    assert img.getpixel((500, 1405))[0] > 245       # white paper below the photo
    # A save that doesn't change page 1 keeps that image; highlighting redraws it.
    again = ink.save("alice", note_id, as_doc(note), final=True)
    assert again["image_url"] == url and len(sent) == 1
    body = as_doc(again)
    body["pages"][0]["strokes"] = [stroke("h1", HL, y=600)]
    ink.save("alice", note_id, body, final=True)
    assert len(sent) == 2


# ------------------------------------------------------------------ transcription
def test_photo_cuts_prefer_blank_rows_and_overlap_through_writing():
    rows = [0.0] * 1000
    for y in range(0, 1000, 50):            # a line of writing every 50 rows, 20 tall
        for k in range(y + 15, y + 35):
            rows[k] = 0.3
    cuts = tr.photo_cuts(rows, 400, 40)
    assert cuts[0][0] == 0 and cuts[-1][1] == 1000
    for (top, bottom, _), (top2, _, joined) in itertools.pairwise(cuts):
        assert not joined and top2 == bottom and rows[bottom] == 0.0
    solid = [0.3] * 1000                    # no blank row at all (a drawing, a shadow)
    cuts = tr.photo_cuts(solid, 400, 40)
    for (top, bottom, _), (top2, _, joined) in itertools.pairwise(cuts):
        assert joined and bottom - top2 == 80
        assert bottom - top <= 400
    assert tr.photo_cuts([0.0] * 300, 400, 40) == [(0, 300, False)]


def test_photo_bands_are_at_most_render_px_wide_and_cover_the_photo(env):
    note_id, _ = with_photos("alice", paper(2000, 2800, lines=range(100, 2700, 90)))
    page = ink.load_sidecar("alice", note_id)["pages"][0]
    page["_photo"] = str(ink.photo_path("alice", note_id, page["id"]))
    bands = tr.page_bands(page)
    assert len(bands) >= 3
    for b in bands:
        img = Image.open(io.BytesIO(b["png"]))
        assert img.format == "JPEG" and img.width == tr.RENDER_PX and b["mime"] == "image/jpeg"
        assert b["photo"] is True
    assert bands[0]["top"] == 0 and bands[-1]["bottom"] == pytest.approx(1400, abs=1)
    del page["_photo"]
    with pytest.raises(ValueError):
        tr.page_bands(page)


def test_transcribing_a_photo_page_reads_tiles_and_joins_them(env):
    lines = list(range(80, 2300, 120))
    note_id, _ = with_photos("alice", paper(1600, 2400, lines=lines))
    seen = []

    async def fake(messages):
        user = messages[1]["content"]
        seen.append(user[0]["text"])
        assert user[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert messages[0]["content"] == tr.PROMPT
        return json.dumps({"text": f"line {len(seen)}"})

    async def go():
        got = await tr.start("alice", note_id, reader=("fake-vl", fake))
        assert got["pages"][0]["status"] == "pending" and got["pages"][0]["photo"] is True
        await tr.wait_idle()

    asyncio.run(go())
    assert len(seen) >= 2 and all("photo of a paper page" in s for s in seen)
    t = ink.load_sidecar("alice", note_id)["pages"][0]["transcript"]
    assert t["status"] == "machine" and t["model"] == "fake-vl"
    assert t["text"].split("\n") == [f"line {i + 1}" for i in range(len(seen))]
    assert "_photo" not in ink.load_sidecar("alice", note_id)["pages"][0]
    view = tr.transcripts("alice", note_id)["pages"][0]
    assert view["ink"] is True and view["photo"] is True and not view["changed"]
    # Only changed pages: the photo page is current now.
    asyncio.run(tr.start("alice", note_id, only_changed=True, reader=("fake-vl", fake)))
    assert tr.transcripts("alice", note_id)["pages"][0]["status"] == "machine"


def test_an_unreviewed_photo_page_keeps_the_note_from_being_sent(env):
    from src import ink_athena

    note_id, _ = with_photos()
    stored = ink.load_sidecar("alice", note_id)
    assert ink_athena.waiting_pages(stored) == [{"page": 1, "why": "not transcribed"}]


# ------------------------------------------------------------------ the routes
def client_for(user="alice"):
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = user
        request.state.api_token = False
        return await call_next(request)

    app.include_router(ir.setup_ink_routes())
    return TestClient(app)


def upload(c, note_id, *images, **data):
    files = [("photos", (f"page{i}.jpg", img, "image/jpeg")) for i, img in enumerate(images)]
    return c.post(f"/api/ink/notes/{note_id}/photos", files=files, data=data)


def test_routes_upload_serve_and_auth(env):
    c = client_for("alice")
    note_id = nid()
    r = upload(c, note_id, paper(), paper(fmt="PNG"), edited_at="123", title="Board")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Board" and len(body["pages"]) == 2 and body["edited_at"] == 123
    pid = body["pages"][0]["id"]
    got = c.get(f"/api/ink/notes/{note_id}/photos/{pid}")
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    assert "private" in got.headers["cache-control"]
    assert Image.open(io.BytesIO(got.content)).size == (800, 1100)
    assert c.get(f"/api/ink/notes/{note_id}").json()["pages"][0]["photo"]["width"] == 800
    # Another user: 404 for the photo and for an upload to that note.
    bob = client_for("bob")
    assert bob.get(f"/api/ink/notes/{note_id}/photos/{pid}").status_code == 404
    assert upload(bob, note_id, paper()).status_code == 404
    # Not a photo page, a bad page id, a strokes-only page.
    assert c.get(f"/api/ink/notes/{note_id}/photos/nope").status_code == 404
    assert c.get(f"/api/ink/notes/{note_id}/photos/..%2Fx").status_code == 404
    # Deleted: 410 for uploads, 404 for the photo, files gone.
    assert c.delete(f"/api/ink/notes/{note_id}").status_code == 200
    assert upload(c, note_id, paper()).status_code == 410
    assert c.get(f"/api/ink/notes/{note_id}/photos/{pid}").status_code == 404
    assert not ink.photo_dir("alice", note_id).exists()


def test_routes_refuse_bad_uploads(env, monkeypatch):
    c = client_for()
    note_id = nid()
    r = c.post(f"/api/ink/notes/{note_id}/photos", files=[("photos", ("x.txt", b"hello", "text/plain"))])
    assert r.status_code == 400
    assert c.post(f"/api/ink/notes/{note_id}/photos", data={"edited_at": "1"}).status_code == 400
    r = upload(c, note_id, paper(), edited_at="soon")
    assert r.status_code == 400
    monkeypatch.setattr(ir, "MAX_PHOTO_BODY", 1000)
    assert upload(c, note_id, paper()).status_code == 413
    monkeypatch.setattr(ir, "MAX_PHOTO_BODY", 10**9)
    too_many = [paper(200, 200)] * (ink.MAX_PHOTOS_PER_UPLOAD + 1)
    assert upload(c, note_id, *too_many).status_code in (400, 413)
    # Nothing was created by the refused uploads.
    assert c.get(f"/api/ink/notes/{note_id}").status_code == 404
    assert c.get("/api/ink/notes").json()["notes"] == []


def test_routes_add_a_photo_page_to_a_written_note(env):
    c = client_for()
    note_id = nid()
    c.put(f"/api/ink/notes/{note_id}", json={"edited_at": 50, "pages": [
        {"id": "p0", "width": 1000, "height": 1414, "strokes": [stroke()]}]})
    image = c.get(f"/api/ink/notes/{note_id}").json()["image_url"]
    r = upload(c, note_id, paper(), after="p0", edited_at="60")
    assert r.status_code == 200
    pages = r.json()["pages"]
    assert pages[0]["id"] == "p0" and ink.is_photo(pages[1])
    assert r.json()["image_url"] == image   # page 1 is still the written page
