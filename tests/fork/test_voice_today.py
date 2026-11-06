"""Fork: Today for voice devices (fork/voice/today_route.py). Fictional data only."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fork.insight.today_candidates import todo_candidates
from fork.voice import today_route as tr
from fork.voice.routes import PREFIX

TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 6, 7, 45, tzinfo=TZ)
TODAY = NOW.date()


def note(id, title, items, *, due=None, pinned=False, note_type="checklist"):
    return SimpleNamespace(id=id, title=title, note_type=note_type, archived=False,
                           source=None, label=None, due_date=due, pinned=pinned,
                           items=json.dumps(items))


def todos(notes):
    return todo_candidates(notes, TODAY, TZ, names={"shopping"})


NOTES = [
    note(41, "Home", [{"text": "Call the plumber", "done": False},
                      {"text": "Fix the gate", "done": True}], due="2026-11-06"),
    note(42, "Garden", [{"text": "Rake leaves", "done": False}], due="2026-11-02"),
    note(43, "Ideas", [{"text": "Paint the shed", "done": False}], pinned=True),
    note(44, "Someday", [{"text": "Learn the banjo", "done": False}]),
    note(45, "Trip", [{"text": "Book the ferry", "done": False}], due="2026-11-20"),
]


def test_empty_sections_shape():
    out = tr.shape_today(now_local=NOW)
    assert out == {"date": "2026-11-06", "generated": "2026-11-06T07:45:00-05:00",
                   "headline": None, "focus": [], "events": [], "todos": [],
                   "action_items": [], "counts": {"todos_open": 0, "action_items_open": 0}}


def test_todos_due_first_then_pinned_undated():
    out = tr.shape_today(now_local=NOW, todo_candidates=todos(NOTES))
    assert out["todos"] == [
        {"ref": "todo:42:0", "title": "Rake leaves", "due": "2026-11-02", "list": "Garden"},
        {"ref": "todo:41:0", "title": "Call the plumber", "due": "2026-11-06", "list": "Home"},
        {"ref": "todo:43:0", "title": "Paint the shed", "due": None, "list": "Ideas"},
    ]
    assert out["counts"]["todos_open"] == 5


def test_todos_capped_at_eight():
    many = [note(100 + i, "Chores", [{"text": f"Chore {i}", "done": False}], due="2026-11-06")
            for i in range(12)]
    out = tr.shape_today(now_local=NOW, todo_candidates=todos(many))
    assert len(out["todos"]) == 8
    assert out["counts"]["todos_open"] == 12


def test_events_today_only_sorted_and_capped():
    occ = [
        {"uid": "dentist", "summary": "Dentist", "dtstart": "2026-11-06T20:00:00Z",
         "dtend": "2026-11-06T20:30:00Z", "all_day": False, "location": "Main St"},
        {"uid": "standup::2026-11-06T14:00:00", "summary": "Standup",
         "dtstart": "2026-11-06T14:00:00Z", "dtend": "2026-11-06T14:15:00Z",
         "all_day": False, "location": ""},
        {"uid": "holiday", "summary": "Bin day", "dtstart": "2026-11-06",
         "dtend": "2026-11-07", "all_day": True},
        {"uid": "tomorrow", "summary": "Tomorrow", "dtstart": "2026-11-07T15:00:00Z",
         "dtend": "2026-11-07T16:00:00Z", "all_day": False},
        {"uid": "yesterday", "summary": "Old", "dtstart": "2026-11-05",
         "dtend": "2026-11-06", "all_day": True},
        {"uid": "gone", "summary": "Cancelled", "dtstart": "2026-11-06T18:00:00Z",
         "dtend": "2026-11-06T19:00:00Z", "status": "cancelled"},
    ]
    out = tr.shape_today(now_local=NOW, occurrences=occ)["events"]
    assert [e["uid"] for e in out] == ["holiday", "standup::2026-11-06T14:00:00", "dentist"]
    assert out[0] == {"uid": "holiday", "title": "Bin day",
                      "start": "2026-11-06T00:00:00-05:00", "end": "2026-11-07T00:00:00-05:00",
                      "all_day": True, "location": None}
    assert out[2] == {"uid": "dentist", "title": "Dentist",
                      "start": "2026-11-06T15:00:00-05:00", "end": "2026-11-06T15:30:00-05:00",
                      "all_day": False, "location": "Main St"}
    many = [{"uid": f"e{i}", "summary": f"E{i}", "dtstart": f"2026-11-06T{10 + i}:00:00",
             "dtend": f"2026-11-06T{10 + i}:30:00", "all_day": False} for i in range(12)]
    assert len(tr.shape_today(now_local=NOW, occurrences=many)["events"]) == 10


def test_action_items_open_unfiled_capped():
    rows = [{"id": f"ai_{i}", "text": f"Send Sam photo {i}", "status": "open"} for i in range(8)]
    rows.append({"id": "ai_done", "text": "Done thing", "status": "done"})
    rows[0]["title"] = "Send Sam the photos"
    out = tr.shape_today(now_local=NOW, action_rows=rows, filed={"ai_1"})
    assert out["action_items"][0] == {"id": "ai_0", "title": "Send Sam the photos",
                                      "source": "athena"}
    assert len(out["action_items"]) == 5
    assert "ai_1" not in {a["id"] for a in out["action_items"]}
    assert out["counts"]["action_items_open"] == 7


def test_headline_and_focus_drop_ticked_todos():
    insight = {"headline": "Two meetings this afternoon.",
               "focus": [{"ref": "todo:41:0", "title": "Call the plumber", "why": "Due today"},
                         {"ref": "todo:41:1", "title": "Fix the gate", "why": "Ticked"},
                         {"ref": "event:dentist", "title": "Dentist", "why": "At 3"}],
               "coming_up": [{"ref": "x"}]}
    out = tr.shape_today(now_local=NOW, insight=insight, todo_candidates=todos(NOTES))
    assert out["headline"] == "Two meetings this afternoon."
    assert out["focus"] == [
        {"ref": "todo:41:0", "title": "Call the plumber", "why": "Due today"},
        {"ref": "event:dentist", "title": "Dentist", "why": "At 3"},
    ]
    # to-dos unreadable: focus kept as written
    assert len(tr.shape_today(now_local=NOW, insight=insight)["focus"]) == 3


# ------------------------------------------------------------------- loading, route
@pytest.fixture
def loaders(monkeypatch):
    state = SimpleNamespace(fail=set(), athena_delay=0.0)

    def maybe_fail(name, value):
        if name in state.fail:
            raise RuntimeError(f"{name} broke")
        return value

    async def rows(owner):
        if state.athena_delay:
            await asyncio.sleep(state.athena_delay)
        return maybe_fail("athena", [{"id": "ai_17", "text": "Send Sam the photos",
                                      "status": "open"}])

    monkeypatch.setattr(tr, "_now_local", lambda owner, now=None: NOW)
    monkeypatch.setattr(tr, "load_insight", lambda o, n: maybe_fail(
        "insight", {"headline": "A quiet day.", "focus": []}))
    monkeypatch.setattr(tr, "load_todos", lambda o, n: maybe_fail("todos", todos(NOTES)))
    monkeypatch.setattr(tr, "load_events", lambda o, n: maybe_fail("events", [
        {"uid": "dentist", "summary": "Dentist", "dtstart": "2026-11-06T20:00:00Z",
         "dtend": "2026-11-06T20:30:00Z", "all_day": False}]))
    monkeypatch.setattr(tr, "load_filed", lambda o: maybe_fail("filed", set()))
    monkeypatch.setattr(tr, "load_action_rows", rows)
    monkeypatch.setattr(tr, "ATHENA_TIMEOUT", 0.2)
    return state


def test_build_today_all_sections(loaders):
    out = asyncio.run(tr.build_today("alice"))
    assert out["headline"] == "A quiet day."
    assert [e["uid"] for e in out["events"]] == ["dentist"]
    assert len(out["todos"]) == 3
    assert out["action_items"] == [{"id": "ai_17", "title": "Send Sam the photos",
                                    "source": "athena"}]


def test_each_section_fails_on_its_own(loaders, caplog):
    loaders.fail = {"insight", "todos", "events", "filed", "athena"}
    out = asyncio.run(tr.build_today("alice"))
    assert out["headline"] is None and out["focus"] == [] and out["events"] == []
    assert out["todos"] == [] and out["action_items"] == []
    assert "voice today" in caplog.text


def test_slow_athena_is_left_out(loaders):
    loaders.athena_delay = 1.0
    out = asyncio.run(tr.build_today("alice"))
    assert out["action_items"] == [] and len(out["events"]) == 1


def _client(router=None):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        request.state.api_token_owner = "alice" if scopes is not None else None
        request.state.api_token_scopes = scopes.split(",") if scopes else []
        return await call_next(request)

    app.include_router(router or tr.setup_voice_today_routes())
    return TestClient(app)


def test_route_needs_voice_scope_and_answers_200(loaders):
    client = _client()
    assert client.get(PREFIX + "/today").status_code == 403
    assert client.get(PREFIX + "/today", headers={"x-test-scopes": "todos:read"}).status_code == 403
    loaders.fail = {"insight", "todos", "events", "athena"}
    r = client.get(PREFIX + "/today", headers={"x-test-scopes": "voice"})
    assert r.status_code == 200
    assert set(r.json()) == {"date", "generated", "headline", "focus", "events", "todos",
                             "action_items", "counts"}


def test_load_action_rows_reads_module_answer(monkeypatch):
    from fork.athena import call_recorder as rec

    calls = []

    async def fake(method, path, **kw):
        calls.append((method, path, kw))
        return 200, {"action_items": [{"id": 3, "text": "Water plants", "status": "open"}, 7]}

    monkeypatch.setattr(rec, "module_json", fake)
    assert asyncio.run(tr.load_action_rows("alice")) == [
        {"id": 3, "text": "Water plants", "status": "open"}]
    assert calls == [("GET", "/action-items", {"params": {"status": "open"}})]

    async def down(method, path, **kw):
        return 503, {"detail": "down"}

    monkeypatch.setattr(rec, "module_json", down)
    with pytest.raises(RuntimeError):
        asyncio.run(tr.load_action_rows("alice"))


def test_loaders_use_owner_day(monkeypatch, data_dir):
    """The real loaders: today's stored insight in the owner's order."""
    from fork.insight import today as ti

    ti.save("alice", {"date": "2026-11-06", "generated": "2026-11-06T12:00:00Z",
                      "headline": "Hello", "focus": [], "coming_up": [], "also": [],
                      "input_hash": "x", "votes": []})
    assert tr.load_insight("alice", NOW)["headline"] == "Hello"
    assert tr.load_insight("alice", NOW + timedelta(days=1)) is None
    assert date.fromisoformat(tr.shape_today(now_local=NOW)["date"]) == TODAY


def test_voice_router_serves_today(loaders):
    from fork.voice.routes import setup_voice_routes

    client = _client(setup_voice_routes())
    r = client.get(PREFIX + "/today", headers={"x-test-scopes": "voice"})
    assert r.status_code == 200 and r.json()["date"] == "2026-11-06"


# ------------------------------------------------------------------- edge cases
def _calendar(monkeypatch, *events):
    import core.database as cdb
    from tests.fork.brain_fakes import notes_db

    factory = notes_db(monkeypatch)
    with factory() as db:
        db.add(cdb.CalendarCal(id="cal-a", owner="alice", name="Personal"))
        db.add(cdb.CalendarCal(id="cal-b", owner="bob", name="Bob's"))
        for ev in events:
            db.add(cdb.CalendarEvent(**ev))
        db.commit()
    return factory


def test_a_long_event_that_started_days_ago_is_today(monkeypatch, data_dir):
    """A week's holiday (all day) and a conference (timed) that began before yesterday
    are still on today's page; one that ended yesterday and Bob's aren't."""
    _calendar(
        monkeypatch,
        dict(uid="holiday", calendar_id="cal-a", summary="Holiday", all_day=True,
             dtstart=datetime(2026, 11, 2), dtend=datetime(2026, 11, 10)),
        dict(uid="conf", calendar_id="cal-a", summary="Conference", is_utc=True,
             dtstart=datetime(2026, 11, 3, 14), dtend=datetime(2026, 11, 7, 22)),
        dict(uid="over", calendar_id="cal-a", summary="Over", all_day=True,
             dtstart=datetime(2026, 11, 1), dtend=datetime(2026, 11, 6)),
        dict(uid="bobs", calendar_id="cal-b", summary="Bob's trip", all_day=True,
             dtstart=datetime(2026, 11, 1), dtend=datetime(2026, 11, 20)),
    )
    out = tr.shape_today(now_local=NOW, occurrences=tr.load_events("alice", NOW))
    assert [(e["uid"], e["start"], e["end"]) for e in out["events"]] == [
        ("holiday", "2026-11-02T00:00:00-05:00", "2026-11-10T00:00:00-05:00"),
        ("conf", "2026-11-03T09:00:00-05:00", "2026-11-07T17:00:00-05:00"),
    ]


