"""Fork: a forget that is complete across hub and module (src/belief_forget.py, the
erase in src/evidence_intake.py; personal-context stage 4.2).

Fictional data only.
"""
import datetime
import json
import sqlite3
from pathlib import Path

import pytest

from src import belief_forget as bf
from src import evidence_intake as ledger
from src import module_facts as mf
from src.memory import MemoryManager

T0 = datetime.datetime(2026, 10, 21, 9, 0, tzinfo=datetime.timezone.utc)
T1 = T0 + datetime.timedelta(hours=1)
LINK = "http://h:8000/view/c{sid}?sig=abc#t=10"


def ev(sid, text, source=mf.SOURCE, subject="Me", about="owner", confirmed=True):
    return ledger.Evidence(source=source, source_id=str(sid), subject=subject, about=about,
                           text=text, link=LINK.format(sid=sid), seen_first="2026-10-01",
                           seen_last="2026-10-15", times=3, days=2, source_confirmed=confirmed)


class Module:
    """erase_fact / retire_fact as the module's MCP server answers them."""

    def __init__(self, erase=True, down=False):
        self.erase, self.down, self.calls = erase, down, []

    async def __call__(self, tool, args):
        self.calls.append((tool, args["fact_id"]))
        if self.down:
            raise RuntimeError(f"{tool}: connection refused")
        if tool == "erase_fact" and not self.erase:
            raise RuntimeError("erase_fact: Unknown tool: erase_fact")
        return f"Fact #{args['fact_id']} done."


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mf, "_LOCKS", {})
    for var in ("MODULE_FACTS_SYNC", "BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES",
                "BELIEF_CONFIRM_DAYS", "BELIEF_STALE_DAYS", "BELIEF_CONSOLIDATE"):
        monkeypatch.delenv(var, raising=False)
    ledger.submit("alice", [ev(1, "Keeps bees on the roof terrace"),
                            ev(2, "Plays the cello")], now=T0)
    return tmp_path


def belief_id(text, owner="alice"):
    return next(b["id"] for b in ledger.list_beliefs(owner, status=None)
                if b["shown_text"] == text)


def memory():
    import src.constants as constants

    return MemoryManager(constants.DATA_DIR)


def ledger_bytes(data: Path) -> bytes:
    return b"".join(p.read_bytes() for p in data.glob("beliefs.db*") if p.is_file())


async def test_forget_erases_the_ledger_copy_and_memory(data):
    bid = belief_id("Keeps bees on the roof terrace")
    assert any("bees" in m["text"] for m in memory().load_all())
    module = Module()
    out = await bf.forget("alice", bid, "private", call=module)
    assert module.calls == [("erase_fact", 1)]
    assert out["propagated"]["erased"] == 1 and out["erased"]["pending"] == 0
    b = ledger.get_belief("alice", bid)
    assert (b["status"], b["text"], b["owner_text"], b["subject"], b["note"]) == \
        ("rejected", "", None, "", None)
    assert b["evidence"][0]["text"] == "" and b["evidence"][0]["link"] == ""
    assert [e["kind"] for e in b["events"]][0] == "erased"
    assert all(e["detail"] == "" for e in b["events"])
    assert not any("bees" in m["text"] for m in memory().load_all())
    assert b"bees" not in ledger_bytes(data) and b"cello" in ledger_bytes(data)


async def test_the_claim_said_again_stays_erased_without_its_words(data):
    bid = belief_id("Keeps bees on the roof terrace")
    await bf.forget("alice", bid, call=Module())
    # a note says it, and the module reports it again under a new fact id
    counts = ledger.submit("alice", [ev("n1:0", "Keeps bees on the roof terrace.",
                                        source="notes"),
                                     ev(5, "keeps bees on the roof terrace")], now=T1)
    assert counts["beliefs_added"] == 0
    b = ledger.get_belief("alice", bid)
    assert b["status"] == "rejected" and len(b["evidence"]) == 3
    assert all(e["text"] == "" for e in b["evidence"])
    states = {e["source_id"]: e["payload"].get("erase") for e in b["evidence"]}
    assert states == {"1": "done", "n1:0": "source", "5": "pending"}
    assert not any("bees" in m["text"] for m in memory().load_all())
    assert b"bees" not in ledger_bytes(data)
    # the facts job erases the new fact in the module too
    module = Module()
    assert await bf.retry_pending("alice", module) == 1
    assert module.calls == [("erase_fact", 5)]
    assert ledger.erase_pending("alice") == []


async def test_an_older_module_is_retried_until_it_can_erase(data):
    bid = belief_id("Keeps bees on the roof terrace")
    old = Module(erase=False)
    out = await bf.forget("alice", bid, call=old)
    assert out["propagated"]["retired"] == 1 and out["erased"]["pending"] == 1
    assert old.calls == [("erase_fact", 1), ("retire_fact", 1)]
    assert await bf.retry_pending("alice", Module(erase=False)) == 0
    assert ledger.erase_pending("alice") == [{"belief_id": bid, "fact_ids": [1]}]
    assert await bf.retry_pending("alice", Module()) == 1  # updated module
    assert ledger.erase_pending("alice") == []


