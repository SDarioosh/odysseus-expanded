"""Today insight (src/today_insight.py, routes/today_insight_routes.py). Fictional data only."""

import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src import today_insight as ti

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 21, 7, 0, tzinfo=timezone.utc)  # 09:00 in Berlin
TODAY = date(2026, 10, 21)


def _note(id, title="Errands", items=(), note_type="checklist", due_date=None, pinned=False,
          archived=False, source="user", label=None):
    return SimpleNamespace(id=id, title=title, note_type=note_type, due_date=due_date,
                           pinned=pinned, archived=archived, source=source, label=label,
                           items=json.dumps([{"text": t, "done": d} for t, d in items]))


def _event(uid, start, end, summary="Meeting", all_day=False, is_utc=False, status=None,
           location="", importance="normal", event_type=None):
    """An occurrence as the calendar route's ``_expand_rrule`` returns it."""
    if all_day:
        ds, de = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    else:
        z = "Z" if is_utc else ""
        ds, de = start.isoformat() + z, end.isoformat() + z
    d = {"uid": uid, "dtstart": ds, "dtend": de, "summary": summary, "all_day": all_day,
         "is_utc": is_utc, "location": location, "importance": importance,
         "event_type": event_type}
    if status:
        d["status"] = status
    return d


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ti, "owner_timezone", lambda owner: TZ)
    monkeypatch.setattr(ti, "_FILE_LOCKS", {})
    monkeypatch.setattr(ti, "_RUN_LOCKS", {})
    return tmp_path


# ------------------------------------------------------------------ buckets and lists
def test_buckets():
    assert ti.bucket_for(None, TODAY) == "undated"
    assert ti.bucket_for(date(2026, 10, 20), TODAY) == "overdue"
    assert ti.bucket_for(TODAY, TODAY) == "due_today"
    assert ti.bucket_for(date(2026, 10, 28), TODAY) == "due_soon"
    assert ti.bucket_for(date(2026, 10, 29), TODAY) == "later"


def test_due_date_forms_are_read_as_local_dates():
    assert ti.local_date("2026-10-21", TZ) == TODAY
    assert ti.local_date("2026-10-21T08:00:00+02:00", TZ) == TODAY
    assert ti.local_date("2026-10-20T23:30:00+00:00", TZ) == TODAY  # 01:30 in Berlin
    assert ti.local_date("2026-10-22T08:00", TZ) == date(2026, 10, 22)
    assert ti.local_date("", TZ) is None and ti.local_date("soon", TZ) is None


def test_shopping_lists_are_skipped_unless_dated():
    names = {"shopping", "groceries", "grocery"}
    for title in ("Shopping", "shopping list", "Groceries", "GROCERY LIST"):
        assert ti.is_skipped_list(title, None, names), title
        assert not ti.is_skipped_list(title, "2026-10-22", names), title
    assert not ti.is_skipped_list("Shopping for Mira's party", None, names)
    assert not ti.is_skipped_list("Work", None, names)


def test_skip_list_setting(monkeypatch):
    monkeypatch.setenv("TODAY_INSIGHT_SKIP_LISTS", "Hardware store, Pharmacy List")
    assert ti.skip_lists() == {"hardware store", "pharmacy"}
    monkeypatch.delenv("TODAY_INSIGHT_SKIP_LISTS")
    assert ti.skip_lists() == {"shopping", "groceries", "grocery"}


def test_todo_candidates_types_done_and_shopping():
    notes = [
        _note("n1", "Work", [("Send the Q4 deck", False), ("Book room", True)],
              due_date="2026-10-20"),
        _note("n2", "Shopping", [("Oat milk", False)], note_type="todo"),
        _note("n3", "Groceries list", [("Bread", False)], note_type="goal", due_date="2026-10-21"),
        _note("n4", "Run 10k", [("Long run Sunday", False)], note_type="goal"),
        _note("n5", "Idea", note_type="note"),
        _note("n6", "Old", [("Archived thing", False)], archived=True),
        _note("n7", "Meet Jason", [("Meet Jason", False)], source="calendar", label="calendar"),
    ]
    out = ti.todo_candidates(notes, TODAY, TZ, names={"shopping", "groceries", "grocery"})
    assert [(c["ref"], c["bucket"]) for c in out] == [
        ("todo:n1:0", "overdue"), ("todo:n3:0", "due_today"), ("todo:n4:0", "undated")]
    assert out[0]["title"] == "Send the Q4 deck" and out[0]["list"] == "Work"


