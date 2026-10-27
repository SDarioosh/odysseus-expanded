"""Fork: "Send Athena" on calendar events (src/meeting_auto_join.py).

Fictional meetings only: numbers, passcodes, hosts and titles below are made up.
"""
import asyncio
import importlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.database as cdb
from core.middleware import require_admin
from src import call_recorder as rec
from src import meeting_auto_join as auto
from tests.helpers.sqlite_db import make_temp_sqlite

OWNER = "admin"
TOKEN = "t" * 32
ROWS = [("srv1", "convo", f"http://module:8001/mcp?access_token={TOKEN}", True)]
MID = "0123456789abcdef0123456789abcdef"
ZOOM = "https://us06web.zoom.us/j/85012345678?pwd=FictionalPwd1"
ZOOM2 = "https://acme.zoom.us/j/9123456789"
NOW = datetime(2099, 3, 2, 9, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)  # naive UTC


class Notes:
    def __init__(self):
        self.items = []

    def add_notification(self, task_name, status, task_id=None, owner=None, body=None):
        self.items.append({"task_name": task_name, "status": status, "task_id": task_id,
                           "owner": owner, "body": body})


@pytest.fixture
def env(monkeypatch, tmp_path):
    factory, engine, _ = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(auto, "_utcnow", lambda: NOW)
    notes = Notes()
    import src.event_bus as bus
    monkeypatch.setattr(bus, "_task_scheduler", notes)
    with factory() as db:
        db.add(cdb.CalendarCal(id="cal1", owner=OWNER, name="Work", color="#5b8abf"))
        db.add(cdb.CalendarCal(id="cal2", owner="someone-else", name="Theirs",
                               color="#5b8abf"))
        db.commit()
    auto._RUNNING.clear()
    env = type("Env", (), {})()
    env.db = factory
    env.notes = notes
    yield env
    engine.dispose()


def add_event(env, uid, start, minutes=30, location=ZOOM, description="", rrule="",
              summary="Fictional sync", cal="cal1", all_day=False):
    with env.db() as db:
        db.add(cdb.CalendarEvent(uid=uid, calendar_id=cal, summary=summary,
                                 location=location, description=description,
                                 dtstart=start, dtend=start + timedelta(minutes=minutes),
                                 all_day=all_day, is_utc=True, rrule=rrule))
        db.commit()


def move_event(env, uid, start, minutes=30):
    with env.db() as db:
        ev = db.query(cdb.CalendarEvent).filter_by(uid=uid).one()
        ev.dtstart, ev.dtend = start, start + timedelta(minutes=minutes)
        db.commit()


def delete_event(env, uid):
    with env.db() as db:
        db.query(cdb.CalendarEvent).filter_by(uid=uid).delete()
        db.commit()


def tasks(env):
    with env.db() as db:
        return [(t.id, t.prompt, t.next_run, t.status, t.action)
                for t in db.query(cdb.ScheduledTask).all()]


def _real_utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ toggle and tasks
def test_toggle_off_by_default_creates_no_task(env):
    add_event(env, "e1", NOW + timedelta(hours=2))
    info = auto.describe(OWNER, "e1")
    assert info["eligible"] and info["zoom"] and not info["on"]
    assert auto.reconcile(OWNER) == {"scheduled": 0, "busy": 0, "cancelled": 0}
    run(auto.after_calendar_change(OWNER))
    assert tasks(env) == []


def test_toggle_on_creates_one_task_a_minute_before(env):
    start = NOW + timedelta(hours=2)
    add_event(env, "e1", start)
    info = auto.set_toggle(OWNER, "e1", True)
    assert info["on"] and info["start"] == "2099-03-02T11:00:00Z"
    assert info["due"] == "2099-03-02T10:59:00Z"
    rows = tasks(env)
    assert len(rows) == 1
    tid, prompt, next_run, status, action = rows[0]
    assert (prompt, next_run, status, action) == ("e1", start - timedelta(minutes=1),
                                                  "active", auto.ACTION)
    assert info["task_id"] == tid
    # Idempotent: again and again, still one task with the same id.
    auto.set_toggle(OWNER, "e1", True)
    auto.reconcile(OWNER)
    assert [r[0] for r in tasks(env)] == [tid]


