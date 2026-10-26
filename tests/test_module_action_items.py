"""Fork: odysseus-sound action items -> ntfy (src/module_action_items.py)."""
import datetime

import pytest

from src import module_action_items as bai

LIST = """3 all action items (times are Europe/Berlin). When answering, put the link next to each fact you use:
- #12 [open] Send Alice the revised spreadsheet (owner Me) due 2026-09-19 (Friday) — from "Q4 budget" 2026-09-15 12:00 http://h:8000/view/a?sig=x#t=40
- #13 [open] Pick up Aunt Rosa from the station due that afternoon — from "Dinner plans" 2026-10-16 22:00 http://h:8000/view/b?sig=y#t=16
- #14 [done] Review Ben's pull request — from "Standup" 2026-10-07 11:30 http://h:8000/view/c?sig=z#t=24"""

DIGEST = """2026-10-18: Dentist and dinner plans
You booked a crown and planned Mom's dinner.
* Crown on October 21
TODO: Pick up Aunt Rosa
Conversation: http://h:8000/view/b?sig=y
When answering, put the link next to each fact you use."""


def test_parse_action_items():
    items = bai.parse_action_items(LIST)
    assert [(i.id, i.status) for i in items] == [(12, "open"), (13, "open"), (14, "done")]
    a, b, c = items
    assert (a.text, a.owner, a.due_date, a.due) == (
        "Send Alice the revised spreadsheet", "Me", "2026-09-19", "Friday")
    assert (b.text, b.owner, b.due_date, b.due) == (
        "Pick up Aunt Rosa from the station", None, None, "that afternoon")
    assert a.link.endswith("#t=40") and a.conversation == "Q4 budget"
    assert (c.text, c.due) == ("Review Ben's pull request", None)
    assert bai.parse_action_items("No open action items found.") == []


class FakeModule:
    def __init__(self, text=LIST, digest=DIGEST):
        self.text, self.digest, self.calls = text, digest, []

    async def __call__(self, tool, args):
        self.calls.append((tool, args))
        if tool == "list_action_items":
            assert args == {"status": "open"}  # read live, never written by the sync
            return "\n".join(ln for ln in self.text.splitlines() if "[done]" not in ln)
        if tool == "get_daily_digest":
            return self.digest
        raise AssertionError(f"the sync must not call {tool}")


@pytest.fixture
def pushes(tmp_path, monkeypatch):
    from routes import note_routes

    sent = []

    async def fake_dispatch(title, body, key, owner="", settings_override=None, **kw):
        sent.append({"title": title, "body": body, "key": key, **(settings_override or {})})
        return {"ntfy_sent": True}

    monkeypatch.setattr(note_routes, "dispatch_reminder", fake_dispatch)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    for var in ("MODULE_NOTIFY_CHANNEL", "MODULE_DIGEST_HOUR",
                "BARVIS_NOTIFY_CHANNEL", "BARVIS_DIGEST_HOUR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MODULE_FILE_ITEMS", "off")  # the pre-filing tests; see `hub`
    return sent


EARLY = datetime.datetime(2026, 10, 19, 7, 0)  # before the digest hour


@pytest.mark.asyncio
async def test_first_run_remembers_the_backlog_then_pushes_only_new_items(pushes):
    module = FakeModule()
    assert await bai.sync("alice", module, now=EARLY) == {"new": 2, "pushed": 0}
    assert pushes == []
    module.text += ('\n- #15 [open] Book the restaurant due 2026-10-21 (Wednesday) — from '
                    '"Dinner" 2026-10-19 06:30 http://h:8000/view/d?sig=w#t=5')
    assert await bai.sync("alice", module, now=EARLY) == {"new": 1, "pushed": 1}
    (p,) = pushes
    assert p["title"] == "New action item" and "Book the restaurant (due 2026-10-21)" in p["body"]
    assert p["reminder_channel"] == "ntfy" and p["reminder_llm_synthesis"] is False
    assert await bai.sync("alice", module, now=EARLY) == {"new": 0, "pushed": 0}
    assert [c[0] for c in module.calls] == ["list_action_items"] * 3


@pytest.mark.asyncio
async def test_items_due_today_and_digest_are_pushed_once_a_day(pushes):
    module = FakeModule()
    await bai.sync("alice", module, now=EARLY)
    due_day = datetime.datetime(2026, 9, 19, 9, 0)  # #12 is due 2026-09-19
    await bai.sync("alice", module, now=due_day)
    titles = [p["title"] for p in pushes]
    assert titles == ["Action item due today", "Yesterday: Dentist and dinner plans"]
    assert "Send Alice the revised spreadsheet" in pushes[0]["body"]
    assert ("get_daily_digest", {"date": "2026-09-18"}) in module.calls
    assert "TODO: Pick up Aunt Rosa" in pushes[1]["body"]
    assert "When answering" not in pushes[1]["body"]
    await bai.sync("alice", module, now=due_day.replace(hour=20))
    assert len(pushes) == 2


@pytest.mark.asyncio
async def test_missing_digest_is_retried_and_notifications_can_be_off(pushes, monkeypatch):
    module = FakeModule(text="", digest="No digest for 2026-10-18. Try search_conversations.")
    morning = datetime.datetime(2026, 10, 19, 9, 0)
    await bai.sync("alice", module, now=morning)
    await bai.sync("alice", module, now=morning)
    assert len([c for c in module.calls if c[0] == "get_daily_digest"]) == 2 and pushes == []
    monkeypatch.setenv("MODULE_NOTIFY_CHANNEL", "off")
    module.digest = DIGEST
    await bai.sync("alice", module, now=morning)
    assert pushes == []


def test_settings_fall_back_to_former_barvis_names(monkeypatch):
    for var in ("MODULE_DIGEST_HOUR", "BARVIS_DIGEST_HOUR"):
        monkeypatch.delenv(var, raising=False)
    assert bai.digest_hour() == 8
    monkeypatch.setenv("BARVIS_DIGEST_HOUR", "6")
    assert bai.digest_hour() == 6
    monkeypatch.setenv("MODULE_DIGEST_HOUR", "7")
    assert bai.digest_hour() == 7


def test_module_server_lookup():
    class M:
        _connections = {"abc123": {"name": "convo", "status": "connected"},
                        "zzz": {"name": "other", "status": "connected"},
                        "off1": {"name": "convo2", "status": "error"}}
    assert bai.module_server_id(M(), "convo") == "abc123"
    assert bai.module_server_id(M(), "ABC123") == "abc123"
    assert bai.module_server_id(M(), "convo2") is None


def test_registered_as_builtin_task():
    from src.builtin_actions import BUILTIN_ACTION_INFO, BUILTIN_ACTIONS
    from src.task_scheduler import HOUSEKEEPING_DEFAULTS, TaskScheduler

    assert BUILTIN_ACTIONS[bai.ACTION] is bai.action_sync_action_items
    assert bai.ACTION in BUILTIN_ACTION_INFO
    assert HOUSEKEEPING_DEFAULTS[bai.ACTION]["ship_paused"] is True
    assert bai.ACTION in TaskScheduler._SILENT_ACTIONS