def test_event_candidates_drop_ended_cancelled_and_far():
    n = datetime  # naive local rows unless is_utc
    events = [
        _event("e-past", n(2026, 10, 21, 7, 0), n(2026, 10, 21, 8, 0)),  # ended at 08:00
        _event("e-now", n(2026, 10, 21, 8, 30), n(2026, 10, 21, 9, 30), "Standup"),
        _event("e-utc", n(2026, 10, 21, 10, 0), n(2026, 10, 21, 11, 0), "Dentist", is_utc=True),
        _event("e-allday", n(2026, 10, 21), n(2026, 10, 22), "Mira's birthday", all_day=True),
        _event("e-soon", n(2026, 10, 24, 18, 0), n(2026, 10, 24, 20, 0), "Dinner at Tom's"),
        _event("e-cancel", n(2026, 10, 22, 9, 0), n(2026, 10, 22, 10, 0), status="cancelled"),
        _event("e-far", n(2026, 11, 2, 9, 0), n(2026, 11, 2, 10, 0)),
    ]
    out = {c["ref"]: c for c in ti.event_candidates(events, NOW.astimezone(TZ))}
    assert set(out) == {"event:e-now", "event:e-utc", "event:e-allday", "event:e-soon"}
    assert out["event:e-utc"]["time"] == "12:00"  # stored in UTC
    assert out["event:e-allday"]["bucket"] == "due_today" and out["event:e-allday"]["time"] is None
    assert out["event:e-soon"]["bucket"] == "due_soon"


def test_event_candidates_keep_occurrence_uids():
    occ = [_event("wk::2026-10-21T07:00", datetime(2026, 10, 21, 7), datetime(2026, 10, 21, 7, 30)),
           _event("wk::2026-10-21T15:00", datetime(2026, 10, 21, 15), datetime(2026, 10, 21, 15, 30)),
           _event("day::2026-10-24", datetime(2026, 10, 24), datetime(2026, 10, 25), all_day=True)]
    out = ti.event_candidates(occ, NOW.astimezone(TZ))
    assert [(c["ref"], c["bucket"], c["time"]) for c in out] == [
        ("event:wk::2026-10-21T15:00", "due_today", "15:00"),
        ("event:day::2026-10-24", "due_soon", None)]


def test_candidate_order_and_cap():
    todos = ti.todo_candidates([
        _note("u", "Someday", [("Undated A", False)]),
        _note("p", "Pinned", [("Pinned B", False)], pinned=True),
        _note("s", "Soon", [("Soon C", False)], due_date="2026-10-25"),
        _note("t", "Today", [("Today D", False)], due_date="2026-10-21"),
        _note("o", "Late", [("Overdue E", False)], due_date="2026-10-19"),
        _note("l", "Later", [("Later F", False)], due_date="2026-12-01"),
    ], TODAY, TZ, names=set())
    events = ti.event_candidates([_event("e1", datetime(2026, 10, 23, 9), datetime(2026, 10, 23, 10),
                                         "Review")], NOW.astimezone(TZ))
    ordered = ti.order_candidates(todos, events)
    assert [c["title"] for c in ordered] == [
        "Overdue E", "Today D", "Review", "Soon C", "Pinned B", "Undated A", "Later F"]
    many = ti.todo_candidates([_note("big", "Big", [(f"Item {i}", False) for i in range(40)],
                                     due_date="2026-10-21")], TODAY, TZ, names=set())
    assert len(ti.order_candidates(many, [])) == 30


def test_memory_selection():
    entries = [
        {"text": "Prefers mornings for deep work", "category": "preference", "uses": 5, "timestamp": 1},
        {"text": "Tom is Mira's brother", "category": "contact", "uses": 1, "timestamp": 9},
        {"text": "Likes cats", "category": "fact", "uses": 1, "timestamp": 3},
        {"text": "Mira's birthday is 21 October", "category": "event", "source": "odysseus-sound"},
        {"text": "Use bullet points", "category": "instruction", "uses": 99},
        {"text": "  ", "category": "fact"},
    ]
    assert ti.select_memory(entries) == ["Prefers mornings for deep work", "Tom is Mira's brother",
                                         "Likes cats", "Mira's birthday is 21 October"]
    assert len(ti.select_memory([{"text": f"f{i}", "category": "fact"} for i in range(50)])) == 40


# ------------------------------------------------------------------ model answers
CANDS = [{"ref": "todo:n1:0", "title": "Send the Q4 deck"}, {"ref": "event:e1", "title": "Dentist"},
         {"ref": "todo:n4:0", "title": "Long run Sunday"}]


def test_validate_drops_invented_and_duplicate_refs_and_truncates():
    answer = {"headline": "A full day.  Start with the deck.",
              "focus": [{"ref": "todo:n1:0", "why": "x" * 120}, {"ref": "todo:zz:9", "why": "made up"},
                        "garbage"],
              "coming_up": [{"ref": "event:e1", "why": "12:00, bring the card"},
                            {"ref": "todo:n1:0", "why": "again"}],
              "also": [{"ref": "todo:n4:0", "why": "Sunday"}] * 3}
    out = ti.validate(answer, CANDS)
    assert out["headline"] == "A full day. Start with the deck."
    assert [r["ref"] for r in out["focus"]] == ["todo:n1:0"]
    assert len(out["focus"][0]["why"]) == 80 and out["focus"][0]["why"].endswith("…")
    assert out["focus"][0]["title"] == "Send the Q4 deck"
    assert [r["ref"] for r in out["coming_up"]] == ["event:e1"]
    assert [r["ref"] for r in out["also"]] == ["todo:n4:0"]


def test_validate_caps_sections_at_five():
    cands = [{"ref": f"todo:n:{i}", "title": f"T{i}"} for i in range(8)]
    out = ti.validate({"headline": "h", "focus": [{"ref": c["ref"], "why": "w"} for c in cands]}, cands)
    assert len(out["focus"]) == 5 and out["coming_up"] == [] and out["also"] == []


