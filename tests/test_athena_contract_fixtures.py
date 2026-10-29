"""Fork: Athena's JSON tool results, as the hub's parsers read them.

The cross-repo check of the module contract (docs/fork/modules.md): the files in
``tests/fixtures/athena_contract/`` are Athena's answers to ``list_facts``,
``list_action_items`` and ``list_people`` with ``format="json"``. Every row must get
through the hub's parsers (src/module_facts.py, src/module_action_items.py,
src/sound_module_view.py, src/insight_inputs.py) with the fields the hub uses. When
Athena's JSON output changes, refresh these fixtures in the same PR pair.

PLACEHOLDER: the fixtures are hand-made, fictional payloads shaped like the contract in
Athena's ``server/README.md`` ("Action items in JSON", "Confirmed facts", "People").
Replace them with Athena's contract snapshots (fictional seed data) when those exist.
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

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "athena_contract"
NOW = datetime.datetime(2025, 10, 13, 9, 0, tzinfo=datetime.UTC)


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


FACTS_TEXT = _text("list_facts.json")
ITEMS_TEXT = _text("list_action_items.json")
PEOPLE_TEXT = _text("list_people.json")
FACT_ROWS = json.loads(FACTS_TEXT)["facts"]
ITEM_ROWS = json.loads(ITEMS_TEXT)
PEOPLE_ROWS = json.loads(PEOPLE_TEXT)["people"]

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


def test_fixtures_are_not_empty_and_carry_the_contract_fields() -> None:
    assert FACT_ROWS and ITEM_ROWS and PEOPLE_ROWS
    for rows, fields in ((FACT_ROWS, FACT_FIELDS), (ITEM_ROWS, ITEM_FIELDS),
                         (PEOPLE_ROWS, PEOPLE_FIELDS)):
        for row in rows:
            assert fields <= set(row), (row.get("id"), sorted(fields - set(row)))


# ------------------------------------------------------------------- list_facts
def test_facts_parse_into_ledger_evidence() -> None:
    rows = mf.parse_facts_json(FACTS_TEXT)
    assert rows is not None
    assert [r["id"] for r in rows] == [int(r["id"]) for r in FACT_ROWS]
    for raw, row in zip(FACT_ROWS, rows):
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


def test_people_parse() -> None:
    people = mf.parse_people_json(PEOPLE_TEXT)
    assert people is not None
    assert [p["id"] for p in people] == [p["id"] for p in PEOPLE_ROWS]
    assert sum(bool(p["is_owner"]) for p in people) <= 1
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
        calls.append(tool)
        return {"list_facts": FACTS_TEXT, "list_people": PEOPLE_TEXT}[tool]

    stats = await mf.sync("alice", call, now=NOW)
    assert stats is not None
    assert stats["facts"] == len(FACT_ROWS)
    assert stats["people"] == len(PEOPLE_ROWS)
    # every row has ``confirmed``: no second list_facts call
    assert calls.count("list_facts") == 1
    copies = {m["module_fact_id"] for m in MemoryManager(str(data)).load_all()
              if "module_fact_id" in m}
    assert copies == {r["id"] for r in FACT_ROWS if r["confirmed"]}


# ------------------------------------------------------------ list_action_items
def test_action_items_parse_for_filing() -> None:
    rows = mai.parse_items_json(ITEMS_TEXT)
    assert rows is not None
    assert [r["id"] for r in rows] == [int(r["id"]) for r in ITEM_ROWS]
    for raw, row in zip(ITEM_ROWS, rows):
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
        assert (item.id, item.text, item.link) == (raw["id"], raw["text"], raw["link"])


def test_action_items_parse_for_the_page() -> None:
    items = view.parse_action_items_result(ITEMS_TEXT)
    mine = [r for r in ITEM_ROWS if r["mine"] is not False]
    assert [i.id for i in items] == [r["id"] for r in mine]
    for raw, item in zip(mine, items):
        assert item.status == raw["status"]
        assert item.kind == raw["kind"]
        assert item.due_date == raw["due_date"] and item.due_time == raw["due_time"]
        assert item.voice_note is (raw["conversation_kind"] == "note")
        assert item.list_name == raw["list_name"] and item.note_title == raw["note_title"]
        assert item.link == raw["link"]
    for raw in ITEM_ROWS:
        assert view.item_from_json(raw) is not None


async def test_action_items_page_reads_json_and_finds_items() -> None:
    async def call(tool: str, args: dict) -> str:
        assert tool == "list_action_items" and args.get("format") == "json"
        return ITEMS_TEXT

    rows = await view.action_items(call, status="all")
    assert {r["id"] for r in rows} == {r["id"] for r in ITEM_ROWS if r["mine"] is not False}
    for raw in ITEM_ROWS:
        assert (await view.find_item(call, raw["id"]))["id"] == raw["id"]


def test_action_items_people_and_ownership() -> None:
    for raw in ITEM_ROWS:
        assert insight_inputs.is_mine(raw) is bool(raw["mine"])
        assert insight_inputs.people(raw) == [p["name"] for p in raw["with"]]
