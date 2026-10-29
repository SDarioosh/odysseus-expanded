"""Fork: Athena's JSON tool results, as the hub's parsers read them.

The cross-repo check of the module contract (docs/fork/modules.md): the files in
``tests/fixtures/athena_contract/`` are Athena's contract snapshots (fictional seed data)
of ``list_facts``, ``list_action_items`` and ``list_people`` with ``format="json"``; see
the README there. Every row must get through the hub's parsers (src/module_facts.py,
src/module_action_items.py, src/sound_module_view.py, src/insight_inputs.py) with the
fields the hub uses. When Athena's JSON output changes, both repos' copies are refreshed
in the same PR pair.

The snapshots are stored with sorted keys and indent 2; the tool answers compact JSON.
Each parser test runs on both forms, so a parser that depended on the layout would fail.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from src import evidence_intake as ledger
from src import insight_inputs
from src import module_action_items as mai
from src import module_facts as mf
from src import sound_module_view as view
from src.memory import MemoryManager

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "athena_contract"
NOW = datetime.datetime(2026, 10, 20, 9, 0, tzinfo=datetime.UTC)

FACT_FILES = ("list_facts.json", "list_facts_confirmed.json")
ITEM_FILES = ("list_action_items.json", "list_action_items_status_all.json")
PEOPLE_FILES = ("list_people.json", "list_people_after_merge.json")

# Fields the hub reads from each row (Athena's server/README.md documents them).
FACT_FIELDS = {"id", "subject", "about", "person_id", "fact", "kind", "conversation_id",
               "quote", "times_said", "days", "first_said", "last_said", "confirmed",
               "valid_until", "source", "written_by_hand", "note_link", "link"}
ITEM_FIELDS = {"id", "status", "kind", "mine", "title", "explicit", "text", "owner", "due",
               "due_date", "due_time", "duration_minutes", "location", "list_name",
               "note_title", "time_unclear", "conversation_id", "conversation_title",
               "conversation_kind", "conversation_at", "created_at", "timezone", "link",
               "with", "key_points", "source", "written_by_hand", "note_link"}
PEOPLE_FIELDS = {"id", "name", "aliases", "is_owner", "has_voice", "conversations",
                 "first_seen", "last_seen"}


def _stored(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _compact(name: str) -> str:
    return json.dumps(json.loads(_stored(name)), separators=(",", ":"), ensure_ascii=False)


def _rows(name: str) -> list[dict]:
    data = json.loads(_stored(name))
    if isinstance(data, list):
        return data
    return data.get("facts", data.get("people"))


FORMS = pytest.mark.parametrize("form", [_stored, _compact], ids=["stored", "compact"])


@pytest.mark.parametrize("name,fields", [
    *((n, FACT_FIELDS) for n in FACT_FILES),
    *((n, ITEM_FIELDS) for n in ITEM_FILES),
    *((n, PEOPLE_FIELDS) for n in PEOPLE_FILES),
])
def test_rows_carry_exactly_the_contract_fields(name: str, fields: set[str]) -> None:
    rows = _rows(name)
    assert rows
    for row in rows:
        assert set(row) == fields, (row.get("id"), sorted(fields - set(row)),
                                    sorted(set(row) - fields))


def test_fixtures_cover_the_cases_the_hub_handles() -> None:
    facts = _rows("list_facts.json")
    assert {f["about"] for f in facts} == {"owner", "person", "other"}
    assert any(f["valid_until"] for f in facts) and any(f["source"] == "meeting" for f in facts)
    assert all(f["confirmed"] for f in _rows("list_facts_confirmed.json"))
    items = _rows("list_action_items.json")
    assert {"task", "reminder", "event", "note"} <= {i["kind"] for i in items}
    assert any(i["key_points"] for i in items) and any(i["mine"] is False for i in items)
    assert any(i["written_by_hand"] for i in items) and any(i["with"] for i in items)
    assert {"open", "done", "dismissed"} <= {
        i["status"] for i in _rows("list_action_items_status_all.json")}
    assert any(p["aliases"] for p in _rows("list_people_after_merge.json"))


# ------------------------------------------------------------------- list_facts
@FORMS
@pytest.mark.parametrize("name", FACT_FILES)
def test_facts_parse_into_ledger_evidence(form, name: str) -> None:
    raws = _rows(name)
    rows = mf.parse_facts_json(form(name))
    assert rows is not None
    assert [r["id"] for r in rows] == [int(r["id"]) for r in raws]
    for raw, row in zip(raws, rows):
        ev = mf.evidence(row)
        assert ev.source == ledger.MODULE_SOURCE
        assert ev.source_id == str(raw["id"])
        assert ev.text == raw["fact"].strip()
        assert ev.link == raw["link"]
        assert ev.source_confirmed is bool(raw["confirmed"])
        assert ev.person_id == (None if raw["person_id"] is None else str(raw["person_id"]))
        assert ev.valid_until == raw["valid_until"]
        assert ev.category in {"fact", "preference", "contact"}
        assert mf.fact_kind(row) == raw["kind"]
        payload = mf.payload(row)
        assert payload.get("kind") == raw["kind"]
        assert payload.get("conversation_id") == raw["conversation_id"]
        assert ("quote" in payload) == bool(raw["quote"])


def test_confirmed_facts_are_a_subset_of_all_facts() -> None:
    every = {f["id"]: f for f in _rows("list_facts.json")}
    for fact in _rows("list_facts_confirmed.json"):
        assert every[fact["id"]] == fact


@FORMS
@pytest.mark.parametrize("name", PEOPLE_FILES)
def test_people_parse(form, name: str) -> None:
    raws = _rows(name)
    people = mf.parse_people_json(form(name))
    assert people == raws
    assert sum(bool(p["is_owner"]) for p in people) == 1
    for person in people:
        assert isinstance(person["aliases"], list) and person["name"]


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mf, "_LOCKS", {})
    for var in ("MODULE_FACTS_SYNC", "BARVIS_FACTS_SYNC", "MODULE_ACTION_ITEMS_SERVER",
                "BARVIS_ACTION_ITEMS_SERVER", "BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES",
                "BELIEF_CONFIRM_DAYS", "BELIEF_STALE_DAYS"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


async def test_facts_sync_takes_every_row_and_person(data) -> None:
    calls = []

    async def call(tool: str, args: dict) -> str:
        calls.append((tool, args))
        if tool == "list_people":
            return _compact("list_people.json")
        assert tool == "list_facts"
        return _compact("list_facts_confirmed.json" if args.get("confirmed")
                        else "list_facts.json")

    stats = await mf.sync("alice", call, now=NOW)
    facts = _rows("list_facts.json")
    assert stats is not None
    assert stats["facts"] == len(facts)
    assert stats["people"] == len(_rows("list_people.json"))
    # every row has ``confirmed``: no second list_facts call
    assert [t for t, _ in calls].count("list_facts") == 1
    copies = {m["module_fact_id"] for m in MemoryManager(str(data)).load_all()
              if "module_fact_id" in m}
    assert copies == {f["id"] for f in facts if f["confirmed"]}


# ------------------------------------------------------------ list_action_items
@FORMS
@pytest.mark.parametrize("name", ITEM_FILES)
def test_action_items_parse_for_filing(form, name: str) -> None:
    raws = _rows(name)
    rows = mai.parse_items_json(form(name))
    assert rows is not None
    assert [r["id"] for r in rows] == [int(r["id"]) for r in raws]
    for raw, row in zip(raws, rows):
        assert mai.is_explicit(row) is bool(raw["explicit"] or raw["kind"] == "note")
        title = mai.item_title(row)
        assert title and len(title) <= mai.TITLE_MAX
        if raw["title"] and len(raw["title"]) <= mai.TITLE_MAX:
            assert title == raw["title"]
        target = mai.default_target(row)
        assert target in {"note", "calendar", "todo"}
        if raw["kind"] == "note":
            assert target == "note"
            content = mai._note_content(row, row["text"])
            for point in raw["key_points"]:
                assert f"- {point}" in content
            assert raw["link"] in content
        item = mai._row_item(row)
        assert (item.id, item.status, item.text, item.link) == \
            (raw["id"], raw["status"], raw["text"], raw["link"])


@FORMS
@pytest.mark.parametrize("name", ITEM_FILES)
def test_action_items_parse_for_the_page(form, name: str) -> None:
    raws = _rows(name)
    items = view.parse_action_items_result(form(name))
    mine = [r for r in raws if r["mine"] is not False]
    assert [i.id for i in items] == [r["id"] for r in mine]
    for raw, item in zip(mine, items):
        assert item.status == raw["status"]
        assert item.kind == raw["kind"]
        assert item.due_date == raw["due_date"] and item.due_time == raw["due_time"]
        assert item.voice_note is (raw["conversation_kind"] == "note")
        assert item.list_name == raw["list_name"] and item.note_title == raw["note_title"]
        assert item.link == raw["link"]
    for raw in raws:
        assert view.item_from_json(raw) is not None


async def test_action_items_page_reads_json_and_finds_items() -> None:
    async def call(tool: str, args: dict) -> str:
        assert tool == "list_action_items" and args.get("format") == "json"
        return _compact("list_action_items_status_all.json")

    raws = _rows("list_action_items_status_all.json")
    rows = await view.action_items(call, status="all")
    assert {r["id"] for r in rows} == {r["id"] for r in raws if r["mine"] is not False}
    for raw in raws:
        assert (await view.find_item(call, raw["id"]))["id"] == raw["id"]


@pytest.mark.parametrize("name", ITEM_FILES)
def test_action_items_people_and_ownership(name: str) -> None:
    for raw in _rows(name):
        assert insight_inputs.is_mine(raw) is bool(raw["mine"])
        assert insight_inputs.people(raw) == [p["name"] for p in raw["with"]]
