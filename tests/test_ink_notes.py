"""Fork: handwritten notes, H1 sidecar, validation, render and sweep (src/ink_notes.py).

Runs on a temporary notes database and a temporary upload store. Fictional strokes only.
"""
import io
import json
import uuid

import pytest
from PIL import Image

import core.database as cdb
from core.database import Note
from src import ink_notes as ink
from src import tool_utils
from src.upload_handler import UploadHandler
from tests.helpers.sqlite_db import make_temp_sqlite

BLACK = {"color": "#1f1f1f", "width": 2.5, "highlighter": False}
YELLOW = {"color": "#ffd400", "width": 18, "highlighter": True}


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


def nid():
    return str(uuid.uuid4())


def stroke(sid="s1", pen=BLACK, y=100, n=6):
    return {"id": sid, "pen": dict(pen), "at": 1_793_000_000_000,
            "points": [[100 + 40 * i, y + (i % 2) * 6, 16 * i] for i in range(n)]}


def body(*pages, edited_at=1000, ruled=True):
    pages = pages or ([stroke()],)
    return {"ruled": ruled, "edited_at": edited_at,
            "pages": [{"id": f"p{i}", "width": 1000, "height": 1414, "strokes": strokes}
                      for i, strokes in enumerate(pages)]}


def note_row(factory, note_id):
    s = factory()
    try:
        return s.query(Note).filter(Note.id == note_id).first()
    finally:
        s.close()


# ------------------------------------------------------------------- round trip
def test_first_save_creates_a_handwritten_note_and_round_trips(env):
    note_id = nid()
    doc = body([stroke("a"), stroke("b", YELLOW, y=300)], [stroke("c", y=500)])
    saved = ink.save("alice", note_id, doc, title="Shopping ideas")
    assert saved["title"] == "Shopping ideas"
    assert saved["link"] == f"/#open=notes&note={note_id}"
    row = note_row(env, note_id)
    assert (row.owner, row.label, row.note_type, row.content) == ("alice", "handwritten", "note", "")
    assert row.image_url and row.image_url.startswith("/api/upload/")

    loaded = ink.load("alice", note_id)
    assert [p["id"] for p in loaded["pages"]] == ["p0", "p1"]
    assert [s["id"] for s in loaded["pages"][0]["strokes"]] == ["a", "b"]
    assert loaded["pages"][0]["strokes"][1]["pen"] == YELLOW
    assert loaded["pages"][0]["strokes"][0]["points"] == doc["pages"][0]["strokes"][0]["points"]
    assert loaded["pages"][0]["transcript"]["status"] == "none"
    assert loaded["athena"] == {"sent_hash": None, "sent_at": None, "conversation_id": None}
    assert loaded["version"] == 1 and loaded["ruled"] is True

    path = ink.sidecar_path("alice", note_id)
    assert path.parent.name == "alice" and path.parent.parent.name == "ink"
    on_disk = json.loads(path.read_text())
    assert on_disk["pages"][1]["strokes"][0]["id"] == "c"


def test_later_save_keeps_transcript_and_athena_state(env):
    note_id = nid()
    ink.save("alice", note_id, body(edited_at=1))
    path = ink.sidecar_path("alice", note_id)
    data = json.loads(path.read_text())
    data["pages"][0]["transcript"] = {"text": "milk", "model": "m", "status": "reviewed",
                                      "strokes_hash": "x", "at": "2026-10-26T10:00:00+00:00"}
    data["athena"] = {"sent_hash": "h", "sent_at": "t", "conversation_id": "c1"}
    path.write_text(json.dumps(data))
    sneaky = body([stroke(), stroke("s2", y=200)], edited_at=2)
    sneaky["pages"][0]["transcript"] = {"text": "injected", "status": "reviewed"}
    sneaky["athena"] = {"sent_hash": "forged"}
    saved = ink.save("alice", note_id, sneaky)
    assert saved["pages"][0]["transcript"]["text"] == "milk"
    assert saved["athena"]["sent_hash"] == "h"
    assert len(saved["pages"][0]["strokes"]) == 2


def test_last_edit_wins(env):
    note_id = nid()
    ink.save("alice", note_id, body([stroke("new")], edited_at=2000))
    with pytest.raises(ink.Stale) as exc:
        ink.save("alice", note_id, body([stroke("old")], edited_at=1000))
    assert exc.value.status == 409
    assert exc.value.current["pages"][0]["strokes"][0]["id"] == "new"
    # The same edit again (a retried upload) is fine.
    ink.save("alice", note_id, body([stroke("new2")], edited_at=2000))
    assert ink.load("alice", note_id)["pages"][0]["strokes"][0]["id"] == "new2"


