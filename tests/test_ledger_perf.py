"""Fork: belief ledger performance at small scale (src/evidence_intake.py).

Intake must stay flat as the ledger grows, the two hot queries must be index searches
(also with planner statistics present), and an owner action must touch only its own
memory entry. Fictional data only.
"""
import datetime
import json
import sqlite3
import time

import pytest

from src import evidence_intake as ei
from src.memory import MemoryManager

T0 = datetime.datetime(2026, 10, 21, 9, 0, tzinfo=datetime.timezone.utc)
CHUNK = 500


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    for var in ("BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS",
                "BELIEF_STALE_DAYS"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def ev(i, *, confirmed=False):
    who = f"Person {i % 300}"
    return ei.Evidence(source="notes", source_id=f"n{i}", subject=who, about="person",
                       text=f"{who} mentioned item number {i}", link=f"/#open=notes&note={i}",
                       seen_first="2026-10-01", seen_last="2026-10-01",
                       source_confirmed=confirmed)


def seed(n, start=0):
    """Submit ``n`` new beliefs in chunks of 500; the seconds each chunk took."""
    took = []
    for first in range(start, start + n, CHUNK):
        items = [ev(i) for i in range(first, min(first + CHUNK, start + n))]
        t = time.perf_counter()
        ei.submit("alice", items, now=T0, mirror=False)
        took.append(time.perf_counter() - t)
    return took


def plan(sql, args):
    with sqlite3.connect(ei.db_path()) as db:
        return [row[3] for row in db.execute(f"EXPLAIN QUERY PLAN {sql}", args)]


def test_submitting_new_evidence_stays_flat(data):
    took = seed(2000)
    first, last = took[0], took[-1]
    assert last <= 3 * first, f"last 500 took {last:.3f}s, first 500 {first:.3f}s"


def test_match_query_searches_both_indexes_with_statistics(data):
    seed(1000)
    ei.correct("alice", ei.list_beliefs("alice", limit=1)[0]["id"], "Person 0 likes figs")
    with sqlite3.connect(ei.db_path()) as db:
        db.execute("ANALYZE")  # the hard case: statistics of a one-owner ledger
    steps = plan(ei.MATCH_SQL, ("alice", "x", -1, "alice", "x", -1))
    searches = [s for s in steps if s.startswith("SEARCH beliefs")]
    assert len(searches) == 2, steps
    assert any("beliefs_owner_norm (owner=? AND norm=?)" in s for s in searches), steps
    assert any("beliefs_owner_corrected (owner=? AND owner_norm=?)" in s for s in searches), \
        steps
    assert not any(s.startswith("SCAN beliefs") for s in steps), steps
    # the matching itself still works for text and for the owner's correction
    counts = ei.submit("alice", [ev(5), ei.Evidence(
        source="chat", source_id="c1", subject="Person 0", about="person",
        text="person 0 likes figs.", link="/#c1", seen_first="2026-10-02",
        seen_last="2026-10-02")], now=T0, mirror=False)
    assert counts["beliefs_added"] == 0


def test_subject_counts_query_uses_the_subject_index(data):
    seed(1000)
    with sqlite3.connect(ei.db_path()) as db:
        db.execute("ANALYZE")
    for where, args in (("", ("alice",)), (" AND subject_id = ?", ("alice", 3))):
        steps = plan(ei.SUBJECT_COUNTS_SQL.format(where=where), args)
        assert any(s.startswith("SEARCH beliefs USING INDEX beliefs_owner_subject")
                   for s in steps), steps
        assert not any(s.startswith("SCAN beliefs") for s in steps), steps
    subjects = ei.list_subjects("alice")
    assert len(subjects) == 300
    assert sum(s["counts"]["total"] for s in subjects) == 1000
    one = ei.get_subject("alice", subjects[0]["id"])
    assert one["counts"] == subjects[0]["counts"]


def test_an_old_full_owner_norm_index_is_replaced(data):
    seed(10)
    with sqlite3.connect(ei.db_path()) as db:
        db.execute("DROP INDEX beliefs_owner_corrected")
        db.execute("CREATE INDEX beliefs_owner_owner_norm ON beliefs (owner, owner_norm)")
    ei.stats("alice")
    with sqlite3.connect(ei.db_path()) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE "
                                          "type = 'index' AND tbl_name = 'beliefs'")}
    assert "beliefs_owner_corrected" in names and "beliefs_owner_owner_norm" not in names


# ------------------------------------------------------------------ partial memory sync
def entries(path):
    return json.loads((path / "memory.json").read_text())


def test_owner_action_touches_only_its_own_memory_entry(data):
    ei.submit("alice", [ev(i, confirmed=True) for i in range(40)], now=T0)
    manager = MemoryManager(str(data))
    own = manager.add_entry("Keeps the spare key under the blue pot", owner="alice")
    manager.save([*manager.load_all_for_update(), own])
    before = entries(data)
    assert len(before) == 41
    target = ei.list_beliefs("alice", limit=1)[0]["id"]

    ei.correct("alice", target, "Person 0 mentioned the corrected item", now=T0)
    after = entries(data)
    changed = [a for b, a in zip(before, after, strict=True)
               if json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True)]
    assert [e["belief_id"] for e in changed] == [target]
    assert changed[0]["text"].startswith("Person 0 mentioned the corrected item (source: ")

    ei.reject("alice", target, now=T0)
    gone = entries(data)
    assert len(gone) == 40 and target not in {e.get("belief_id") for e in gone}
    assert gone == [e for e in after if e.get("belief_id") != target]

    # a partial sync leaves other beliefs' entries alone, even ones a full sync removes
    other = gone[0]
    stray = {**other, "id": "stray", "belief_id": 999999}
    MemoryManager(str(data)).save([*gone, stray])
    raw = (data / "memory.json").read_bytes()
    assert ei.sync_memory("alice", now=T0, belief_ids=[other["belief_id"]]) == \
        {"kept": 1, "added": 0, "updated": 0, "removed": 0}
    assert (data / "memory.json").read_bytes() == raw  # nothing changed: nothing written
    assert ei.sync_memory("alice", now=T0)["removed"] == 1
    assert ei.sync_memory("alice", now=T0, belief_ids=[]) == \
        {"kept": 0, "added": 0, "updated": 0, "removed": 0}


def test_partial_sync_adds_a_missing_entry(data):
    ei.submit("alice", [ev(1, confirmed=True)], now=T0, mirror=False)
    (belief,) = ei.list_beliefs("alice")
    assert ei.sync_memory("alice", now=T0, belief_ids=[belief["id"]])["added"] == 1
    assert ei.get_belief("alice", belief["id"])["memory_id"] == entries(data)[0]["id"]
