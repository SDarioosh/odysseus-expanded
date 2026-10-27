"""Fork: handwritten notes, stage H5: the recognizer eval (scripts/ink_eval.py) and the
model text ``save_review`` keeps for it (src/ink_transcribe.py ``transcript.machine``).

Fictional strokes and text only; a fake reader in place of the vision model.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import sys
import uuid
from pathlib import Path

import pytest

import core.database as cdb
from src import ink_notes as ink
from src import ink_transcribe as tr
from src import tool_utils
from src.upload_handler import UploadHandler
from tests.helpers.sqlite_db import make_temp_sqlite

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ink_eval.py"
_SPEC = importlib.util.spec_from_file_location("ink_eval", _PATH)
ev = importlib.util.module_from_spec(_SPEC)
sys.modules["ink_eval"] = ev
_SPEC.loader.exec_module(ev)

PEN = {"color": "#1f1f1f", "width": 2.0, "highlighter": False}
SECRET = "Zorbulon quintessa"  # fictional page text that must never reach the output


def _dp(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------------- metrics
@pytest.mark.parametrize("a,b,d", [
    ("", "", 0), ("abc", "", 3), ("", "abc", 3), ("kitten", "sitting", 3),
    ("flaw", "lawn", 2), ("café", "cafe", 1), ("naïve 日本", "naive 日本語", 2),
    (["buy", "oat", "milk"], ["buy", "milk"], 1),
])
def test_levenshtein_known_values(a, b, d):
    assert ev.levenshtein(a, b) == d
    assert ev.levenshtein(b, a) == d


def test_levenshtein_matches_the_table_and_is_fast_on_long_pages():
    r = random.Random(7)
    for _ in range(500):
        a = "".join(r.choice("ab cé\n") for _ in range(r.randint(0, 90)))
        b = "".join(r.choice("ab cé\n") for _ in range(r.randint(0, 90)))
        assert ev.levenshtein(a, b) == _dp(a, b)
    long = "".join(r.choice("abcdefgh ij\n") for _ in range(20_000))
    changed = "".join("z" if i % 50 == 0 else ch for i, ch in enumerate(long))
    assert ev.levenshtein(long, changed) <= 400


def test_normalizations():
    text = "  Buy   oat milk \r\n\n\tCall Dana!  "
    assert ev.norm_default(text) == "Buy oat milk\nCall Dana!"
    assert ev.norm_loose(text) == "buy oat milk call dana"
    assert ev.norm_default("Café") == "Café"            # NFC
    assert ev.norm_default("a [illegible] b") == "a [?] b"


def test_score_page_cer_wer_exact_and_unreadable():
    pair = ev.Pair("o", "n", "p", 0, {}, "Plan the garden shed\nOrder 12 tiles", None)
    s = ev.score_page(pair, "Plan the garden shad\nOrder 12 tiles")
    assert s.counts["default"]["char"] == [1, 35]
    assert s.counts["default"]["word"] == [1, 7]
    assert not s.exact and s.unreadable == 0
    s = ev.score_page(pair, "plan the garden shed, order 12 tiles.")
    assert s.counts["loose"]["char"][0] == 0 and not s.exact
    s = ev.score_page(pair, "Plan  the garden shed \n\nOrder 12 tiles")
    assert s.exact and s.counts["default"]["char"][0] == 0
    s = ev.score_page(pair, "Plan the [?] shed\nOrder 12 [?]")
    assert s.unreadable == 2
    empty = ev.score_page(ev.Pair("o", "n", "p", 0, {}, "abc", None), "")
    assert empty.counts["default"]["char"] == [3, 3]


def test_summary_micro_rates_percentiles_and_auto_send_numbers():
    ref = "aaaa bbbb"                                      # 9 chars, 2 words
    pages = [ev.Pair("o", "n", f"p{i}", i, {}, ref, None) for i in range(4)]
    scores = [ev.score_page(pages[0], ref, seconds=1.0),
              ev.score_page(pages[1], "aaaa bbbx", seconds=2.0),
              ev.score_page(pages[2], "aaaa [?]", seconds=3.0),
              ev.score_page(pages[3], None, error="RuntimeError", seconds=9.0)]
    rep = ev.summarize(scores, mode="reread", skipped={}, pairs=4)
    assert (rep["scored"], rep["failed"]) == (3, 1)
    d = rep["default"]
    assert d["char_edits"] == 0 + 1 + 4 and d["ref_chars"] == 27
    assert d["cer"] == pytest.approx(5 / 27)
    assert d["wer"] == pytest.approx(2 / 6)
    assert d["page_wer"]["median"] == pytest.approx(0.5)
    assert d["page_cer"]["p90"] == pytest.approx(4 / 9)
    assert d["exact"] == 1
    a = rep["auto_send"]
    assert a["pages"] == 2 and a["share_of_scored"] == pytest.approx(2 / 3)
    assert a["exact"] == 1 and a["exact_share"] == pytest.approx(0.5)
    assert a["cer"] == pytest.approx(1 / 18)
    assert rep["unreadable"] == {"pages": 1, "marks": 1}
    assert rep["seconds"]["mean"] == pytest.approx(2.0)   # failed page left out
    assert rep["seconds"]["p90"] == pytest.approx(3.0)


# ---------------------------------------------------------------------- the set
@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ink, "_LOCKS", {})
    return tmp_path


def _stroke(y, sid):
    pts = [[120 + i * 10, y + (8 if i % 2 else -8), i * 16] for i in range(30)]
    return {"id": sid, "pen": dict(PEN), "at": 0, "points": pts}


def _page(pid, text, *, status="reviewed", machine=None, lines=2, stale=False, **extra):
    page = {"id": pid, "width": 1000, "height": 1414,
            "strokes": [_stroke(150 + 44 * k, f"{pid}-{k}") for k in range(lines)]}
    transcript = extra.pop("transcript", {})
    page.update(extra)  # a photo is part of the page's writing hash (H6)
    h = tr.writing_hash(page)
    t = {"text": text, "model": None, "status": status, "strokes_hash": h, "at": "x"}
    if machine is not None:
        t["machine"] = {"text": machine, "model": "fake-vl", "at": "w", "strokes_hash": h}
    t.update(transcript)
    page["transcript"] = t
    if stale:
        page["strokes"].append(_stroke(500, f"{pid}-new"))
    return page


def _note(owner, *pages):
    note_id = str(uuid.uuid4())
    ink._write_json(ink.sidecar_path(owner, note_id),
                    {"paper": "lined", "ruled": True, "edited_at": 1, "pages": list(pages)})
    return note_id


def test_pairs_skip_machine_stale_auto_empty_inkless_and_photo_pages(data):
    _note("alice",
          _page("ok", "Buy oat milk", machine="Buy oat mik"),
          _page("mach", "Buy oat milk", status="machine"),
          _page("stale", "Buy oat milk", stale=True),
          _page("auto", "Buy oat milk", transcript={"auto": True}),
          _page("empty", "   "),
          _page("noink", "Buy oat milk", lines=0),
          _page("photo", "Buy oat milk", photo={"id": "fictional"}))
    _note("bob", _page("bob1", "Water the ferns"))
    pairs, skipped = ev.collect_pairs(ink.owner_folders())
    assert [(p.owner, p.page_id) for p in pairs] == [("alice", "ok"), ("bob", "bob1")]
    assert pairs[0].stored == "Buy oat mik" and pairs[1].stored is None
    assert {k: v for k, v in skipped.items() if v} == {
        "not_reviewed": 1, "stale": 1, "auto": 1, "empty_text": 1, "no_ink": 1, "photo": 1}
    pairs, _ = ev.collect_pairs(["alice"], include_photos=True)
    assert [p.page_id for p in pairs] == ["ok", "photo"]
    assert len(ev.collect_pairs(["alice", "bob"], limit=1)[0]) == 1


def test_a_stored_machine_for_older_strokes_is_not_used(data):
    page = _page("p", "Buy oat milk", machine="Buy oat mik")
    page["transcript"]["machine"]["strokes_hash"] = "older"
    _note("alice", page)
    pairs, _ = ev.collect_pairs(["alice"])
    assert pairs[0].stored is None


def test_stored_mode_scores_the_kept_machine_text_without_a_model(data, monkeypatch, capsys):
    _note("alice", _page("a", SECRET + "\nline two", machine=SECRET + "\nline tw0"),
          _page("b", "Call Dana", machine="Call Dana"), _page("c", "No machine kept"))

    def boom(owner):
        raise AssertionError("no model in --stored")

    monkeypatch.setattr(tr, "default_reader", boom)
    out = data / "rep.json"
    assert ev.main(["--stored", "--out", str(out), "--per-page"]) == 0
    printed = capsys.readouterr().out
    rep = json.loads(out.read_text())
    assert rep["mode"] == "stored" and rep["scored"] == 2
    assert rep["skipped"]["no_stored_machine"] == 1
    assert rep["default"]["char_edits"] == 1
    assert rep["auto_send"]["pages"] == 2 and rep["auto_send"]["exact"] == 1
    assert "fake-vl" in rep["models"]
    for text in (SECRET, "Zorbulon", "Call Dana", "line tw0"):
        assert text not in printed and text not in out.read_text()


def test_reread_mode_uses_the_hub_reader_counts_failures_and_times_pages(data, monkeypatch,
                                                                       capsys):
    _note("alice", _page("a", SECRET), _page("b", "Order 12 tiles"),
          _page("c", "Water the ferns"))
    answers = {"a": SECRET, "b": "Order 12 [?]"}
    owners = []

    def fake_default_reader(owner):
        owners.append(owner)

        async def call(messages):
            # one band per fixture page: answer in page order
            key = min(answers) if answers else None
            if key is None:
                raise RuntimeError("model offline: " + SECRET)
            return json.dumps({"text": answers.pop(key)})

        return "fake-vl", call

    monkeypatch.setattr(tr, "default_reader", fake_default_reader)
    assert ev.main(["--json"]) == 0
    printed = capsys.readouterr().out
    rep = json.loads(printed)
    assert owners == ["alice"]                         # one reader per owner
    assert (rep["mode"], rep["scored"], rep["failed"]) == ("reread", 2, 1)
    assert rep["unreadable"] == {"pages": 1, "marks": 1}
    assert rep["auto_send"]["pages"] == 1 and rep["auto_send"]["exact_share"] == 1.0
    assert rep["seconds"]["mean"] >= 0 and rep["seconds"]["p90"] is not None
    failed = [p for p in rep["pages"] if p["error"]]
    assert failed[0]["page_id"] == "c" and failed[0]["error"] == "RuntimeError"
    assert SECRET not in printed and "Zorbulon" not in printed


def test_show_diffs_prints_text_only_when_asked(data, monkeypatch, capsys):
    _note("alice", _page("a", "Pick up the parcel", machine="Pick up the parcle"))
    assert ev.main(["--stored"]) == 0
    assert "parcel" not in capsys.readouterr().out
    assert ev.main(["--stored", "--show-diffs"]) == 0
    out = capsys.readouterr().out
    assert "- Pick up the parcel" in out and "+ Pick up the parcle" in out


def test_no_pairs_exits_2_and_no_model_exits_3(data, monkeypatch, capsys):
    assert ev.main([]) == 2
    _note("alice", _page("a", "Buy oat milk"))

    def no_model(owner):
        raise tr.NoModel("No vision model is set")

    monkeypatch.setattr(tr, "default_reader", no_model)
    assert ev.main([]) == 3
    assert "No vision model" in capsys.readouterr().err


def test_a_cloud_base_url_is_refused(data, capsys):
    _note("alice", _page("a", "Buy oat milk"))
    assert ev.main(["--model", "m", "--base-url", "https://api.example.com/v1"]) == 3
    assert "not on your network" in capsys.readouterr().err


@pytest.mark.parametrize("stored", [False, True])
def test_fixtures_run_end_to_end_without_a_model(monkeypatch, tmp_path, capsys, stored):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "unused"))
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path / "unused"))
    monkeypatch.setattr(ink, "_LOCKS", {})
    assert ev.main(["--fixtures", "--json"] + (["--stored"] if stored else [])) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["pairs"] == 4
    assert {k: v for k, v in rep["skipped"].items() if v} == {
        "not_reviewed": 1, "auto": 1, "empty_text": 1, "photo": 1, "stale": 1,
        **({"no_stored_machine": 1} if stored else {})}
    assert rep["scored"] == 3 and rep["failed"] == (0 if stored else 1)
    assert rep["auto_send"]["pages"] == 2 and rep["auto_send"]["exact"] == 1
    assert rep["unreadable"]["marks"] == 1


# ------------------------------------------------------- save_review keeps machine
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


def _doc(*lines, extra=()):
    strokes = [_stroke(y, f"s{i}") for i, y in enumerate(lines)] + list(extra)
    return {"ruled": True, "edited_at": 1000, "pages": [
        {"id": "p0", "width": 1000, "height": 1414, "strokes": strokes}]}


async def _transcribe(note_id, answer):
    async def fake(messages):
        return json.dumps({"text": answer})

    await tr.start("alice", note_id, page_ids=["p0"], reader=("fake-vl", fake))
    await tr.wait_idle()


def _t(note_id):
    return ink.load_sidecar("alice", note_id)["pages"][0]["transcript"]


def test_save_review_keeps_the_machine_text_for_the_eval(env):
    note_id = str(uuid.uuid4())
    ink.save("alice", note_id, _doc(150, 194))
    asyncio.run(_transcribe(note_id, "Buy oat mik"))
    tr.save_review("alice", note_id, "p0", "Buy oat milk")
    t = _t(note_id)
    assert t["status"] == "reviewed" and t["text"] == "Buy oat milk"
    assert t["machine"]["text"] == "Buy oat mik" and t["machine"]["model"] == "fake-vl"
    assert t["machine"]["strokes_hash"] == t["strokes_hash"]
    # a second review of the same strokes keeps it
    tr.save_review("alice", note_id, "p0", "Buy oat milk!")
    assert _t(note_id)["machine"]["text"] == "Buy oat mik"
    # the page as the eval sees it
    pairs, _ = ev.collect_pairs(["alice"])
    assert pairs[0].stored == "Buy oat mik" and pairs[0].reference == "Buy oat milk!"


def test_machine_is_dropped_when_strokes_change_or_a_new_run_replaces_it(env):
    note_id = str(uuid.uuid4())
    ink.save("alice", note_id, _doc(150, 194))
    asyncio.run(_transcribe(note_id, "Buy oat mik"))
    tr.save_review("alice", note_id, "p0", "Buy oat milk")
    # strokes change, the owner reviews without transcribing again: machine is stale
    ink.save("alice", note_id, {**_doc(150, 194, 238), "edited_at": 2000})
    tr.save_review("alice", note_id, "p0", "Buy oat milk\nand bread")
    assert "machine" not in _t(note_id)
    # a new run writes fresh model text; reviewing then keeps that one
    asyncio.run(_transcribe(note_id, "Buy oat milk\nand bead"))
    t = _t(note_id)
    assert t["status"] == "machine" and "machine" not in t
    tr.save_review("alice", note_id, "p0", "Buy oat milk\nand bread")
    assert _t(note_id)["machine"]["text"] == "Buy oat milk\nand bead"
    asyncio.run(_transcribe(note_id, "Buy oat milk\nand bread"))
    assert "machine" not in _t(note_id)


# ------------------------------------------------- QC findings on PR 104 (H5 and H6)
def _pair(reference="Buy milk"):
    return ev.Pair("alice", "n1", "p0", 0, {}, reference, None)


def test_loose_keeps_unreadable_marks_as_errors():
    # [?] is all punctuation: dropping it scored an unreadable word as no error (F-01).
    assert ev.norm_loose("Buy [?] milk!") == "buy [?] milk"
    assert ev.norm_loose("[illegible], ok") == "[?] ok"
    s = ev.score_page(_pair(), "Buy [?] milk")
    assert s.counts["loose"]["word"][0] == 1 and s.counts["loose"]["char"][0] > 0
    s = ev.score_page(_pair("Pick up the parcel"), "Pick up the [?]")
    assert s.counts["loose"]["word"] == [1, 4]


@pytest.mark.parametrize("answer", ["", "   ", "\x01\x02", "x" * tr.MAX_PAGE_TEXT, "Buy [?]"])
def test_auto_send_numbers_count_only_pages_auto_send_would_accept(answer):
    # Empty, control characters only or cut at the page limit: INK_AUTO_SEND leaves them
    # for review, so they are not in its numbers (F-02).
    rep = ev.summarize([ev.score_page(_pair(), answer), ev.score_page(_pair(), "Buy milk")],
                       mode="reread", skipped={}, pairs=2)
    assert rep["scored"] == 2
    assert rep["auto_send"]["pages"] == 1 and rep["auto_send"]["exact"] == 1
    assert rep["auto_send"]["cer"] == 0.0


def test_owner_single_reads_the_single_user_folder(data, capsys):
    # owner_folders() calls the single-user folder "_single"; --owner _single reads it (F-11).
    _note(None, _page("a", "Buy oat milk", machine="Buy oat mik"))
    assert ev.main(["--owner", "_single", "--stored", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["scored"] == 1


async def _transcribe_fails(note_id):
    async def broken(messages):
        raise RuntimeError("model down")

    await tr.start("alice", note_id, page_ids=["p0"], reader=("fake-vl", broken))
    await tr.wait_idle()


def test_machine_survives_a_failed_new_run(env):
    # Transcribe again on the same strokes fails, then the owner reviews: the model text
    # for these strokes is still kept for the eval (F-08).
    note_id = str(uuid.uuid4())
    ink.save("alice", note_id, _doc(150, 194))
    asyncio.run(_transcribe(note_id, "Buy oat mik"))
    asyncio.run(_transcribe_fails(note_id))
    assert _t(note_id)["status"] == "failed"
    tr.save_review("alice", note_id, "p0", "Buy oat milk")
    t = _t(note_id)
    assert t["machine"]["text"] == "Buy oat mik" and t["machine"]["strokes_hash"] == t["strokes_hash"]


def test_erasing_a_page_drops_its_machine_text(env):
    note_id = str(uuid.uuid4())
    ink.save("alice", note_id, _doc(150, 194))
    asyncio.run(_transcribe(note_id, "Buy oat mik"))
    tr.save_review("alice", note_id, "p0", "Buy oat milk")
    assert "machine" in _t(note_id)
    ink.save("alice", note_id, {**_doc(), "edited_at": 2000})  # all ink erased
    asyncio.run(tr.start("alice", note_id, page_ids=["p0"], reader=("fake-vl", None)))
    t = _t(note_id)
    assert t["status"] == "none" and "machine" not in t
