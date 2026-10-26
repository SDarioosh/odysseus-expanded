"""Fork: odysseus-sound's facts -> belief ledger -> Odysseus memory (src/module_facts.py).

Fictional data only.
"""
import datetime
import json

import pytest

from src import evidence_intake as ledger
from src import module_facts as mf
from src.memory import MemoryManager

T0 = datetime.datetime(2026, 10, 21, 9, 0, tzinfo=datetime.timezone.utc)
T1 = T0 + datetime.timedelta(hours=1)
ALL = {"format": "json", "limit": 1000}
CONFIRMED = {"confirmed": True, "format": "json", "limit": 1000}
PEOPLE = {"format": "json"}


def fact(fid, text, about="owner", subject="Me", confirmed=True, **kw):
    return {"id": fid, "subject": subject, "about": about, "fact": text,
            "times_said": kw.get("times_said", 2), "days": kw.get("days", 2),
            "first_said": kw.get("first_said", "2026-10-01"),
            "last_said": kw.get("last_said", "2026-10-15"),
            "link": f"http://h:8000/view/c{fid}?sig=abc#t={fid * 10}",
            "_confirmed": confirmed}


class MissingTool(Exception):
    """What an MCP server answers for a tool it doesn't have."""


class FakeModule:
    """The module's list_facts (and retire_fact, list_people), as the MCP call returns
    them.

    ``flag``: the module sends the per-row ``confirmed`` field; without it, the hub asks
    a second time with ``confirmed=true``. ``retire``: whether retire_fact exists.
    ``people``: list_people's JSON rows, a text answer (str), or None (an older module
    without the tool)."""

    def __init__(self, facts=None, text=None, flag=False, retire=True, people=None):
        self.facts = list(facts or [])
        self.text = text
        self.flag = flag
        self.retire = retire
        self.people = people
        self.calls = []

    def _row(self, row):
        out = {k: v for k, v in row.items() if k != "_confirmed"}
        if self.flag:
            out["confirmed"] = row.get("_confirmed", True)
        return out

    async def __call__(self, tool, args):
        self.calls.append((tool, args))
        if tool == "retire_fact":
            if not self.retire:
                raise RuntimeError("retire_fact: Unknown tool: retire_fact")
            self.facts = [f for f in self.facts if f["id"] != args["fact_id"]]
            return f"Fact {args['fact_id']} retired."
        if tool == "list_people":
            assert args == PEOPLE
            if self.people is None:
                raise RuntimeError("list_people: Unknown tool: list_people")
            if isinstance(self.people, str):
                return self.people
            return json.dumps({"people": self.people})
        assert tool == "list_facts", f"the sync must not call {tool}"
        if self.text is not None:
            return self.text
        if args == ALL:
            rows = self.facts
        elif args == CONFIRMED:
            rows = [f for f in self.facts if f.get("_confirmed", True)]
        else:
            raise AssertionError(f"unexpected list_facts arguments {args}")
        return json.dumps({"facts": [self._row(r) for r in rows]}, separators=(",", ":"))


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mf, "_LOCKS", {})
    for var in ("MODULE_FACTS_SYNC", "BARVIS_FACTS_SYNC", "MODULE_ACTION_ITEMS_SERVER",
                "BARVIS_ACTION_ITEMS_SERVER", "BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES",
                "BELIEF_CONFIRM_DAYS", "BELIEF_STALE_DAYS"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def memories(path, owner="alice"):
    return [m for m in MemoryManager(str(path)).load_all() if (m.get("owner") or "") == owner]


def copies(path, owner="alice"):
    return {m["module_fact_id"]: m for m in memories(path, owner) if "module_fact_id" in m}


BASE = [
    fact(1, "Prefers oat milk in coffee"),
    fact(2, "Lives in Lisbon with her dog Pixel", about="person", subject="Kath"),
    fact(3, "The office moves to Building C in November", about="other", subject="Office"),
]


def counts(stats, *keys):
    return {k: stats[k] for k in keys}


@pytest.mark.asyncio
async def test_first_run_adds_one_memory_per_confirmed_fact(data):
    stats = await mf.sync("alice", FakeModule(BASE), now=T0)
    assert counts(stats, "facts", "beliefs_added", "evidence_added", "confirmed",
                  "memory_added") == {"facts": 3, "beliefs_added": 3, "evidence_added": 3,
                                      "confirmed": 3, "memory_added": 3}
    got = copies(data)
    assert set(got) == {1, 2, 3}
    one = got[1]
    assert one["text"] == ("Prefers oat milk in coffee "
                           "(source: http://h:8000/view/c1?sig=abc#t=10)")
    assert one["source"] == "odysseus-sound" and one["owner"] == "alice"
    assert one["link"] == "http://h:8000/view/c1?sig=abc#t=10"
    assert (one["subject"], one["about"], one["times_said"], one["days"]) == ("Me", "owner", 2, 2)
    assert (one["first_said"], one["last_said"]) == ("2026-10-01", "2026-10-15")
    assert (one["first_seen"], one["last_seen"]) == ("2026-10-01", "2026-10-15")
    assert one["synced_at"] == "2026-10-21T09:00:00+00:00"
    assert one["uses"] == 0 and one["id"]
    assert [got[i]["category"] for i in (1, 2, 3)] == ["preference", "contact", "fact"]
    belief = ledger.get_belief("alice", one["belief_id"])
    assert (belief["status"], belief["confirmed_by"], belief["memory_id"]) == \
        ("confirmed", "source", one["id"])
    state = json.loads((data / "module_facts_alice.json").read_text())
    assert state["memory_added"] == 3 and state["last_run"] == "2026-10-21T09:00:00+00:00"


@pytest.mark.asyncio
async def test_unconfirmed_facts_are_tentative_not_in_memory(data):
    module = FakeModule([fact(1, "Prefers oat milk in coffee"),
                         fact(4, "Plays the cello", confirmed=False, times_said=1, days=1)])
    stats = await mf.sync("alice", module, now=T0)
    assert (stats["beliefs_added"], stats["confirmed"]) == (2, 1)
    assert set(copies(data)) == {1}
    assert ledger.stats("alice") == {"tentative": 1, "confirmed": 1, "stale": 0,
                                     "rejected": 0, "total": 2, "structured": 0}
    # the module confirms it later
    module.facts[1]["_confirmed"] = True
    stats = await mf.sync("alice", module, now=T1)
    assert stats["confirmed"] == 1 and set(copies(data)) == {1, 4}


@pytest.mark.asyncio
async def test_per_row_flag_saves_the_second_call(data):
    flagged = FakeModule([fact(1, "Likes jazz"), fact(2, "Hates queues", confirmed=False)],
                         flag=True)
    await mf.sync("alice", flagged, now=T0)
    assert flagged.calls == [("list_facts", ALL), ("list_people", PEOPLE)]
    assert set(copies(data)) == {1}
    unflagged = FakeModule([fact(1, "Likes jazz"), fact(2, "Hates queues", confirmed=False)])
    await mf.sync("bob", unflagged, now=T0)
    assert unflagged.calls == [("list_facts", ALL), ("list_facts", CONFIRMED),
                               ("list_people", PEOPLE)]
    assert set(copies(data, "bob")) == {1}
    # no facts at all: no second call
    empty = FakeModule([])
    await mf.sync("carol", empty, now=T0)
    assert empty.calls == [("list_facts", ALL), ("list_people", PEOPLE)]


@pytest.mark.asyncio
async def test_rerun_is_idempotent(data):
    module = FakeModule(BASE)
    await mf.sync("alice", module, now=T0)
    before = (data / "memory.json").read_text()
    stats = await mf.sync("alice", module, now=T1)
    assert counts(stats, "unchanged", "memory_kept") == {"unchanged": 3, "memory_kept": 3}
    assert not mf.changed(stats)
    assert (data / "memory.json").read_text() == before  # not even synced_at moves


@pytest.mark.asyncio
async def test_changed_fact_is_updated_in_place(data):
    module = FakeModule(BASE)
    await mf.sync("alice", module, now=T0)
    old = copies(data)[1]
    # the memory was used in chat meanwhile
    MemoryManager(str(data)).increment_uses([old["id"]])
    module.facts[0] = fact(1, "Prefers soy milk in coffee", times_said=3, days=3,
                           last_said="2026-10-20")
    stats = await mf.sync("alice", module, now=T1)
    assert counts(stats, "evidence_updated", "memory_updated", "memory_kept") == \
        {"evidence_updated": 1, "memory_updated": 1, "memory_kept": 2}
    new = copies(data)[1]
    assert new["id"] == old["id"] and new["uses"] == 1
    assert new["belief_id"] == old["belief_id"]
    assert new["text"] == "Prefers soy milk in coffee (source: http://h:8000/view/c1?sig=abc#t=10)"
    assert (new["times_said"], new["last_said"]) == (3, "2026-10-20")
    assert new["synced_at"] == "2026-10-21T10:00:00+00:00"
    # metadata alone also counts as a change
    module.facts[1] = {**module.facts[1], "link": "http://h:8000/view/c9?sig=def#t=5"}
    assert (await mf.sync("alice", module, now=T1))["memory_updated"] == 1
    # a renamed speaker moves the subject along
    module.facts[1] = {**module.facts[1], "subject": "Katherine"}
    await mf.sync("alice", module, now=T1)
    assert copies(data)[2]["subject"] == "Katherine"


@pytest.mark.asyncio
async def test_rewritten_copy_is_restored(data):
    module = FakeModule(BASE)
    await mf.sync("alice", module, now=T0)
    mm = MemoryManager(str(data))
    entries = mm.load_all_for_update()
    old = next(m for m in entries if m.get("module_fact_id") == 2)
    old["text"] = "Lives in Lisbon with her dog Pixel"
    mm.save(entries)
    stats = await mf.sync("alice", module, now=T1)
    assert counts(stats, "memory_kept", "memory_updated") == {"memory_kept": 2,
                                                              "memory_updated": 1}
    got = copies(data)[2]
    assert got["id"] == old["id"]
    assert got["text"] == ("Lives in Lisbon with her dog Pixel "
                           "(source: http://h:8000/view/c2?sig=abc#t=20)")


@pytest.mark.asyncio
async def test_raised_thresholds_are_counted_as_unconfirmed(data, monkeypatch):
    monkeypatch.setenv("BELIEF_CONFIRM_TIMES", "1")
    monkeypatch.setenv("BELIEF_CONFIRM_DAYS", "1")
    module = FakeModule([fact(1, "Likes jazz", confirmed=False, times_said=1, days=1),
                         fact(2, "Hates queues", confirmed=False, times_said=1, days=1)],
                        flag=True)
    assert (await mf.sync("alice", module, now=T0))["confirmed"] == 2
    monkeypatch.setenv("BELIEF_CONFIRM_TIMES", "3")
    monkeypatch.setenv("BELIEF_CONFIRM_DAYS", "2")
    stats = await mf.sync("alice", module, now=T1)
    assert counts(stats, "unconfirmed", "confirmed", "memory_removed") == \
        {"unconfirmed": 2, "confirmed": 0, "memory_removed": 2}
    assert mf.changed(stats)
    assert ", 2 unconfirmed, " in mf.summary(stats)
    assert copies(data) == {}


def test_category_ignores_words_in_the_link():
    row = {**fact(5, "Works night shifts"), "link": "http://h:8000/view/likes-love?sig=want#t=1"}
    assert mf.category(row) == "fact"
    assert mf.evidence(row).category == "fact"


@pytest.mark.asyncio
async def test_fact_no_longer_returned_is_retired(data):
    module = FakeModule(BASE)
    await mf.sync("alice", module, now=T0)
    module.facts = module.facts[1:]
    stats = await mf.sync("alice", module, now=T1)
    assert counts(stats, "gone", "stale", "memory_removed", "memory_kept") == \
        {"gone": 1, "stale": 1, "memory_removed": 1, "memory_kept": 2}
    assert set(copies(data)) == {2, 3}
    assert [b["text"] for b in ledger.list_beliefs("alice", status="stale")] == \
        ["Prefers oat milk in coffee"]


@pytest.mark.asyncio
async def test_own_memories_are_untouched_and_deleted_copy_comes_back(data):
    mm = MemoryManager(str(data))
    mine = mm.add_entry("I like hiking", source="user", category="preference", owner="alice")
    mm.save([mine])
    module = FakeModule(BASE)
    await mf.sync("alice", module, now=T0)
    # Memory Tidy (or the owner) deletes a copy and rewrites another
    entries = mm.load_all_for_update()
    entries = [m for m in entries if m.get("module_fact_id") != 2]
    for m in entries:
        if m.get("module_fact_id") == 3:
            m["text"], m["category"] = "Office moving", "project"
    mm.save(entries)
    stats = await mf.sync("alice", module, now=T1)
    assert counts(stats, "memory_kept", "memory_added", "memory_updated") == \
        {"memory_kept": 1, "memory_added": 1, "memory_updated": 1}
    got = copies(data)
    assert got[3]["text"] == ("The office moves to Building C in November "
                              "(source: http://h:8000/view/c3?sig=abc#t=30)")
    assert got[3]["category"] == "fact"
    assert any(m["id"] == mine["id"] and m["text"] == "I like hiking"
               for m in memories(data))


@pytest.mark.asyncio
async def test_phase0_copies_are_adopted_not_duplicated(data):
    """Copies written before the ledger (module_fact_id, no belief_id) keep id and uses."""
    mm = MemoryManager(str(data))
    old = []
    for row in BASE[:2]:
        entry = mm.add_entry(f"{row['fact']} (source: {row['link']})", source="odysseus-sound",
                             category=mf.category(row), owner="alice")
        entry.update(module_fact_id=row["id"], link=row["link"], uses=4)
        old.append(entry)
    gone = mm.add_entry("Retracted long ago", source="odysseus-sound", owner="alice")
    gone.update(module_fact_id=99)
    mm.save(old + [gone])
    # the ledger hasn't heard from the module yet: nothing is touched
    assert ledger.sync_memory("alice", now=T0)["removed"] == 0
    assert len(memories(data)) == 3
    stats = await mf.sync("alice", FakeModule(BASE), now=T0)
    assert counts(stats, "memory_added", "memory_updated", "memory_removed") == \
        {"memory_added": 1, "memory_updated": 2, "memory_removed": 1}
    got = copies(data)
    assert set(got) == {1, 2, 3} and len(memories(data)) == 3
    for entry in old:
        new = got[entry["module_fact_id"]]
        assert (new["id"], new["uses"]) == (entry["id"], 4)
        assert ledger.get_belief("alice", new["belief_id"])["memory_id"] == entry["id"]
    before = (data / "memory.json").read_text()
    await mf.sync("alice", FakeModule(BASE), now=T1)
    assert (data / "memory.json").read_text() == before


@pytest.mark.asyncio
async def test_other_owners_copies_are_untouched(data):
    await mf.sync("bob", FakeModule([fact(1, "Prefers tea"), fact(7, "Enjoys chess")]), now=T0)
    bob_before = copies(data, "bob")
    await mf.sync("alice", FakeModule(BASE), now=T0)
    await mf.sync("alice", FakeModule([]), now=T1)  # alice's facts all retracted
    assert copies(data) == {}
    assert copies(data, "bob") == bob_before
    assert (data / "module_facts_bob.json").exists()


@pytest.mark.asyncio
async def test_auth_off_owner_none_and_empty_are_the_same(data):
    await mf.sync(None, FakeModule(BASE[:1]), now=T0)
    (entry,) = memories(data, "")
    assert "owner" not in entry
    assert (await mf.sync("", FakeModule(BASE[:1]), now=T1))["memory_kept"] == 1


@pytest.mark.asyncio
async def test_text_answer_does_nothing_and_logs_once(data, caplog):
    module = FakeModule(text="3 facts. When answering, put the link next to each fact.")
    with caplog.at_level("INFO", logger="src.module_facts"):
        assert await mf.sync("alice", module, now=T0) is None
        assert await mf.sync("alice", module, now=T1) is None
    assert sum("answered list_facts with text" in r.getMessage() for r in caplog.records) == 1
    assert memories(data) == []
    assert mf.parse_facts_json("[1, 2]") is None and mf.parse_facts_json("12") is None


def test_parse_skips_bad_rows_and_repeated_ids():
    rows = mf.parse_facts_json(json.dumps({"facts": [
        fact(1, "Likes jazz"), {"id": "x", "fact": "no id"}, {"id": 2, "fact": "  "},
        fact(1, "Duplicate"), "junk", {**fact(4, "Hates queues"), "id": "4"}]}))
    assert [(r["id"], r["fact"]) for r in rows] == [(1, "Likes jazz"), (4, "Hates queues")]


def test_evidence_from_a_row():
    row = {**fact(4, "Hates queues", times_said=0), "confirmed": "false"}
    ev = mf.evidence(row, confirmed_ids={4})
    assert (ev.source, ev.source_id, ev.text, ev.times, ev.days) == \
        ("odysseus-sound", "4", "Hates queues", 1, 2)
    assert ev.source_confirmed is False  # the row's own flag wins over the fallback set
    del row["confirmed"]
    assert mf.evidence(row, confirmed_ids={4}).source_confirmed is True
    assert mf.evidence(row).source_confirmed is False


@pytest.mark.parametrize("about,text,want", [
    ("owner", "Likes spicy food", "preference"),
    ("owner", "Favourite band is The Lumens", "preference"),
    ("owner", "Doesn't want meetings before ten", "preference"),
    ("owner", "Works night shifts on Tuesdays", "fact"),
    ("owner", "Likely to move next spring", "fact"),  # whole words only
    ("person", "Loves sailing", "contact"),
    ("other", "The gym prefers cash", "fact"),
    ("", "Something", "fact"),
])
def test_category_mapping(about, text, want):
    assert mf.category({"about": about, "fact": text}) == want


def test_evidence_carries_the_module_person_id():
    row = fact(1, "Priya is allergic to peanuts", about="person", subject="Priya")
    assert mf.evidence(row).person_id is None
    assert mf.evidence({**row, "person_id": 12}).person_id == "12"
    assert mf.evidence({**row, "person_id": " "}).person_id is None


def test_summary_has_counts_only():
    stats = {"facts": 12, "beliefs_added": 2, "confirmed": 1, "unconfirmed": 3, "gone": 0,
             "stale": 1, "memory_kept": 10, "memory_added": 1, "memory_updated": 0,
             "memory_removed": 1}
    assert mf.summary(stats) == ("facts: 12 seen, 2 new, 1 confirmed, 3 unconfirmed, 0 gone, "
                                 "1 stale; "
                                 "memory: 10 kept, 1 added, 0 updated, 1 removed")


# ------------------------------------------------------------------ forgetting
@pytest.mark.asyncio
async def test_forget_retires_the_module_fact_and_it_stays_forgotten(data):
    module = FakeModule(BASE)
    await mf.sync("alice", module, now=T0)
    belief_id = copies(data)[1]["belief_id"]
    evidence = ledger.reject("alice", belief_id, "wrong", now=T1)
    assert 1 not in copies(data)
    result = await mf.propagate_forget("alice", evidence, call=module)
    assert result == {"retired": 1, "failed": 0, "unsupported": 0}
    assert ("retire_fact", {"fact_id": 1, "reason": "owner forgot it in Odysseus"}) in module.calls
    stats = await mf.sync("alice", module, now=T1)
    assert stats["gone"] == 1
    belief = ledger.get_belief("alice", belief_id)
    assert belief["status"] == "rejected" and belief["evidence"][0]["gone"]
    assert 1 not in copies(data)


@pytest.mark.asyncio
async def test_forget_tolerates_a_module_without_retire_fact(data, caplog):
    module = FakeModule(BASE, retire=False)
    await mf.sync("alice", module, now=T0)
    belief = ledger.get_belief("alice", copies(data)[1]["belief_id"])
    ledger.reject("alice", belief["id"], now=T1)
    with caplog.at_level("INFO", logger="src.module_facts"):
        for _ in range(2):
            assert await mf.propagate_forget("alice", belief, call=module) == \
                {"retired": 0, "failed": 0, "unsupported": 1}
    assert sum("no retire_fact tool" in r.getMessage() for r in caplog.records) == 1
    # the module keeps returning it; the ledger keeps it rejected and out of memory
    await mf.sync("alice", module, now=T1)
    assert ledger.get_belief("alice", belief["id"])["status"] == "rejected"
    assert 1 not in copies(data)
    # nothing to retire: no call
    assert await mf.propagate_forget("alice", {"evidence": []}, call=module) == \
        {"retired": 0, "failed": 0, "unsupported": 0}


@pytest.mark.asyncio
async def test_forget_counts_other_failures(data):
    async def broken(tool, args):
        raise RuntimeError("retire_fact: database is locked")

    evidence = [{"source": "odysseus-sound", "source_id": "5"},
                {"source": "notes", "source_id": "n1"}]
    assert await mf.propagate_forget("alice", evidence, call=broken) == \
        {"retired": 0, "failed": 1, "unsupported": 0}


# ------------------------------------------------------------------ the task entry point
class FakeManager:
    def __init__(self, module):
        self.module = module
        self._connections = {"srv1": {"status": "connected", "name": "convo"}}

    async def call_tool(self, name, arguments):
        assert name.startswith("mcp__srv1__")
        try:
            return {"stdout": await self.module(name.removeprefix("mcp__srv1__"), arguments)}
        except RuntimeError as e:  # the MCP server's error answer
            return {"stdout": "", "stderr": str(e).split(": ", 1)[1], "exit_code": 1}


@pytest.fixture
def manager(monkeypatch):
    from src import tool_utils

    holder = FakeManager(FakeModule(BASE))
    monkeypatch.setattr(tool_utils, "get_mcp_manager", lambda: holder)
    return holder


@pytest.mark.asyncio
async def test_action_reports_counts_then_noops(data, manager):
    from src.builtin_actions import TaskNoop

    result, ok = await mf.action_sync_facts("alice")
    assert ok is True
    assert result == ("facts: 3 seen, 3 new, 3 confirmed, 0 unconfirmed, 0 gone, 0 stale; "
                      "memory: 0 kept, 3 added, 0 updated, 0 removed")
    assert "oat milk" not in result
    with pytest.raises(TaskNoop, match="3 kept"):
        await mf.action_sync_facts("alice")
    manager.module.text = "No facts."
    with pytest.raises(TaskNoop, match="text"):
        await mf.action_sync_facts("alice")


@pytest.mark.asyncio
async def test_propagate_forget_through_the_module_server(data, manager):
    await mf.action_sync_facts("alice")
    belief = ledger.get_belief("alice", copies(data)[2]["belief_id"])
    assert await mf.propagate_forget("alice", belief) == \
        {"retired": 1, "failed": 0, "unsupported": 0}
    manager.module.retire = False
    assert (await mf.propagate_forget("alice", belief))["unsupported"] == 1
    manager._connections = {}
    assert (await mf.propagate_forget("alice", belief))["unsupported"] == 1


@pytest.mark.asyncio
async def test_action_off_does_nothing(data, manager, monkeypatch):
    from src.builtin_actions import TaskNoop

    monkeypatch.setenv("MODULE_FACTS_SYNC", "off")
    with pytest.raises(TaskNoop, match="off"):
        await mf.action_sync_facts("alice")
    assert manager.module.calls == [] and memories(data) == []
    assert not (data / "module_facts_alice.json").exists()


@pytest.mark.asyncio
async def test_action_without_module_server_noops(data, manager):
    from src.builtin_actions import TaskNoop

    manager._connections = {}
    with pytest.raises(TaskNoop, match="not connected"):
        await mf.action_sync_facts("alice")


@pytest.mark.asyncio
async def test_action_unreadable_memory_store_fails_without_saving(data, manager):
    (data / "memory.json").write_text("{not json", encoding="utf-8")
    result, ok = await mf.action_sync_facts("alice")
    assert ok is False and result.startswith("Facts sync failed")
    assert (data / "memory.json").read_text(encoding="utf-8") == "{not json"


def test_registered_as_hourly_silent_builtin_task():
    from src.builtin_actions import BUILTIN_ACTION_INFO, BUILTIN_ACTIONS
    from src.task_scheduler import HOUSEKEEPING_DEFAULTS, TaskScheduler

    assert BUILTIN_ACTIONS[mf.ACTION] is mf.action_sync_facts
    assert mf.ACTION in BUILTIN_ACTION_INFO
    defaults = HOUSEKEEPING_DEFAULTS[mf.ACTION]
    assert defaults["name"] == "Module Facts"
    assert (defaults["schedule"], defaults["cron_expression"]) == ("cron", "0 * * * *")
    assert mf.ACTION in TaskScheduler._SILENT_ACTIONS
