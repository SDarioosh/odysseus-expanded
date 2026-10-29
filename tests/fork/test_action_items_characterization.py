"""Fork: what the Athena action-items job decides, pinned.

``fork/athena/action_items.py`` is run against a table of fictional module rows (voice-note
events, dated and undated tasks, reminders, named lists, notes, conversation items, older
module rows, a text-only module) through the task's passes and the page's actions (file,
move, undo, clean-up). What it sent to Athena (tool and arguments), the pushes it sent,
the state file it wrote, the Odysseus notes and calendar events it made (with their
calendar uids, which must never change), what each call returned or raised, and what its
pure helpers make of a table of inputs are compared with
``fixtures/action_items_characterization.json``, recorded from the job as one module.

The fakes sit at the edges every layout goes through: Athena is a callable, the
notification dispatcher is ``routes.note_routes.dispatch_reminder``, the hub's notes and
calendar are a temp SQLite database behind ``core.database.SessionLocal``. Random note ids
(uuid4) are numbered in order of appearance and wall-clock stamps masked;
calendar uids (uuid5 of the item) are kept as they are.

Record the fixture (only on code whose behaviour is the reference):
``UPDATE_CHARACTERIZATION=1 python -m pytest -q tests/fork/test_action_items_characterization.py``.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
from pathlib import Path

import pytest

import core.database as cdb
from fork.athena import action_items as mai
from tests.helpers.sqlite_db import make_temp_sqlite

FIXTURE = Path(__file__).with_name("fixtures") / "action_items_characterization.json"
UTC = datetime.UTC
T0 = datetime.datetime(2031, 10, 20, 5, 0, tzinfo=UTC)  # 07:00 in Berlin
TZ = "Europe/Berlin"
LINK = "http://h:8000/view/{c}?sig=s#t=4"
ENV = ("MODULE_FILE_ITEMS", "BARVIS_FILE_ITEMS", "MODULE_NOTIFY_CHANNEL",
       "BARVIS_NOTIFY_CHANNEL", "MODULE_DIGEST_HOUR", "BARVIS_DIGEST_HOUR",
       "MODULE_ACTION_ITEMS_SERVER", "BARVIS_ACTION_ITEMS_SERVER")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


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


# taken when this file is imported, before any test patches the module
SURFACE = surface(mai)


class Ids:
    """Random uuid4 ids -> ``id1``, ``id2`` ... in order of appearance; stamps of the wall
    clock (``undone_at``, and ``at`` of an entry the page filed) -> ``<now>``."""

    @staticmethod
    def clock(key, value) -> bool:
        return key == "undone_at" or (key == "at" and isinstance(value, str)
                                      and not value.startswith("2031-"))

    def __init__(self):
        self.seen: dict[str, str] = {}

    def __call__(self, value):
        if isinstance(value, str):
            return _UUID4.sub(lambda m: self.seen.setdefault(m[0], f"id{len(self.seen) + 1}"),
                              value)
        if isinstance(value, dict):
            return {k: ("<now>" if self.clock(k, v) else self(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value]
        return value


def row(id, text, kind="task", *, note=True, status="open", due_date=None, due_time=None,
        created="2031-10-20T09:05:00+00:00", **extra):
    return {"id": id, "status": status, "kind": kind, "text": text, "owner": "Me",
            "due": None, "due_date": due_date, "due_time": due_time,
            "duration_minutes": None, "location": None, "time_unclear": False,
            "conversation_id": f"c{id}", "conversation_title": f"Note {id}",
            "conversation_kind": "note" if note else "conversation",
            "conversation_at": "2031-10-20T09:00:00+00:00", "created_at": created,
            "timezone": TZ, "link": LINK.format(c=f"c{id}"), **extra}


class Module:
    """Athena: ``list_action_items`` (JSON, or text when ``text`` is set),
    ``update_action_item`` and ``get_daily_digest``; every call is logged."""

    def __init__(self, log, rows=(), text=None, digest="No digest for that day."):
        self.log, self.rows, self.text, self.digest = log, {}, text, digest
        for r in rows:
            self.add(r)

    def add(self, r):
        self.rows[r["id"]] = dict(r)

    async def __call__(self, tool, args):
        self.log.append(["call", tool, args])
        if tool == "list_action_items":
            if self.text is not None:
                return self.text
            return json.dumps(list(self.rows.values()))
        if tool == "update_action_item":
            r = self.rows.get(args["item_id"])
            if r is None:
                return f"No action item #{args['item_id']}."
            for k in ("status", "kind", "due_date", "due_time", "list_name"):
                if args.get(k):
                    r[k] = None if args[k] == "none" else args[k]
            return f"Action item #{args['item_id']} is now {r['status']}."
        if tool == "get_daily_digest":
            return self.digest
        raise AssertionError(f"unexpected tool {tool}")


TEXT_LIST = """2 open action items (times are Europe/Berlin). When answering, put the link next to each fact you use:
- #12 [open] Send Alice the revised spreadsheet (owner Me) due 2031-10-21 (Tuesday) — from "Q4 budget" 2031-10-15 12:00 http://h:8000/view/a?sig=x#t=40
- #13 [open] Pick up Aunt Rosa from the station due that afternoon — from "Dinner plans" (voice note) 2031-10-16 22:00 http://h:8000/view/b?sig=y#t=16"""
DIGEST = """2031-10-20: Dentist and dinner plans
You booked a crown and planned Mom's dinner.
When answering, put the link next to each fact you use."""


