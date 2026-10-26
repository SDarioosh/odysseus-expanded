"""Fork: provenance-aware consolidation (src/belief_consolidation.py, stage 4.1).

Fictional data only.
"""
import datetime
import json
import sqlite3
import sys

import pytest

from src import belief_consolidation as bc
from src import evidence_intake as ei
from src.memory import MemoryManager

T0 = datetime.datetime(2026, 10, 21, 9, 0, tzinfo=datetime.timezone.utc)
T1 = T0 + datetime.timedelta(hours=1)


def ev(source="notes", source_id="n1", text="Prefers window seats on long flights", *,
       about="owner", subject="Me", seen="2026-10-01", **kw):
    return ei.Evidence(source=source, source_id=source_id, subject=subject, about=about,
                       text=text, link=kw.pop("link", f"http://h/{source}/{source_id}"),
                       seen_first=seen, seen_last=seen, **kw)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    for var in ("BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS",
                "BELIEF_STALE_DAYS", "BELIEF_CONSOLIDATE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def memories(path, owner="alice"):
    return [m for m in MemoryManager(str(path)).load_all() if (m.get("owner") or "") == owner]


def beliefs(owner="alice"):
    return ei.list_beliefs(owner, status=["tentative", "confirmed", "stale", "rejected"])


# ------------------------------------------------------------ the matching rule
@pytest.mark.parametrize("a,b", [
    ("owner prefers window seats on long flights", "owner prefers window seats on long flight"),
    ("owner's sister lives in lisbon", "owner's sister lives in lisbon."),
    ("alice is allergic to peanuts", "alice is very allergic to peanut"),
    ("owner still works at the harbour office", "owner works at harbour office"),
])
def test_same_claim_in_nearly_the_same_words(a, b):
    assert bc.same_claim(a, b)


@pytest.mark.parametrize("a,b", [
    ("owner drinks coffee in the morning", "owner no longer drinks coffee in the morning"),
    ("owner does drink coffee every morning", "owner doesn't drink coffee every morning"),
    ("alice's daughter is 7 years old", "alice's daughter is 8 years old"),
    ("owner prefers window seats", "owner prefers aisle seats"),
    ("owner likes jazz", "owner likes jazz and blues"),
    ("robin has a rabbit called bruno", "robin has a rabbit called luna"),
    ("robin has a rabbit called bruno", "robin has a rabbit called bruna"),
    ("nadia is allergic to pollen", "nadia is allergic to latex"),
    ("anouk dislikes airports", "anouk dislikes cats"),
    ("owner works at the harbour office", "owner worked at the harbour office"),
])
def test_different_claims_stay_apart(a, b):
    assert not bc.same_claim(a, b)
    assert not bc.same_rewording(a, b)  # one source's reworded item: still apart


@pytest.mark.parametrize("a,b", [
    ("shahyah owns a carbureted motorcycle with no computer on it",
     "shahyah owns a carbureted motorcycle that has no computer"),
    ("robin has a rabbit called luna", "robin has a rabbit that is called luna"),
])
def test_a_rewording_in_other_glue_words(a, b):
    assert bc.same_rewording(a, b) and not bc.same_claim(a, b)


# ------------------------------------------------------------------ the merge
def test_near_duplicates_from_one_source_merge_and_keep_ids_and_links(data):
    ei.submit("alice", [ev(source_confirmed=True)], now=T0)
    (kept,) = beliefs()
    memory_id = kept["memory_id"]
    assert memory_id
    ei.submit("alice", [ev(source_id="n2", text="Prefers window seats on long flight.")],
              now=T1)
    assert len(beliefs()) == 2

    counts = bc.consolidate("alice", "notes", now=T1)

    assert counts == {"merged": 1, "confirmed": 0}
    (belief,) = beliefs()
    assert belief["id"] == kept["id"] and belief["memory_id"] == memory_id
    assert belief["text"] == "Prefers window seats on long flights"
    assert sorted((e["source_id"], e["link"]) for e in belief["evidence"]) == [
        ("n1", "http://h/notes/n1"), ("n2", "http://h/notes/n2")]
    events = ei.get_belief("alice", belief["id"])["events"]
    assert events[0]["kind"] == "merged" and "long flight." in events[0]["detail"]
    (entry,) = memories(data)
    assert entry["id"] == memory_id and entry["belief_id"] == belief["id"]

    # the source keeps mapping its ids to the kept belief: nothing new on a resubmit
    again = ei.submit("alice", [ev(source_confirmed=True),
                                ev(source_id="n2", text="Prefers window seats on long flight.")],
                      now=T1)
    assert again["unchanged"] == 2 and again["beliefs_added"] == 0
    assert bc.consolidate("alice", "notes", now=T1) == {"merged": 0, "confirmed": 0}


def test_merged_sightings_can_confirm_by_the_rule(data):
    ei.submit("alice", [ev(seen="2026-10-01", times=2),
                        ev(source_id="n2", text="Prefers window seats on long flight",
                           seen="2026-10-03")], now=T0)
    assert {b["status"] for b in beliefs()} == {"tentative"}
    assert memories(data) == []

    assert bc.consolidate("alice", "notes", now=T0) == {"merged": 1, "confirmed": 1}
    (belief,) = beliefs()
    assert (belief["status"], belief["confirmed_by"]) == ("confirmed", "rule")
    assert len(memories(data)) == 1


def test_only_within_one_source(data):
    ei.submit("alice", [ev(source="odysseus-sound", source_id="7"),
                        ev(source_id="n2", text="Prefers window seats on long flight")],
              now=T0)
    assert bc.consolidate("alice", "notes", now=T0)["merged"] == 0
    assert bc.consolidate("alice", "odysseus-sound", now=T0)["merged"] == 0
    assert len(beliefs()) == 2


def test_a_belief_with_evidence_from_two_sources_is_left_alone(data):
    ei.submit("alice", [ev(), ev(source="odysseus-sound", source_id="7"),
                        ev(source_id="n2", text="Prefers window seats on long flight")],
              now=T0)
    assert len(beliefs()) == 2
    assert bc.consolidate("alice", "notes", now=T0)["merged"] == 0


def test_owner_decisions_and_other_subjects_are_never_merged(data):
    ei.submit("alice", [ev(), ev(source_id="n2", text="Prefers window seats on long flight"),
                        ev(source_id="n3", text="Bob prefers window seats on long flights",
                           about="person", subject="Bob"),
                        ev(source_id="n4", text="Bob prefers window seats on long flight",
                           about="person", subject="Bobby")],
              now=T0)
    first = next(b for b in beliefs() if b["text"] == "Prefers window seats on long flights")
    ei.confirm("alice", first["id"], now=T0)
    assert bc.consolidate("alice", "notes", now=T0)["merged"] == 0
    assert len(beliefs()) == 4


def test_structured_beliefs_with_different_values_stay_apart(data):
    ei.submit("alice", [ev(), ev(source_id="n2", text="Prefers window seats on long flight")],
              now=T0)
    a, b = sorted(beliefs(), key=lambda x: x["id"])
    with sqlite3.connect(data / "beliefs.db") as db:
        db.execute("UPDATE beliefs SET attribute = 'likes', value = 'window seats' "
                   "WHERE id = ?", (a["id"],))
        db.execute("UPDATE beliefs SET attribute = 'likes', value = 'long flights' "
                   "WHERE id = ?", (b["id"],))
    assert bc.consolidate("alice", "notes", now=T0)["merged"] == 0


def test_switched_off(data, monkeypatch):
    monkeypatch.setenv("BELIEF_CONSOLIDATE", "off")
    ei.submit("alice", [ev(), ev(source_id="n2", text="Prefers window seats on long flight")],
              now=T0)
    assert bc.consolidate("alice", "notes", now=T0)["merged"] == 0
    assert len(beliefs()) == 2


def test_other_owners_are_untouched(data):
    ei.submit("bob", [ev(), ev(source_id="n2", text="Prefers window seats on long flight")],
              now=T0)
    assert bc.consolidate("alice", "notes", now=T0)["merged"] == 0
    assert len(beliefs("bob")) == 2


def test_an_older_ledger_learns_the_merged_event(data):
    with sqlite3.connect(data / "beliefs.db") as db:
        db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "belief_id INTEGER NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL "
                   "CHECK (kind IN ('added', 'evidence', 'confirmed', 'stale', 'rejected', "
                   "'corrected', 'restored', 'retired_upstream', 'unconfirmed')), "
                   "detail TEXT NOT NULL DEFAULT '')")
    ei.submit("alice", [ev(), ev(source_id="n2", text="Prefers window seats on long flight")],
              now=T0)
    assert bc.consolidate("alice", "notes", now=T0)["merged"] == 1


