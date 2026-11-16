"""Fork: reading handwritten marks on a PDF datasheet (fork/ink/mark_reading.py), the
mark fields it adds (fork/ink/pdf_marks.py), the correction route
(fork/routes/ink_marks_routes.py) and the schedule call after a save
(fork/routes/ink_routes.py).

Temporary notes database and upload store, a fake reader in place of the vision model,
a fictional datasheet (tests/fork/pdf_fixtures.py) and hand-made strokes.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.database as cdb
from fork.ink import mark_reading as mr
from fork.ink import notes as ink
from fork.ink import pdf_marks as pm
from fork.ink import transcribe as tr
from fork.routes import ink_marks_routes as imr
from fork.routes import ink_routes as ir
from src import tool_utils
from src.upload_handler import UploadHandler
from tests.fork.pdf_fixtures import datasheet
from tests.helpers.sqlite_db import make_temp_sqlite

PEN = {"color": "#1f5fbf", "width": 2.5, "highlighter": False}
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
    for name in ("_TASKS", "_NOW_READING", "_LAST_SAVE"):
        monkeypatch.setattr(mr, name, {})
    for name in ("_BUSY", "_AGAIN"):
        monkeypatch.setattr(mr, name, set())
    yield factory
    engine.dispose()


class Fake:
    """A reader answering from a list (default ``{"text": "check EN"}``), recording the
    messages."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls = []

    async def __call__(self, messages):
        self.calls.append(messages)
        await asyncio.sleep(0)
        answer = self.answers.pop(0) if self.answers else json.dumps({"text": "check EN"})
        if isinstance(answer, Exception):
            raise answer
        return answer


def use_reader(monkeypatch, fake):
    monkeypatch.setattr(tr, "default_reader", lambda owner: ("fake-vl", fake))


def run(coro):
    return asyncio.run(coro)


def nid():
    return str(uuid.uuid4())


def stroke(sid, pen, pts, at=0):
    return {"id": sid, "pen": pen, "at": at, "points": [[x, y, i * 16] for i, (x, y) in enumerate(pts)]}


MARGIN = [stroke("n1", PEN, [(820, 300), (840, 320), (860, 300)]),
          stroke("n2", PEN, [(870, 300), (890, 320)])]           # one ink mark, m-n1
HIGHLIGHT = stroke("h1", YELLOW, [(120, 600), (300, 600)])     # a highlight, never read


def pdf_note(owner="alice", strokes=None, edited_at=2000):
    note_id = nid()
    note = ink.add_pdf(owner, note_id, datasheet(), name="zx100.pdf", edited_at=1000)
    return note_id, save(owner, note, strokes if strokes is not None else MARGIN + [HIGHLIGHT],
                         edited_at)


def save(owner, note, strokes, edited_at=None):
    body = {"paper": note["paper"], "edited_at": edited_at or note["edited_at"] + 1,
            "pages": [{"id": p["id"], "width": p["width"], "height": p["height"],
                       "strokes": strokes if i == 0 else p["strokes"]}
                      for i, p in enumerate(note["pages"])]}
    return ink.save(owner, note["note_id"], body)


async def read_now(owner, note_id):
    mr.schedule(owner, note_id)
    await mr.wait_idle()


def marks(owner, note_id):
    return {m["id"]: m for m in pm.note_marks(owner, note_id)["marks"]}


# ------------------------------------------------------------------- pure
def test_mark_page_moves_and_scales_the_mark():
    mark = {"id": "m-a", "box": [100, 200, 50, 30]}
    page = mr.mark_page(mark, [stroke("a", PEN, [(100, 200), (150, 230)], at=5)])
    # padded box 82 x 62 at (84, 184), scaled 4x
    assert page["width"] == 1000 and page["height"] == 248
    assert page["strokes"][0]["points"] == [[64.0, 64.0, 0], [264.0, 184.0, 16]]
    assert page["strokes"][0]["pen"]["width"] == 10.0
    assert page["strokes"][0]["at"] == 5


def test_mark_page_fits_wide_flat_and_tall_marks():
    wide = mr.mark_page({"id": "m", "box": [10, 10, 900, 0]},
                        [stroke("a", PEN, [(10, 10), (910, 10)])])
    assert wide["height"] == mr.MARK_MIN_HEIGHT
    assert max(p[0] for p in wide["strokes"][0]["points"]) <= 1000
    tall = mr.mark_page({"id": "m", "box": [10, 10, 100, 1500]},
                        [stroke("a", PEN, [(10, 10), (110, 1510)])])
    assert tall["height"] <= mr.MARK_MAX_HEIGHT
    assert max(p[1] for p in tall["strokes"][0]["points"]) <= tall["height"]