def test_parse_voice_note_and_due_word_items():
    a, b = bai.parse_action_items(
        '- #20 [open] Buy milk due 2026-10-17 (tomorrow) — from "Grocery list" (voice note) '
        "2026-10-16 08:00 http://h:8000/view/n1?sig=a#t=3\n"
        '- #22 [open] Check the due date with Ana due next week — from '
        '"Invoices" 2026-10-16 08:00 http://h:8000/view/x?sig=a#t=9')
    assert (a.id, a.text, a.due_date, a.due, a.conversation) == (
        20, "Buy milk", "2026-10-17", "tomorrow", "Grocery list")
    assert (b.text, b.due_date, b.due) == ("Check the due date with Ana", None, "next week")


# ------------------------------------------------------------------ filing (JSON)
import json  # noqa: E402

import core.database as cdb  # noqa: E402
from tests.helpers.sqlite_db import make_temp_sqlite  # noqa: E402

TZ = "Europe/Berlin"
LINK = "http://h:8000/view/{c}?sig=s#t=4"


def row(id, text, kind="task", *, note=True, status="open", due_date=None, due_time=None,
        time_unclear=False, created="2031-10-20T09:05:00+00:00", **extra):
    return {"id": id, "status": status, "kind": kind, "text": text, "owner": "Me",
            "due": None, "due_date": due_date, "due_time": due_time,
            "duration_minutes": None, "location": None, "time_unclear": time_unclear,
            "conversation_id": f"c{id}", "conversation_title": f"Note {id}",
            "conversation_kind": "note" if note else "conversation",
            "conversation_at": "2031-10-20T09:00:00+00:00", "created_at": created,
            "timezone": TZ, "link": LINK.format(c=f"c{id}"), **extra}


class JsonModule:
    """odysseus-sound with format="json" and update_action_item."""

    def __init__(self, rows=(), digest="No digest for that day."):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.digest, self.calls, self.updates = digest, [], []

    def add(self, r):
        self.rows[r["id"]] = dict(r)

    async def __call__(self, tool, args):
        self.calls.append((tool, args))
        if tool == "list_action_items":
            assert args == {"status": "all", "format": "json"}
            return json.dumps(list(self.rows.values()))
        if tool == "update_action_item":
            self.updates.append(dict(args))
            r = self.rows[args["item_id"]]
            for k in ("status", "kind", "due_date", "due_time", "list_name"):
                if args.get(k):
                    r[k] = None if args[k] == "none" else args[k]
            return f"Action item #{args['item_id']} is now {r['status']}."
        if tool == "get_daily_digest":
            return self.digest
        raise AssertionError(f"unexpected tool {tool}")


@pytest.fixture
def hub(pushes, monkeypatch):
    """A real Odysseus calendar + notes database in a temp file."""
    import routes.calendar_routes as cr
    from src.user_time import clear_user_time_context

    factory, engine, tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(cr, "SessionLocal", factory, raising=False)
    monkeypatch.delenv("MODULE_FILE_ITEMS", raising=False)
    monkeypatch.delenv("BARVIS_FILE_ITEMS", raising=False)
    monkeypatch.setattr(bai, "_LOCKS", {})
    clear_user_time_context()
    yield factory
    engine.dispose()


def events(factory):
    with factory() as db:
        return db.query(cdb.CalendarEvent).all()


def notes(factory, label=bai.LABEL):
    with factory() as db:
        return [n for n in db.query(cdb.Note).all() if n.label == label]


T0 = datetime.datetime(2031, 10, 20, 5, 0, tzinfo=datetime.timezone.utc)  # 07:00 Berlin


async def _start(module, at=T0):
    """First pass: remembers the backlog, files nothing."""
    stats = await bai.sync("alice", module, now=at)
    assert stats["filed"] == 0
    return stats


@pytest.mark.asyncio
async def test_first_run_files_nothing(hub, pushes):
    module = JsonModule([row(1, "Old voice note task", due_date="2031-10-22")])
    stats = await _start(module)
    assert stats["new"] == 1 and stats["pushed"] == 0
    assert events(hub) == [] and notes(hub) == [] and pushes == []
    assert bai.load_state("alice")["file_since"].startswith("2031-10-20T05:00")


@pytest.mark.asyncio
async def test_voice_note_event_goes_to_calendar_with_reminder(hub, pushes):
    module = JsonModule()
    await _start(module)
    module.add(row(12, "Meet Jason", "event", due_date="2031-10-22", due_time="12:00",
                   location="Cafe Blau"))
    stats = await bai.sync("alice", module, now=T0)
    assert stats["filed"] == 1 and stats["pushed"] == 1
    (ev,) = events(hub)
    # 12:00 in Berlin (CEST, +02:00) is 10:00 UTC; 60 minutes by default
    assert ev.is_utc and ev.dtstart == datetime.datetime(2031, 10, 22, 10, 0)
    assert ev.dtend == datetime.datetime(2031, 10, 22, 11, 0) and not ev.all_day
    assert ev.uid == bai._event_uid(12) and ev.location == "Cafe Blau"
    assert 'voice note "Note 12"' in ev.description and LINK.format(c="c12") in ev.description
    (reminder,) = notes(hub, "calendar")  # Odysseus' own reminder, 30 min before
    assert reminder.due_date == "2031-10-22T09:30:00Z"
    (p,) = pushes
    assert p["title"] == "Filed from your voice note"
    assert p["body"] == "📅 Meet Jason · Wed 22 Oct 12:00 → Calendar"
    assert (await bai.filed_state("alice"))[12] == {
        "target": "calendar", "ref": ev.uid, "open_url": f"/#event-{ev.uid}",
        "kind": "event", "at": "2031-10-20T05:00:00+00:00", "undone": False,
        "removed": False}


@pytest.mark.asyncio
async def test_event_without_time_is_all_day(hub, pushes):
    module = JsonModule()
    await _start(module)
    module.add(row(13, "Dentist", "event", due_date="2031-10-23"))
    await bai.sync("alice", module, now=T0)
    (ev,) = events(hub)
    assert ev.all_day and ev.dtstart == datetime.datetime(2031, 10, 23)
    assert notes(hub, "calendar") == []  # no reminder for all-day events
    assert pushes[0]["body"] == "📅 Dentist · Thu 23 Oct → Calendar"


