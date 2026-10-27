"""Fork: handwritten notes, stage H6: ``INK_AUTO_SEND`` (src/ink_transcribe.py,
src/ink_athena.py ``auto_send``).

Off by default: nothing changes. On: a transcription run accepts clean pages itself
(``transcript.auto``) and sends the note through the usual Send once no page waits.
The fake Athena and notes database of ``tests/test_ink_athena.py`` and the fake reader
of ``tests/test_ink_transcribe.py``. Fictional strokes and text only.
"""
import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.database as cdb
import routes.ink_routes as ir
from src import ink_athena as athena
from src import ink_notes as ink
from src import ink_transcribe as tr
from tests.test_ink_athena import FakeAthena, HUB, env, new_note, page, sidecar, word  # noqa: F401
from tests.test_ink_transcribe import Fake, note_row

CLEAN = json.dumps({"text": "Buy milk\nCall Sam at 5"})
UNSURE = json.dumps({"text": "Meet [?] at noon"})


@pytest.fixture
def auto(env, monkeypatch):
    """Auto-send on (the ``env`` fixture's fake Athena is returned)."""
    monkeypatch.setattr(tr, "_AUTO_SENDING", {})
    monkeypatch.setenv("INK_AUTO_SEND", "1")
    return env


async def transcribe(note_id, fake, owner="alice", **kw):
    view = await tr.start(owner, note_id, reader=("fake-vl", fake), **kw)
    await tr.wait_idle()
    return view


def run(note_id, *answers, owner="alice", **kw):
    asyncio.run(transcribe(note_id, Fake(list(answers)), owner=owner, **kw))
    return tr.transcripts(owner, note_id)


def content(note_id):
    return note_row(cdb.SessionLocal, note_id)["content"]  # the env fixture's database


@pytest.mark.parametrize("value, on", [(None, False), ("", False), ("0", False),
                                       ("false", False), ("off", False), ("1", True),
                                       ("true", True), ("Yes", True), (" on ", True)])
def test_setting_is_read_at_call_time(monkeypatch, value, on):
    if value is None:
        monkeypatch.delenv("INK_AUTO_SEND", raising=False)
    else:
        monkeypatch.setenv("INK_AUTO_SEND", value)
    assert tr.auto_send_enabled() is on


# ------------------------------------------------------------------- off: unchanged
def test_off_by_default_nothing_is_accepted_or_sent(env, monkeypatch):
    monkeypatch.delenv("INK_AUTO_SEND", raising=False)
    note_id = new_note()
    out = run(note_id, CLEAN)
    [p] = out["pages"]
    assert p["status"] == "machine" and p["auto"] is False
    assert "auto" not in sidecar(note_id)["pages"][0]["transcript"]
    assert out["auto_send"] is False and out["auto_sending"] is False
    assert out["athena"]["state"] == "not_sent" and out["athena"]["auto"] is False
    assert env.requests == []
    assert not content(note_id)


# ------------------------------------------------------------------------ on
def test_a_clean_page_is_accepted_and_the_note_sent(auto):
    note_id = new_note()
    out = run(note_id, CLEAN)
    [p] = out["pages"]
    assert p["status"] == "reviewed" and p["auto"] is True and p["text"] == "Buy milk\nCall Sam at 5"
    assert sidecar(note_id)["pages"][0]["transcript"]["auto"] is True
    assert content(note_id) == "Buy milk\nCall Sam at 5"  # as on Save review
    [body] = auto.posts()
    assert body["text"] == "Buy milk\nCall Sam at 5"
    assert body["link"] == f"{HUB}/#open=notes&note={note_id}"
    assert out["auto_send"] is True and out["auto_sending"] is False
    a = out["athena"]
    assert a["state"] == "sent" and a["auto"] is True and a["auto_error"] is None
    assert sidecar(note_id)["athena"]["sending"] is None
    [listed] = ink.list_notes("alice")
    assert listed["athena"] == "sent"
    assert listed["transcripts"]["auto"] == [0] and listed["transcripts"]["auto_sent"] is True


@pytest.mark.parametrize("answer", [UNSURE, json.dumps({"text": ""}), "not json"])
def test_unreadable_empty_or_failed_pages_wait_for_review(auto, answer):
    note_id = new_note()
    out = run(note_id, answer, answer)
    [p] = out["pages"]
    assert p["status"] in ("machine", "failed") and p["auto"] is False
    assert auto.requests == []
    assert out["athena"]["state"] == "not_sent" and out["athena"]["waiting"]