def test_task_authority_admits_the_action(env):
    add_event(env, "e1", NOW + timedelta(hours=2))
    auto.set_toggle(OWNER, "e1", True)
    from src.agent_runtime.authority import restore_task_authority, task_operation
    with env.db() as db:
        t = db.query(cdb.ScheduledTask).one()
        authority = restore_task_authority(t.request_authority_json, t.prompt, t.task_type,
                                           t.action, owner=t.owner)
        assert authority.permits(task_operation(t.task_type, t.action, t.prompt))
        assert t.schedule == "once" and t.notifications_enabled is False


def test_moved_event_reschedules_the_same_task(env):
    add_event(env, "e1", NOW + timedelta(hours=2))
    auto.set_toggle(OWNER, "e1", True)
    tid = tasks(env)[0][0]
    later = NOW + timedelta(days=1, hours=3)
    move_event(env, "e1", later)
    run(auto.after_calendar_change(OWNER))
    rows = tasks(env)
    assert [(r[0], r[2]) for r in rows] == [(tid, later - timedelta(minutes=1))]


def test_deleted_event_cancels_the_task(env):
    add_event(env, "e1", NOW + timedelta(hours=2))
    auto.set_toggle(OWNER, "e1", True)
    delete_event(env, "e1")
    run(auto.after_calendar_change(OWNER))
    assert tasks(env) == []
    assert auto.load(OWNER)["events"] == {}


def test_toggle_off_cancels_the_task(env):
    add_event(env, "e1", NOW + timedelta(hours=2))
    auto.set_toggle(OWNER, "e1", True)
    info = auto.set_toggle(OWNER, "e1", False)
    assert not info["on"] and info["task_id"] is None
    assert tasks(env) == []


def test_removed_zoom_link_cancels_the_task(env):
    add_event(env, "e1", NOW + timedelta(hours=2))
    auto.set_toggle(OWNER, "e1", True)
    with env.db() as db:
        db.query(cdb.CalendarEvent).filter_by(uid="e1").one().location = "Room 4"
        db.commit()
    auto.reconcile(OWNER)
    assert tasks(env) == []


def test_event_without_zoom_link_gets_no_toggle_or_task(env):
    add_event(env, "e1", NOW + timedelta(hours=2), location="Room 4",
              description="Meeting ID: 850 1234 5678")  # a bare ID line isn't a link
    info = auto.describe(OWNER, "e1")
    assert not info["eligible"] and not info["zoom"]
    with pytest.raises(ValueError):
        auto.set_toggle(OWNER, "e1", True)
    assert tasks(env) == []


def test_unjoinable_zoom_link_says_why(env):
    add_event(env, "e1", NOW + timedelta(hours=2), location="https://zoom.us/w/123456789")
    info = auto.describe(OWNER, "e1")
    assert info["zoom"] and not info["eligible"] and "Webinar" in info["reason"]


def test_link_in_the_description_counts(env):
    add_event(env, "e1", NOW + timedelta(hours=2), location="",
              description=f"Join Zoom Meeting\n{ZOOM2}\nMeeting ID: 912 345 6789")
    assert auto.describe(OWNER, "e1")["eligible"]


def test_other_owners_event_is_not_found(env):
    add_event(env, "theirs", NOW + timedelta(hours=2), cal="cal2")
    with pytest.raises(LookupError):
        auto.describe(OWNER, "theirs")