@pytest.mark.asyncio
async def test_task_and_reminder_become_todos(hub, pushes):
    module = JsonModule()
    await _start(module)
    module.add(row(14, "Get the proposal done", "task", due_date="2031-10-22"))
    module.add(row(15, "Call Mom", "reminder", due_date="2031-10-20", due_time="17:00",
                   time_unclear=True))
    module.add(row(16, "Think about holidays", explicit=True))  # no date: "Voice notes"
    stats = await bai.sync("alice", module, now=T0)
    assert stats["filed"] == 3 and stats["pushed"] == 1 and events(hub) == []
    by_title = {n.title: n for n in notes(hub)}
    proposal, mom, holidays = (by_title[t] for t in
                               ("Get the proposal done", "Call Mom", "Voice notes"))
    assert proposal.note_type == "todo" and proposal.due_date == "2031-10-22T08:00:00+02:00"
    assert json.loads(proposal.items) == [{"text": "Get the proposal done", "done": False}]
    assert LINK.format(c="c14") in proposal.content
    assert mom.due_date == "2031-10-20T17:00:00+02:00"
    assert holidays.due_date is None and holidays.note_type == "todo"
    assert json.loads(holidays.items) == [{"text": "Think about holidays", "done": False}]
    (p,) = pushes  # one combined push
    assert p["title"] == "Filed 3 items from your voice notes"
    assert p["body"].splitlines() == [
        "☑️ Get the proposal done · due Wed 22 Oct → To-dos",
        "⏰ Call Mom · due Mon 20 Oct 17:00 → To-dos ⚠️ check the time",
        "☑️ Think about holidays → Voice notes list"]


@pytest.mark.asyncio
async def test_digest_hour_sets_the_task_time(hub, monkeypatch):
    monkeypatch.setenv("MODULE_DIGEST_HOUR", "7")
    module = JsonModule()
    await _start(module)
    module.add(row(14, "Get the proposal done", due_date="2031-10-22"))
    await bai.sync("alice", module, now=T0)
    assert notes(hub)[0].due_date == "2031-10-22T07:00:00+02:00"


@pytest.mark.asyncio
async def test_conversation_items_are_not_filed_or_pushed(hub, pushes):
    module = JsonModule()
    await _start(module)
    module.add(row(20, "Send Alice the spreadsheet", "event", note=False,
                   due_date="2031-10-22", due_time="12:00"))
    stats = await bai.sync("alice", module, now=T0)
    assert stats == {"new": 1, "filed": 0, "synced": 0, "pushed": 0}
    assert events(hub) == [] and notes(hub) == [] and pushes == []


@pytest.mark.asyncio
async def test_filing_is_idempotent_and_items_are_never_refiled(hub, pushes):
    module = JsonModule()
    await _start(module)
    module.add(row(12, "Meet Jason", "event", due_date="2031-10-22", due_time="12:00"))
    await bai.sync("alice", module, now=T0)
    stats = await bai.sync("alice", module, now=T0)
    assert stats == {"new": 0, "filed": 0, "synced": 0, "pushed": 0}
    assert len(events(hub)) == 1 and len(pushes) == 1
    # even with the state's seen list lost, a filed item isn't filed again
    state = bai.load_state("alice")
    state["seen"] = []
    bai.save_state("alice", state)
    await bai.sync("alice", module, now=T0)
    assert len(events(hub)) == 1 and len(pushes) == 1


@pytest.mark.asyncio
async def test_filed_items_are_left_out_of_the_due_today_push(hub, pushes):
    module = JsonModule()
    await _start(module)
    module.add(row(14, "Get the proposal done", due_date="2031-10-20"))
    module.add(row(21, "Book the room", note=False, due_date="2031-10-20"))
    await bai.sync("alice", module, now=T0)  # 07:00 Berlin: before the digest hour
    morning = T0.replace(hour=7)  # 09:00 Berlin
    await bai.sync("alice", module, now=morning)
    titles = [p["title"] for p in pushes]
    assert titles == ["Filed from your voice note", "Action item due today"]
    assert "Book the room" in pushes[1]["body"] and "proposal" not in pushes[1]["body"]


@pytest.mark.asyncio
async def test_status_follows_both_ways_once(hub, pushes):
    from src.tools.notes import do_manage_notes

    module = JsonModule()
    await _start(module)
    module.add(row(14, "Get the proposal done", due_date="2031-10-22"))
    module.add(row(15, "Buy milk", due_date="2031-10-23"))
    module.add(row(12, "Meet Jason", "event", due_date="2031-10-22", due_time="12:00"))
    module.add(row(13, "Lunch with Ana", "event", due_date="2031-10-21", due_time="12:30"))
    await bai.sync("alice", module, now=T0)
    filed = await bai.filed_state("alice")

    # ticked in Odysseus -> done in the module
    await do_manage_notes(json.dumps({"action": "toggle_item", "id": filed[14]["ref"],
                                      "index": 0, "done": True}), owner="alice")
    # done in the module -> ticked in Odysseus
    module.rows[15]["status"] = "done"
    # event deleted in Odysseus -> dismissed in the module
    from src.tools.calendar import do_manage_calendar
    await do_manage_calendar(json.dumps({"action": "delete_event", "uid": filed[12]["ref"]}),
                             owner="alice")
    stats = await bai.sync("alice", module, now=T0)
    assert stats["synced"] == 3
    assert {(u["item_id"], u["status"]) for u in module.updates} == {(14, "done"), (12, "dismissed")}
    assert (await bai.filed_state("alice"))[12]["removed"] is True
    milk = next(n for n in notes(hub) if n.title == "Buy milk")
    assert json.loads(milk.items) == [{"text": "Buy milk", "done": True}]

    # nothing ping-pongs on later passes
    module.updates.clear()
    assert (await bai.sync("alice", module, now=T0))["synced"] == 0
    assert module.updates == []

    # event over -> done (once); reopened in the module afterwards stays open
    after_lunch = datetime.datetime(2031, 10, 21, 12, 0, tzinfo=datetime.timezone.utc)
    assert (await bai.sync("alice", module, now=after_lunch))["synced"] == 1
    assert module.updates == [{"item_id": 13, "status": "done"}]
    module.rows[13]["status"] = "open"
    await bai.sync("alice", module, now=after_lunch)
    await bai.sync("alice", module, now=after_lunch)
    assert module.updates == [{"item_id": 13, "status": "done"}]
    assert len(events(hub)) == 1  # Lunch stays in the calendar


@pytest.mark.asyncio
async def test_dismissed_in_module_removes_the_event(hub):
    module = JsonModule()
    await _start(module)
    module.add(row(12, "Meet Jason", "event", due_date="2031-10-22", due_time="12:00"))
    await bai.sync("alice", module, now=T0)
    module.rows[12]["status"] = "dismissed"
    assert (await bai.sync("alice", module, now=T0))["synced"] == 1
    assert events(hub) == [] and notes(hub, "calendar") == []
    assert module.updates == []
    assert (await bai.sync("alice", module, now=T0))["synced"] == 0