async def test_a_module_that_is_down_is_retried(data):
    bid = belief_id("Keeps bees on the roof terrace")
    out = await bf.forget("alice", bid, call=Module(down=True))
    assert out["propagated"]["failed"] == 1
    assert ledger.get_belief("alice", bid)["erased_at"]  # the hub side is erased anyway
    assert await bf.retry_pending("alice", Module(down=True)) == 0
    assert await bf.retry_pending("alice", Module()) == 1


async def test_the_facts_job_retries_pending_erasures(data):
    bid = belief_id("Keeps bees on the roof terrace")
    await bf.forget("alice", bid, call=None)
    calls = []

    async def module(tool, args):
        calls.append(tool)
        if tool == "list_facts":
            return json.dumps({"facts": []})
        if tool == "list_people":
            return json.dumps({"people": []})
        return "Fact erased."

    stats = await mf.sync("alice", module, now=T1)
    assert stats["erased"] == 1 and "erase_fact" in calls
    assert mf.changed(stats)


async def test_copies_of_its_words_leave_memory(data):
    mgr = memory()
    entries = mgr.load_all_for_update()
    entries += [
        {"id": "chat-1", "text": "keeps bees on the roof terrace!", "owner": "alice",
         "source": "chat"},
        {"id": "chat-2", "text": "Keeps bees at the allotment", "owner": "alice"},
        {"id": "bob-1", "text": "Keeps bees on the roof terrace", "owner": "bob"},
    ]
    mgr.save(entries)
    out = await bf.forget("alice", belief_id("Keeps bees on the roof terrace"), call=Module())
    assert out["erased"]["memory"] == 1  # chat-1; the mirrored entry left with the reject
    left = {m["id"] for m in memory().load_all()}
    assert "chat-1" not in left and {"chat-2", "bob-1"} <= left


async def test_stored_today_insights_drop_it(data):
    from src import today_insight as ti

    bid = belief_id("Keeps bees on the roof terrace")
    ti.save("alice", {"date": "2026-10-20", "headline": "Keeps bees on the roof terrace, so…",
                      "focus": [{"ref": f"belief:{bid}", "why": "plan", "title": "bees"},
                                {"ref": "todo:n1:0", "why": "keeps bees on the roof terrace",
                                 "title": "Buy honey jars"}],
                      "coming_up": [], "also": [], "votes": []})
    ti.save("alice", {"date": "2026-10-19", "headline": "Quiet day", "focus": [],
                      "coming_up": [], "also": [], "votes": []})
    out = await bf.forget("alice", bid, call=Module())
    assert out["erased"]["insights"] == 1
    day = ti.load("alice", "2026-10-20")
    assert day["headline"] == "" and day["focus"] == [
        {"ref": "todo:n1:0", "why": "", "title": "Buy honey jars"}]
    assert ti.load("alice", "2026-10-19")["headline"] == "Quiet day"


async def test_its_search_vector_is_deleted(data):
    bid = belief_id("Keeps bees on the roof terrace")
    with sqlite3.connect(data / "beliefs.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS belief_vectors (belief_id INTEGER PRIMARY KEY, "
                   "text_hash TEXT, model TEXT, vector BLOB, updated_at TEXT)")
        db.execute("INSERT INTO belief_vectors VALUES (?, 'h', 'm', x'00', 't')", (bid,))
    await bf.forget("alice", bid, call=Module())
    with sqlite3.connect(data / "beliefs.db") as db:
        assert db.execute("SELECT COUNT(*) FROM belief_vectors").fetchone()[0] == 0


async def test_a_corrected_belief_is_matched_by_its_correction_too(data):
    bid = belief_id("Plays the cello")
    ledger.correct("alice", bid, "Plays the viola")
    await bf.forget("alice", bid, call=Module())
    ledger.submit("alice", [ev("n2:0", "Plays the viola", source="notes")], now=T1)
    assert all(b["shown_text"] != "Plays the viola"
               for b in ledger.list_beliefs("alice", status=None))
    assert len(ledger.get_belief("alice", bid)["evidence"]) == 2


def test_erase_needs_a_rejected_belief(data):
    with pytest.raises(ValueError):
        ledger.erase("alice", belief_id("Plays the cello"))
    with pytest.raises(ledger.BeliefNotFound):
        ledger.erase("bob", belief_id("Plays the cello"))


async def test_another_owner_is_untouched(data):
    ledger.submit("bob", [ev(1, "Keeps bees on the roof terrace")], now=T0)
    await bf.forget("alice", belief_id("Keeps bees on the roof terrace"), call=Module())
    assert belief_id("Keeps bees on the roof terrace", "bob")
    assert any(m["owner"] == "bob" and "bees" in m["text"] for m in memory().load_all())
