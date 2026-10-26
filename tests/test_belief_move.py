"""Fork: move a belief to the person it is about (src/belief_move.py, ``move`` in
src/evidence_intake.py, ``POST /api/beliefs/{id}/move``).

A line someone else said was heard as the owner's voice, so their fact landed under the
owner. Editing the text kept it there; Move files it under the right person and has
odysseus-sound give the line to them. Fictional data only.
"""
import asyncio
import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.beliefs_routes as br
from core.middleware import require_admin
from src import belief_move as bm
from src import evidence_intake as ledger
from src import module_facts as mf

T0 = datetime.datetime(2026, 10, 21, 9, 0, tzinfo=datetime.timezone.utc)
T1 = T0 + datetime.timedelta(hours=1)


def ev(sid, text, subject="Ana", about="owner", t=42, **kw):
    return ledger.Evidence(
        source=mf.SOURCE, source_id=str(sid), subject=subject, about=about, text=text,
        link=f"http://h:8000/view/conv{sid}?sig=abc#t={t}", seen_first="2026-10-01",
        seen_last="2026-10-01", **kw)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mf, "_LOCKS", {})
    for var in ("MODULE_FACTS_SYNC", "BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES",
                "BELIEF_CONFIRM_DAYS", "BELIEF_STALE_DAYS", "BELIEF_CONSOLIDATE"):
        monkeypatch.delenv(var, raising=False)
    ledger.submit("alice", [ev(1, "Ana owns a sailboat named Gull."),
                            ev(2, "Ana plays the cello."),
                            ev(3, "Dana Reyes lives in Porto.", subject="Dana Reyes",
                               about="person")], now=T0)
    return tmp_path


def belief(text, owner="alice"):
    return next(b for b in ledger.list_beliefs(owner, status=None) if b["text"] == text)


def subject_of(b):
    return ledger.get_subject("alice", b["subject_id"])


# ------------------------------------------------------------------ ledger
def test_move_to_an_existing_person(data):
    b = belief("Ana owns a sailboat named Gull.")
    dana = belief("Dana Reyes lives in Porto.")["subject_id"]
    moved = ledger.move("alice", b["id"], subject_id=dana, now=T1)
    assert moved["subject_id"] == dana
    assert (moved["about"], moved["category"]) == ("person", "contact")
    assert (moved["status"], moved["confirmed_by"]) == ("confirmed", "owner")
    assert moved["subject"] == "Dana Reyes"
    assert moved["structured_at"] is None
    assert any(e["kind"] == "moved" and "Dana Reyes" in e["detail"]
               for e in moved["events"])


def test_move_to_a_new_name_creates_a_person(data):
    b = belief("Ana owns a sailboat named Gull.")
    moved = ledger.move("alice", b["id"], name="  Lee  Okafor ", now=T1)
    s = subject_of(moved)
    assert (s["display"], s["kind"]) == ("Lee Okafor", "person")


def test_move_by_name_finds_the_subject_or_an_alias(data):
    b = belief("Ana owns a sailboat named Gull.")
    dana = belief("Dana Reyes lives in Porto.")["subject_id"]
    assert ledger.move("alice", b["id"], name="dana reyes", now=T1)["subject_id"] == dana


def test_move_back_to_the_owner(data):
    b = belief("Ana owns a sailboat named Gull.")
    owner_subject = b["subject_id"]
    ledger.move("alice", b["id"], name="Lee Okafor", now=T1)
    back = ledger.move("alice", b["id"], name="me", now=T1)
    assert back["subject_id"] == owner_subject
    assert (back["about"], back["category"]) == ("owner", "fact")


def test_a_corrected_text_keeps_its_correction_and_matches_under_the_new_subject(data):
    b = belief("Ana owns a sailboat named Gull.")
    ledger.correct("alice", b["id"], "Lee Okafor owns a sailboat named Gull.", now=T1)
    moved = ledger.move("alice", b["id"], name="Lee Okafor", now=T1)
    assert moved["shown_text"] == "Lee Okafor owns a sailboat named Gull."
    # the same words said about Lee later join this belief
    ledger.submit("alice", [ev(7, "Lee Okafor owns a sailboat named Gull.",
                               subject="Lee Okafor", about="person")], now=T1)
    assert len(ledger.get_belief("alice", b["id"])["evidence"]) == 2


def test_move_errors(data):
    b = belief("Ana plays the cello.")
    with pytest.raises(ValueError):
        ledger.move("alice", b["id"], name="  ")
    with pytest.raises(ledger.SubjectNotFound):
        ledger.move("alice", b["id"], subject_id=999)
    with pytest.raises(ledger.BeliefNotFound):
        ledger.move("bob", b["id"], name="Lee")
    # same subject: nothing changes
    same = ledger.move("alice", b["id"], subject_id=b["subject_id"], now=T1)
    assert same["status"] == b["status"]


def test_an_erased_belief_cant_move(data):
    b = belief("Ana plays the cello.")
    ledger.reject("alice", b["id"], now=T1)
    ledger.erase("alice", b["id"], now=T1)
    with pytest.raises(ledger.BeliefErased):
        ledger.move("alice", b["id"], name="Lee")


# ------------------------------------------------------------- later syncs
def test_the_same_fact_again_leaves_the_move_alone(data):
    b = belief("Ana owns a sailboat named Gull.")
    ledger.move("alice", b["id"], name="Lee Okafor", now=T1)
    ledger.submit("alice", [ev(1, "Ana owns a sailboat named Gull.")], now=T1)
    assert subject_of(ledger.get_belief("alice", b["id"]))["display"] == "Lee Okafor"