@pytest.mark.asyncio
async def test_unfile_item_removes_and_dismisses_for_good(hub, pushes):
    module = JsonModule()
    await _start(module)
    module.add(row(12, "Meet Jason", "event", due_date="2031-10-22", due_time="12:00"))
    module.add(row(14, "Get the proposal done", due_date="2031-10-22"))
    await bai.sync("alice", module, now=T0)
    assert await bai.unfile_item("alice", 12, module) == {
        "ok": True, "target": "calendar", "ref": bai._event_uid(12)}
    await bai.unfile_item("alice", 14, module)
    assert events(hub) == []
    assert [n.archived for n in notes(hub)] == [True]
    assert module.rows[12]["status"] == module.rows[14]["status"] == "dismissed"
    assert {k: (v["undone"], v["removed"]) for k, v in
            (await bai.filed_state("alice")).items()} == {12: (True, False), 14: (True, False)}
    module.updates.clear()
    stats = await bai.sync("alice", module, now=T0)
    assert stats["filed"] == stats["synced"] == 0 and module.updates == [] and events(hub) == []
    with pytest.raises(LookupError):
        await bai.unfile_item("alice", 12, module)


@pytest.mark.asyncio
async def test_move_item_between_calendar_and_todos(hub):
    module = JsonModule()
    await _start(module)
    module.add(row(14, "Get the proposal done", due_date="2031-10-22"))
    await bai.sync("alice", module, now=T0)
    (todo,) = notes(hub)

    moved = await bai.move_item("alice", 14, module, kind="event", due_time="15:00")
    assert module.updates[-1] == {"item_id": 14, "kind": "event", "due_time": "15:00"}
    (ev,) = events(hub)
    assert moved == {"target": "calendar", "ref": ev.uid, "open_url": f"/#event-{ev.uid}"}
    assert ev.dtstart == datetime.datetime(2031, 10, 22, 13, 0)  # 15:00 Berlin
    assert notes(hub) == [] and ev.uid != bai._event_uid(14)  # to-do removed, new uid

    # change time, then back to to-dos
    await bai.move_item("alice", 14, module, kind="event", due_date="2031-10-24",
                        due_time="09:30")
    (ev2,) = events(hub)
    assert ev2.dtstart == datetime.datetime(2031, 10, 24, 7, 30)
    back = await bai.move_item("alice", 14, module, kind="task", due_time="none")
    assert events(hub) == [] and back["target"] == "todo"
    (todo2,) = notes(hub)
    assert todo2.id != todo.id and todo2.due_date == "2031-10-24T08:00:00+02:00"
    assert (await bai.filed_state("alice"))[14]["ref"] == todo2.id
    with pytest.raises(ValueError):
        await bai.move_item("alice", 14, module, kind="event", due_date="none")


@pytest.mark.asyncio
async def test_file_item_files_a_suggestion(hub, pushes):
    module = JsonModule()
    await _start(module)
    module.add(row(20, "Send Alice the spreadsheet", note=False, due_date="2031-10-22"))
    await bai.sync("alice", module, now=T0)
    out = await bai.file_item("alice", module.rows[20], module, target="calendar")
    (ev,) = events(hub)
    assert out["target"] == "calendar" and ev.all_day and "conversation" in ev.description
    assert pushes == []  # tapped by the owner: no push
    # later passes leave it alone
    assert (await bai.sync("alice", module, now=T0))["synced"] == 0
    assert len(events(hub)) == 1


@pytest.mark.asyncio
async def test_file_items_off_keeps_the_old_behaviour(hub, pushes, monkeypatch):
    monkeypatch.setenv("MODULE_FILE_ITEMS", "off")
    module = FakeModule()
    assert await bai.sync("alice", module, now=EARLY) == {"new": 2, "pushed": 0}
    module.text += ('\n- #15 [open] Book the restaurant due 2026-10-21 (Wednesday) — from '
                    '"Dinner" (voice note) 2026-10-19 06:30 http://h:8000/view/d?sig=w#t=5')
    assert await bai.sync("alice", module, now=EARLY) == {"new": 1, "pushed": 1}
    assert pushes[0]["title"] == "New action item"
    assert events(hub) == [] and notes(hub) == []


class TextOnlyModule(FakeModule):
    """An older odysseus-sound: ignores format and answers with text."""

    async def __call__(self, tool, args):
        if tool == "list_action_items" and args.get("format") == "json":
            self.calls.append((tool, args))
            return self.text
        return await super().__call__(tool, args)


@pytest.mark.asyncio
async def test_text_answer_falls_back_to_the_old_behaviour(hub, pushes):
    module = TextOnlyModule()
    assert await bai.sync("alice", module, now=EARLY) == {"new": 2, "pushed": 0}
    module.text += ('\n- #15 [open] Book the restaurant — from "Dinner" (voice note) '
                    '2026-10-19 06:30 http://h:8000/view/d?sig=w#t=5')
    assert await bai.sync("alice", module, now=EARLY) == {"new": 1, "pushed": 1}
    assert pushes[0]["title"] == "New action item"
    assert events(hub) == [] and notes(hub) == []


def test_task_runs_every_five_minutes():
    defaults = bai.HOUSEKEEPING_DEFAULT[bai.ACTION]
    assert defaults["cron_expression"] == "*/5 * * * *"
    assert "*/15 * * * *" in defaults["old_cron_expressions"]


@pytest.mark.asyncio
async def test_auth_off_task_and_page_share_one_state(hub):
    module = JsonModule()
    assert (await bai.sync(None, module, now=T0))["filed"] == 0  # task owner None
    module.add(row(14, "Get the proposal done", due_date="2031-10-22"))
    await bai.sync(None, module, now=T0)
    assert 14 in await bai.filed_state("")  # the page's owner with auth off
    await bai.unfile_item("", 14, module)
    assert (await bai.filed_state(None))[14]["undone"] is True


# ------------------------------------------------------------- named to-do lists
def lines(factory, note_id):
    with factory() as db:
        return json.loads(db.get(cdb.Note, note_id).items)


def make_list(factory, title, items, owner="alice", note_type="checklist"):
    import uuid

    with factory() as db:
        n = cdb.Note(id=str(uuid.uuid4()), owner=owner, title=title, note_type=note_type,
                     items=json.dumps(items))
        db.add(n)
        db.commit()
        return n.id


def test_list_names_match_loosely():
    assert bai.list_key("My Shopping list") == bai.list_key("shopping") == "shopping"
    assert bai.list_key("the work to-do list") == bai.list_key("Work to-dos") == "work"
    assert bai.list_title("shopping") == "Shopping"
    assert bai.list_title("") is None and bai.list_title("my list") is None
    assert bai.list_title("none") is None