def test_mixed_note_waits_and_save_review_never_sends(auto):
    note_id = new_note(pages=[page("p0"), page("p1")])
    out = run(note_id, CLEAN, UNSURE)
    assert [(p["status"], p["auto"]) for p in out["pages"]] == [("reviewed", True),
                                                                 ("machine", False)]
    assert content(note_id) == "Buy milk\nCall Sam at 5"
    assert auto.requests == []
    assert [w["page"] for w in out["athena"]["waiting"]] == [2]
    # The owner checks page 2: Save review stores it, and still sends nothing (H4).
    out = tr.save_review("alice", note_id, "p1", "Meet Ana at noon")
    assert out["athena"]["waiting"] == [] and out["athena"]["state"] == "not_sent"
    assert auto.requests == []
    # Send stays the owner's: a manual send is not marked automatic.
    sent = asyncio.run(athena.send("alice", note_id))
    assert sent["athena"]["state"] == "sent" and sent["athena"]["auto"] is False
    assert auto.posts()[0]["text"] == "Buy milk\nCall Sam at 5\n\nMeet Ana at noon"


def test_never_sent_while_another_run_reads_a_page_of_the_note(auto):
    note_id = new_note(pages=[page("p0"), page("p1")])

    async def go():
        gate = asyncio.Event()
        await tr.start("alice", note_id, page_ids=["p0"],
                       reader=("fake-vl", Fake([json.dumps({"text": "First page"})], gate=gate)))
        await tr.start("alice", note_id, page_ids=["p1"],
                       reader=("fake-vl", Fake([json.dumps({"text": "Second page"})])))
        for _ in range(50):  # the second run ends while the first one still reads
            await asyncio.sleep(0.01)
            if sidecar(note_id)["pages"][1]["transcript"]["status"] == "reviewed":
                break
        await asyncio.sleep(0.05)
        assert auto.requests == []
        gate.set()
        await tr.wait_idle()

    asyncio.run(go())
    [body] = auto.posts()
    assert body["text"] == "First page\n\nSecond page"
    assert tr.transcripts("alice", note_id)["athena"]["state"] == "sent"


def test_strokes_changed_during_the_run_are_not_accepted(auto):
    note_id = new_note()

    async def go():
        gate = asyncio.Event()
        await tr.start("alice", note_id, reader=("fake-vl", Fake([CLEAN], gate=gate)))
        ink.save("alice", note_id, {"ruled": True, "edited_at": 2000, "pages": [
            {**page("p0"), "strokes": page("p0")["strokes"] + [word(300, "late")]}]})
        gate.set()
        await tr.wait_idle()

    asyncio.run(go())
    [p] = tr.transcripts("alice", note_id)["pages"]
    assert p["status"] == "machine" and p["changed"] and p["auto"] is False
    assert auto.requests == []


def test_resend_after_the_writing_changes_and_is_read_again(auto):
    note_id = new_note()
    run(note_id, CLEAN)
    ink.save("alice", note_id, {"ruled": True, "edited_at": 2000, "pages": [
        {**page("p0"), "strokes": page("p0")["strokes"] + [word(300, "more")]}]})
    waiting = tr.transcripts("alice", note_id)
    assert waiting["athena"]["state"] == "changed"
    out = run(note_id, json.dumps({"text": "Buy milk\nCall Sam at 6"}), only_changed=True)
    [p] = out["pages"]
    assert p["status"] == "reviewed" and p["auto"] is True
    assert p["previous"] == {"text": "Buy milk\nCall Sam at 5", "at": p["previous"]["at"],
                             "auto": True}
    first, second = auto.posts()
    assert second["text"] == "Buy milk\nCall Sam at 6"
    assert second["written_at"] == first["written_at"]
    assert out["athena"]["state"] == "sent" and out["athena"]["auto"] is True
    assert content(note_id) == "Buy milk\nCall Sam at 6"
    # The same text read again: Athena already has it, nothing is sent.
    tr.save_review("alice", note_id, "p0", "Buy milk\nCall Sam at 6")
    run(note_id, json.dumps({"text": "Buy milk\nCall Sam at 6"}), page_ids=["p0"])
    assert len(auto.posts()) == 2