# --------------------------------------------------------- upstream's tidy
SOURCED = [
    {"id": "b1", "owner": "alice", "text": "Likes jazz (source: http://h/1)",
     "category": "preference", "source": "odysseus-sound", "belief_id": 1},
    {"id": "b2", "owner": "alice", "text": "Likes jazz (source: http://h/2)",
     "category": "preference", "source": "notes", "belief_id": 2},
    {"id": "p0", "owner": "alice", "text": "Likes jazz", "category": "preference",
     "source": "odysseus-sound", "module_fact_id": 9},
]
PLAIN = [
    {"id": "u1", "owner": "alice", "text": "Likes jazz", "category": "preference",
     "source": "user"},
    {"id": "u2", "owner": "alice", "text": "Likes jazz.", "category": "preference",
     "source": "auto"},
]


def test_is_sourced():
    assert [bc.is_sourced(m) for m in SOURCED + PLAIN] == [True, True, True, False, False]
    assert not bc.is_sourced({"source": "notes", "module_fact_id": 3})
    plain, sourced = bc.split_sourced(SOURCED + PLAIN)
    assert [m["id"] for m in plain] == ["u1", "u2"]
    assert [m["id"] for m in sourced] == ["b1", "b2", "p0"]
    assert [m["id"] for m in bc.restore_sourced(PLAIN[:1], SOURCED + PLAIN + SOURCED[:1])] \
        == ["u1", "b1", "b2", "p0"]