def test_validate_drops_a_reason_that_only_restates_the_title():
    cands = [{"ref": "belief:1", "title": "Slow down feature development for Odysseus after "
                                          "phase 4 to focus on bug fixing"},
             {"ref": "belief:2", "title": "Have Odysseus save notes as flat PNGs, transcribe "
                                          "them, and feed them into the…",
              "text": "Shahyah plans to have Odysseus save notes as flat PNGs, transcribe "
                      "them, and feed them into the personal context system."},
             {"ref": "event:e1", "title": "Dentist"}]
    answer = {"headline": "h",
              "coming_up": [{"ref": "event:e1", "why": "12:00, bring the insurance card"}],
              "also": [{"ref": "belief:1", "why": "Slow feature dev after phase 4; focus on "
                                                  "bug fixing for Odysseus."},
                       {"ref": "belief:2", "why": "Save notes as PNGs and transcribe; feeds "
                                                  "into personal context system."}]}
    out = ti.validate(answer, cands)
    assert [r["why"] for r in out["also"]] == ["", ""]
    assert out["coming_up"][0]["why"] == "12:00, bring the insurance card"


@pytest.mark.parametrize("why,title,repeats", [
    ("Due Friday; Mira needs it for the board", "Send the Q4 deck", False),
    ("Send the deck", "Send the Q4 deck", True),
    ("", "Send the Q4 deck", False),
])
def test_repeats_title(why, title, repeats):
    assert ti.repeats_title(why, title) is repeats


def test_parse_answer():
    assert ti.parse_answer("not json at all") is None
    assert ti.parse_answer('{"focus": []}') is None  # no headline
    assert ti.parse_answer('```json\n{"headline": "Hi", "focus": []}\n```')["headline"] == "Hi"
    assert ti.parse_answer('<think>hmm</think>{"headline": "Ok"}')["headline"] == "Ok"


# ------------------------------------------------------------------ run, storage, votes
class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, messages):
        self.calls.append(messages)
        return self.answers.pop(0) if self.answers else self.last
    last = ""


GOOD = json.dumps({"headline": "The Q4 deck is overdue; send it before the dentist at 12.",
                   "focus": [{"ref": "todo:n1:0", "why": "Overdue since yesterday; Tom waits on it"},
                             {"ref": "todo:nope:1", "why": "invented"}],
                   "coming_up": [{"ref": "event:e1", "why": "12:00 today"}],
                   "also": [{"ref": "todo:n4:0", "why": "Sunday long run, pack shoes"}]})


@pytest.fixture
def inputs(store, monkeypatch):
    state = {"notes": [_note("n1", "Work", [("Send the Q4 deck", False)], due_date="2026-10-20"),
                       _note("n4", "Run 10k", [("Long run Sunday", False)], note_type="goal")],
             "events": [_event("e1", datetime(2026, 10, 21, 12), datetime(2026, 10, 21, 13), "Dentist")],
             "memory": [{"text": "Tom is a colleague", "category": "contact"}]}
    monkeypatch.setattr(ti, "_load_notes", lambda owner: state["notes"])
    monkeypatch.setattr(ti, "_load_events", lambda owner, now_local: state["events"])
    monkeypatch.setattr(ti, "_load_memory", lambda owner, query=None: ti.select_memory(
        state["memory"], owner=owner, query=query))
    return state


def _run(**kw):
    return asyncio.run(ti.run("alice", now=NOW, call=None, **kw))


def test_run_writes_the_documented_json(inputs, store):
    llm = FakeLLM(GOOD)
    status, insight = _run(llm=llm)
    assert status == "generated" and len(llm.calls) == 1
    path = store / "today_insight" / "alice" / "2026-10-21.json"
    stored = json.loads(path.read_text())
    assert set(stored) == {"date", "generated", "headline", "focus", "coming_up", "also",
                           "input_hash", "votes"}
    assert stored["date"] == "2026-10-21" and stored["generated"] == "2026-10-21T07:00:00Z"
    assert stored["focus"] == [{"ref": "todo:n1:0", "why": "Overdue since yesterday; Tom waits on it",
                                "title": "Send the Q4 deck"}]
    assert stored["coming_up"][0]["title"] == "Dentist"
    assert len(stored["input_hash"]) == 64 and stored["votes"] == []
    prompt = llm.calls[0][1]["content"]
    assert "T1 [overdue, 2026-10-20] Send the Q4 deck" in prompt
    assert "E1 [due_today, 2026-10-21 12:00] Dentist" in prompt and "todo:n1:0" not in prompt
    assert "Tom is a colleague" in prompt
    assert ti.public(stored) == {k: stored[k] for k in
                                 ("date", "generated", "headline", "focus", "coming_up", "also")}


def test_unchanged_inputs_are_a_noop_and_changes_rerun(inputs):
    llm = FakeLLM(GOOD, GOOD)
    assert _run(llm=llm)[0] == "generated"
    assert _run(llm=llm)[0] == "noop" and len(llm.calls) == 1
    inputs["notes"][0].items = json.dumps([{"text": "Send the Q4 deck", "done": True}])
    assert _run(llm=llm)[0] == "generated" and len(llm.calls) == 2