def test_manual_review_clears_auto(auto):
    note_id = new_note()
    run(note_id, CLEAN)
    out = tr.save_review("alice", note_id, "p0", "Buy milk\nCall Sam at five")
    [p] = out["pages"]
    assert p["status"] == "reviewed" and p["auto"] is False
    t = sidecar(note_id)["pages"][0]["transcript"]
    assert "auto" not in t and "previous" not in t
    # The owner's correction scores the model for H5 (scripts/ink_eval.py --stored).
    assert t["machine"]["text"] == "Buy milk\nCall Sam at 5"
    assert t["machine"]["strokes_hash"] == t["strokes_hash"]
    assert "auto" not in ink.list_notes("alice")[0]["transcripts"]
    # Athena's copy is still the automatic one until the owner sends again.
    assert out["athena"]["state"] == "changed"


def test_a_failed_send_is_left_for_the_owner(auto):
    auto.down = True
    note_id = new_note()
    out = run(note_id, CLEAN)
    a = out["athena"]
    assert out["pages"][0]["status"] == "reviewed"  # the page stays accepted
    assert a["state"] == "not_sent" and a["sending"] is False
    assert a["auto_error"].startswith("The automatic send failed")
    stored = sidecar(note_id)["athena"]
    assert stored["sending"] is None and stored["maybe_sent"] is True
    # The owner sends by hand once Athena is back; the error goes.
    auto.down = False
    sent = asyncio.run(athena.send("alice", note_id))
    assert sent["athena"]["state"] == "sent" and sent["athena"]["auto_error"] is None
    assert sent["athena"]["auto"] is False


def test_athena_not_set_up_marks_the_error_and_keeps_the_page(auto, monkeypatch):
    from src import call_recorder as rec

    monkeypatch.setattr(rec, "_server_rows", lambda: [])
    note_id = new_note()
    out = run(note_id, CLEAN)
    assert out["pages"][0]["status"] == "reviewed"
    assert out["athena"]["auto_error"] and out["athena"]["state"] == "not_sent"
    assert auto.requests == []


def test_waits_for_a_send_in_flight_then_sends_the_new_text(auto):
    note_id = new_note(pages=[page("p0"), page("p1")])
    tr.save_review("alice", note_id, "p0", "Old page")
    tr.save_review("alice", note_id, "p1", "Other page")
    store = ink._store_owner("alice", note_id)

    async def go():
        athena._SENDING.add((store or "", note_id))  # a manual send of the note in flight
        task = asyncio.create_task(transcribe(note_id, Fake([CLEAN]), page_ids=["p0"]))
        await asyncio.sleep(0.3)
        assert auto.requests == []
        assert tr.transcripts("alice", note_id)["auto_sending"] is True
        athena._SENDING.discard((store or "", note_id))
        await task
        await tr.wait_idle()

    asyncio.run(go())
    [body] = auto.posts()
    assert body["text"] == "Buy milk\nCall Sam at 5\n\nOther page"
    assert tr.transcripts("alice", note_id)["auto_sending"] is False


def test_a_note_deleted_during_the_run_is_not_sent(auto):
    note_id = new_note()

    async def go():
        gate = asyncio.Event()
        await tr.start("alice", note_id, reader=("fake-vl", Fake([CLEAN], gate=gate)))
        ink.delete("alice", note_id)
        gate.set()
        await tr.wait_idle()

    asyncio.run(go())
    assert auto.posts() == []


def test_the_transcribe_route_passes_its_origin_for_the_link(auto, monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: "")
    monkeypatch.setattr(tr, "default_reader", lambda owner: ("fake-vl", Fake([CLEAN])))
    note_id = new_note()
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = "alice"
        request.state.api_token = False
        return await call_next(request)

    app.include_router(ir.setup_ink_routes())
    with TestClient(app, base_url="http://tablet.example:7000") as client:
        res = client.post(f"/api/ink/notes/{note_id}/transcribe", json={})
        assert res.status_code == 200 and res.json()["auto_send"] is True
        deadline = time.time() + 5
        while not auto.posts() and time.time() < deadline:
            time.sleep(0.05)
    [body] = auto.posts()
    assert body["link"] == f"http://tablet.example:7000/#open=notes&note={note_id}"