def _hub_rows(factory) -> dict:
    with factory() as db:
        notes = [{"id": n.id, "owner": n.owner, "title": n.title, "note_type": n.note_type,
                  "label": n.label, "items": n.items, "content": n.content,
                  "due_date": n.due_date, "archived": bool(n.archived), "repeat": n.repeat}
                 for n in db.query(cdb.Note).all()]
        events = [{"uid": e.uid, "summary": e.summary, "description": e.description,
                   "location": e.location, "dtstart": e.dtstart.isoformat(),
                   "dtend": e.dtend.isoformat(), "all_day": bool(e.all_day),
                   "is_utc": bool(e.is_utc), "status": e.status}
                  for e in db.query(cdb.CalendarEvent).order_by(cdb.CalendarEvent.uid).all()]
    notes.sort(key=lambda n: (n["title"] or "", n["content"] or "", n["items"] or "",
                              n["due_date"] or "", n["label"] or ""))
    return {"notes": notes, "events": events}


def _state(owner: str) -> dict:
    return mai.load_state(owner)


async def _record(log: list, label: str, coro):
    try:
        result = await coro
    except Exception as e:  # noqa: BLE001 -- the error is part of the record
        log.append([label, {"raised": type(e).__name__, "message": str(e)}])
        return None
    log.append([label, json.loads(json.dumps(result, default=repr))])
    return result