def test_all_day_events_on_a_dst_day_keep_local_midnights():
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 1, 12, 0, tzinfo=ny)  # clocks went back at 02:00
    occ = [{"uid": "fair", "summary": "Fair", "dtstart": "2026-11-01", "dtend": "2026-11-02",
            "all_day": True},
           {"uid": "late", "summary": "Late show", "dtstart": "2026-11-02T04:30:00Z",
            "dtend": "2026-11-02T05:30:00Z", "all_day": False},  # 23:30 local
           {"uid": "next", "summary": "Next day", "dtstart": "2026-11-02T05:00:00Z",
            "dtend": "2026-11-02T06:00:00Z", "all_day": False}]  # 00:00 on the 2nd
    out = tr.shape_today(now_local=now, occurrences=occ)
    assert [(e["uid"], e["start"], e["end"]) for e in out["events"]] == [
        ("fair", "2026-11-01T00:00:00-04:00", "2026-11-02T00:00:00-05:00"),
        ("late", "2026-11-01T23:30:00-05:00", "2026-11-02T00:30:00-05:00"),
    ]


def test_odd_occurrences_and_athena_rows_never_break_the_page():
    occ = [None, "x", {"uid": "", "dtstart": "2026-11-06"}, {"uid": "a", "dtstart": "soon"},
           {"uid": "b", "dtstart": "2026-11-06T10:00:00", "dtend": "bad", "summary": None}]
    rows = [None, {"id": None}, {"id": 1, "title": "   "}, {"id": 2, "text": ["x"]},
            {"id": 0, "title": "Zero", "status": None}]
    out = tr.shape_today(now_local=NOW, occurrences=occ, action_rows=rows)
    assert [(e["uid"], e["title"], e["start"], e["end"]) for e in out["events"]] == [
        ("b", "(no title)", "2026-11-06T10:00:00-05:00", "2026-11-06T10:00:00-05:00")]
    assert [a["id"] for a in out["action_items"]] == ["2", "0"]
    assert out["counts"]["action_items_open"] == 2


def test_build_today_reads_the_owner_zone_off_the_event_loop(monkeypatch):
    """The owner's zone is a database read: it runs in a worker thread, not on the loop."""
    import threading

    seen = []

    def now_local(owner, now=None):
        seen.append(threading.current_thread() is threading.main_thread())
        return NOW

    monkeypatch.setattr(tr, "_now_local", now_local)
    for name in ("load_insight", "load_todos", "load_events"):
        monkeypatch.setattr(tr, name, lambda o, n: None)
    monkeypatch.setattr(tr, "load_filed", lambda o: set())

    async def no_rows(owner):
        return []

    monkeypatch.setattr(tr, "load_action_rows", no_rows)
    assert asyncio.run(tr.build_today("alice"))["date"] == "2026-11-06"
    assert seen == [False]