def test_mark_strokes_leave_out_the_highlighter_and_other_strokes():
    page = {"strokes": [*MARGIN, HIGHLIGHT, stroke("x", PEN, [(10, 10), (20, 20)])]}
    got = mr.mark_strokes(page, {"strokes": ["n1", "n2", "h1"]})
    assert [s["id"] for s in got] == ["n1", "n2"]


def test_mark_strokes_and_page_are_the_writing_only_when_an_arrow_is_drawn_with_it():
    arrow = stroke("ar", PEN, [(800, 310), (600, 250), (612, 247)])
    page = {"strokes": [*MARGIN, arrow]}
    mark = {"id": "m-n1", "strokes": ["n1", "n2", "ar"], "box": [598, 245, 294, 77],
            "_writing": ["n1", "n2"], "_writing_box": [818.8, 298.8, 72.5, 22.5]}
    assert [s["id"] for s in mr.mark_strokes(page, mark)] == ["n1", "n2"]
    got = mr.mark_page(mark, mr.mark_strokes(page, mark))
    xs = [p[0] for s in got["strokes"] for p in s["points"]]
    assert min(xs) >= 0 and max(xs) <= 1000 and got["width"] == 1000
    # Scaled from the writing's box (4x), not the whole mark's (about 2.1x).
    assert got["strokes"][0]["pen"]["width"] == 10.0


def test_mark_hash_follows_ids_and_points():
    a = mr.mark_hash(MARGIN)
    assert a == mr.mark_hash(json.loads(json.dumps(MARGIN)))
    moved = [MARGIN[0], stroke("n2", PEN, [(871, 300), (890, 320)])]
    assert mr.mark_hash(moved) != a
    assert mr.mark_hash([{**MARGIN[0], "at": 99}, MARGIN[1]]) == a  # time isn't the writing


def test_settled():
    assert mr.settled(0, mr.MARK_SETTLE_MS)
    assert not mr.settled(1000, mr.MARK_SETTLE_MS)
    # a tablet clock ahead: settled once the note had no save for the settle time
    assert not mr.settled(10 ** 12, 50_000, last_save=40_000)
    assert mr.settled(10 ** 12, 60_000, last_save=40_000)


def test_view_states():
    ink_mark = {"id": "m-a", "kind": "ink", "_hash": "h", "text": "", "near": ""}
    hl = {"id": "m-h", "kind": "highlight", "_hash": "x", "text": "VIN"}
    assert mr.view(hl, None) == {"id": "m-h", "kind": "highlight", "text": "VIN",
                                 "written": "", "reading": "none"}
    assert mr.view(ink_mark, None)["reading"] == "waiting"
    assert mr.view(ink_mark, None, reading_now=True)["reading"] == "pending"
    read = {"hash": "h", "text": "pull-up", "status": "read"}
    assert (mr.view(ink_mark, read)["written"], mr.view(ink_mark, read)["reading"]) == ("pull-up", "read")
    old = {**read, "hash": "old"}
    assert (mr.view(ink_mark, old)["written"], mr.view(ink_mark, old)["reading"]) == ("", "waiting")
    failed = mr.view(ink_mark, {"hash": "h", "status": "failed", "error": "No vision model is set"})
    assert failed["reading"] == "failed" and failed["reading_error"] == "No vision model is set"
    assert "reading_error" not in mr.view(ink_mark, read)
    reviewed = {"hash": "h", "text": "EN pull-up", "status": "reviewed"}
    assert mr.view(ink_mark, reviewed, reading_now=True)["reading"] == "reviewed"
    assert "_hash" not in mr.view(ink_mark, reviewed)


def test_needs_reading():
    m = {"kind": "ink", "_hash": "h"}
    assert mr.needs_reading(m, None, 0)
    assert not mr.needs_reading(m, {"hash": "h", "status": "read"}, 0)
    assert mr.needs_reading(m, {"hash": "old", "status": "reviewed"}, 0)
    failed = {"hash": "h", "status": "failed", "at": 1000}
    assert not mr.needs_reading(m, failed, 1000 + mr.FAILED_RETRY_MS - 1)
    assert mr.needs_reading(m, failed, 1000 + mr.FAILED_RETRY_MS)
    assert not mr.needs_reading({"kind": "underline", "_hash": "h"}, None, 0)