async def scenario(factory, monkeypatch) -> dict:
    log: list = []
    module = Module(log, [row(1, "Old voice note task", due_date="2031-10-22")])
    out: dict = {}

    await _record(log, "first pass", mai.sync("alice", module, now=T0))
    for r in (
        row(12, "Meet Jason", "event", due_date="2031-10-22", due_time="12:00",
            location="Cafe Blau", duration_minutes=45, explicit=True),
        row(13, "Dentist", "event", due_date="2031-10-23", explicit=True),
        row(14, "Get the proposal done", due_date="2031-10-22", explicit=True,
            title="Proposal"),
        row(15, "Call Mom", "reminder", due_date="2031-10-20", due_time="17:00",
            time_unclear=True, explicit=True),
        row(16, "Think about the holidays and where to go this winter with the family "
                "and the dog", explicit=True),
        row(17, "Oat milk", list_name="my shopping list", explicit=True),
        row(18, "Ideas for the garden", "note", note_title="Garden ideas",
            key_points=["raised beds", " ", "compost"]),
        row(19, "Solder the second board", "note", list_name="hardware notes"),
        row(20, "Send Alice the spreadsheet", "event", note=False, due_date="2031-10-22",
            due_time="12:00"),
        row(21, "Maybe read more", explicit=False),
        row(22, "Remind me to water the plants"),
        row(23, "Already done thing", status="done", explicit=True),
        row(24, "Someone else's job", mine=False, explicit=True),
        row(25, "Old row", created="2031-10-19T09:00:00+00:00", explicit=True),
    ):
        module.add(r)
    await _record(log, "filing pass", mai.sync("alice", module, now=T0))
    out["after filing"] = _hub_rows(factory)

    # status changes on both sides
    module.rows[14]["status"] = "done"
    module.rows[13]["status"] = "dismissed"
    with factory() as db:
        lst = next(n for n in db.query(cdb.Note).all() if n.title == "My shopping list"
                   or n.title.lower().startswith("my shopping"))
        lst.items = json.dumps([{"text": i["text"], "done": True}
                                for i in json.loads(lst.items)])
        todo = next(n for n in db.query(cdb.Note).all() if n.title == "Call Mom")
        todo.archived = True
        db.commit()
    await _record(log, "status pass", mai.sync("alice", module,
                                               now=T0 + datetime.timedelta(hours=1)))

    # the daily pushes after the digest hour
    module.digest = DIGEST
    module.add(row(26, "Pay the rent", note=False, due_date="2031-10-21"))
    await _record(log, "morning pass", mai.sync(
        "alice", module, now=datetime.datetime(2031, 10, 21, 7, 30, tzinfo=UTC)))
    await _record(log, "morning pass again", mai.sync(
        "alice", module, now=datetime.datetime(2031, 10, 21, 8, 30, tzinfo=UTC)))

    # the page's actions
    await _record(log, "file suggestion", mai.file_item("alice", module.rows[21], module))
    await _record(log, "file as note", mai.file_item("alice", module.rows[20], module,
                                                     target="note"))
    await _record(log, "file bad target", mai.file_item("alice", module.rows[20], module,
                                                        target="desk"))
    await _record(log, "move to todo", mai.move_item("alice", 12, module, kind="task",
                                                     due_date="none"))
    await _record(log, "move to event", mai.move_item("alice", 16, module, kind="event",
                                                      due_date="2031-10-25", due_time="09:30"))
    await _record(log, "move to list", mai.move_item("alice", 22, module, kind="task",
                                                     list_name="Garden"))
    await _record(log, "move bad kind", mai.move_item("alice", 12, module, kind="chore"))
    await _record(log, "move bad date", mai.move_item("alice", 12, module, kind="task",
                                                      due_date="tomorrow"))
    await _record(log, "move event without date", mai.move_item("alice", 21, module,
                                                                kind="event"))
    await _record(log, "move missing", mai.move_item("alice", 404, module, kind="task"))
    await _record(log, "undo", mai.unfile_item("alice", 15, module))
    await _record(log, "undo note line", mai.unfile_item("alice", 19, module))
    await _record(log, "undo list line", mai.unfile_item("alice", 22, module))
    await _record(log, "undo again", mai.unfile_item("alice", 15, module))
    await _record(log, "undo never filed", mai.unfile_item("alice", 404, module))

    # the clean-up of to-dos filed one per item
    module.add(row(30, "Stop prioritizing side projects", explicit=False))
    module.add(row(31, "A page-filed one", explicit=False))
    with factory() as db:
        for item_id, title in ((30, "Stop prioritizing side projects"),
                               (31, "A page-filed one")):
            db.add(cdb.Note(id=f"old-{item_id}", owner="alice", title=title,
                            note_type="todo", label=mai.LABEL,
                            items=json.dumps([{"text": title, "done": False}])))
        db.commit()
    state = mai.load_state("alice")
    state["filed"]["30"] = {"kind": "task", "target": "todo", "ref": "old-30",
                            "at": "2031-10-19T09:00:00+00:00", "rev": 0, "module": "open",
                            "hub": "open"}
    state["filed"]["31"] = {**state["filed"]["30"], "ref": "old-31", "filed_mode": "page"}
    state["seen"] = sorted(set(state["seen"]) | {30, 31})
    mai.save_state("alice", state)
    await _record(log, "clean-up dry run", mai.unfile_auto_todos("alice", module))
    await _record(log, "clean-up all dry run", mai.unfile_auto_todos(
        "alice", module, include_all=True))
    await _record(log, "clean-up apply", mai.unfile_auto_todos("alice", module, apply=True,
                                                               only={30, 31}))
    await _record(log, "filed state", mai.filed_state("alice"))
    log.append(["todo lists", mai.todo_lists("alice")])

    # a text-only module and MODULE_FILE_ITEMS=off (owner bob)
    text = Module(log, text=TEXT_LIST, digest=DIGEST)
    await _record(log, "text first", mai.sync("bob", text, now=T0))
    text.text += ('\n- #14 [open] Book the restaurant due 2031-10-23 (Thursday) — from '
                  '"Dinner" 2031-10-19 06:30 http://h:8000/view/d?sig=w#t=5')
    await _record(log, "text second", mai.sync("bob", text, now=T0))
    monkeypatch.setenv("MODULE_FILE_ITEMS", "off")
    monkeypatch.setenv("MODULE_DIGEST_HOUR", "6")
    await _record(log, "off pass", mai.sync("carol", Module(log, text=TEXT_LIST,
                                                            digest=DIGEST), now=T0))
    await _record(log, "off pass 2", mai.sync(
        "carol", Module(log, text=TEXT_LIST + TEXT_LIST.replace("#12", "#15").replace(
            "#13", "#16").split("\n", 1)[1], digest=DIGEST),
        now=datetime.datetime(2031, 10, 21, 7, 30)))
    monkeypatch.setenv("MODULE_NOTIFY_CHANNEL", "off")
    await _record(log, "push off", mai.push("alice", "t", "b", "k"))
    monkeypatch.delenv("MODULE_NOTIFY_CHANNEL")
    monkeypatch.delenv("MODULE_FILE_ITEMS")
    monkeypatch.delenv("MODULE_DIGEST_HOUR")

    out["log"] = log
    out["hub"] = _hub_rows(factory)
    out["state"] = {o: _state(o) for o in ("alice", "bob", "carol")}
    return out


