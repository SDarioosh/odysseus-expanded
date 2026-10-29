"""Fork: the belief ledger's schema, migrations and API results, pinned.

``fork/beliefs/evidence_intake.py`` (the ledger) is run against a table of fictional
evidence and owner actions; the database schema after a fresh start and after migrating
an old ledger file, what each call returned or raised, every table's rows at the end and
the memory entries it mirrored are compared with ``fixtures/ledger_characterization.json``,
recorded from the ledger as one module. The ledger must keep doing exactly that, however
its code is laid out. Memory ids (random) are numbered in order of appearance; nothing
else is masked.

Record the fixture (only on code whose behaviour is the reference):
``UPDATE_CHARACTERIZATION=1 python -m pytest -q tests/fork/test_ledger_characterization.py``.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

from fork.beliefs import evidence_intake as ledger

FIXTURE = Path(__file__).with_name("fixtures") / "ledger_characterization.json"
UTC = datetime.UTC
T0 = datetime.datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
ENV = ("BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS",
       "BELIEF_STALE_DAYS")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def surface(module) -> list[str]:
    """The names the module offers its importers: everything at its top level but dunders,
    modules and what it imports from outside the fork (stdlib, typing, upstream)."""
    out = []
    for name, value in vars(module).items():
        if name.startswith("__") or type(value).__name__ == "module":
            continue
        owner = getattr(value, "__module__", None)
        if isinstance(owner, str) and not owner.startswith("fork.") and (
                callable(value) or isinstance(value, type)):
            continue
        out.append(name)
    return sorted(out)


class Ids:
    """Random memory ids -> ``m1``, ``m2`` ... in order of appearance."""

    def __init__(self):
        self.seen: dict[str, str] = {}

    def __call__(self, value):
        if isinstance(value, str):
            return _UUID.sub(lambda m: self.seen.setdefault(m[0], f"m{len(self.seen) + 1}"),
                             value)
        if isinstance(value, dict):
            return {k: self(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value]
        if isinstance(value, set):
            return sorted(self(v) for v in value)
        return value


def ev(source, source_id, text, *, about="person", subject="Priya Nair",
       seen=("2026-10-01", "2026-10-01"), **kw):
    return ledger.Evidence(source=source, source_id=str(source_id), subject=subject,
                           about=about, text=text,
                           link=kw.pop("link", f"http://h/{source}/{source_id}"),
                           seen_first=seen[0], seen_last=seen[1], **kw)


def at(days: int = 0, hours: int = 0) -> datetime.datetime:
    return T0 + datetime.timedelta(days=days, hours=hours)


def _schema(path: Path) -> dict:
    db = sqlite3.connect(str(path))
    try:
        rows = db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                          "ORDER BY type, name").fetchall()
        version = db.execute("PRAGMA user_version").fetchone()[0]
    finally:
        db.close()
    return {"user_version": version, "objects": [list(r) for r in rows]}


def _tables(path: Path, mask_stamps: bool = False) -> dict:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    out = {}
    try:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = "
                                          "'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            rows = []
            for r in db.execute(f"SELECT * FROM {name} ORDER BY rowid"):
                row = dict(r)
                if mask_stamps:  # a migration stamps subjects with the wall clock
                    for k in ("created_at", "updated_at"):
                        if name == "subjects" and k in row:
                            row[k] = "<now>"
                rows.append(row)
            out[name] = rows
    finally:
        db.close()
    return out


def _memory(path: Path) -> list:
    from src.memory import MemoryManager

    return MemoryManager(str(path)).load_all()


def _call(log: list, ids: Ids, label: str, fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 -- the error is part of the record
        log.append([label, {"raised": type(e).__name__, "message": str(e)}])
        return None
    log.append([label, ids(json.loads(json.dumps(result, default=repr)))])
    return result


# The ledger file as an old version wrote it: no Phase 2 columns, no ``erased_at``, the
# events CHECK without ``unconfirmed`` and ``merged``, user_version 0.
OLD_SCHEMA = """
CREATE TABLE beliefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '', about TEXT NOT NULL DEFAULT 'other'
    CHECK (about IN ('owner', 'person', 'other')), text TEXT NOT NULL, norm TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'fact' CHECK (category IN ('fact', 'preference', 'contact')),
    status TEXT NOT NULL DEFAULT 'tentative'
    CHECK (status IN ('tentative', 'confirmed', 'stale', 'rejected')),
    confirmed_by TEXT CHECK (confirmed_by IN ('source', 'owner', 'rule')), first_seen TEXT,
    last_seen TEXT, valid_until TEXT, memory_id TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, owner_text TEXT, owner_norm TEXT, note TEXT);