def test_band_messages_hint_is_optional():
    plain = tr.band_messages(b"png", 0, 1)
    assert plain[1]["content"][0]["text"] == "Transcribe this handwriting."
    hinted = tr.band_messages(b"png", 0, 1, hint=mr.MARK_HINT)
    assert hinted[1]["content"][0]["text"] == "Transcribe this handwriting. " + mr.MARK_HINT


# ---------------------------------------------------------------- reading
def test_a_settled_ink_mark_is_read_and_found(env, monkeypatch):
    fake = Fake()
    use_reader(monkeypatch, fake)
    note_id, _ = pdf_note()
    run(read_now("alice", note_id))
    assert len(fake.calls) == 1                     # the ink mark only, not the highlight
    assert mr.MARK_HINT in fake.calls[0][1]["content"][0]["text"]
    got = marks("alice", note_id)
    assert (got["m-n1"]["written"], got["m-n1"]["reading"]) == ("check EN", "read")
    assert got["m-h1"]["reading"] == "none" and got["m-h1"]["written"] == ""
    entry = ink.load_sidecar("alice", note_id)["mark_readings"]["m-n1"]
    assert entry["status"] == "read" and entry["model"] == "fake-vl" and entry["hash"]
    hits = pm.search("alice", "check en", 10)
    assert [h["mark"]["id"] for h in hits] == ["m-n1"] and hits[0]["mark"]["written"] == "check EN"
    # nothing left to read: a new run asks nothing
    run(read_now("alice", note_id))
    assert len(fake.calls) == 1


def test_a_mark_still_being_written_waits_to_settle(env, monkeypatch):
    monkeypatch.setattr(mr, "MARK_SETTLE_MS", 300)
    fake = Fake()
    use_reader(monkeypatch, fake)
    now = mr.now_ms()
    note_id, _ = pdf_note(strokes=[{**s, "at": now} for s in MARGIN])

    async def go():
        mr.schedule("alice", note_id, saved=True)
        await asyncio.sleep(0.1)
        assert mr.is_active(note_id) and not fake.calls
        assert marks("alice", note_id)["m-n1"]["reading"] == "waiting"
        mr.schedule("alice", note_id, saved=True)    # another save: debounced, one run
        await mr.wait_idle()

    run(go())
    assert len(fake.calls) == 1
    assert marks("alice", note_id)["m-n1"]["reading"] == "read"
    assert not mr.is_active(note_id)


def test_no_model_fails_once_and_retries_after_ten_minutes(env, monkeypatch):
    def no_model(owner):
        raise tr.NoModel("whatever the hub says")

    monkeypatch.setattr(tr, "default_reader", no_model)
    note_id, _ = pdf_note()
    run(read_now("alice", note_id))
    m = marks("alice", note_id)["m-n1"]
    assert m["reading"] == "failed" and m["reading_error"] == mr.NO_MODEL and m["written"] == ""
    assert pm.note_marks("alice", note_id)["unread"] is False   # not again right away
    fake = Fake()
    use_reader(monkeypatch, fake)
    run(read_now("alice", note_id))
    assert fake.calls == []
    later = mr.now_ms() + mr.FAILED_RETRY_MS + 1
    monkeypatch.setattr(mr, "now_ms", lambda: later)
    assert pm.note_marks("alice", note_id)["unread"] is True
    run(read_now("alice", note_id))
    assert len(fake.calls) == 1 and marks("alice", note_id)["m-n1"]["reading"] == "read"


def test_a_model_error_is_stored_as_failed(env, monkeypatch):
    use_reader(monkeypatch, Fake([RuntimeError("boom")]))
    note_id, _ = pdf_note()
    run(read_now("alice", note_id))
    m = marks("alice", note_id)["m-n1"]
    assert m["reading"] == "failed" and "boom" in m["reading_error"]


def test_correction_is_kept_until_the_strokes_change(env, monkeypatch):
    fake = Fake()
    use_reader(monkeypatch, fake)
    note_id, note = pdf_note()
    got = mr.correct("alice", note_id, "m-n1", "  EN needs a pull-up \n")
    assert (got["written"], got["reading"]) == ("EN needs a pull-up", "reviewed")
    run(read_now("alice", note_id))
    assert fake.calls == []                      # reviewed and current: not read
    # The owner adds a stroke to the note: read again.
    note = save("alice", note, MARGIN + [HIGHLIGHT, stroke("n3", PEN, [(895, 300), (905, 320)])])
    assert marks("alice", note_id)["m-n1"]["reading"] == "waiting"
    run(read_now("alice", note_id))
    assert len(fake.calls) == 1
    assert marks("alice", note_id)["m-n1"]["written"] == "check EN"