def test_title_change_and_default_title(env):
    note_id = nid()
    saved = ink.save("alice", note_id, body())
    assert saved["title"].startswith("Handwritten ")
    ink.save("alice", note_id, body(edited_at=2000), title="Renamed")
    assert note_row(env, note_id).title == "Renamed"


# -------------------------------------------------------------------- validation
@pytest.mark.parametrize("bad", [
    None,
    {"pages": []},
    {"pages": [{"id": "p0", "strokes": [{"id": "s", "pen": BLACK, "points": []}]}]},
    {"pages": [{"id": "p0", "strokes": [{"id": "s", "pen": {"color": "red", "width": 2},
                                         "points": [[1, 2, 0]]}]}]},
    {"pages": [{"id": "p0", "strokes": [{"id": "s", "pen": BLACK, "points": [[1e9, 2, 0]]}]}]},
    {"pages": [{"id": "p0", "strokes": [{"id": "s", "pen": BLACK, "points": [["x", 2, 0]]}]}]},
    {"pages": [{"id": "p0", "strokes": []}, {"id": "p0", "strokes": []}]},
    {"pages": [{"id": "../x", "strokes": []}]},
])
def test_malformed_saves_are_refused_and_store_nothing(env, bad):
    note_id = nid()
    with pytest.raises(ink.InkError):
        ink.save("alice", note_id, bad)
    assert note_row(env, note_id) is None
    assert not ink.sidecar_path("alice", note_id).exists()


def test_note_ids_cannot_escape_the_ink_folder(env):
    for bad in ("../../etc/passwd", "a/b", "short", "", "x" * 80, "has_underscore_123"):
        assert not ink.valid_note_id(bad)
        with pytest.raises(ink.InkError):
            ink.sidecar_path("alice", bad)


def test_odd_owner_names_get_their_own_folder(env):
    assert ink.ink_dir(None).name == "_single"
    assert ink.ink_dir("alice").name == "alice"
    weird = ink.ink_dir("../bob")
    assert weird.name.startswith("u-") and weird.parent.name == "ink"
    assert ink.ink_dir("a/b") != ink.ink_dir("a_b")


# ------------------------------------------------------------------------ render
def test_render_is_stable_and_shows_the_ink(env):
    page = ink.clean_document(body([stroke("a"), stroke("h", YELLOW, y=300)]))["pages"][0]
    one, two = ink.render_page_png(page), ink.render_page_png(json.loads(json.dumps(page)))
    assert one == two
    img = Image.open(io.BytesIO(one)).convert("RGB")
    assert img.size == (1000, 1414)
    assert img.getpixel((100, 100))[0] < 80  # black ink
    hl = img.getpixel((140, 303))
    assert hl[2] < 230 and hl[0] > 200  # light yellow, under any ink
    assert img.getpixel((900, 1300)) == (255, 255, 255)  # paper, no ruling
    other = dict(page, strokes=page["strokes"][:1])
    assert ink.render_page_png(other) != one


def test_page_image_is_only_redrawn_when_page_one_changes(env):
    note_id = nid()
    ink.save("alice", note_id, body([stroke("a")], [], edited_at=1), now=1000)
    first = note_row(env, note_id).image_url
    ink.save("alice", note_id, body([stroke("a")], [stroke("b")], edited_at=2), now=2000)
    assert note_row(env, note_id).image_url == first  # page 2 only
    ink.save("alice", note_id, body([stroke("a"), stroke("c", y=400)], edited_at=3), now=3000)
    assert note_row(env, note_id).image_url != first


def test_page_image_waits_while_writing_unless_the_page_is_left(env):
    note_id = nid()
    ink.save("alice", note_id, body([stroke("a")], edited_at=1), now=1000)
    first = note_row(env, note_id).image_url
    ink.save("alice", note_id, body([stroke("a"), stroke("b", y=300)], edited_at=2), now=1005)
    assert note_row(env, note_id).image_url == first  # autosave while writing
    ink.save("alice", note_id, body([stroke("a"), stroke("b", y=300)], edited_at=2), now=1006,
             final=True)                             # the view saves on leaving
    second = note_row(env, note_id).image_url
    assert second != first
    ink.save("alice", note_id, body([stroke("a"), stroke("b", y=300), stroke("c", y=500)],
                                    edited_at=3), now=1006 + ink.RENDER_MIN_SECONDS)
    assert note_row(env, note_id).image_url not in (first, second)  # a minute later


def test_empty_first_page_has_no_image(env):
    note_id = nid()
    ink.save("alice", note_id, body([], edited_at=1))
    assert note_row(env, note_id).image_url is None