def test_overlap_earlier_wins_and_later_is_told_once(env):
    add_event(env, "a", NOW + timedelta(hours=2), minutes=60, summary="Fictional planning")
    add_event(env, "b", NOW + timedelta(hours=2, minutes=30), location=ZOOM2,
              summary="Fictional review")
    auto.set_toggle(OWNER, "b", True)
    assert [r[1] for r in tasks(env)] == ["b"]
    auto.set_toggle(OWNER, "a", True)
    assert [r[1] for r in tasks(env)] == ["a"]
    busy = [n for n in env.notes.items if n["status"] == "error"]
    assert len(busy) == 1 and busy[0]["owner"] == OWNER
    assert "Fictional planning" in busy[0]["body"] and "Fictional review" in busy[0]["body"]
    assert auto.describe(OWNER, "b")["busy"] == "2099-03-02T11:30:00Z"
    auto.reconcile(OWNER)
    run(auto.after_calendar_change(OWNER))
    assert len([n for n in env.notes.items if n["status"] == "error"]) == 1  # not again
    # The earlier one goes away: the later one gets its task back.
    auto.set_toggle(OWNER, "a", False)
    assert [r[1] for r in tasks(env)] == ["b"]


def test_repeating_meeting_schedules_the_next_occurrence(env):
    first = NOW - timedelta(days=1) + timedelta(hours=2)  # yesterday 11:00
    add_event(env, "r1", first, rrule="FREQ=DAILY;COUNT=5")
    info = auto.set_toggle(OWNER, "r1", True)
    assert info["start"] == "2099-03-02T11:00:00Z"
    with env.db() as db:
        db.query(cdb.CalendarEvent).filter_by(uid="r1").one().recurrence_exdates = \
            json.dumps(["2099-03-02T11:00"])
        db.commit()
    auto.reconcile(OWNER)
    assert auto.describe(OWNER, "r1")["start"] == "2099-03-03T11:00:00Z"
    assert len(tasks(env)) == 1


def test_all_day_event_is_not_eligible(env):
    add_event(env, "d1", NOW + timedelta(days=1), minutes=24 * 60, all_day=True)
    info = auto.describe(OWNER, "d1")
    assert info["zoom"] and not info["eligible"]


def test_occurrence_uid_maps_to_the_series(env):
    add_event(env, "r1", NOW + timedelta(hours=2), rrule="FREQ=WEEKLY")
    info = auto.set_toggle(OWNER, "r1::2099-03-02T11:00", True)
    assert info["uid"] == "r1" and info["on"]


# ------------------------------------------------------------------ the task's run
class Module:
    def __init__(self, status=202, body=None):
        self.status = status
        self.body = body if body is not None else {"id": MID, "state": "starting"}
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def module(monkeypatch):
    monkeypatch.delenv(rec.SERVER_ENV, raising=False)
    monkeypatch.setattr(rec, "_server_rows", lambda: ROWS)

    def use(status=202, body=None):
        fake = Module(status, body)
        monkeypatch.setattr(auto, "TRANSPORT", httpx.MockTransport(fake))
        return fake

    return use


def fire(env, uid, minutes_before=1):
    """Run the task as the scheduler would, at its due time."""
    start = auto._parse_iso(auto.describe(OWNER, uid)["start"])
    at = start - timedelta(minutes=minutes_before)
    auto._utcnow = lambda: at
    try:
        return run(auto.action_meeting_auto_join(owner=OWNER, prompt=uid))
    finally:
        auto._utcnow = lambda: NOW


def test_run_joins_through_meeting_bot(env, module, monkeypatch):
    fake = module()
    add_event(env, "e1", NOW + timedelta(hours=2), summary="Fictional sync")
    auto.set_toggle(OWNER, "e1", True)
    monkeypatch.setattr(auto, "_utcnow", lambda: NOW)
    text, ok = fire(env, "e1")
    assert ok and "Fictional sync" in text and "Join the meeting too" in text
    sent = json.loads(fake.requests[0].content)
    assert sent == {"url": ZOOM, "passcode": "FictionalPwd1", "title": "Fictional sync"}
    assert fake.requests[0].url.path == "/meetings"
    assert fake.requests[0].url.params["access_token"] == TOKEN
    assert any(n["status"] == "success" for n in env.notes.items)
    assert auto.load(OWNER)["events"]["e1"]["joined"] == "2099-03-02T11:00:00Z"


