"""Fork: the Belief Structure task (src/belief_structure.py, plan stage 2.2).

Fictional data only.
"""
import asyncio
import datetime
import json
import re

import pytest

from src import belief_structure as bs
from src import evidence_intake as ei

T0 = datetime.datetime(2026, 10, 21, 9, 0, tzinfo=datetime.timezone.utc)
T1 = T0 + datetime.timedelta(hours=1)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bs, "_LOCKS", {})
    for var in ("BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS",
                "BELIEF_STALE_DAYS", "BELIEF_STRUCTURE", "BELIEF_STRUCTURE_BATCH"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def seed(*facts, owner="alice", confirmed=True, last="2026-10-15"):
    """facts: (id, subject, about, text)."""
    ei.submit(owner, [ei.Evidence(
        source="odysseus-sound", source_id=str(fid), subject=subject, about=about, text=text,
        link=f"http://h:8000/view/c{fid}?sig=abc#t=10", seen_first="2026-10-01",
        seen_last=last, times=3 if confirmed else 1, days=2 if confirmed else 1,
        source_confirmed=confirmed)
        for fid, subject, about, text in facts], now=T0)
    return {b["text"]: b for b in ei.list_beliefs(owner)}


class FakeLlm:
    """Answers each batch from ``answer(rows, call_no)``; ``rows`` are the numbered facts
    it was sent (``n``, ``subject``, ``kind``, ``text``, ``said``)."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def __call__(self, messages):
        self.calls.append(messages)
        body = messages[1]["content"]
        sent = [json.loads(line) for line in body.split("\n") if line.startswith("{")]
        return self.answer(sent, len(self.calls))


VALUES = {
    "Priya is allergic to peanuts": ("allergy", "peanuts", None, None),
    "Priya lives in Porto since March 2025": ("lives_in", "Porto", "2025-03-01", None),
    "Heron launches before 2027-01-31": ("deadline", "2027-01-31", None, "2027-01-31"),
    "Tomas works at Acme as a designer": ("shoe_size", "Acme designer", None, None),
}


def item(row, attribute, value, valid_from=None, valid_to=None):
    return {"n": row["n"], "attribute": attribute, "value": value,
            "valid_from": valid_from, "valid_to": valid_to}


def good(sent, _n):
    return json.dumps({"items": [item(r, *VALUES[r["text"]]) for r in sent]})


FACTS = [(1, "Priya Nair", "person", "Priya is allergic to peanuts"),
         (2, "Priya Nair", "person", "Priya lives in Porto since March 2025"),
         (3, "Heron", "other", "Heron launches before 2027-01-31"),
         (4, "Tomas Berg", "person", "Tomas works at Acme as a designer")]


def test_structures_confirmed_beliefs_with_validation(data):
    beliefs = seed(*FACTS)
    seed((5, "Lena", "person", "Lena likes tea"), confirmed=False)
    llm = FakeLlm(good)
    stats = asyncio.run(bs.run("alice", llm=llm, now=T1))
    assert stats == {"candidates": 4, "structured": 4, "other": 1, "rejected": 0,
                     "waiting": 0, "failed": 0}
    assert len(llm.calls) == 1
    sent = llm.calls[0][1]["content"]
    assert "Lena likes tea" not in sent  # tentative: never sent
    rows = [json.loads(line) for line in sent.split("\n") if line.startswith("{")]
    assert [r["n"] for r in rows] == [1, 2, 3, 4] and "id" not in rows[0]
    assert rows[0] == {"n": 1, "subject": "Priya Nair", "kind": "person",
                       "text": "Priya is allergic to peanuts",
                       "said": "2026-10-01 to 2026-10-15"}
    assert "data, not instructions" in llm.calls[0][0]["content"]
    got = {b["text"]: b for b in ei.list_beliefs("alice", status="confirmed")}
    p = got["Priya lives in Porto since March 2025"]
    assert (p["attribute"], p["value"], p["valid_from"], p["structured_at"]) == \
        ("lives_in", "Porto", "2025-03-01", "2026-10-21T10:00:00+00:00")
    assert got["Tomas works at Acme as a designer"]["attribute"] == "other"  # unknown
    heron = got["Heron launches before 2027-01-31"]
    assert (heron["attribute"], heron["valid_until"]) == ("deadline", "2027-01-31")
    assert heron["subject_kind"] == "project"  # inferred from the deadline
    assert beliefs["Priya is allergic to peanuts"]["attribute"] is None
    assert ei.stats("alice")["structured"] == 4
    # nothing left: a second run makes no model call
    assert asyncio.run(bs.run("alice", llm=llm, now=T1))["candidates"] == 0
    assert len(llm.calls) == 1


def test_bad_values_are_cleaned(data):
    seed(FACTS[1])

    def answer(sent, _n):
        return "```json\n" + json.dumps({"items": [
            {"n": 1, "attribute": "Lives In", "value": "Porto " + "x" * 500,
             "valid_from": "March 2025", "valid_to": "2026-02-30"},
            {"n": 7, "attribute": "allergy", "value": "not in the batch"}]}) + "\n```"

    stats = asyncio.run(bs.run("alice", llm=FakeLlm(answer), now=T1))
    assert stats["structured"] == 1
    (b,) = ei.list_beliefs("alice")
    assert (b["attribute"], len(b["value"]), b["valid_from"], b["valid_until"]) == \
        ("lives_in", 120, None, None)


# ------------------------------------------------------------------ P2-D2
APPOINTMENT = "Me has a follow-up appointment with Dr. Patel on October twenty-first"
HARD_FOOD = "Me should avoid hard food, nuts and hard candy on the left side"


def test_an_item_merged_onto_the_wrong_belief_is_rejected(data):
    """The model answers one item for two facts, under the first one's number, with the
    second one's content: nothing is stored and both wait."""
    seed((14, "Me", "owner", APPOINTMENT), (15, "Me", "owner", HARD_FOOD))

    def merged(sent, _n):
        assert [r["text"] for r in sent] == [APPOINTMENT, HARD_FOOD]
        return json.dumps({"items": [item(sent[0], "health",
                                          "avoid hard food, nuts, and hard candy")]})

    stats = asyncio.run(bs.run("alice", llm=FakeLlm(merged), now=T1))
    assert stats == {"candidates": 2, "structured": 0, "other": 0, "rejected": 1,
                     "waiting": 2, "failed": 0}
    assert all(b["attribute"] is None and b["structured_at"] is None
               for b in ei.list_beliefs("alice"))
    assert ei.beliefs_by_attribute("alice", "health") == []
    assert "1 rejected" in bs.summary(stats)

    def right(sent, _n):
        values = {APPOINTMENT: ("plans", "follow-up appointment with Dr. Patel"),
                  HARD_FOOD: ("health", "avoid hard food and nuts")}
        return json.dumps({"items": [item(r, *values[r["text"]]) for r in sent]})

    stats = asyncio.run(bs.run("alice", llm=FakeLlm(right), now=T1))
    assert stats["structured"] == 2
    got = {b["text"]: b["attribute"] for b in ei.list_beliefs("alice")}
    assert got == {APPOINTMENT: "plans", HARD_FOOD: "health"}


@pytest.mark.parametrize("attribute,value,text,ok", [
    ("allergy", "peanuts", "Priya is allergic to peanuts", True),
    ("allergy", "allergies to nuts", "Priya is allergic to nuts", True),  # same stem
    ("pet", "cat", "Priya has a cat", True),  # no 4-letter word: any shared word
    ("pet", "dog", "Priya has a cat", False),
    ("health", "avoid hard food", APPOINTMENT, False),
    ("birthday", "21 May", "Mom turns seventy on November eighth", True),
    ("birthday", "21 May", "Mom likes roses", False),
    ("other", "anything at all", "Mom likes roses", True),
    ("likes", "the owner", "The owner likes roses", False),  # stop words don't count
])
def test_fits(attribute, value, text, ok):
    assert bs.fits(attribute, value, text) is ok


def test_items_map_back_by_number_not_id(data):
    beliefs = seed(*FACTS[:2])

    def ids_instead(sent, _n):  # a model that answers belief ids: nothing maps
        return json.dumps({"items": [{"id": beliefs[r["text"]]["id"], "attribute": "allergy",
                                      "value": "peanuts"} for r in sent]})

    stats = asyncio.run(bs.run("alice", llm=FakeLlm(ids_instead), now=T1))
    assert (stats["structured"], stats["waiting"]) == (0, 2)

    def reversed_order(sent, _n):
        return json.dumps({"items": [item(r, *VALUES[r["text"]]) for r in reversed(sent)]})

    stats = asyncio.run(bs.run("alice", llm=FakeLlm(reversed_order), now=T1))
    assert stats["structured"] == 2
    assert ei.get_belief("alice", beliefs["Priya is allergic to peanuts"]["id"])["value"] == \
        "peanuts"


# ------------------------------------------------------------------ P2-D3
DEMO = "The client demo is on October twenty-third"
RENEWAL = "Thomas needs the signed renewal by October 31"


def test_guessed_years_are_dropped_and_never_expire_a_belief(data):
    seed((20, "Client demo", "other", DEMO), (21, "Thomas", "person", RENEWAL))

    def guessed(sent, _n):
        assert {r["said"] for r in sent} == {"2026-10-01 to 2026-10-15"}
        return json.dumps({"items": [
            item(sent[0], "deadline", "October twenty-third", "2023-10-23", "2023-10-23"),
            item(sent[1], "deadline", "signed renewal by October 31", "2023-01-01",
                 "2023-10-31")]})

    stats = asyncio.run(bs.run("alice", llm=FakeLlm(guessed), now=T1))
    assert stats["structured"] == 2
    for b in ei.list_beliefs("alice"):
        assert (b["valid_from"], b["valid_until"], b["status"]) == (None, None, "confirmed")
    assert ei.apply_decay("alice", now=T1) == {"stale": 0}


def test_a_date_after_it_was_said_with_a_month_in_the_text_is_kept(data):
    seed((21, "Thomas", "person", RENEWAL))

    def right(sent, _n):
        return json.dumps({"items": [item(sent[0], "deadline", "signed renewal October 31",
                                          None, "2026-10-31")]})

    asyncio.run(bs.run("alice", llm=FakeLlm(right), now=T1))
    (b,) = ei.list_beliefs("alice")
    assert (b["attribute"], b["valid_until"], b["status"]) == ("deadline", "2026-10-31",
                                                              "confirmed")


def test_an_end_before_it_was_said_or_in_the_past_is_not_stored(data):
    seed((30, "Tomas Berg", "person", "Tomas lived in Berlin until 2020"),
         (31, "Tomas Berg", "person", "Tomas rents the boat until 2026-10-18"))

    def answer(sent, _n):
        values = {"Tomas lived in Berlin until 2020": ("lives_in", "Berlin", None,
                                                       "2020-12-31"),
                  "Tomas rents the boat until 2026-10-18": ("owns", "boat", None,
                                                            "2026-10-18")}
        return json.dumps({"items": [item(r, *values[r["text"]]) for r in sent]})

    stats = asyncio.run(bs.run("alice", llm=FakeLlm(answer), now=T1))
    assert stats["structured"] == 2
    assert {b["valid_until"] for b in ei.list_beliefs("alice")} == {None}


@pytest.mark.parametrize("value,text,last,want", [
    ("2026-10-23", DEMO, "2026-10-15", "2026-10-23"),
    ("2023-10-23", DEMO, "2026-10-15", None),          # guessed year
    ("2028-10-23", DEMO, "2026-10-15", None),          # more than a year after
    ("2026-10-23", "The demo went well", "2026-10-15", None),  # no month or day named
    ("2025-03-01", "Priya lives in Porto since March 2025", "2026-10-15", "2025-03-01"),
    ("2026-02-30", DEMO, "2026-10-15", None),
    ("March 2025", DEMO, "2026-10-15", None),
    ("2026-11-08", "Mom turns seventy on the 8th", "2026-10-15", "2026-11-08"),
])
def test_plausible_date(value, text, last, want):
    assert bs.plausible_date(value, text, last) == want


# ------------------------------------------------------------------ runs
def test_garbage_once_then_retry(data):
    seed(FACTS[0])

    def answer(sent, n):
        return "Sure! Here you go." if n == 1 else good(sent, n)

    llm = FakeLlm(answer)
    stats = asyncio.run(bs.run("alice", llm=llm, now=T1))
    assert stats["structured"] == 1 and stats["failed"] == 0 and len(llm.calls) == 2
    assert llm.calls[1][-1]["content"].startswith("That was not valid JSON")


def test_garbage_twice_leaves_the_batch_for_next_run(data):
    seed(*FACTS)
    stats = asyncio.run(bs.run("alice", llm=FakeLlm(lambda s, n: "{oops"), now=T1))
    assert stats == {"candidates": 4, "structured": 0, "other": 0, "rejected": 0,
                     "waiting": 4, "failed": 1}
    assert len(ei.structure_candidates("alice")) == 4
    stats = asyncio.run(bs.run("alice", llm=FakeLlm(good), now=T1))
    assert stats["structured"] == 4


def test_missing_items_and_a_model_error_wait(data):
    seed(*FACTS)

    def partial(sent, _n):
        return json.dumps({"items": [item(sent[0], "allergy", "peanuts")]})

    stats = asyncio.run(bs.run("alice", llm=FakeLlm(partial), now=T1))
    assert (stats["structured"], stats["waiting"]) == (1, 3)

    async def boom(messages):
        raise RuntimeError("endpoint down")

    stats = asyncio.run(bs.run("alice", llm=boom, now=T1))
    assert (stats["failed"], stats["waiting"]) == (1, 3)


def likes(sent, _n):
    return json.dumps({"items": [item(r, "likes", re.sub(r".* ", "", r["text"]))
                                 for r in sent]})


def test_batches_of_five_and_at_most_forty_per_run(data):
    seed(*[(i, f"Person {i}", "person", f"Person {i} likes number{i}") for i in range(1, 46)])
    llm = FakeLlm(likes)
    stats = asyncio.run(bs.run("alice", llm=llm, now=T1))
    assert stats["candidates"] == 40 and stats["structured"] == 40
    assert len(llm.calls) == 8 and all(
        len([x for x in c[1]["content"].split("\n") if x.startswith("{")]) == 5
        for c in llm.calls)
    stats = asyncio.run(bs.run("alice", llm=llm, now=T1))
    assert stats["candidates"] == 5 and len(llm.calls) == 9


def test_batch_setting(data, monkeypatch):
    assert bs.batch_size() == 5
    for raw, want in (("10", 10), ("2", 2), ("50", 10), ("0", 1), ("x", 5)):
        monkeypatch.setenv("BELIEF_STRUCTURE_BATCH", raw)
        assert bs.batch_size() == want
    assert bs.max_tokens(10) > bs.max_tokens(5) > bs.max_tokens(1)
    monkeypatch.setenv("BELIEF_STRUCTURE_BATCH", "10")
    seed(*[(i, f"Person {i}", "person", f"Person {i} likes number{i}") for i in range(1, 21)])
    llm = FakeLlm(likes)
    asyncio.run(bs.run("alice", llm=llm, now=T1))
    assert len(llm.calls) == 2


def test_beliefs_that_keep_failing_do_not_starve_the_rest(data):
    seed(*[(i, f"Person {i}", "person", f"Person {i} likes number{i}") for i in range(1, 4)])

    def first_two_wrong(sent, _n):
        return json.dumps({"items": [
            item(r, "likes", "unrelated words" if r["text"].startswith(("Person 1 ",
                                                                         "Person 2 "))
                 else re.sub(r".* ", "", r["text"])) for r in sent]})

    stats = asyncio.run(bs.run("alice", llm=FakeLlm(first_two_wrong), now=T0, limit=2))
    assert (stats["rejected"], stats["waiting"]) == (2, 2)
    llm = FakeLlm(first_two_wrong)
    stats = asyncio.run(bs.run("alice", llm=llm, now=T1, limit=2))
    sent = llm.calls[0][1]["content"]
    assert "Person 3 likes" in sent  # never tried goes first
    assert stats["structured"] == 1
    state = json.loads((data / "belief_structure_alice.json").read_text())
    assert len(state["tried"]) == 2


def test_correction_is_structured_again(data):
    beliefs = seed(FACTS[0])
    asyncio.run(bs.run("alice", llm=FakeLlm(good), now=T1))
    bid = beliefs["Priya is allergic to peanuts"]["id"]
    ei.correct("alice", bid, "Priya is allergic to tree nuts")

    def answer(sent, _n):
        assert sent[0]["text"] == "Priya is allergic to tree nuts"
        return json.dumps({"items": [item(sent[0], "allergy", "tree nuts")]})

    stats = asyncio.run(bs.run("alice", llm=FakeLlm(answer), now=T1))
    assert stats["structured"] == 1
    assert ei.get_belief("alice", bid)["value"] == "tree nuts"


def test_text_changed_while_the_model_answered_waits(data):
    beliefs = seed(FACTS[0])
    bid = beliefs["Priya is allergic to peanuts"]["id"]

    def answer(sent, n):
        ei.correct("alice", bid, "Priya is allergic to tree nuts")  # meanwhile
        return good(sent, n)

    stats = asyncio.run(bs.run("alice", llm=FakeLlm(answer), now=T1))
    assert (stats["structured"], stats["waiting"]) == (0, 1)
    assert ei.get_belief("alice", bid)["structured_at"] is None


def test_default_llm_runs_in_the_background_without_thinking(monkeypatch):
    import src.interactive_gate as gate
    import src.llm_core as llm_core  # noqa: PLR0402
    import src.task_endpoint as te

    waited, sent = [], []

    async def fake_wait(label):
        waited.append(label)

    async def fake_call(cands, messages, **kw):
        sent.append(kw)
        return '{"items": []}'

    monkeypatch.setattr(gate, "wait_for_interactive_quiet", fake_wait)
    monkeypatch.setattr(llm_core, "llm_call_async_with_fallback", fake_call)
    monkeypatch.setattr(te, "resolve_task_candidates", lambda **kw: [("http://llm", "m", {})])
    monkeypatch.setenv("BELIEF_STRUCTURE_BATCH", "3")

    async def go():
        call = await bs.default_llm("alice")
        return await call([{"role": "user", "content": "hi"}])

    assert asyncio.run(go()) == '{"items": []}'
    assert waited == ["belief structure"]
    assert sent[0]["workload"] == "background" and sent[0]["thinking_mode"] == "off"
    assert sent[0]["max_tokens"] == bs.max_tokens(3)


def test_action_counts_only_noop_and_switch(data, monkeypatch):
    from src.builtin_actions import TaskNoop

    with pytest.raises(TaskNoop):
        asyncio.run(bs.action_belief_structure("alice"))  # nothing to do: no model call
    seed(FACTS[0])

    async def fake_llm(owner):
        return FakeLlm(good)

    monkeypatch.setattr(bs, "default_llm", fake_llm)
    text, ok = asyncio.run(bs.action_belief_structure("alice"))
    assert ok and text == "structure: 1 to do, 1 structured (0 other), 0 waiting"
    assert "Priya" not in text
    monkeypatch.setenv("BELIEF_STRUCTURE", "off")
    with pytest.raises(TaskNoop):
        asyncio.run(bs.action_belief_structure("alice"))


def test_registered_as_a_silent_model_backed_hourly_task():
    from src.builtin_actions import BUILTIN_ACTION_INFO, BUILTIN_ACTIONS
    from src.task_scheduler import HOUSEKEEPING_DEFAULTS, TaskScheduler

    assert BUILTIN_ACTIONS[bs.ACTION] is bs.action_belief_structure
    assert bs.ACTION in BUILTIN_ACTION_INFO
    assert HOUSEKEEPING_DEFAULTS[bs.ACTION]["cron_expression"] == "15 * * * *"
    assert bs.ACTION in TaskScheduler._SILENT_ACTIONS
    assert bs.ACTION in TaskScheduler._MODEL_BACKED_ACTIONS