def test_correction_errors(env, monkeypatch):
    note_id, _ = pdf_note()
    with pytest.raises(ink.InkError) as e:
        mr.correct("alice", note_id, "m-h1", "VIN")
    assert e.value.status == 400
    with pytest.raises(ink.InkError) as e:
        mr.correct("alice", note_id, "m-nope", "x")
    assert e.value.status == 404
    with pytest.raises(ink.InkError) as e:
        mr.correct("bob", note_id, "m-n1", "x")
    assert e.value.status == 404
    with pytest.raises(ink.InkError) as e:
        mr.correct("alice", note_id, "m-n1", "x" * (mr.MAX_WRITTEN + 1))
    assert e.value.status == 400
    with pytest.raises(ink.InkError):
        mr.correct("alice", note_id, "m-n1", 5)


def test_saves_keep_readings_and_a_run_drops_readings_of_gone_marks(env, monkeypatch):
    use_reader(monkeypatch, Fake())
    note_id, note = pdf_note()
    run(read_now("alice", note_id))
    note = save("alice", note, MARGIN + [HIGHLIGHT, stroke("far", PEN, [(500, 900), (520, 920)])])
    readings = ink.load_sidecar("alice", note_id)["mark_readings"]
    assert readings["m-n1"]["status"] == "read"          # kept by the save
    save("alice", note, [stroke("far", PEN, [(500, 900), (520, 920)])])   # m-n1 erased
    run(read_now("alice", note_id))
    assert set(ink.load_sidecar("alice", note_id)["mark_readings"]) == {"m-far"}


def test_a_note_without_pdf_is_never_read(env, monkeypatch):
    fake = Fake()
    use_reader(monkeypatch, fake)
    note_id = nid()
    ink.save("alice", note_id, {"paper": "lined", "edited_at": 1, "pages": [
        {"id": "p0", "width": 1000, "height": 1414, "strokes": MARGIN}]})
    run(read_now("alice", note_id))
    assert fake.calls == [] and "mark_readings" not in ink.load_sidecar("alice", note_id)


# ------------------------------------------------------------------- routes
def client_for(user="alice"):
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = user
        request.state.api_token = False
        return await call_next(request)

    app.include_router(ir.setup_ink_routes())
    app.include_router(imr.setup_ink_marks_routes())
    return TestClient(app)


def test_routes_schedule_and_correct(env, monkeypatch):
    calls = []
    monkeypatch.setattr(mr, "schedule", lambda owner, note_id, **kw: calls.append((note_id, kw)))
    c = client_for()
    note_id, note = pdf_note()
    # A save of a PDF note schedules a run; a save of another note doesn't.
    body = {"paper": note["paper"], "edited_at": note["edited_at"] + 1,
            "pages": [{"id": p["id"], "width": p["width"], "height": p["height"],
                       "strokes": p["strokes"]} for p in note["pages"]]}
    assert c.put(f"/api/ink/notes/{note_id}", json=body).status_code == 200
    assert calls == [(note_id, {"saved": True})]
    plain = nid()
    assert c.put(f"/api/ink/notes/{plain}", json={"paper": "lined", "edited_at": 1, "pages": [
        {"id": "p0", "width": 1000, "height": 1414, "strokes": MARGIN}]}).status_code == 200
    assert len(calls) == 1
    # GET marks with an unread mark schedules too; reading reflects the run state.
    r = c.get(f"/api/ink/notes/{note_id}/marks").json()
    assert len(calls) == 2 and r["reading"] is False and "unread" not in r
    by_id = {m["id"]: m for m in r["marks"]}
    assert by_id["m-n1"]["reading"] == "waiting" and by_id["m-h1"]["reading"] == "none"
    # Correction
    r = c.put(f"/api/ink/notes/{note_id}/marks/m-n1", json={"written": "check EN pull-up"})
    assert r.status_code == 200, r.text
    assert (r.json()["written"], r.json()["reading"]) == ("check EN pull-up", "reviewed")
    assert c.put(f"/api/ink/notes/{note_id}/marks/m-h1", json={"written": "x"}).status_code == 400
    assert c.put(f"/api/ink/notes/{note_id}/marks/m-zz", json={"written": "x"}).status_code == 404
    assert c.put(f"/api/ink/notes/{note_id}/marks/bad!", json={"written": "x"}).status_code == 404
    assert c.put(f"/api/ink/notes/{note_id}/marks/m-n1", json={"written": 3}).status_code == 400
    assert client_for("bob").put(f"/api/ink/notes/{note_id}/marks/m-n1",
                                 json={"written": "x"}).status_code == 404
    r = c.get(f"/api/ink/notes/{note_id}/marks").json()
    assert len(calls) == 2                       # nothing unread now
    assert {m["id"]: m for m in r["marks"]}["m-n1"]["written"] == "check EN pull-up"
    hits = c.get("/api/ink/marks", params={"q": "pull-up"}).json()["results"]
    assert [h["mark"]["id"] for h in hits] == ["m-n1"]