def test_a_fact_the_module_reattributes_stays_with_the_moved_belief(data):
    b = belief("Ana owns a sailboat named Gull.")
    ledger.move("alice", b["id"], name="Lee Okafor", now=T1)
    before = len(ledger.list_beliefs("alice", status=None))
    # reassign_line made the module redo the summary: same fact id, Lee's now
    ledger.submit("alice", [ev(1, "Lee Okafor owns a sailboat named Gull.",
                               subject="Lee Okafor", about="person")], now=T1)
    assert len(ledger.list_beliefs("alice", status=None)) == before
    got = ledger.get_belief("alice", b["id"])
    assert [e["subject"] for e in got["evidence"]] == ["Lee Okafor"]


def test_a_changed_claim_still_splits_off(data):
    b = belief("Ana plays the cello.")
    ledger.confirm("alice", b["id"], now=T1)
    before = len(ledger.list_beliefs("alice", status=None))
    ledger.submit("alice", [ev(2, "Ana plays the viola.")], now=T1)
    assert len(ledger.list_beliefs("alice", status=None)) == before + 1


# ---------------------------------------------------------------- module side
class Module:
    """get_transcript / reassign_line as odysseus-sound answers them."""

    def __init__(self, speaker="Ana", second=42, tools=True, down=False):
        self.speaker, self.second, self.tools, self.down = speaker, second, tools, down
        self.calls = []

    async def __call__(self, tool, args):
        self.calls.append((tool, args))
        if self.down:
            raise RuntimeError(f"{tool}: connection refused")
        if not self.tools:
            raise RuntimeError(f"Unknown tool: {tool}")
        if tool == "get_transcript":
            return ('"Boats" 2026-10-01 10:00:00 http://h:8000/view/x?sig=s\n'
                    f"#77 [{self.second}s] {self.speaker}: I keep her at the marina.\n"
                    "Cite each fact with its link.")
        return f"Line #{args['line_id']} moved from {self.speaker} to {args['name']}."


def run(coro):
    return asyncio.run(coro)


def test_source_lines_from_links():
    evidence = [{"source": mf.SOURCE, "link": "http://h/view/abc?sig=1#t=42"},
                {"source": mf.SOURCE, "link": "http://h/view/abc?sig=1#t=42"},
                {"source": "notes", "link": "/notes/1"},
                {"source": mf.SOURCE, "link": ""}]
    assert bm.source_lines(evidence) == [("abc", 42)]


def test_the_owners_line_is_given_to_the_person(data):
    b = belief("Ana owns a sailboat named Gull.")
    module = Module()
    r = run(bm.move("alice", b["id"], name="Lee Okafor", call=module))
    assert r["lines"]["reassigned"] == 1
    assert module.calls[0] == ("get_transcript", {"conversation_id": "conv1",
                                                  "start_seconds": 42, "end_seconds": 42,
                                                  "max_lines": 10})
    assert module.calls[1] == ("reassign_line", {"line_id": 77, "name": "Lee Okafor"})


def test_a_line_someone_else_said_is_left_alone(data):
    b = belief("Ana owns a sailboat named Gull.")
    module = Module(speaker="Speaker 2")
    r = run(bm.move("alice", b["id"], name="Lee Okafor", call=module))
    assert r["lines"]["kept"] == 1
    assert [c[0] for c in module.calls] == ["get_transcript"]


def test_no_line_at_that_second(data):
    b = belief("Ana owns a sailboat named Gull.")
    r = run(bm.move("alice", b["id"], name="Lee Okafor", call=Module(second=50)))
    assert r["lines"]["not_found"] == 1


@pytest.mark.parametrize("module, key", [(None, "unsupported"),
                                         (Module(tools=False), "unsupported"),
                                         (Module(down=True), "failed")])
def test_the_move_stands_when_the_module_cant_help(data, module, key):
    b = belief("Ana owns a sailboat named Gull.")
    r = run(bm.move("alice", b["id"], name="Lee Okafor", call=module))
    assert r["lines"][key] == 1
    assert subject_of(ledger.get_belief("alice", b["id"]))["display"] == "Lee Okafor"


def test_without_fix_line_the_module_is_not_called(data):
    b = belief("Ana owns a sailboat named Gull.")
    module = Module()
    r = run(bm.move("alice", b["id"], name="Lee Okafor", fix_line=False, call=module))
    assert r["lines"] is None and module.calls == []


# ------------------------------------------------------------------- route
def client_for(user="alice"):
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = user
        request.state.api_token = False
        return await call_next(request)

    app.dependency_overrides[require_admin] = lambda: None
    app.include_router(br.setup_beliefs_routes())
    return TestClient(app)


def test_move_route(data, monkeypatch):
    module = Module()
    monkeypatch.setattr(br, "module_caller", lambda: module)
    b = belief("Ana owns a sailboat named Gull.")
    c = client_for()
    r = c.post(f"/api/beliefs/{b['id']}/move", json={"name": "Lee Okafor"})
    assert r.status_code == 200
    body = r.json()
    assert body["subject_display"] == "Lee Okafor"
    assert body["lines"]["reassigned"] == 1
    assert c.post(f"/api/beliefs/{b['id']}/move", json={}).status_code == 400
    assert c.post(f"/api/beliefs/{b['id']}/move", json={"subject_id": 999}).status_code == 404
    assert c.post("/api/beliefs/999/move", json={"name": "Lee"}).status_code == 404
    assert client_for("bob").post(f"/api/beliefs/{b['id']}/move",
                                  json={"name": "Lee"}).status_code == 404