@pytest.mark.parametrize("status,body,code_word", [
    (409, {"detail": "busy", "active": MID}, "busy in another meeting"),
    (501, {"detail": "zoom_disabled"}, "Meetings aren't set up"),
    (503, {"detail": "zoom_signin_needed"}, "sign in to Zoom on the Record a call page"),
])
def test_run_maps_errors_to_a_failed_run(env, module, monkeypatch, status, body, code_word):
    module(status, body)
    add_event(env, "e1", NOW + timedelta(hours=2), summary="Fictional sync")
    auto.set_toggle(OWNER, "e1", True)
    text, ok = fire(env, "e1")
    assert not ok and code_word in text and "Fictional sync" in text
    # The scheduler notifies a failed run itself (error notification with this text).
    assert not any(n["status"] == "success" for n in env.notes.items)


def test_run_for_a_moved_event_reschedules_instead(env, module):
    from src.builtin_actions import TaskNoop

    fake = module()
    add_event(env, "e1", NOW + timedelta(hours=2))
    auto.set_toggle(OWNER, "e1", True)
    tid = tasks(env)[0][0]
    move_event(env, "e1", NOW + timedelta(hours=5))  # moved locally, no hook ran
    auto._utcnow = lambda: NOW + timedelta(hours=1, minutes=59)
    try:
        with pytest.raises(TaskNoop):
            run(auto.action_meeting_auto_join(owner=OWNER, prompt="e1"))
    finally:
        auto._utcnow = lambda: NOW
    assert fake.requests == []
    assert [(r[0], r[2]) for r in tasks(env)] == [(tid, NOW + timedelta(hours=4, minutes=59))]


def test_run_when_toggled_off_does_nothing(env, module):
    from src.builtin_actions import TaskNoop

    fake = module()
    with pytest.raises(TaskNoop):
        run(auto.action_meeting_auto_join(owner=OWNER, prompt="nope"))
    assert fake.requests == []


def test_run_of_a_repeating_meeting_schedules_the_next(env, module):
    module()
    add_event(env, "r1", NOW + timedelta(hours=2), rrule="FREQ=DAILY;COUNT=3")
    auto.set_toggle(OWNER, "r1", True)
    first = tasks(env)[0][0]
    auto._RUNNING.add(first)  # what the run does while it joins
    _, ok = fire(env, "r1")
    assert ok
    rows = {r[0]: r for r in tasks(env)}
    assert first in rows  # the running row is never deleted
    nxt = [r for r in rows.values() if r[0] != first]
    assert len(nxt) == 1 and nxt[0][2] == NOW + timedelta(days=1, hours=2, minutes=-1)


def test_registered_as_a_builtin_action():
    from src.builtin_actions import BUILTIN_ACTIONS
    assert BUILTIN_ACTIONS[auto.ACTION] is auto.action_meeting_auto_join


def test_time_critical_only_for_this_action():
    task = type("T", (), {"task_type": "action", "action": auto.ACTION})()
    other = type("T", (), {"task_type": "action", "action": "tidy_sessions"})()
    assert auto.is_time_critical(task) and not auto.is_time_critical(other)