def test_garbage_once_then_retry(inputs):
    llm = FakeLLM("Sure! Here is your day: lots to do.", GOOD)
    status, insight = _run(llm=llm)
    assert status == "generated" and len(llm.calls) == 2
    assert llm.calls[1][-1]["role"] == "user" and "JSON" in llm.calls[1][-1]["content"]


def test_garbage_twice_keeps_previous(inputs):
    _run(llm=FakeLLM(GOOD))
    inputs["memory"].append({"text": "Allergic to peanuts", "category": "fact"})
    status, kept = _run(llm=FakeLLM("nope", "still nope"))
    assert status == "kept" and kept["focus"][0]["ref"] == "todo:n1:0"
    assert ti.load("alice", "2026-10-21")["headline"].startswith("The Q4 deck")


def test_no_candidates_writes_without_model(inputs):
    inputs["notes"], inputs["events"] = [], []
    llm = FakeLLM()
    status, insight = _run(llm=llm)
    assert status == "empty" and llm.calls == []
    assert insight["headline"] == ti.EMPTY_HEADLINE and insight["focus"] == []


def test_votes_are_stored_and_survive_regeneration(inputs):
    assert asyncio.run(ti.add_vote("alice", "up", now=NOW)) is None  # nothing yet
    _run(llm=FakeLLM(GOOD))
    asyncio.run(ti.add_vote("alice", "down", now=NOW))
    insight = asyncio.run(ti.add_vote("alice", "up", now=NOW))
    assert [v["vote"] for v in insight["votes"]] == ["down", "up"]
    assert insight["votes"][0]["at"] == "2026-10-21T07:00:00Z"
    with pytest.raises(ValueError):
        asyncio.run(ti.add_vote("alice", "meh", now=NOW))
    asyncio.run(ti.generate_now("alice", llm=FakeLLM(GOOD), now=NOW, call=None))
    assert len(ti.load("alice", "2026-10-21")["votes"]) == 2


def test_context_block(inputs):
    assert ti.context_block("alice", now=NOW) == ""
    _run(llm=FakeLLM(GOOD))
    block = ti.context_block("alice", now=NOW)
    assert block.splitlines()[0] == ("Today's plan, for questions about today, priorities or "
                                     "what to do first. For anything else use your tools as "
                                     "usual. (2026-10-21)")
    assert "Do first: 1. Send the Q4 deck: Overdue since yesterday; Tom waits on it" in block
    assert "Coming up: 1. Dentist: 12:00 today" in block
    assert len(block) <= 800
    insight = ti.load("alice", "2026-10-21")
    insight["focus"] = [{"ref": f"todo:n:{i}", "title": "T" * 70, "why": "w" * 80} for i in range(5)]
    insight["also"] = insight["focus"]
    ti.save("alice", insight)
    assert len(ti.context_block("alice", now=NOW)) <= 800
    assert ti.context_block("bob", now=NOW) == ""


def test_module_inputs_are_optional(inputs):
    async def call(tool, args):
        if tool == "get_daily_digest":
            assert args == {"date": "2026-10-21"}
            return ("Digest 2026-10-21: Planning day\n- Talked with Tom about the deck\n"
                    "When answering, cite the link.")
        assert args == {"status": "open", "format": "json"}
        return json.dumps([{"id": 7, "status": "open", "text": "Email Tom the numbers",
                            "due_date": "2026-10-22"}])

    llm = FakeLLM(GOOD)
    asyncio.run(ti.run("alice", now=NOW, call=call, llm=llm))
    prompt = llm.calls[0][1]["content"]
    assert "Talked with Tom about the deck" in prompt and "When answering" not in prompt
    assert "Email Tom the numbers (due 2026-10-22)" in prompt

    async def broken(tool, args):
        raise RuntimeError("module down")

    assert asyncio.run(ti._module_inputs("alice", TODAY, broken)) == (None, [], [], None)


def test_action_noop_and_disabled(inputs, monkeypatch):
    from src.builtin_actions import TaskNoop

    async def fake_run(owner, **kw):
        return "noop", {}

    monkeypatch.setattr(ti, "run", fake_run)
    with pytest.raises(TaskNoop):
        asyncio.run(ti.action_today_insight("alice"))
    monkeypatch.setenv("TODAY_INSIGHT_ENABLED", "off")
    with pytest.raises(TaskNoop):
        asyncio.run(ti.action_today_insight("alice"))


def test_registered_as_silent_housekeeping_action():
    from src.builtin_actions import BUILTIN_ACTIONS
    from src.task_scheduler import HOUSEKEEPING_DEFAULTS, TaskScheduler

    assert HOUSEKEEPING_DEFAULTS[ti.ACTION]["cron_expression"] == "*/15 * * * *"
    assert HOUSEKEEPING_DEFAULTS[ti.ACTION]["name"] == "Today Insight"
    assert BUILTIN_ACTIONS[ti.ACTION] is ti.action_today_insight
    assert ti.ACTION in TaskScheduler._SILENT_ACTIONS
    assert ti.ACTION in TaskScheduler._MODEL_BACKED_ACTIONS