# -------------------------------------------------------------- owner and delete
def test_another_owners_note_is_invisible(env):
    note_id = nid()
    ink.save("alice", note_id, body())
    assert ink.load("bob", note_id) is None
    assert ink.delete("bob", note_id) is False
    with pytest.raises(ink.InkError) as exc:
        ink.save("bob", note_id, body(edited_at=5000))
    assert exc.value.status == 404
    assert ink.load("alice", note_id)["pages"][0]["strokes"][0]["id"] == "s1"
    assert [n["id"] for n in ink.list_notes("bob")] == []


def test_delete_removes_note_and_sidecar_and_blocks_a_late_save(env):
    note_id = nid()
    ink.save("alice", note_id, body())
    assert ink.delete("alice", note_id) is True
    assert note_row(env, note_id) is None
    assert not ink.sidecar_path("alice", note_id).exists()
    with pytest.raises(ink.Gone):
        ink.save("alice", note_id, body(edited_at=9999))  # queued on a tablet
    assert note_row(env, note_id) is None


# ------------------------------------------------------------------------- sweep
def test_sweep_removes_sidecars_of_notes_deleted_elsewhere(env):
    keep, gone, archived = nid(), nid(), nid()
    for note_id in (keep, gone, archived):
        ink.save("alice", note_id, body())
    s = env()
    s.delete(s.query(Note).filter(Note.id == gone).first())
    s.query(Note).filter(Note.id == archived).first().archived = True
    s.commit()
    s.close()
    assert ink.sweep("alice") == [gone]
    assert ink.sidecar_ids("alice") == sorted([keep, archived])
    assert ink.sweep("alice") == []
    with pytest.raises(ink.Gone):
        ink.save("alice", gone, body(edited_at=9999))


def test_list_runs_the_sweep_and_lists_newest_first(env):
    a, b = nid(), nid()
    ink.save("alice", a, body([stroke()], [stroke()]), title="First")
    ink.save("alice", b, body(), title="Second")
    s = env()
    s.add(Note(id=nid(), owner="alice", title="Typed", label="other", note_type="note"))
    s.commit()
    s.close()
    listed = ink.list_notes("alice")
    assert {n["title"] for n in listed} == {"First", "Second"}
    first = next(n for n in listed if n["id"] == a)
    assert first["pages"] == 2 and first["link"] == f"/#open=notes&note={a}"
    ink.delete_note("alice", a)  # deleted in the Notes window
    assert [n["id"] for n in ink.list_notes("alice")] == [b]
    assert ink.sidecar_ids("alice") == [b]


def test_strokes_hash_follows_the_strokes(env):
    page = ink.clean_document(body())["pages"][0]
    same = json.loads(json.dumps(page))
    assert ink.strokes_hash(page) == ink.strokes_hash(same)
    same["strokes"][0]["points"][0][0] += 1
    assert ink.strokes_hash(page) != ink.strokes_hash(same)


# ------------------------------------------------- review findings (PR 88 QC report)
def _plain_note(factory, owner="alice", label="work", note_type="checklist", title="Groceries"):
    note_id = nid()
    s = factory()
    s.add(Note(id=note_id, owner=owner, title=title, label=label, note_type=note_type))
    s.commit()
    s.close()
    return note_id


@pytest.mark.parametrize("label", ["work", "odysseus-sound", None])
def test_other_notes_of_the_same_owner_are_never_touched(env, label):
    note_id = _plain_note(env, label=label)
    with pytest.raises(ink.InkError) as exc:
        ink.save("alice", note_id, body(), title="Hijacked")
    assert exc.value.status == 404
    assert ink.load("alice", note_id) is None
    assert ink.delete("alice", note_id) is False
    row = note_row(env, note_id)
    assert row is not None and row.title == "Groceries" and row.image_url is None
    assert not ink.sidecar_path("alice", note_id).exists()


def test_relabelled_note_keeps_its_strokes_but_leaves_write(env):
    note_id = nid()
    ink.save("alice", note_id, body())
    s = env()
    s.query(Note).filter(Note.id == note_id).first().label = "work"
    s.commit()
    s.close()
    assert ink.sweep("alice") == []
    assert ink.sidecar_path("alice", note_id).exists()
    assert ink.list_notes("alice") == []
    with pytest.raises(ink.InkError) as exc:
        ink.save("alice", note_id, body(edited_at=9999))
    assert exc.value.status == 404  # not Gone: its strokes stay


def test_sweep_never_tombstones_a_note_that_still_exists(env):
    a, b = nid(), nid()
    ink.save("alice", a, body())
    ink.save("alice", b, body())
    ink.delete_note("alice", b)
    assert ink.sweep("alice") == [b]
    assert not ink.is_deleted("alice", a) and ink.is_deleted("alice", b)
    assert ink.sidecar_path("alice", a).exists()