def test_scheduler_runs_a_due_join_while_odysseus_is_in_use(env, monkeypatch):
    """The scheduler's 15-minute deferral for foreground use skips Send Athena tasks,
    and dispatches them outside the model queue (bypass_model_slot)."""
    from src import interactive_gate
    from src import task_scheduler as ts

    add_event(env, "e1", NOW + timedelta(hours=2))
    add_event(env, "e2", NOW + timedelta(hours=5), location=ZOOM2)
    auto.set_toggle(OWNER, "e1", True)
    with env.db() as db:
        t = db.query(cdb.ScheduledTask).one()
        t.next_run = _real_utcnow() - timedelta(seconds=5)
        db.add(cdb.ScheduledTask(id="other1", owner=OWNER, name="Chat Sessions Tidy",
                                 task_type="action", action="tidy_sessions",
                                 status="active",
                                 next_run=_real_utcnow() - timedelta(seconds=5)))
        db.commit()
        join_id = t.id
    monkeypatch.setattr(interactive_gate, "has_foreground_activity", lambda: True)
    calls = []

    async def fake_execute(self, task_id, *, bypass_model_slot=False, release_executing=True):
        calls.append((task_id, bypass_model_slot))

    monkeypatch.setattr(ts.TaskScheduler, "_execute_task", fake_execute)
    scheduler = ts.TaskScheduler(None)

    async def go():
        await scheduler._check_due_tasks()
        await asyncio.sleep(0)
    run(go())
    assert calls == [(join_id, True)]
    with env.db() as db:
        other = db.query(cdb.ScheduledTask).filter_by(id="other1").one()
        assert other.next_run > _real_utcnow() + timedelta(minutes=10)  # deferred


# ------------------------------------------------------------------ routes
@pytest.fixture
def client(env):
    import routes.sound_module_routes as smr
    from routes.sound_module_routes import setup_sound_module_routes

    app = FastAPI()
    app.include_router(setup_sound_module_routes())
    app.dependency_overrides[require_admin] = lambda: None
    smr_owner = smr._owner
    smr._owner = lambda request: OWNER
    try:
        with TestClient(app) as c:
            yield c
    finally:
        smr._owner = smr_owner


H = {"X-Odysseus-Recorder": "1"}


def test_routes_read_and_set_the_toggle(env, client):
    add_event(env, "e1", NOW + timedelta(hours=2))
    r = client.get("/api/modules/sound/auto-join", params={"uid": "e1"})
    assert r.status_code == 200 and r.json()["eligible"] and not r.json()["on"]
    r = client.put("/api/modules/sound/auto-join", json={"uid": "e1", "on": True})
    assert r.status_code == 400  # header missing
    r = client.put("/api/modules/sound/auto-join", json={"uid": "e1", "on": True}, headers=H)
    assert r.status_code == 200 and r.json()["on"] and r.json()["due"]
    assert len(tasks(env)) == 1
    r = client.get("/api/modules/sound/auto-join", params={"uid": "missing"})
    assert r.status_code == 404
    add_event(env, "e2", NOW + timedelta(hours=3), location="Room 4")
    r = client.put("/api/modules/sound/auto-join", json={"uid": "e2", "on": True}, headers=H)
    assert r.status_code == 400


def test_calendar_route_delete_cancels_the_task(env, monkeypatch):
    # A fresh import against the real core.database: another test
    # (test_calendar_owner_scope.py) imports calendar_routes against fake models and
    # leaves that copy in sys.modules when it ran first.
    monkeypatch.delitem(sys.modules, "routes.calendar_routes", raising=False)
    cr = importlib.import_module("routes.calendar_routes")

    monkeypatch.setattr(cr, "SessionLocal", cdb.SessionLocal, raising=False)
    add_event(env, "e1", NOW + timedelta(hours=2))
    auto.set_toggle(OWNER, "e1", True)
    app = FastAPI()
    app.include_router(cr.setup_calendar_routes())
    monkeypatch.setattr(cr, "_require_user", lambda request: OWNER)
    with TestClient(app) as c:
        r = c.delete("/api/calendar/events/e1")
        assert r.status_code == 200, r.text
    assert tasks(env) == []


def test_importing_starts_nothing():
    """No thread, timer or poll: tasks change only when the calendar or a toggle does."""
    code = ("import threading; n = threading.active_count(); import src.meeting_auto_join; "
            "assert threading.active_count() == n, threading.enumerate()")
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True, timeout=60)