def pure() -> dict:
    out: dict = {}
    out["parse_action_items"] = [list(vars(i).values()) for i in
                                 mai.parse_action_items(TEXT_LIST + "\nnot a line")]
    out["parse_items_json"] = [mai.parse_items_json(t) for t in (
        '[{"id": "3", "text": "a"}, {"id": "x"}, {"text": "no id"}, 7]', "{}", "text", "")]
    items = [{"kind": "note"}, {"explicit": False, "due_date": "2031-10-20"},
             {"due_date": "2031-10-20"}, {"due_time": "09:00"}, {"list_name": "Shopping"},
             {"list_name": "list"}, {"kind": "reminder"}, {"kind": "event"},
             {"text": "please add milk to my shopping list"}, {"text": "a TODO for later"},
             {"text": "just a thought"}]
    out["is_explicit"] = [mai.is_explicit(i) for i in items]
    out["item_title"] = [mai.item_title(i) for i in (
        {"title": "  Short   one "}, {"text": "x" * 70}, {"text": "word " * 20},
        {"text": "An exactly sixty character long text that stops right there."},
        {"id": 7}, {"text": "Ends with punctuation, and then a lot more words to cut here"})]
    names = ("My Shopping list", "shopping", "the to-do list", "Groceries to-dos", "none",
             "  ", "x" * 61, "our hardware notes", "Notes", "my Garden Notes")
    out["lists"] = [[n, mai.list_key(n), mai.list_title(n), mai.notes_key(n),
                     mai.notes_title(n)] for n in names]
    out["default_target"] = [mai.default_target(i) for i in (
        {"kind": "note"}, {"kind": "event", "due_date": "2031-10-20"}, {"kind": "event"},
        {"kind": "task"}, {})]
    out["open_url"] = [mai.open_url("calendar", "u1"), mai.open_url("todo", "n1"),
                       mai.open_url("note", "n2")]
    out["names"] = {"ACTION": mai.ACTION, "LABEL": mai.LABEL, "TASK_NAME": mai.TASK_NAME,
                    "HOUSEKEEPING_DEFAULT": mai.HOUSEKEEPING_DEFAULT, "INFO": mai.INFO,
                    "state_file": mai._state_path("alice").name,
                    "VOICE_LIST": mai.VOICE_LIST, "FILE_MODES": list(mai.FILE_MODES)}
    actions, info, defaults = {}, {}, {}
    mai.register_builtin_action(actions, info)
    mai.register_housekeeping(defaults)
    out["registered"] = [sorted(actions), info, defaults,
                         actions[mai.ACTION] is mai.action_sync_action_items]
    return out