def _write(path, entries):
    (path / "memory.json").write_text(json.dumps(entries), encoding="utf-8")


def _read(path):
    return json.loads((path / "memory.json").read_text(encoding="utf-8"))


def _unchanged(saved):
    """Every sourced entry is saved as it was (the loader may add default fields)."""
    return all({k: saved[m["id"]].get(k) for k in m} == m for m in SOURCED)


def _consolidate_action():
    mod = sys.modules.get("src.builtin_actions")
    if mod is not None and not hasattr(mod, "action_consolidate_memory"):
        sys.modules.pop("src.builtin_actions", None)
    from src.builtin_actions import action_consolidate_memory

    return action_consolidate_memory


@pytest.mark.asyncio
async def test_memory_tidy_skips_sourced_entries(data, monkeypatch):
    from src import task_endpoint

    action = _consolidate_action()
    _write(data, SOURCED + PLAIN)
    monkeypatch.setattr(task_endpoint, "resolve_task_candidates", lambda *a, **k: [])

    message, ok = await action("alice")

    assert ok and "Removed 1 duplicate" in message
    saved = {m["id"]: m for m in _read(data)}
    assert set(saved) == {"b1", "b2", "p0", "u1"}
    assert _unchanged(saved)


@pytest.mark.asyncio
async def test_memory_tidy_model_never_sees_sourced_entries(data, monkeypatch):
    from src import llm_core, task_endpoint

    action = _consolidate_action()
    _write(data, SOURCED + PLAIN[:1] + [
        {"id": "u3", "owner": "alice", "text": "Plays chess on Sundays", "category": "fact"}])
    monkeypatch.setattr(task_endpoint, "resolve_task_candidates",
                        lambda *a, **k: [("http://llm", "model", {})])
    prompts = []

    async def fake(_candidates, **kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return json.dumps({"keep": [{"id": "u1", "text": "Likes jazz", "category": "fact"}],
                           "drop": [{"id": "u3", "reason": "trivial"},
                                    {"id": "b1", "reason": "duplicate"}]})

    monkeypatch.setattr(llm_core, "llm_call_async_with_fallback", fake)

    message, ok = await action("alice")

    assert ok and len(prompts) == 1
    assert '"b1"' not in prompts[0] and '"p0"' not in prompts[0]
    saved = {m["id"]: m for m in _read(data)}
    assert set(saved) == {"b1", "b2", "p0", "u1"}
    assert _unchanged(saved)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["alice", None])
async def test_memory_audit_skips_sourced_entries_and_writes_them_back(data, monkeypatch,
                                                                       owner):
    from services.memory import memory_extractor
    from src import llm_core, utility_effort

    bob = {"id": "x1", "owner": "bob", "text": "Bob likes tea", "category": "preference"}
    _write(data, SOURCED + PLAIN + [bob])
    seen = []

    async def fake(_url, _model, messages, **kwargs):
        sent = json.loads(messages[1]["content"])
        seen.append({m["id"] for m in sent})
        return json.dumps([{"id": "u1", "text": "Likes jazz", "category": "preference"}]
                          + ([] if owner else [bob]))

    monkeypatch.setattr(llm_core, "llm_call_async", fake)
    monkeypatch.setattr(utility_effort, "effort_for_call", lambda *a, **k: None)
    manager = MemoryManager(str(data))

    result = await memory_extractor.audit_memories(manager, None, "http://llm", "model",
                                                   owner=owner)

    assert result["before"] == (2 if owner else 3) and result["after"] == (1 if owner else 2)
    assert seen == [{"u1", "u2"} | (set() if owner else {"x1"})]
    saved = {m["id"]: m for m in _read(data)}
    assert set(saved) == {"b1", "b2", "p0", "u1", "x1"}
    assert _unchanged(saved)