@pytest.mark.asyncio
async def test_item_with_a_list_goes_onto_that_list(hub, pushes):
    shopping = make_list(hub, "Shopping list", [{"text": "Bread", "done": True}])
    make_list(hub, "Shopping", [{"text": "Soap", "done": False}], owner="bob")  # not alice's
    module = JsonModule()
    await _start(module)
    module.add(row(30, "Milk", list_name="shopping"))
    module.add(row(31, "Eggs", list_name="Shopping", due_date="2031-10-22"))
    stats = await bai.sync("alice", module, now=T0)
    assert stats["filed"] == 2 and notes(hub) == []  # no to-dos of their own
    assert lines(hub, shopping) == [{"text": "Bread", "done": True},
                                    {"text": "Milk", "done": False},
                                    {"text": "Eggs (due Wed 22 Oct)", "done": False}]
    (p,) = pushes
    assert p["body"].splitlines() == ["☑️ Milk → Shopping list",
                                      "☑️ Eggs · due Wed 22 Oct → Shopping list"]
    assert (await bai.filed_state("alice"))[30] == {
        "target": "todo", "ref": shopping, "open_url": f"/#open=notes&note={shopping}",
        "list": "Shopping list", "kind": "task", "at": "2031-10-20T05:00:00+00:00",
        "undone": False, "removed": False}


@pytest.mark.asyncio
async def test_missing_list_is_made_once(hub, pushes):
    module = JsonModule()
    await _start(module)
    module.add(row(32, "Batteries", list_name="hardware store"))
    await bai.sync("alice", module, now=T0)
    module.add(row(33, "Screws", list_name="Hardware store list"))
    module.add(row(34, "Batteries", list_name="hardware store"))  # same open line
    await bai.sync("alice", module, now=T0)
    (made,) = notes(hub)
    assert (made.title, made.note_type, made.owner) == ("Hardware store", "todo", "alice")
    assert lines(hub, made.id) == [{"text": "Batteries", "done": False},
                                   {"text": "Screws", "done": False}]
    assert bai.todo_lists("alice") == ["Hardware store"]


@pytest.mark.asyncio
async def test_list_line_status_follows_both_ways(hub):
    groceries = make_list(hub, "Groceries", [{"text": "Apples", "done": False}])
    module = JsonModule()
    await _start(module)
    for i, text in ((40, "Milk"), (41, "Eggs"), (42, "Rice")):
        module.add(row(i, text, list_name="groceries"))
    await bai.sync("alice", module, now=T0)

    # ticked / removed in Odysseus -> done / dismissed in the module
    items = lines(hub, groceries)
    items[1]["done"] = True  # Milk
    items = [i for i in items if i["text"] != "Eggs"]
    _set_items(hub, groceries, items)
    stats = await bai.sync("alice", module, now=T0)
    assert stats["synced"] == 2
    assert (module.rows[40]["status"], module.rows[41]["status"]) == ("done", "dismissed")
    assert lines(hub, groceries)[0] == {"text": "Apples", "done": False}  # untouched

    # done in the module -> only that line is ticked
    module.rows[42]["status"] = "done"
    await bai.sync("alice", module, now=T0)
    assert [(i["text"], i["done"]) for i in lines(hub, groceries)] == [
        ("Apples", False), ("Milk", True), ("Rice", True)]


def _set_items(factory, note_id, items):
    with factory() as db:
        db.get(cdb.Note, note_id).items = json.dumps(items)
        db.commit()


@pytest.mark.asyncio
async def test_archived_list_marks_its_items_done(hub):
    chores = make_list(hub, "Chores", [{"text": "Vacuum", "done": False}])
    module = JsonModule()
    await _start(module)
    module.add(row(45, "Water the plants", list_name="chores"))
    await bai.sync("alice", module, now=T0)
    with hub() as db:
        db.get(cdb.Note, chores).archived = True
        db.commit()
    await bai.sync("alice", module, now=T0)
    assert module.rows[45]["status"] == "done"


@pytest.mark.asyncio
async def test_undo_and_move_take_only_the_line(hub):
    shopping = make_list(hub, "Shopping", [{"text": "Bread", "done": False}])
    module = JsonModule()
    await _start(module)
    module.add(row(50, "Milk", list_name="shopping"))
    module.add(row(51, "Call the bank", due_date="2031-10-22"))
    await bai.sync("alice", module, now=T0)
    assert len(notes(hub)) == 1  # Call the bank: a to-do of its own (it has a date)

    # a to-do of its own onto a list, then onto another list, then off lists again
    moved = await bai.move_item("alice", 51, module, kind="task", list_name="shopping")
    assert module.updates[-1] == {"item_id": 51, "kind": "task", "list_name": "shopping"}
    assert moved == {"target": "todo", "ref": shopping,
                     "open_url": f"/#open=notes&note={shopping}", "list": "Shopping"}
    assert notes(hub) == []  # its own to-do is gone
    assert [i["text"] for i in lines(hub, shopping)] == ["Bread", "Milk",
                                                       "Call the bank (due Wed 22 Oct)"]
    moved = await bai.move_item("alice", 51, module, kind="task", list_name="Errands")
    assert [i["text"] for i in lines(hub, shopping)] == ["Bread", "Milk"]
    errands = bai.find_list("alice", "errands")
    assert moved["ref"] == errands.id and moved["list"] == "Errands"
    back = await bai.move_item("alice", 51, module, kind="task", list_name="none")
    assert "list" not in back and module.rows[51]["list_name"] is None
    assert lines(hub, errands.id) == []
    with pytest.raises(ValueError):
        await bai.move_item("alice", 51, module, kind="task", list_name="my list")

    # Undo takes Milk off the list; Bread stays
    await bai.unfile_item("alice", 50, module)
    assert lines(hub, shopping) == [{"text": "Bread", "done": False}]
    assert module.rows[50]["status"] == "dismissed"


@pytest.mark.asyncio
async def test_todo_lists_leave_out_single_todos(hub):
    make_list(hub, "Shopping", [{"text": "Bread", "done": False},
                                {"text": "Milk", "done": False}])
    make_list(hub, "Packing", [{"text": "Passport", "done": False}])
    make_list(hub, "Call Mom", [{"text": "Call Mom", "done": False}], note_type="todo")
    make_list(hub, "shopping list", [{"text": "Soap", "done": False}])  # same list name
    got = bai.todo_lists("alice")
    assert len(got) == 2 and got[0] == "Packing" and bai.list_key(got[1]) == "shopping"


@pytest.mark.asyncio
async def test_list_edits_keep_other_lines_as_stored(hub):
    odd = [{"text": "Bread", "done": False, "pos": 3}, "Legacy line"]
    shopping = make_list(hub, "Shopping", odd)
    module = JsonModule()
    await _start(module)
    module.add(row(60, "Milk", list_name="shopping"))
    await bai.sync("alice", module, now=T0)
    module.rows[60]["status"] = "done"
    await bai.sync("alice", module, now=T0)
    assert lines(hub, shopping) == odd + [{"text": "Milk", "done": True}]
    await bai.unfile_item("alice", 60, module)
    assert lines(hub, shopping) == odd