def uids(monkeypatch) -> dict:
    out = {}
    for server in (None, "convo", "Athena", "BARVIS"):
        for name in ("MODULE_ACTION_ITEMS_SERVER", "BARVIS_ACTION_ITEMS_SERVER"):
            monkeypatch.delenv(name, raising=False)
        if server == "BARVIS":
            monkeypatch.setenv("BARVIS_ACTION_ITEMS_SERVER", "old-name")
        elif server:
            monkeypatch.setenv("MODULE_ACTION_ITEMS_SERVER", server)
        out[str(server)] = [mai._event_uid(i, rev) for i in (1, 12, 9999) for rev in (0, 1, 2)]
    for name in ("MODULE_ACTION_ITEMS_SERVER", "BARVIS_ACTION_ITEMS_SERVER"):
        monkeypatch.delenv(name, raising=False)
    return out


async def task_entry(monkeypatch) -> list:
    from fork.athena import client
    from src.builtin_actions import TaskNoop

    seen = []
    monkeypatch.setattr(client.ATHENA, "caller", lambda: None)
    try:
        seen.append(await mai.action_sync_action_items("alice"))
    except TaskNoop as e:
        seen.append([type(e).__name__, str(e)])

    async def fake(owner, call, *, now=None):
        return stats

    monkeypatch.setattr(client.ATHENA, "caller", lambda: object())
    monkeypatch.setattr(mai, "sync", fake)
    for stats in ({"new": 0, "pushed": 0}, {"new": 2, "pushed": 1},
                  {"new": 1, "filed": 1, "synced": 2, "pushed": 1}):
        try:
            seen.append(list(await mai.action_sync_action_items("alice")))
        except TaskNoop as e:
            seen.append([type(e).__name__, str(e)])

    async def broken(owner, call, *, now=None):
        raise RuntimeError("Athena is down")

    monkeypatch.setattr(mai, "sync", broken)
    seen.append(list(await mai.action_sync_action_items("alice")))
    return seen


@pytest.fixture
def hub(data_dir, monkeypatch):
    import routes.calendar_routes as cr
    from routes import note_routes
    from src.user_time import clear_user_time_context

    factory, engine, _tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(cr, "SessionLocal", factory, raising=False)
    monkeypatch.setattr(mai, "_LOCKS", {})
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TZ", "UTC")
    clear_user_time_context()
    pushed: list = []

    async def fake_dispatch(title, body, key, owner="", settings_override=None, **kw):
        pushed.append({"owner": owner, "title": title, "body": body, "key": key,
                       **(settings_override or {})})
        return {"ntfy_sent": True}

    monkeypatch.setattr(note_routes, "dispatch_reminder", fake_dispatch)
    yield factory, pushed
    engine.dispose()


def test_the_action_items_job_behaves_as_recorded(hub, monkeypatch, caplog):
    factory, pushed = hub
    ids = Ids()
    caplog.set_level(logging.DEBUG, logger="fork")
    seen = {"scenario": asyncio.run(scenario(factory, monkeypatch)), "pushes": pushed,
            "pure": pure(), "uids": uids(monkeypatch),
            "task": asyncio.run(task_entry(monkeypatch))}
    # log lines are pinned with the logger's name: it must not change either
    seen["logged"] = [[r.name, r.levelname, r.getMessage()] for r in caplog.records
                      if r.name.startswith("fork.athena.action_items")]
    seen = ids(json.loads(json.dumps(seen, default=repr)))
    seen["surface"] = SURFACE
    if os.environ.get("UPDATE_CHARACTERIZATION"):
        FIXTURE.write_text(json.dumps(seen, indent=1, sort_keys=True, ensure_ascii=False)
                           + "\n", encoding="utf-8")
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # every name the module offered stays importable from it (more may be added)
    assert set(expected.pop("surface")) <= set(seen.pop("surface"))
    for key in expected:
        assert seen[key] == expected[key], key
    assert set(seen) == set(expected)