# ------------------------------------------------------------------ token route
def _client(state):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes.today_insight_routes import setup_today_insight_routes

    app = FastAPI()

    @app.middleware("http")
    async def fake_token(request, call_next):
        request.state.api_token = True
        request.state.current_user = "api"
        request.state.api_token_owner = "alice"
        request.state.api_token_scopes = state["scopes"]
        return await call_next(request)

    app.include_router(setup_today_insight_routes())
    return TestClient(app)


def test_token_route_scope(inputs):
    from routes.api_token_routes import ALLOWED_SCOPES

    assert "insight:read" in ALLOWED_SCOPES
    state = {"scopes": ["todos:read"]}
    client = _client(state)
    assert client.get("/api/codex/insight/today").status_code == 403
    state["scopes"] = ["todos:read", "insight:read"]
    r = client.get("/api/codex/insight/today")
    assert r.status_code == 404 and r.json() == {"error": "no insight yet"}
    asyncio.run(ti.run("alice", call=None, llm=FakeLLM(GOOD)))  # today, real clock
    r = client.get("/api/codex/insight/today")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"date", "generated", "headline", "focus", "coming_up", "also"}
    assert body["focus"][0]["title"] == "Send the Q4 deck"


def test_page_routes_refuse_api_tokens(inputs):
    client = _client({"scopes": ["insight:read"]})
    assert client.get("/api/insight/today").status_code in (401, 403)
    assert client.post("/api/insight/today/vote", json={"vote": "up"}).status_code in (401, 403)


# ------------------------------------------------------------------ real database
def test_gather_reads_the_owners_notes_events_and_timezone(tmp_path, monkeypatch):
    import core.database as cdb
    from tests.helpers.sqlite_db import make_temp_sqlite

    factory, engine, _tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    try:
        with factory() as db:
            db.add(cdb.CrewMember(id="c1", name="Assistant", owner="alice",
                                  is_default_assistant=True, timezone="Europe/Berlin"))
            db.add(cdb.CalendarCal(id="cal-a", owner="alice", name="Personal"))
            db.add(cdb.CalendarCal(id="cal-b", owner="bob", name="Bob's"))
            db.add(cdb.CalendarEvent(uid="ev-a", calendar_id="cal-a", summary="Dentist",
                                     dtstart=datetime(2026, 10, 21, 10), is_utc=True,
                                     dtend=datetime(2026, 10, 21, 11)))
            # weekly series started in September: Wed and Sat at 07:00 and 15:00 until
            # Sunday 25 Oct, Saturday 15:00 skipped. Today 07:00 is over (it's 09:00).
            db.add(cdb.CalendarEvent(uid="series-1", calendar_id="cal-a", summary="Team sync",
                                     dtstart=datetime(2026, 9, 2, 7), dtend=datetime(2026, 9, 2, 7, 30),
                                     rrule="FREQ=WEEKLY;BYDAY=WE,SA;BYHOUR=7,15;BYMINUTE=0;"
                                           "UNTIL=20261025T000000",
                                     recurrence_exdates=json.dumps(["2026-10-24T15:00"])))
            db.add(cdb.CalendarEvent(uid="series-gone", calendar_id="cal-a", summary="Cancelled",
                                     dtstart=datetime(2026, 9, 2, 8), dtend=datetime(2026, 9, 2, 9),
                                     rrule="FREQ=DAILY", status="cancelled"))
            db.add(cdb.CalendarEvent(uid="ev-b", calendar_id="cal-b", summary="Not Alice's",
                                     dtstart=datetime(2026, 10, 21, 12),
                                     dtend=datetime(2026, 10, 21, 13)))
            db.add(cdb.Note(id="n-a", owner="alice", title="Work", note_type="todo",
                            items=json.dumps([{"text": "Send the Q4 deck", "done": False}]),
                            due_date="2026-10-21T08:00:00+02:00"))
            db.add(cdb.Note(id="n-b", owner="bob", title="Bob", note_type="checklist",
                            items=json.dumps([{"text": "Bob's item", "done": False}])))
            db.commit()
        assert str(ti.owner_timezone("alice")) == "Europe/Berlin"
        got = asyncio.run(ti.gather("alice", now=NOW, call=None))
        assert [(c["ref"], c["bucket"], c["time"]) for c in got["candidates"]] == [
            ("todo:n-a:0", "due_today", None),
            ("event:ev-a", "due_today", "12:00"),
            ("event:series-1::2026-10-21T15:00", "due_today", "15:00"),
            ("event:series-1::2026-10-24T07:00", "due_soon", "07:00")]
        assert got["candidates"][2]["title"] == "Team sync"
    finally:
        engine.dispose()


# ------------------------------------------------------------------ sandbox defects
def _answer(**sections):
    return json.dumps({"headline": "A busy Wednesday.", **sections})