# ------------------------------------------------------------------------ notes
def note_row(id, text, *, title=None, notes=None, **extra):
    return row(id, text, "note", owner=None, list_name=notes, note_title=title, **extra)


def make_note(factory, title, content, owner="alice"):
    import uuid

    with factory() as db:
        n = cdb.Note(id=str(uuid.uuid4()), owner=owner, title=title, note_type="note",
                     content=content)
        db.add(n)
        db.commit()
        return n.id


def content(factory, note_id):
    with factory() as db:
        return db.get(cdb.Note, note_id).content


def test_notes_names_match_loosely():
    assert bai.notes_key("My Hardware notes") == bai.notes_key("hardware") == "hardware"
    assert bai.notes_key("the car note") == "car"
    assert bai.notes_title("hardware") == "Hardware notes"
    assert bai.notes_title("my Hardware notes") == "Hardware notes"
    assert bai.notes_title("") is None and bai.notes_title("my notes") is None
    assert bai.notes_title(None) is None


@pytest.mark.asyncio
async def test_create_a_note_files_an_odysseus_note(hub, pushes):
    module = JsonModule()
    await _start(module)
    module.add(note_row(70, "Battery dimensions are 13 mm x 14 mm x 3 mm",
                        title="Battery dimensions"))
    stats = await bai.sync("alice", module, now=T0)
    assert stats["filed"] == 1 and stats["pushed"] == 1
    (note,) = notes(hub)
    assert (note.title, note.note_type, note.owner, note.items) == (
        "Battery dimensions", "note", "alice", None)
    assert note.content.startswith("Battery dimensions are 13 mm x 14 mm x 3 mm\n\n")
    assert 'voice note "Note 70"' in note.content and LINK.format(c="c70") in note.content
    # a note is no to-do: done in the module at once
    assert module.updates == [{"item_id": 70, "status": "done"}]
    assert module.rows[70]["status"] == "done"
    (p,) = pushes
    assert p["title"] == "Filed from your voice note"
    assert p["body"] == "📝 Battery dimensions are 13 mm x 14 mm x 3 mm → Notes"
    assert (await bai.filed_state("alice"))[70] == {
        "target": "note", "ref": note.id, "open_url": f"/#open=notes&note={note.id}",
        "kind": "note", "at": "2031-10-20T05:00:00+00:00", "undone": False,
        "removed": False}
    # later passes change nothing, also when the owner edits or archives the note
    module.updates.clear()
    with hub() as db:
        db.get(cdb.Note, note.id).archived = True
        db.commit()
    stats = await bai.sync("alice", module, now=T0)
    assert stats["filed"] == stats["synced"] == 0 and module.updates == []


@pytest.mark.asyncio
async def test_note_without_title_takes_the_start_of_its_text(hub):
    module = JsonModule()
    await _start(module)
    module.add(note_row(71, "The spare key is under the blue flower pot by the back door"))
    await bai.sync("alice", module, now=T0)
    (note,) = notes(hub)
    assert note.title == "The spare key is under the blue flower …"


@pytest.mark.asyncio
async def test_add_to_named_notes_appends_a_line(hub, pushes):
    mine = make_note(hub, "Hardware", "Old line")
    make_note(hub, "Hardware notes", "", owner="bob")  # not alice's
    module = JsonModule()
    await _start(module)
    module.add(note_row(72, "The screen is 1.4 inch", title="Screen", notes="hardware"))
    module.add(note_row(73, "Battery is 3 mm thick", notes="Hardware notes"))
    module.add(note_row(74, "Tyre pressure is 2.4 bar", notes="car"))
    stats = await bai.sync("alice", module, now=T0)
    assert stats["filed"] == 3
    assert content(hub, mine) == "Old line\n- The screen is 1.4 inch\n- Battery is 3 mm thick"
    (car,) = notes(hub)  # the missing notes are made, titled "<Name> notes"
    assert (car.title, car.note_type, car.content) == (
        "Car notes", "note", "- Tyre pressure is 2.4 bar")
    assert {r["status"] for r in module.rows.values()} == {"done"}
    (p,) = pushes
    assert p["body"].splitlines() == ["📝 The screen is 1.4 inch → Hardware notes",
                                      "📝 Battery is 3 mm thick → Hardware notes",
                                      "📝 Tyre pressure is 2.4 bar → Car notes"]
    state = await bai.filed_state("alice")
    assert state[72] == {"target": "note", "ref": mine,
                         "open_url": f"/#open=notes&note={mine}", "notes": "Hardware",
                         "kind": "note", "at": "2031-10-20T05:00:00+00:00",
                         "undone": False, "removed": False}
    # the same line again is not repeated; a line the owner deleted shows as removed
    module.add(note_row(75, "the screen is 1.4 inch", notes="hardware"))
    await bai.sync("alice", module, now=T0)
    assert content(hub, mine).count("1.4 inch") == 1
    with hub() as db:
        db.get(cdb.Note, mine).content = "Old line\n- Battery is 3 mm thick"
        db.commit()
    assert (await bai.filed_state("alice"))[72]["removed"] is True


@pytest.mark.asyncio
async def test_undo_removes_the_note_or_only_its_line(hub):
    mine = make_note(hub, "Hardware notes", "Old line")
    module = JsonModule()
    await _start(module)
    module.add(note_row(76, "Battery is 3 mm", title="Battery"))
    module.add(note_row(77, "The screen is 1.4 inch", notes="hardware"))
    await bai.sync("alice", module, now=T0)
    assert await bai.unfile_item("alice", 77, module) == {
        "ok": True, "target": "note", "ref": mine}
    assert content(hub, mine) == "Old line"
    (battery,) = notes(hub)
    await bai.unfile_item("alice", 76, module)
    assert [n.archived for n in notes(hub)] == [True]
    assert module.rows[76]["status"] == module.rows[77]["status"] == "dismissed"


@pytest.mark.asyncio
async def test_marking_a_note_done_is_retried(hub):
    class Flaky(JsonModule):
        fail = True

        async def __call__(self, tool, args):
            if tool == "update_action_item" and self.fail:
                self.fail = False
                raise RuntimeError("module down")
            return await super().__call__(tool, args)

    module = Flaky()
    await _start(module)
    module.add(note_row(78, "Battery is 3 mm"))
    stats = await bai.sync("alice", module, now=T0)  # the status pass tries again
    assert stats["filed"] == stats["synced"] == 1 and module.rows[78]["status"] == "done"
    stats = await bai.sync("alice", module, now=T0)
    assert stats["filed"] == stats["synced"] == 0
    assert len(notes(hub)) == 1  # filed once