CREATE INDEX beliefs_owner_owner_norm ON beliefs (owner, owner_norm);
CREATE TABLE evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    belief_id INTEGER NOT NULL REFERENCES beliefs (id) ON DELETE CASCADE,
    owner TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, source_id TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '', about TEXT NOT NULL DEFAULT 'other',
    category TEXT NOT NULL DEFAULT 'fact', text TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '', seen_first TEXT, seen_last TEXT,
    times INTEGER NOT NULL DEFAULT 1, days INTEGER NOT NULL DEFAULT 1,
    source_confirmed INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL, UNIQUE (belief_id, source, source_id),
    UNIQUE (owner, source, source_id));
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    belief_id INTEGER NOT NULL REFERENCES beliefs (id) ON DELETE CASCADE,
    at TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('added', 'evidence', 'confirmed',
    'stale', 'rejected', 'corrected', 'restored', 'retired_upstream')),
    detail TEXT NOT NULL DEFAULT '');
INSERT INTO beliefs (owner, subject, about, text, norm, category, status, confirmed_by,
    first_seen, last_seen, created_at, updated_at, owner_text, owner_norm)
VALUES ('alice', 'Sam', 'owner', 'I am vegetarian', 'i am vegetarian', 'fact', 'confirmed',
        'rule', '2026-09-01', '2026-09-03', '2026-09-03T00:00:00+00:00',
        '2026-09-03T00:00:00+00:00', 'My diet is vegetarian', 'my diet is vegetarian'),
       ('alice', 'Launch plan', 'other', 'The launch plan ships in May', 'the launch plan ships in may',
        'fact', 'tentative', NULL, '2026-09-02', '2026-09-02', '2026-09-02T00:00:00+00:00',
        '2026-09-02T00:00:00+00:00', NULL, NULL),
       ('bob', 'Tom', 'person', 'Tom plays the cello', 'tom plays the cello', 'contact',
        'confirmed', 'source', '2026-09-04', '2026-09-04', '2026-09-04T00:00:00+00:00',
        '2026-09-04T00:00:00+00:00', NULL, NULL);
INSERT INTO evidence (belief_id, owner, source, source_id, subject, about, category, text,
    link, seen_first, seen_last, times, days, source_confirmed, payload, updated_at)
VALUES (1, 'alice', 'odysseus-sound', '11', 'Sam', 'owner', 'fact', 'I am vegetarian',
        'http://h/view/a', '2026-09-01', '2026-09-03', 3, 2, 0, '{}', '2026-09-03T00:00:00+00:00'),
       (3, 'bob', 'odysseus-sound', '12', 'Tom', 'person', 'contact', 'Tom plays the cello',
        'http://h/view/b', '2026-09-04', '2026-09-04', 1, 1, 1, '{}', '2026-09-04T00:00:00+00:00');