def test_labels_in_the_prompt_map_back_to_refs(inputs):
    llm = FakeLLM(_answer(focus=[{"ref": "T1", "why": "Overdue"}, {"ref": "e1", "why": "At 12"}],
                          also=[{"ref": "[T2]", "why": "Sunday"}, {"ref": "T9", "why": "no such"}]))
    status, insight = _run(llm=llm)
    assert status == "generated"
    assert [r["ref"] for r in insight["focus"]] == ["todo:n1:0", "event:e1"]
    assert [r["ref"] for r in insight["also"]] == ["todo:n4:0"]


def test_bare_ids_resolve_when_unique():
    cands = [{"ref": "todo:d4a48534-aaaa:0", "title": "Print flyers"},
             {"ref": "todo:d4a48534-aaaa:1", "title": "Email slides"},
             {"ref": "todo:c946d7f3-bbbb:0", "title": "Ask Alice"},
             {"ref": "event:56c59904-cccc", "title": "Plumber"},
             {"ref": "event:c0a50e0b-dddd::2026-10-22T09:30", "title": "Team sync"},
             {"ref": "event:allday-eeee::2026-10-23", "title": "Holiday"}]
    r = lambda raw: ti.resolve_ref(raw, cands)  # noqa: E731
    assert r("T2") == "todo:d4a48534-aaaa:1" and r("E2") == "event:c0a50e0b-dddd::2026-10-22T09:30"
    assert r("todo:c946d7f3-bbbb:0") == "todo:c946d7f3-bbbb:0"  # exact
    assert r("d4a48534-aaaa:0") == "todo:d4a48534-aaaa:0"  # prefix missing
    assert r("c946d7f3-bbbb") == "todo:c946d7f3-bbbb:0"  # bare note id, one item
    assert r("56c59904-cccc") == "event:56c59904-cccc"
    assert r("c0a50e0b-dddd") == "event:c0a50e0b-dddd::2026-10-22T09:30"  # bare series uid
    assert r("d4a48534-aaaa") is None  # two items: ambiguous
    assert r("2026-10-23") is None and r("2026-10-21T09:00") is None  # dates never match
    assert r("0") is None and r("") is None and r("T9") is None


def test_model_answering_with_bare_ids(inputs):
    inputs["notes"] = [_note("d4a48534-8bfd-4aa9", "Work", [("Send the Q4 deck", False)],
                             due_date="2026-10-20")]
    inputs["events"] = [_event("c0a50e0b-86ab::2026-10-21T15:00", datetime(2026, 10, 21, 15),
                               datetime(2026, 10, 21, 16), "Team sync")]
    llm = FakeLLM(_answer(focus=[{"ref": "d4a48534-8bfd-4aa9:0", "why": "Overdue"},
                                 {"ref": "c0a50e0b-86ab", "why": "At 15:00"},
                                 {"ref": "2026-10-23", "why": "a date"}]))
    status, insight = _run(llm=llm)
    assert status == "generated" and len(llm.calls) == 1
    assert [r["ref"] for r in insight["focus"]] == [
        "todo:d4a48534-8bfd-4aa9:0", "event:c0a50e0b-86ab::2026-10-21T15:00"]


def test_all_sections_empty_is_unusable(inputs, store):
    empty = _answer(focus=[{"ref": "2026-10-23", "why": "x"}], coming_up=[], also=[])
    llm = FakeLLM(empty, empty)
    assert _run(llm=llm) == ("kept", None) and len(llm.calls) == 2
    assert "labels" in llm.calls[1][-1]["content"]
    assert not (store / "today_insight" / "alice" / "2026-10-21.json").exists()
    _run(llm=FakeLLM(GOOD))
    inputs["memory"].append({"text": "Allergic to peanuts", "category": "fact"})
    status, kept = _run(llm=FakeLLM(empty, empty))
    assert status == "kept" and kept["focus"][0]["ref"] == "todo:n1:0"
    assert ti.load("alice", "2026-10-21")["focus"][0]["ref"] == "todo:n1:0"
    llm = FakeLLM(empty, GOOD)
    assert _run(llm=llm)[0] == "generated" and len(llm.calls) == 2
    assert asyncio.run(ti.generate_now("alice", llm=FakeLLM(empty, empty), now=NOW,
                                       call=None)) is None


def test_hash_ignores_memory_order(inputs):
    c = [{"ref": "todo:n:0", "title": "x"}]
    assert ti.input_hash(c, ["b", "a"], "d") == ti.input_hash(c, ["a", "b"], "d")
    assert ti.input_hash(c, ["a"], "d") != ti.input_hash(c, ["a", "c"], "d")
    inputs["memory"] = [{"text": "Tom is a colleague", "category": "contact", "uses": 1},
                        {"text": "Likes cats", "category": "fact", "uses": 2}]
    llm = FakeLLM(GOOD)
    assert _run(llm=llm)[0] == "generated"
    assert llm.calls[0][1]["content"].index("Likes cats") < \
        llm.calls[0][1]["content"].index("Tom is a colleague")  # prompt keeps most used first
    inputs["memory"][0]["uses"] = 9  # a chat used it: order changes, content doesn't
    assert _run(llm=llm)[0] == "noop" and len(llm.calls) == 1