@pytest.mark.asyncio
async def test_file_item_as_note_and_move_a_note_to_todos(hub, pushes):
    module = JsonModule()
    await _start(module)
    item = note_row(79, "Battery is 3 mm", title="Battery", created="2031-10-19T09:05:00+00:00")
    module.add(item)  # made before filing started: left for the page
    await bai.sync("alice", module, now=T0)
    assert notes(hub) == []
    out = await bai.file_item("alice", module.rows[79], module)
    (note,) = notes(hub)
    assert out == {"target": "note", "ref": note.id,
                   "open_url": f"/#open=notes&note={note.id}"}
    assert module.rows[79]["status"] == "done" and pushes == []
    # a note that should have been a to-do
    back = await bai.move_item("alice", 79, module, kind="task")
    (todo,) = notes(hub)
    assert back["target"] == "todo" and todo.note_type == "todo"


# ------------------------------------------- explicit filing and the "Voice notes" list
REFLECTIVE = [  # one reflective voice note (fictional): no task among them but the last
    (90, "Stop prioritizing side projects over the main build this month",
     "Stop prioritizing side projects"),
    (91, "Focus on testing the existing bugs before adding anything new",
     "Focus on testing existing bugs"),
    (92, "Figure out how the recorder could use less battery", "Use less battery"),
]


def test_explicit_is_the_default_file_mode(monkeypatch):
    for var in ("MODULE_FILE_ITEMS", "BARVIS_FILE_ITEMS"):
        monkeypatch.delenv(var, raising=False)
    assert bai.file_mode() == "explicit"
    for value, mode in (("auto", "auto"), ("ALL", "auto"), ("off", "off"),
                        ("explicit", "explicit"), ("bogus", "explicit")):
        monkeypatch.setenv("MODULE_FILE_ITEMS", value)
        assert bai.file_mode() == mode


@pytest.mark.asyncio
async def test_only_explicit_voice_note_items_are_filed(hub, pushes):
    module = JsonModule()
    await _start(module)
    for i, text, title in REFLECTIVE:
        module.add(row(i, text, title=title, explicit=False))
    module.add(row(93, "Remind me to call the bike shop", "reminder", due_date="2031-10-21",
                   due_time="10:00", title="Call the bike shop", explicit=True))
    stats = await bai.sync("alice", module, now=T0)
    assert stats["filed"] == 1 and stats["pushed"] == 2
    (todo,) = notes(hub)
    assert todo.title == "Call the bike shop"
    filed, suggested = pushes
    assert filed["body"] == "⏰ Call the bike shop · due Tue 21 Oct 10:00 → To-dos"
    assert suggested["title"] == "3 suggestions from your voice notes"
    assert suggested["body"].splitlines() == [
        "💡 Stop prioritizing side projects", "💡 Focus on testing existing bugs",
        "💡 Use less battery", "Add or dismiss them on the Athena page."]
    assert {r["status"] for i, r in module.rows.items() if i != 93} == {"open"}
    assert set(await bai.filed_state("alice")) == {93}
    # suggested once: later passes neither file nor push them
    assert await bai.sync("alice", module, now=T0) == {
        "new": 0, "filed": 0, "synced": 0, "pushed": 0}
    # one suggestion, the owner taps Add to to-dos: a line on "Voice notes"
    module.add(row(94, "See how much GPS tagging reduces battery", explicit=False))
    await bai.sync("alice", module, now=T0)
    assert pushes[-1]["title"] == "Suggested from your voice note"
    out = await bai.file_item("alice", module.rows[94], module, target="todo")
    assert out["list"] == "Voice notes"
    voice = bai.find_list("alice", "voice notes")
    assert lines(hub, voice.id) == [
        {"text": "See how much GPS tagging reduces battery", "done": False}]


def test_older_module_rows_derive_explicit_and_title():
    def old(text, **kw):
        return {"id": 1, "kind": kw.pop("kind", "task"), "text": text, **kw}

    assert not bai.is_explicit(old("Figure out what to do about the battery"))
    assert not bai.is_explicit(old("Stop multitasking during calls"))
    for item in (old("Remind me to water the plants"), old("Put the invoice on my todo"),
                 old("New task: sand the shelf"), old("Add batteries to my hardware list"),
                 old("Send the slides", due_date="2031-10-22"),
                 old("Call Ana", due_time="17:00"), old("Milk", list_name="shopping"),
                 old("Dentist", kind="event"), old("Call Mom", kind="reminder"),
                 old("Battery is 3 mm", kind="note")):
        assert bai.is_explicit(item), item
    # the module's field wins over the derivation (notes are always filed)
    assert not bai.is_explicit(old("Remind me to relax more", explicit=False))
    assert bai.is_explicit(old("Battery is 3 mm", kind="note", explicit=False))

    long = ("Figure out how the recorder could use a lot less battery when it is "
            "recording all day long")
    title = bai.item_title(old(long))
    assert title == "Figure out how the recorder could use a lot less battery…"
    assert len(title) <= 60 and long.startswith(title[:-1])
    assert bai.item_title(old(long, title="Use less battery")) == "Use less battery"
    assert bai.item_title(old("Buy milk")) == "Buy milk"
    assert len(bai.item_title(old("x" * 80))) == 60


@pytest.mark.asyncio
async def test_undated_tasks_share_the_voice_notes_list(hub, pushes):
    module = JsonModule()
    await _start(module)
    module.add(row(95, "Make a to-do to clean up the old recorder branches before Friday's "
                       "review so nobody trips on them", title="Clean up recorder branches"))
    await bai.sync("alice", module, now=T0)
    module.add(row(96, "Task: oil the bike chain"))
    await bai.sync("alice", module, now=T0)
    (voice,) = notes(hub)
    assert (voice.title, voice.note_type, voice.label, voice.due_date) == (
        "Voice notes", "todo", bai.LABEL, None)
    assert lines(hub, voice.id) == [{"text": "Clean up recorder branches", "done": False},
                                    {"text": "Task: oil the bike chain", "done": False}]
    assert [p["body"] for p in pushes] == ["☑️ Clean up recorder branches → Voice notes list",
                                           "☑️ Task: oil the bike chain → Voice notes list"]
    assert (await bai.filed_state("alice"))[95]["list"] == "Voice notes"
    assert bai.todo_lists("alice") == ["Voice notes"]
    # status per line, both ways
    items = lines(hub, voice.id)
    items[0]["done"] = True
    _set_items(hub, voice.id, items)
    assert (await bai.sync("alice", module, now=T0))["synced"] == 1
    assert (module.rows[95]["status"], module.rows[96]["status"]) == ("done", "open")
    module.rows[96]["status"] = "done"
    await bai.sync("alice", module, now=T0)
    assert all(i["done"] for i in lines(hub, voice.id))  # the auto-archive rule's case
    # Undo takes the line only
    await bai.unfile_item("alice", 95, module)
    assert [i["text"] for i in lines(hub, voice.id)] == ["Task: oil the bike chain"]