INSERT INTO events (belief_id, at, kind, detail)
VALUES (1, '2026-09-03T00:00:00+00:00', 'confirmed', 'by rule');
"""


def observe(data_dir: Path) -> dict:
    ids = Ids()
    out: dict = {}

    # 1. a fresh ledger file: the schema every migration ends at
    with ledger._connect():
        pass
    out["fresh_schema"] = _schema(ledger.db_path())
    ledger.db_path().unlink()

    # 2. an old ledger file, migrated on first open
    old = sqlite3.connect(str(ledger.db_path()))
    old.executescript(OLD_SCHEMA)
    old.close()
    with ledger._connect():
        pass
    out["migrated_schema"] = _schema(ledger.db_path())
    out["migrated_rows"] = _tables(ledger.db_path(), mask_stamps=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(ledger.db_path()) + suffix).unlink(missing_ok=True)

    # 3. the API on a fresh ledger, step by step
    log: list = []
    m = ledger.MODULE_SOURCE
    first = [
        ev(m, 1, "Priya Nair is allergic to peanuts", person_id="p1", times=2, days=1,
           payload={"kind": "personal", "quote": "Priya can't have peanuts"}),
        ev(m, 2, "Priya lives in Porto", subject="Priya", person_id="p1",
           payload={"kind": "personal"}),
        ev(m, 3, "I am vegetarian", about="owner", subject="Sam", times=3, days=2,
           seen=("2026-10-01", "2026-10-03")),
        ev(m, 4, "I love jazz", about="owner", subject="Me", source_confirmed=True),
        ev(m, 5, "The launch plan ships in May", about="other", subject="Launch plan",
           payload={"kind": "plan"}, valid_until="2026-11-30"),
        ev(m, 6, "Dentist on Friday", about="owner", subject="Sam",
           valid_until="2026-10-24", payload={"kind": "plan"}),
        ev("notes", "n1", "Tom plays the cello", subject="Tom"),
        ev("notes", "n2", "No link here", subject="Tom", link=""),
        ev("chat", "c1", "Tom plays the cello", subject="Tom"),
        ev(m, 7, "Me prefers tea", about="owner", subject="Me", times=1, days=1),
    ]
    _call(log, ids, "submit 1", ledger.submit, "alice", first, now=at())
    second = [
        ev(m, 1, "Priya Nair is allergic to peanuts", person_id="p1", times=4, days=3,
           seen=("2026-10-01", "2026-10-05"), payload={"kind": "personal"}),
        ev(m, 2, "Priya lives in Lisbon", subject="Priya", person_id="p1",
           payload={"kind": "personal"}),
        ev(m, 8, "Owner is vegetarian.", about="owner", subject="Me"),
        ev(m, 9, "Lena collects vintage maps", subject="Lena", person_id="p3"),
        ev("notes", "n3", "Project Atlas deadline is in March", about="other",
           subject="Project Atlas"),
    ]
    _call(log, ids, "submit 2", ledger.submit, "alice", second, now=at(1))
    _call(log, ids, "submit bob", ledger.submit, "bob",
          [ev(m, 1, "Tom plays the viola", subject="Tom", person_id="t1",
              source_confirmed=True)], now=at(1))
    _call(log, ids, "sync_people", ledger.sync_people, "alice", m, [
        {"id": "p0", "name": "Sam", "aliases": ["Sammy"], "is_owner": True},
        {"id": "p1", "name": "Priya Nair", "aliases": ["Priya", "P. Nair"]},
        {"id": "p2", "name": "Jonas", "aliases": []},
        {"id": "p4", "name": "Me", "aliases": []},
        {"id": "", "name": "Nobody"}, "not a mapping"], now=at(1, 1))
    _call(log, ids, "reconcile", ledger.reconcile_source, "alice", m,
          {"1", "2", "3", "4", "5", "8", "9"}, now=at(2))
    _call(log, ids, "decay", ledger.apply_decay, "alice", now=at(5))
    _call(log, ids, "decay stale_days", ledger.apply_decay, "alice", now=at(200),
          stale_days=150, mirror=False)
    beliefs = {b["text"]: b["id"] for b in ledger.list_beliefs("alice")}
    log.append(["beliefs by text", beliefs])
    cello = beliefs["Tom plays the cello"]
    _call(log, ids, "confirm", ledger.confirm, "alice", cello, now=at(6))
    _call(log, ids, "confirm missing", ledger.confirm, "alice", 9999, now=at(6))
    _call(log, ids, "correct", ledger.correct, "alice", beliefs["I am vegetarian"],
          "I eat no meat or fish", now=at(6, 1))
    _call(log, ids, "correct empty", ledger.correct, "alice", cello, "  ", now=at(6))
    _call(log, ids, "move to name", ledger.move, "alice",
          beliefs["Lena collects vintage maps"], name="Lena Fischer", now=at(6, 2))
    _call(log, ids, "move to owner", ledger.move, "alice", beliefs["I love jazz"],
          name="me", now=at(6, 3))
    _call(log, ids, "move nowhere", ledger.move, "alice", cello)
    _call(log, ids, "reject", ledger.reject, "alice", beliefs["Priya Nair is allergic to peanuts"],
          "  wrong   person ", now=at(7))
    _call(log, ids, "restore", ledger.restore, "alice", beliefs["Priya Nair is allergic to peanuts"],
          now=at(7, 1))
    _call(log, ids, "reject again", ledger.reject, "alice",
          beliefs["Priya Nair is allergic to peanuts"], now=at(7, 2))
    _call(log, ids, "erase not rejected", ledger.erase, "alice", cello, now=at(7))
    _call(log, ids, "erase", ledger.erase, "alice",
          beliefs["Priya Nair is allergic to peanuts"], {"1": "retired"}, now=at(8))
    _call(log, ids, "erase pending", ledger.erase_pending, "alice")
    _call(log, ids, "erase again", ledger.erase, "alice",
          beliefs["Priya Nair is allergic to peanuts"], {"1": "erased"}, now=at(8, 1))
    _call(log, ids, "confirm erased", ledger.confirm, "alice",
          beliefs["Priya Nair is allergic to peanuts"])
    _call(log, ids, "said again after erase", ledger.submit, "alice",
          [ev("notes", "n9", "Priya Nair is allergic to peanuts")], now=at(9))
    _call(log, ids, "scrub quotes", ledger.scrub_quotes, "alice", ["Priya can't have peanuts", ""])
    subjects = {s["display"]: s["id"] for s in ledger.list_subjects("alice")}
    log.append(["subjects by name", subjects])
    _call(log, ids, "set kind", ledger.set_subject_kind, "alice", subjects["Tom"], "project",
          now=at(10))
    _call(log, ids, "set kind owner", ledger.set_subject_kind, "alice",
          ledger.list_subjects("alice", kind="owner")[0]["id"], "thing")
    _call(log, ids, "set kind bad", ledger.set_subject_kind, "alice", subjects["Tom"], "owner")
    _call(log, ids, "link contact", ledger.link_contact, "alice", subjects["Tom"], "uid-tom",
          now=at(10))
    _call(log, ids, "link contact twice", ledger.link_contact, "alice", subjects["Jonas"],
          "uid-tom")
    _call(log, ids, "for contact", ledger.beliefs_for_contact, "alice", "uid-tom")
    _call(log, ids, "unlink", ledger.unlink_contact, "alice", subjects["Tom"], now=at(10, 1))
    _call(log, ids, "rename", ledger.rename_subject, "alice", subjects["Jonas"],
          "Jonas Berg", now=at(10, 2))
    _call(log, ids, "rename clash", ledger.rename_subject, "alice", subjects["Jonas"], "Tom")
    _call(log, ids, "merge", ledger.merge_subjects, "alice", subjects["Tom"], subjects["Jonas"],
          now=at(10, 3))
    _call(log, ids, "merge self", ledger.merge_subjects, "alice", subjects["Tom"],
          subjects["Tom"])
    _call(log, ids, "find subject", ledger.find_subject, "alice", "sammy")
    _call(log, ids, "find none", ledger.find_subject, "alice", "nobody at all")
    _call(log, ids, "get subject missing", ledger.get_subject, "alice", 9999)
    _call(log, ids, "list subjects bad", ledger.list_subjects, "alice", kind="pet")
    _call(log, ids, "structure candidates", ledger.structure_candidates, "alice", limit=10)
    for b in ledger.structure_candidates("alice", limit=10):
        attribute = {"Tom plays the cello": "likes",
                     "The launch plan ships in May": "project_status"}.get(b["text"], "diet")
        _call(log, ids, f"structure {b['id']}", ledger.apply_structure, "alice", b["id"],
              attribute, f"value of {b['text']}" * 4, "2026-10-01", "2026-12-31",
              expected_text=b["text"], now=at(11))
    _call(log, ids, "structure stale text", ledger.apply_structure, "alice", cello, "likes",
          "x", expected_text="other words", now=at(11))
    _call(log, ids, "clean attribute", lambda: [ledger.clean_attribute(v) for v in
                                                 ("Lives In", "works-at", "nonsense", None)])
    for name in ("Sam", "Tom", "Launch plan", "Priya Nair"):
        found = ledger.find_subject("alice", name)
        log.append([f"found {name}", found is not None])
        if found:
            _call(log, ids, f"summary {name}", ledger.subject_summary, "alice", found["id"])
    _call(log, ids, "by attribute", ledger.beliefs_by_attribute, "alice", "likes")
    _call(log, ids, "by attribute bad", ledger.beliefs_by_attribute, "alice", "colour")
    _call(log, ids, "list bad status", ledger.list_beliefs, "alice", status="maybe")
    _call(log, ids, "list confirmed", ledger.list_beliefs, "alice",
          status=("confirmed", "stale"), limit=3)
    _call(log, ids, "list subject", ledger.list_beliefs, "alice", subject="priya")
    _call(log, ids, "get belief", ledger.get_belief, "alice", cello)
    _call(log, ids, "stats", ledger.stats, "alice")
    _call(log, ids, "stats bob", ledger.stats, "bob")
    _call(log, ids, "export", ledger.export_rows, "alice")
    _call(log, ids, "memory sync partial", ledger.sync_memory, "alice", now=at(12),
          belief_ids=[cello])
    _call(log, ids, "memory sync none", ledger.sync_memory, "alice", now=at(12), belief_ids=[])
    _call(log, ids, "memory sync", ledger.sync_memory, "alice", now=at(12))
    _call(log, ids, "confirmation_for", lambda: [
        ledger.confirmation_for(b, e, ledger.Settings()) for b, e in (
            ({"status": "rejected", "confirmed_by": None}, []),
            ({"status": "tentative", "confirmed_by": "owner"}, []),
            ({"status": "tentative", "confirmed_by": None},
             [{"source": "a", "source_confirmed": 0, "times": 2, "days": 1,
               "seen_first": "2026-10-01", "seen_last": "2026-10-02", "gone": False}]),
            ({"status": "tentative", "confirmed_by": None},
             [{"source": "a", "source_confirmed": 0, "times": 1, "days": 1,
               "seen_first": "2026-10-01", "seen_last": "2026-10-01", "gone": False},
              {"source": "b", "source_confirmed": 0, "times": 1, "days": 1,
               "seen_first": "2026-10-01", "seen_last": "2026-10-01", "gone": True}]))])
    _call(log, ids, "normalize", lambda: [ledger.normalize(t, s, a) for t, s, a in (
        ("I'm   vegetarian!", None, "owner"), ("My sister lives in Porto", "Sam", "owner"),
        ("Sam's dog is Rex.", "Sam", "owner"), ("Meals are fine", None, "owner"),
        ("Tom plays CELLO.", "Tom", "person"))])
    _call(log, ids, "category_for", lambda: [ledger.category_for(a, t, k) for a, t, k in (
        ("owner", "I love jazz", None), ("owner", "I live in Porto", None),
        ("person", "Tom likes tea", "preference"), ("owner", "I plan a trip", "plan"),
        ("other", "x", "preference"))])
    out["calls"] = log
    out["tables"] = ids(_tables(ledger.db_path()))
    out["memory"] = ids(_memory(data_dir))
    return out


@pytest.fixture
def ledger_dir(data_dir, monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    return data_dir


def test_the_ledger_behaves_as_recorded(ledger_dir):
    seen = json.loads(json.dumps(observe(ledger_dir), default=repr))
    seen["surface"] = surface(ledger)
    if os.environ.get("UPDATE_CHARACTERIZATION"):
        FIXTURE.write_text(json.dumps(seen, indent=1, sort_keys=True, ensure_ascii=False)
                           + "\n", encoding="utf-8")
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # every name the module offered stays importable from it (more may be added)
    assert set(expected.pop("surface")) <= set(seen.pop("surface"))
    for key in expected:
        assert seen[key] == expected[key], key
    assert set(seen) == set(expected)