def test_refresh_skips_the_interactive_quiet_wait(monkeypatch):
    import src.interactive_gate as gate
    import src.llm_core as llm_core
    import src.task_endpoint as te

    waited, sent = [], []

    async def fake_wait(label):
        waited.append(label)

    async def fake_call(cands, messages, **kw):
        sent.append(kw["workload"])
        return "{}"

    monkeypatch.setattr(gate, "wait_for_interactive_quiet", fake_wait)
    monkeypatch.setattr(llm_core, "llm_call_async_with_fallback", fake_call)
    monkeypatch.setattr(te, "resolve_task_candidates", lambda **kw: [("http://llm", "m", {})])

    async def go(flag):
        call = await ti.default_llm("alice", wait_quiet=flag)
        await call([{"role": "user", "content": "hi"}])

    asyncio.run(go(False))
    assert waited == [] and sent == ["foreground"]
    asyncio.run(go(True))
    assert waited == ["today insight"] and sent[-1] == "background"

    seen = {}

    async def fake_run(owner, **kw):
        seen.update(kw)
        return "generated", {"date": "2026-10-21"}

    monkeypatch.setattr(ti, "run", fake_run)
    asyncio.run(ti.generate_now("alice"))
    assert seen["wait_quiet"] is False and seen["force"] is True


def test_refresh_route_returns_202_and_runs_in_the_background(inputs, monkeypatch):
    import threading
    import time as _time

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routes.today_insight_routes as tir
    from core.middleware import require_admin

    release, calls = threading.Event(), []

    async def slow_generate(owner, **kw):
        calls.append((owner, kw))
        while not release.is_set():
            await asyncio.sleep(0.01)
        return {"generated": "new"}

    monkeypatch.setattr(ti, "generate_now", slow_generate)
    monkeypatch.setattr(tir, "_REFRESHES", {})
    monkeypatch.setattr(tir, "_REFRESH_ERRORS", {})
    asyncio.run(ti.run("alice", call=None, llm=FakeLLM(GOOD)))  # a previous insight, today
    previous = ti.load_today("alice")["generated"]

    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = "alice"
        request.state.api_token = False
        return await call_next(request)

    app.dependency_overrides[require_admin] = lambda: None
    app.include_router(tir.setup_today_insight_routes())
    with TestClient(app) as client:
        t0 = _time.monotonic()
        r = client.post("/api/insight/refresh")
        assert r.status_code == 202 and _time.monotonic() - t0 < 5
        assert r.json() == {"status": "started", "generated": previous}
        assert client.post("/api/insight/refresh").json()["status"] == "running"
        body = client.get("/api/insight/today").json()
        assert body["refreshing"] is True and body["refresh_error"] is None
        release.set()
        for _ in range(200):
            if not client.get("/api/insight/today").json()["refreshing"]:
                break
            _time.sleep(0.02)
        assert client.get("/api/insight/today").json()["refreshing"] is False
    assert calls == [("alice", {"wait_quiet": False})]


# ------------------------------------------------------------------ round 2
@pytest.mark.parametrize("message", [
    "What should I do first today?", "what's on my agenda this afternoon", "Plan my evening",
    "What are my priorities?", "what's next?", "What do I do now?", "what now",
    "Anything tonight?", "What should I focus on?", "Which task first?", "So what do I do?",
])
def test_wants_insight_for_plan_questions(message):
    assert ti.wants_insight(message)


@pytest.mark.parametrize("message", [
    # module questions (odysseus-sound bakeoff wording) must reach their tools
    "What did the doctor say I should do?", "What did Dr. Patel tell me to avoid?",
    "When is my next dentist appointment?", "What do I need to do before October 31?",
    "What did Maria ask me to do last week?", "What did I promise Ben?",
    "What did Alice say about the budget in September?", "What is Jonas allergic to?",
    # slash commands and slash-invoked skills
    "/sound what should I do first today?", "/context my priorities today",
    "Apply the skill below to my request, following its Procedure. --- today first",
    "", "   ",
])
def test_no_insight_for_other_questions(message):
    from src.skill_invocation import INVOKE_PREFIX

    if message.startswith("Apply the skill"):
        message = INVOKE_PREFIX + "---\nname: sound\n---\nwhat should I do first today"
    assert not ti.wants_insight(message)


def test_context_block_only_for_plan_questions(inputs):
    _run(llm=FakeLLM(GOOD))
    assert ti.context_block("alice", now=NOW, message="What should I do first today?")
    assert ti.context_block("alice", now=NOW, message="What did Dr. Patel tell me to avoid?") == ""
    assert ti.context_block("alice", now=NOW, message="/sound what should I do today?") == ""


def test_chat_preface_adds_the_block_only_for_plan_questions(inputs, monkeypatch):
    from src.chat_processor import ChatProcessor

    _run(llm=FakeLLM(GOOD))
    monkeypatch.setattr(ti, "_now_local", lambda tz, now=None: NOW.astimezone(tz))

    class _Memory:
        def load(self, owner=None):
            return []

    class _Docs:
        rag_manager = None

    def preface(message):
        out, _, _ = ChatProcessor(memory_manager=_Memory(), personal_docs_manager=_Docs()) \
            .build_context_preface(message=message, session=SimpleNamespace(), use_rag=False,
                                   use_memory=True, owner="alice")
        return "\n".join(m.get("content", "") for m in out)

    assert "Do first: 1. Send the Q4 deck" in preface("What should I do first today?")
    assert "Send the Q4 deck" not in preface("What did Dr. Patel tell me to avoid?")
    assert "Send the Q4 deck" not in preface("/sound what should I do first today?")