def test_concurrent_deletes_keep_every_tombstone(env):
    import threading

    ids = [nid() for _ in range(12)]
    for note_id in ids:
        ink.save("alice", note_id, body())
    threads = [threading.Thread(target=ink.delete, args=("alice", i)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(ink.is_deleted("alice", i) for i in ids)


@pytest.mark.parametrize("size", [{"width": 4000}, {"height": 20000}, {"width": 999}])
def test_pages_must_have_the_write_view_size(env, size):
    doc = body()
    doc["pages"][0].update(size)
    with pytest.raises(ink.InkError):
        ink.clean_document(doc)


def test_empty_title_is_kept_and_non_text_titles_are_refused(env):
    note_id = nid()
    ink.save("alice", note_id, body(), title="My Title")
    assert ink.save("alice", note_id, body(edited_at=2000), title="")["title"] == ""
    assert ink.save("alice", note_id, body(edited_at=3000))["title"] == ""  # no title sent
    for bad in (["a", "b"], 5, {"x": 1}):
        with pytest.raises(ink.InkError):
            ink.save("alice", note_id, body(edited_at=4000), title=bad)
    assert note_row(env, note_id).title == ""
    fresh = nid()
    assert ink.save("alice", fresh, body(), title="")["title"] == ""


def test_failed_first_save_leaves_no_note_behind(env, monkeypatch):
    note_id = nid()

    def boom(*_a, **_k):
        raise RuntimeError("render failed")

    monkeypatch.setattr(ink, "render_page_png", boom)
    with pytest.raises(RuntimeError):
        ink.save("alice", note_id, body())
    assert note_row(env, note_id) is None
    assert not ink.sidecar_path("alice", note_id).exists()
    monkeypatch.undo()
    assert ink.save("alice", note_id, body())["note_id"] == note_id  # a retry works


def test_failed_later_save_keeps_the_note(env, monkeypatch):
    note_id = nid()
    ink.save("alice", note_id, body(), now=1000)

    def boom(*_a, **_k):
        raise RuntimeError("render failed")

    monkeypatch.setattr(ink, "render_page_png", boom)
    with pytest.raises(RuntimeError):
        ink.save("alice", note_id, body([stroke("x", y=500)], edited_at=2000), final=True)
    assert note_row(env, note_id) is not None
    assert ink.load("alice", note_id)["pages"][0]["strokes"][0]["id"] == "s1"


def test_single_user_mode_finds_each_owners_strokes(env):
    note_id = nid()
    ink.save("dave", note_id, body([stroke("a")], [stroke("b")]))
    listed = [n for n in ink.list_notes(None) if n["id"] == note_id]
    assert listed and listed[0]["pages"] == 2
    assert ink.load(None, note_id)["pages"][1]["strokes"][0]["id"] == "b"
    ink.save(None, note_id, body([stroke("c")], edited_at=5000))
    assert not ink.sidecar_path(None, note_id).exists()  # no second copy in _single
    assert ink.load("dave", note_id)["pages"][0]["strokes"][0]["id"] == "c"
    mine = nid()
    ink.save(None, mine, body())
    assert ink.sidecar_path(None, mine).exists() and ink.load(None, mine)
    assert ink.delete(None, note_id) and not ink.sidecar_path("dave", note_id).exists()


def test_server_render_follows_the_smoothed_curve(env):
    pts = [[100, 100, 0], [300, 400, 1], [500, 100, 2]]
    smooth = ink.smooth_points(pts)
    assert smooth[0] == (100.0, 100.0) and smooth[-1] == (500.0, 100.0)
    # Like the canvas, the curve doesn't reach the middle point (300, 400): it peaks
    # at y = 300 and passes (275, 287.5) halfway along its first piece.
    assert max(y for _x, y in smooth) <= 300.5
    page = ink.clean_document(body([{"id": "c", "pen": BLACK, "points": pts}]))["pages"][0]
    img = Image.open(io.BytesIO(ink.render_page_png(page))).convert("RGB")
    assert img.getpixel((300, 400)) == (255, 255, 255)  # no sharp corner at the point
    assert img.getpixel((275, 287))[0] < 80             # the curve passes here


def test_thin_round_caps_are_the_pen_width(env):
    page = ink.clean_document(body([{"id": "d", "pen": BLACK, "points": [[200, 200, 0]]}]))["pages"][0]
    img = Image.open(io.BytesIO(ink.render_page_png(page))).convert("L")
    dark = [(x, y) for x in range(195, 206) for y in range(195, 206) if img.getpixel((x, y)) < 128]
    xs = {x for x, _ in dark}
    assert len(xs) <= 3  # a 2.5-unit pen: about 3 px across, not 4
