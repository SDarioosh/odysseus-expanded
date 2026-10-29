"""Fork: what the daily Today insight decides and writes, pinned.

``fork/insight/today.py`` is run with fakes at the points tests and the eval patch on it
(``owner_timezone``, ``_load_notes``, ``_load_events``, ``_load_memory``,
``default_llm``), a fake Athena and a fake model, through generations, no-ops, retries,
kept and empty insights, votes, Refresh, the task entry and the chat context; its pure
helpers run over a table of inputs. What the model was sent, what Athena was asked, what
each call returned or raised, the pushes, every file it (and the outcome log, weights and
push it calls) wrote and what it logged are compared with
``fixtures/today_characterization.json``, recorded from the insight as one module.

Record the fixture (only on code whose behaviour is the reference):
``UPDATE_CHARACTERIZATION=1 python -m pytest -q tests/fork/test_today_characterization.py``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from fork.insight import today as ti

RUN = ti.run

FIXTURE = Path(__file__).with_name("fixtures") / "today_characterization.json"
TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)  # 09:00 in Berlin
TODAY = date(2026, 10, 21)
ENV = ("TODAY_INSIGHT_ENABLED", "TODAY_INSIGHT_SKIP_LISTS", "TODAY_INSIGHT_PUSH",
       "MODULE_NOTIFY_CHANNEL", "BARVIS_NOTIFY_CHANNEL", "MODULE_DIGEST_HOUR",
       "BARVIS_DIGEST_HOUR", "MODULE_ACTION_ITEMS_SERVER", "BARVIS_ACTION_ITEMS_SERVER")


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


def note(id, title="Errands", items=(), note_type="checklist", due_date=None, pinned=False,
         archived=False, source="user", label=None):
    return SimpleNamespace(id=id, title=title, note_type=note_type, due_date=due_date,
                           pinned=pinned, archived=archived, source=source, label=label,
                           items=json.dumps([{"text": t, "done": d} for t, d in items]))


def event(uid, start, end, summary="Meeting", all_day=False, is_utc=False, status=None,
          location="", importance="normal", event_type=None):
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


NOTES = [
    note("n1", "Work", [("Send the Q4 deck", False), ("Book room", True)],
         due_date="2026-10-20"),
    note("n2", "Shopping", [("Oat milk", False)], note_type="todo"),
    note("n3", "Groceries list", [("Bread", False)], note_type="goal", due_date="2026-10-21"),
    note("n4", "Run 10k", [("Long run Sunday", False)], note_type="goal"),
    note("n5", "Idea", note_type="note"),
    note("n6", "Old", [("Archived thing", False)], archived=True),
    note("n7", "Meet Jason", [("Meet Jason", False)], source="calendar", label="calendar"),
    note("n8", "Pinned", [("Pinned task", False)], pinned=True),
    note("n9", "Later", [("Far away", False)], due_date="2026-12-01"),
    note("n10", "Soon", [("Soon thing", False)], due_date="2026-10-25T08:00:00+02:00"),
]
EVENTS = [
    event("e-past", datetime(2026, 10, 21, 7, 0), datetime(2026, 10, 21, 8, 0)),
    event("e1", datetime(2026, 10, 21, 12), datetime(2026, 10, 21, 13), "Dentist",
          location="Dr. Patel", importance="high", event_type="health"),
    event("e-utc", datetime(2026, 10, 21, 10, 0), datetime(2026, 10, 21, 11, 0), "Call",
          is_utc=True),
    event("e-allday", datetime(2026, 10, 21), datetime(2026, 10, 22), "Mira's birthday",
          all_day=True),
    event("wk::2026-10-24T18:00", datetime(2026, 10, 24, 18), datetime(2026, 10, 24, 20),
          "Dinner at Tom's"),
    event("e-cancel", datetime(2026, 10, 22, 9), datetime(2026, 10, 22, 10),
          status="cancelled"),
    event("e-far", datetime(2026, 11, 2, 9), datetime(2026, 11, 2, 10)),
]
MEMORY = [
    {"text": "Tom is a colleague", "category": "contact", "uses": 2, "timestamp": 5},
    {"text": "Jonas is allergic to peanuts (source: http://10.0.0.5:8000/view/abc?sig=x#t=40)",
     "category": "contact", "source": "odysseus-sound"},
    {"text": "Use bullet points", "category": "instruction", "uses": 99},
    {"text": "Prefers tea", "category": "preference", "uses": 7, "timestamp": 1},
]
ROWS = [
    {"id": 1, "status": "open", "text": "Send Alice the slides", "due_date": "2026-10-22",
     "due_time": "10:00"},
    {"id": 2, "status": "open", "text": "Water   the plants", "due": "Friday"},
    {"id": 3, "status": "done", "text": "Already done"},
    {"id": "x", "status": "open", "text": "Odd id"},
    {"status": "open", "text": "No id"},
]
GOOD = json.dumps({"headline": "The Q4 deck is overdue; send it before the dentist at 12.",
                   "focus": [{"ref": "T1", "why": "Overdue since yesterday; Tom waits on it"},
                             {"ref": "todo:nope:1", "why": "invented"},
                             {"ref": "T1", "why": "twice"}],
                   "coming_up": [{"ref": "E1", "why": "12:00 today"},
                                 {"ref": "e-utc", "why": "short fragment"},
                                 {"ref": "wk::2026-10-24T18:00", "why": "Saturday"}],
                   "also": [{"ref": "T2", "why": "Long run Sunday"},
                            {"ref": "2026-10-21", "why": "a date"}, "garbage"]})


class FakeLLM:
    def __init__(self, log, *answers):
        self.log, self.answers = log, list(answers)

    async def __call__(self, messages):
        self.log.append(["model", messages])
        return self.answers.pop(0) if self.answers else ""


class Athena:
    def __init__(self, log, digest="2026-10-21: Busy day\nYou met Tom.\nWhen answering, "
                                   "put the link next to each fact you use.", rows=ROWS,
                 fail=()):
        self.log, self.digest, self.rows, self.fail = log, digest, rows, set(fail)

    async def __call__(self, tool, args):
        self.log.append(["athena", tool, args])
        if tool in self.fail:
            raise RuntimeError(f"{tool} failed")
        if tool == "get_daily_digest":
            return self.digest
        if tool == "list_action_items":
            return json.dumps([r for r in self.rows if r.get("status") == args["status"]])
        raise AssertionError(tool)


async def _record(log: list, label: str, coro):
    from src.builtin_actions import TaskNoop

    try:
        result = await coro
    except (Exception, TaskNoop) as e:  # noqa: BLE001 -- the error is part of the record
        log.append([label, {"raised": type(e).__name__, "message": str(e)}])
        return None
    log.append([label, json.loads(json.dumps(result, default=str))])
    return result


def _files(root: Path) -> dict:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in (".json", ".jsonl"):
            out[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
    return out


async def scenario(monkeypatch, state) -> list:
    log: list = []
    athena = Athena(log)
    await _record(log, "generate", ti.run("alice", now=NOW, call=athena,
                                          llm=FakeLLM(log, GOOD)))
    await _record(log, "noop", ti.run("alice", now=NOW + timedelta(minutes=15), call=athena,
                                      llm=FakeLLM(log, GOOD)))
    state["notes"] = NOTES[:4]
    await _record(log, "retry", ti.run("alice", now=NOW + timedelta(minutes=30), call=athena,
                                       llm=FakeLLM(log, "not json", json.dumps(
                                           {"headline": "Nothing named", "focus": []}),
                                           GOOD)))
    await _record(log, "vote up", ti.add_vote("alice", "up", now=NOW + timedelta(minutes=31)))
    await _record(log, "vote bad", ti.add_vote("alice", "meh", now=NOW))
    state["notes"] = NOTES[:2]
    await _record(log, "kept", ti.run("alice", now=NOW + timedelta(minutes=45), call=athena,
                                      llm=FakeLLM(log, "nope", "still nope")))
    await _record(log, "refresh", ti.generate_now("alice", now=NOW + timedelta(hours=1),
                                                  call=Athena(log, fail={"get_daily_digest"}),
                                                  llm=FakeLLM(log, GOOD)))
    await _record(log, "refresh kept", ti.generate_now("alice", now=NOW + timedelta(hours=1),
                                                       call=None, llm=FakeLLM(log)))
    state["notes"], state["events"] = [], []
    await _record(log, "empty", ti.run("bob", now=NOW, call=Athena(
        log, digest="No digest for 2026-10-21.", rows=[]), llm=FakeLLM(log)))
    await _record(log, "vote none", ti.add_vote("carol", "down", now=NOW))
    state["notes"], state["events"] = NOTES, EVENTS
    await _record(log, "unreachable module", ti.run(
        "dave", now=NOW, call=Athena(log, fail={"list_action_items"}), llm=FakeLLM(log, GOOD)))

    # the model chain when no model is passed (``default_llm`` faked)
    answers = [GOOD, "", ""]

    async def fake_default_llm(owner, *, wait_quiet=True):
        log.append(["default_llm", owner, wait_quiet])
        return FakeLLM(log, answers.pop(0) if answers else "")

    monkeypatch.setattr(ti, "default_llm", fake_default_llm)
    await _record(log, "default model", ti.run("erin", now=NOW, call=Athena(log)))
    await _record(log, "default model refresh", ti.generate_now("erin", now=NOW,
                                                                call=Athena(log)))

    # the task entry: off, then each status of ``run`` (faked)
    monkeypatch.setenv("TODAY_INSIGHT_ENABLED", "off")
    await _record(log, "task off", ti.action_today_insight("erin"))
    monkeypatch.delenv("TODAY_INSIGHT_ENABLED")
    for status, insight in (("noop", None), ("generated", ti.load_today("alice", now=NOW)),
                            ("empty", {"headline": "x"}), ("kept", None), ("boom", None)):
        async def fake_run(owner, *, status=status, insight=insight, **kw):
            log.append(["run", owner, kw])
            if status == "boom":
                raise RuntimeError("the model is down")
            return status, insight

        monkeypatch.setattr(ti, "run", fake_run)
        await _record(log, f"task {status}", ti.action_today_insight("erin"))
    monkeypatch.setattr(ti, "run", RUN)

    log.append(["load_today", ti.load_today("alice", now=NOW)])
    log.append(["public", ti.public(ti.load_today("alice", now=NOW) or {})])
    log.append(["today_str", ti.today_str("alice", NOW)])
    for message in (None, "What should I do first today?", "What did Dr. Patel tell me?",
                    "/sound what should I do today?", "what now", "ok, what should i do next?",
                    "What do I need to do before October 31?"):
        log.append(["context", message, ti.context_block("alice", now=NOW, message=message)])
    log.append(["context short", ti.context_block("alice", now=NOW, limit=120)])
    log.append(["context none", ti.context_block("nobody", now=NOW)])
    return log


def pure() -> dict:
    out: dict = {}
    names = {"shopping", "groceries", "grocery"}
    todos = ti.todo_candidates(NOTES, TODAY, TZ, names=names)
    events = ti.event_candidates(EVENTS, NOW.astimezone(TZ))
    out["todos"], out["events"] = todos, events
    ordered = ti.order_candidates(todos, events, dismissed=["todo:n4:0"])
    out["ordered"] = ordered
    out["ordered capped"] = [c["ref"] for c in ti.order_candidates(todos, events, limit=3)]
    out["labels"] = ti.labels(ordered, [{"ref": "loop:7"}, {"ref": "belief:3"}])
    out["memory_query"] = ti.memory_query(ordered)
    out["select_memory"] = ti.select_memory(MEMORY)
    out["select_memory 2"] = ti.select_memory(MEMORY, 2)
    out["input_hash"] = ti.input_hash(ordered, ["b", "a"], "digest", [["x"], [], ""])
    extra = [{"ref": "loop:7", "title": "Send Alice the slides", "bucket": "due_soon",
              "date": "2026-10-22", "time": "10:00", "person": "Alice",
              "link": "http://h/view/7"},
             {"ref": "belief:3", "title": "Move to Lisbon", "text": "Plans to move to Lisbon",
              "attribute": "decision", "bucket": "undated"}]
    inputs = {"now": NOW.astimezone(TZ), "today": TODAY, "candidates": ordered, "extra": extra,
              "routines": "Gym on Mondays", "memory": ["Tom is a colleague"],
              "digest": "Busy day", "action_items": ["Water the plants (due Friday)"]}
    out["build_messages"] = ti.build_messages(inputs)
    out["build_messages bare"] = ti.build_messages({**inputs, "extra": [], "routines": "",
                                                    "memory": [], "digest": None,
                                                    "action_items": []})
    answers = [json.loads(GOOD),
               {"headline": "  A   long " + "word " * 80, "focus": [
                   {"ref": c["ref"], "why": "x" * 100} for c in ordered]},
               {"headline": "h", "coming_up": [{"ref": "L1", "why": "Alice waits"}],
                "also": [{"ref": "P1", "why": "Plans to move to Lisbon"}]},
               {"headline": "h", "focus": "not a list"}]
    out["validate"] = [ti.validate(a, ordered, extra) for a in answers]
    out["resolve_ref"] = [ti.resolve_ref(r, ordered + extra) for r in (
        "T1", "e1", "[E2]", "todo:n1:0", "n1:0", "n1", "2026-10-21", "short", "", None,
        "event:wk::2026-10-24T18:00", "2026-10-24T18:00")]
    out["repeats_title"] = [ti.repeats_title(w, t) for w, t in (
        ("Due Friday; Mira needs it", "Send the Q4 deck"), ("Send the deck", "Send the Q4 deck"),
        ("", "x"), ("dev work", "development work"))]
    out["parse_answer"] = [ti.parse_answer(r) for r in (
        "not json", '{"focus": []}', '```json\n{"headline": "Hi"}\n```',
        '<think>hmm</think>{"headline": "Ok"}', None)]
    out["local_date"] = [str(ti.local_date(v, TZ)) for v in (
        "2026-10-21", "2026-10-21T08:00:00+02:00", "2026-10-20T23:30:00+00:00",
        "2026-10-22T08:00", "", "soon", "2026-13-45", "2026-10-21 junk")]
    out["bucket_for"] = [ti.bucket_for(d, TODAY) for d in (
        None, date(2026, 10, 20), TODAY, date(2026, 10, 28), date(2026, 10, 29))]
    out["is_skipped_list"] = [ti.is_skipped_list(t, d, names) for t, d in (
        ("Shopping", None), ("shopping list", ""), ("GROCERY LIST", "2026-10-22"),
        ("Shopping for Mira's party", None), ("", None))]
    out["digest_text"] = [ti._digest_text(t) for t in (
        "", "No digest for that day.", "2026-10-21: Day\nline\nWhen answering, cite.",
        "x" * 2000)]
    out["action_item_lines"] = ti._action_item_lines("alice", json.dumps(ROWS), skip=["2"])
    out["notes_state"] = ti.notes_state(NOTES[:3])
    out["make_insight"] = ti.make_insight(TODAY, {"headline": "h", "focus": [{"ref": "a"}]},
                                          "abc", votes=[{"vote": "up"}], now=NOW)
    out["insight_path"] = str(ti.insight_path("Alice@Example", "2026-10-21").relative_to(
        Path(ti.insight_path("x", "2026-10-21")).parents[2]))
    try:
        ti.insight_path("alice", "21.10.2026")
    except ValueError as e:
        out["insight_path bad"] = str(e)
    out["wants_insight"] = [ti.wants_insight(m) for m in (
        "What should I do first today?", "priorities?", "what now", "Where do I start",
        "What do I need to do before October 31?", "/context today", "", None,
        "ok, what should i do next?", "Tell me about Tom")]
    out["names"] = {k: getattr(ti, k) for k in (
        "ACTION", "TASK_NAME", "HOUSEKEEPING_DEFAULT", "INFO", "TODO_TYPES", "SECTIONS",
        "MAX_CANDIDATES", "MAX_MEMORY", "MAX_PER_SECTION", "MAX_WHY", "MAX_HEADLINE",
        "MAX_DIGEST", "MAX_ACTION_ITEMS", "CONTEXT_LIMIT", "MEMORY_SOURCE",
        "DEFAULT_SKIP_LISTS", "VOTES", "EMPTY_HEADLINE", "SYSTEM_PROMPT")}
    out["names"]["MEMORY_CATEGORIES"] = sorted(ti.MEMORY_CATEGORIES)
    actions, info, defaults = {}, {}, {}
    ti.register_builtin_action(actions, info)
    ti.register_housekeeping(defaults)
    out["registered"] = [sorted(actions), info, defaults,
                         actions[ti.ACTION] is ti.action_today_insight]
    return out


@pytest.fixture
def today(data_dir, monkeypatch):
    from routes import note_routes

    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MODULE_DIGEST_HOUR", "8")
    monkeypatch.setenv("TODAY_INSIGHT_PUSH", "on")
    state = {"notes": NOTES, "events": EVENTS, "memory": MEMORY}
    monkeypatch.setattr(ti, "owner_timezone", lambda owner: TZ)
    monkeypatch.setattr(ti, "_FILE_LOCKS", {})
    monkeypatch.setattr(ti, "_RUN_LOCKS", {})
    monkeypatch.setattr(ti, "_load_notes", lambda owner: state["notes"])
    monkeypatch.setattr(ti, "_load_events", lambda owner, now_local: state["events"])
    monkeypatch.setattr(ti, "_load_memory", lambda owner, query=None: ti.select_memory(
        state["memory"], owner=owner, query=query))
    pushed: list = []

    async def fake_dispatch(title, body, key, owner="", settings_override=None, **kw):
        pushed.append([owner, title, body, key])
        return {"ntfy_sent": True}

    monkeypatch.setattr(note_routes, "dispatch_reminder", fake_dispatch)
    return data_dir, state, pushed


def test_the_today_insight_behaves_as_recorded(today, monkeypatch, caplog):
    data_dir, state, pushed = today
    caplog.set_level(logging.DEBUG, logger="fork")
    seen = {"pure": pure(), "scenario": asyncio.run(scenario(monkeypatch, state)),
            "pushes": pushed}
    seen["files"] = _files(Path(data_dir))
    seen["logged"] = [[r.name, r.levelname, r.getMessage()] for r in caplog.records
                      if r.name.startswith("fork.insight.today")]
    seen = json.loads(json.dumps(seen, default=str))
    seen["surface"] = surface(ti)
    if os.environ.get("UPDATE_CHARACTERIZATION"):
        FIXTURE.write_text(json.dumps(seen, indent=1, sort_keys=True, ensure_ascii=False)
                           + "\n", encoding="utf-8")
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # every name the module offered stays importable from it (more may be added)
    assert set(expected.pop("surface")) <= set(seen.pop("surface"))
    for key in expected:
        assert seen[key] == expected[key], key
    assert set(seen) == set(expected)