def test_memory_source_links_are_stripped(inputs):
    entries = [
        {"text": "Jonas is allergic to peanuts (source: http://10.0.0.5:8000/view/abc?sig=x#t=40)",
         "category": "contact", "source": "odysseus-sound"},
        {"text": "Mentions (source: http://x/y) in the middle stay", "category": "fact"},
        {"text": "Prefers tea (source: https://host/view/d?sig=1)  ", "category": "preference"},
    ]
    assert ti.select_memory(entries) == ["Jonas is allergic to peanuts",
                                         "Mentions (source: http://x/y) in the middle stay",
                                         "Prefers tea"]
    inputs["memory"] = [entries[0]]
    llm = FakeLLM(GOOD)
    assert _run(llm=llm)[0] == "generated"
    assert "source:" not in llm.calls[0][1]["content"]
    inputs["memory"] = [{**entries[0], "text": "Jonas is allergic to peanuts (source: "
                                               "http://10.0.0.5:8000/view/abc?sig=rotated)"}]
    assert _run(llm=llm)[0] == "noop"  # only the link changed: same hash


# ------------------------------------------------------------------ belief ranking (2.3)
def _seed_ledger(owner="alice"):
    from src import evidence_intake as ei

    facts = [(1, "Priya Nair is allergic to peanuts", "Priya Nair"),
             (2, "Priya Nair lives in Porto", "Priya Nair"),
             (3, "Tom is a colleague", "Tom"),
             (4, "Lena collects vintage maps", "Lena")]
    ei.submit(owner, [ei.Evidence(source="odysseus-sound", source_id=str(i), subject=s,
                                  about="person", text=t, link=f"http://h/view/c{i}?sig=x",
                                  seen_first="2026-10-01", seen_last="2026-10-15",
                                  times=3, days=2, source_confirmed=True)
                      for i, t, s in facts], now=NOW)


@pytest.fixture
def no_lane(monkeypatch):
    from src import belief_search

    monkeypatch.setattr(belief_search, "embedder", lambda: None)  # keyword ranking


def test_memory_query_from_candidates():
    cands = [{"title": "Lunch with Priya", "list": None, "location": "Casa Lupo"},
             {"title": "Send the Q4 deck", "list": "Work"}, {"title": "Lunch with Priya"}]
    assert ti.memory_query(cands) == "Lunch with Priya\nCasa Lupo\nSend the Q4 deck\nWork"
    assert ti.memory_query([]) == ""


def test_select_memory_ranks_the_days_beliefs_first(store, no_lane):
    from src.memory import MemoryManager

    _seed_ledger()
    entries = MemoryManager(str(store)).load("alice")
    entries.append({"text": "Prefers mornings for deep work", "category": "preference",
                    "uses": 50, "owner": "alice"})
    out = ti.select_memory(entries, owner="alice", query="Lunch with Priya\nSend the Q4 deck")
    assert set(out[:2]) == {"Priya Nair is allergic to peanuts", "Priya Nair lives in Porto"}
    assert out[2] == "Prefers mornings for deep work"  # then the rest, most used first
    assert sorted(out) == sorted(set(out)) and len(out) == 5  # no duplicates
    assert not any("source:" in t for t in out)
    # nothing in the ledger for the query, or no query: most used first, as before
    assert ti.select_memory(entries, owner="alice", query="") == ti.select_memory(entries)
    assert ti.select_memory(entries, owner="bob", query="Lunch with Priya") == \
        ti.select_memory(entries)
    assert len(ti.select_memory(entries, 3, owner="alice", query="Priya")) == 3


def test_ranked_memory_keeps_the_hash_stable(inputs, no_lane):
    _seed_ledger()
    inputs["notes"].append(_note("n9", "People", [("Lunch with Priya", False)],
                                 due_date="2026-10-21"))
    inputs["memory"] = [{"text": "Tom is a colleague (source: http://h/view/c3?sig=x)",
                         "category": "contact", "uses": 1},
                        {"text": "Likes cats", "category": "fact", "uses": 2}]
    llm = FakeLLM(GOOD)
    assert _run(llm=llm)[0] == "generated"
    prompt = llm.calls[0][1]["content"]
    assert prompt.index("Priya Nair is allergic to peanuts") < prompt.index("Likes cats")
    inputs["memory"][0]["uses"] = 9  # a chat used it: order changes, content doesn't
    assert _run(llm=llm)[0] == "noop" and len(llm.calls) == 1


def test_prompt_gives_the_date_not_the_time_of_day(inputs):
    """The headline is read all day (hub page, T5), so the prompt has no "now" time."""
    llm = FakeLLM(GOOD)
    _run(llm=llm)
    user = llm.calls[0][-1]["content"]
    first = user.splitlines()[0]
    assert first == "Today is Wednesday 2026-10-21."
    assert "current time" in llm.calls[0][0]["content"]