def test_a_route_run_reads_on_the_servers_loop(env, monkeypatch):
    fake = Fake()
    use_reader(monkeypatch, fake)
    note_id, _ = pdf_note()
    with client_for() as c:
        r = c.get(f"/api/ink/notes/{note_id}/marks").json()
        assert r["reading"] is True
        for _ in range(100):
            r = c.get(f"/api/ink/notes/{note_id}/marks").json()
            if not r["reading"]:
                break
            import time
            time.sleep(0.05)
    assert {m["id"]: m for m in r["marks"]}["m-n1"]["written"] == "check EN"
    assert len(fake.calls) == 1


# ------------------------------------------------------------ circles and arrows
def _line_end(owner, note_id, text):
    from fork.ink import pdf as ink_pdf

    words = ink_pdf.page_words(ink.pdf_file(owner, note_id), 1)
    first = next(w for w in words if w.text == text)
    line = [w for w in words if w.line == first.line]
    return max(w.x1 for w in line), (first.y0 + first.y1) / 2


def test_an_arrow_drawn_with_a_note_is_not_read_and_does_not_make_it_read_again(env, monkeypatch):
    fake = Fake()
    use_reader(monkeypatch, fake)
    note_id, note = pdf_note()
    run(read_now("alice", note_id))
    assert len(fake.calls) == 1
    x1, y = _line_end("alice", note_id, "Quiescent")
    tip, tail = (x1 + 8, y), (812, 305)
    arrow = stroke("ar", PEN, [tail, ((tail[0] + tip[0]) / 2, (tail[1] + tip[1]) / 2), tip,
                               (tip[0] + 9, tip[1] - 6)], at=9000)
    sent = []
    real = mr.mark_page
    monkeypatch.setattr(mr, "mark_page", lambda m, st: sent.append([s["id"] for s in st])
                        or real(m, st))
    save("alice", note, MARGIN + [HIGHLIGHT, arrow], edited_at=note["edited_at"] + 1)
    got = marks("alice", note_id)["m-n1"]
    assert got["kind"] == "ink" and "ar" in got["strokes"] and got["points_to"] is True
    assert got["near"].startswith("Quiescent current")
    assert (got["written"], got["reading"]) == ("check EN", "read")   # the same writing
    run(read_now("alice", note_id))
    assert len(fake.calls) == 1 and sent == []
    # New writing: read again, without the arrow.
    more = stroke("n3", PEN, [(895, 300), (905, 318)], at=9500)
    stored = ink.load("alice", note_id)
    save("alice", stored, MARGIN + [more, HIGHLIGHT, arrow], edited_at=stored["edited_at"] + 1)
    run(read_now("alice", note_id))
    assert len(fake.calls) == 2 and sent == [["n1", "n2", "n3"]]


def test_circles_and_arrows_alone_are_never_read(env, monkeypatch):
    fake = Fake()
    use_reader(monkeypatch, fake)
    note_id, _ = pdf_note(strokes=[])
    from fork.ink import pdf as ink_pdf

    words = ink_pdf.page_words(ink.pdf_file("alice", note_id), 1)
    w = next(w for w in words if w.text == "uA")
    loop = stroke("c1", PEN, [(w.x0 - 6, w.y0 - 6), (w.x1 + 6, w.y0 - 6), (w.x1 + 6, w.y1 + 6),
                              (w.x0 - 6, w.y1 + 6), (w.x0 - 6, w.y0 - 5)])
    x1, y = _line_end("alice", note_id, "Quiescent")
    arrow = stroke("a1", PEN, [(x1 + 300, y + 200), (x1 + 8, y), (x1 + 17, y - 6)])
    note_id2, _ = pdf_note(strokes=[loop, arrow])
    got = marks("alice", note_id2)
    assert {m["kind"] for m in got.values()} == {"circle", "arrow"}
    assert all(m["reading"] == "none" for m in got.values())
    run(read_now("alice", note_id2))
    assert fake.calls == []