@pytest.mark.asyncio
async def test_dated_task_has_a_short_title_and_the_full_text(hub, pushes):
    module = JsonModule()
    await _start(module)
    text = ("Get the battery measurements for the new recorder case done and sent to "
            "the workshop")
    module.add(row(97, text, due_date="2031-10-22", title="Send battery measurements"))
    await bai.sync("alice", module, now=T0)
    (todo,) = notes(hub)
    assert todo.title == "Send battery measurements"
    assert json.loads(todo.items) == [{"text": text, "done": False}]
    assert todo.due_date == "2031-10-22T08:00:00+02:00"
    assert LINK.format(c="c97") in todo.content
    assert pushes[0]["body"] == "☑️ Send battery measurements · due Wed 22 Oct → To-dos"
    assert bai.todo_lists("alice") == []  # a to-do of its own, no list


@pytest.mark.asyncio
async def test_auto_mode_files_every_voice_note_item(hub, pushes, monkeypatch):
    monkeypatch.setenv("MODULE_FILE_ITEMS", "auto")
    module = JsonModule()
    await _start(module)
    for i, text, title in REFLECTIVE:
        module.add(row(i, text, title=title, explicit=False))
    stats = await bai.sync("alice", module, now=T0)
    assert stats["filed"] == 3 and stats["pushed"] == 1
    (voice,) = notes(hub)  # one list, not three to-dos
    assert [i["text"] for i in lines(hub, voice.id)] == [t for _, _, t in REFLECTIVE]
    assert pushes[0]["title"] == "Filed 3 items from your voice notes"


def _old_todo(factory, module, item_id, text, *, filed_mode=None, **extra):
    """A to-do filed one per item the way filing did before explicit mode (whole sentence
    as title and line, no date), with its state entry."""
    import uuid

    module.add(row(item_id, text, explicit=False, **extra))
    with factory() as db:
        n = cdb.Note(id=str(uuid.uuid4()), owner="alice", title=text, note_type="todo",
                     label=bai.LABEL, items=json.dumps([{"text": text, "done": False}]))
        db.add(n)
        db.commit()
        ref = n.id
    state = bai.load_state("alice")
    entry = {"kind": "task", "target": "todo", "ref": ref, "at": "2031-10-19T09:00:00+00:00",
             "rev": 0, "module": "open", "hub": "open"}
    if filed_mode:
        entry["filed_mode"] = filed_mode
    state.setdefault("filed", {})[str(item_id)] = entry
    state["seen"] = sorted(set(state.get("seen", [])) | {item_id})
    bai.save_state("alice", state)
    return ref


@pytest.mark.asyncio
async def test_unfile_auto_todos_removes_only_old_per_item_todos(hub, monkeypatch):
    module = JsonModule()
    await _start(module)
    _old_todo(hub, module, 80, "Stop prioritizing side projects over the main build")
    _old_todo(hub, module, 82, "Focus on testing the existing bugs first", status="done")
    _old_todo(hub, module, 85, "Figure out how it could use less battery",
              filed_mode="explicit")
    _old_todo(hub, module, 86, "See how much GPS tagging reduces battery", filed_mode="page")
    _old_todo(hub, module, 88, "Send the slides to Greta")
    # filed correctly today: never cleaned up, not even with include_all
    module.add(row(81, "Call Mom", "reminder", due_date="2031-10-21", due_time="17:00"))
    module.add(row(83, "Get the proposal done", due_date="2031-10-22"))
    module.add(row(87, "Milk", list_name="shopping"))  # a list line
    module.add(row(89, "Book the room", note=False))  # a conversation item
    await bai.sync("alice", module, now=T0)
    await bai.file_item("alice", module.rows[89], module, target="todo")
    # an old one the owner gave a date: a wanted reminder now (the sandbox's #17)
    await bai.move_item("alice", 88, module, kind="reminder", due_date="2031-10-23",
                        due_time="09:00")
    titles = {n.title for n in notes(hub)}

    dry = await bai.unfile_auto_todos("alice", module)
    assert dry["apply"] is False and dry["removed"] == 0
    assert [(t["id"], t["status"]) for t in dry["todos"]] == [(80, "open"), (82, "done")]
    assert dry["todos"][0]["why"] == ["filed before explicit filing", "not explicit now",
                                      "a to-do of its own", "no due date or reminder"]
    assert {n.title for n in notes(hub)} == titles  # a dry run changes nothing
    every = await bai.unfile_auto_todos("alice", module, include_all=True)
    assert [t["id"] for t in every["todos"]] == [80, 82, 85, 86]
    assert every["todos"][2]["why"][0] == "filed in explicit mode (all)"

    only = await bai.unfile_auto_todos("alice", module, apply=True, only={82})
    assert only["removed"] == 1 and module.rows[82]["status"] == "done"  # done stays done
    done = await bai.unfile_auto_todos("alice", module, apply=True)
    assert done["removed"] == 1 and module.rows[80]["status"] == "dismissed"
    left = {n.title for n in notes(hub)}
    assert titles - left == {"Stop prioritizing side projects over the main build",
                             "Focus on testing the existing bugs first"}
    assert {"Call Mom", "Get the proposal done", "Send the slides to Greta", "Shopping",
            "Book the room"} <= left
    assert module.rows[81]["status"] == module.rows[88]["status"] == "open"
    # recorded as undone: never filed again, nothing left to clean up by default
    state = await bai.filed_state("alice")
    assert state[80]["undone"] and state[82]["undone"] and not state[88]["undone"]
    assert (await bai.sync("alice", module, now=T0))["filed"] == 0
    assert (await bai.unfile_auto_todos("alice", module))["todos"] == []


@pytest.mark.asyncio
async def test_emptied_voice_notes_list_is_archived(hub):
    module = JsonModule()
    await _start(module)
    module.add(row(98, "Task: oil the bike chain"))
    await bai.sync("alice", module, now=T0)
    (voice,) = notes(hub)
    await bai.move_item("alice", 98, module, kind="task", due_date="2031-10-24")
    with hub() as db:
        assert db.get(cdb.Note, voice.id).archived is True  # its only line moved off
    # a second line keeps it; a pinned list stays even when emptied
    module.add(row(99, "Task: wax the skis"))
    module.add(row(100, "Task: fix the gate"))
    await bai.sync("alice", module, now=T0)
    fresh = bai.find_list("alice", VOICE)
    assert fresh.id != voice.id
    await bai.unfile_item("alice", 99, module)
    with hub() as db:
        db.get(cdb.Note, fresh.id).pinned = True
        db.commit()
        assert db.get(cdb.Note, fresh.id).archived is False
    await bai.unfile_item("alice", 100, module)
    with hub() as db:
        n = db.get(cdb.Note, fresh.id)
        assert json.loads(n.items) == [] and n.archived is False


VOICE = bai.VOICE_LIST
