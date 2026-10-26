"""Fork: odysseus-sound page data from the module's MCP tools (src/sound_module_view.py)."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import sound_module_view as view

CONVS = """2 conversations (times are Europe/Berlin). When answering, put the link next to each fact you use:
- 2026-10-14 17:00–17:12 "Alice hands over vendor contracts" id c1; people: Alice, Me
  Alice moves to Berlin; the user takes over her vendor contracts.
  http://h:8000/view/c1?sig=a
- 2026-10-17 23:00–23:02 "(not summarized: looked like tv_or_media)" id c2; people: —
  
  http://h:8000/view/c2?sig=b"""

UNNAMED = """2 unnamed speakers (times are UTC). When answering, put the link next to each fact you use:
- Speaker 1 in "Standup" 2026-10-07 09:30 (conversation id c3), 4 lines; sounds like Ben (0.47)
  #31 "I finished the API migration yesterday." http://h:8000/view/c3?sig=c#t=0
  #33 "I'm a bit worried about database load." http://h:8000/view/c3?sig=c#t=16
- Speaker 2 in "Standup" 2026-10-07 09:30 (conversation id c3), 1 lines
  #32 "Nice work." http://h:8000/view/c3?sig=c#t=8"""


def test_parse_conversations():
    a, b = view.parse_conversations(CONVS)
    assert (a.id, a.start, a.end, a.title, a.people) == (
        "c1", "2026-10-14 17:00", "17:12", "Alice hands over vendor contracts", ["Alice", "Me"])
    assert a.summary.startswith("Alice moves") and a.link == "http://h:8000/view/c1?sig=a"
    assert not a.voice_note
    assert (b.people, b.summary, b.link) == ([], "", "http://h:8000/view/c2?sig=b")
    assert view.parse_conversations("No conversations found.") == []


def test_parse_unnamed():
    s1, s2 = view.parse_unnamed(UNNAMED)
    assert (s1.label, s1.conversation_id, s1.lines, s1.guess, s1.guess_score) == (
        "Speaker 1", "c3", 4, "Ben", 0.47)
    assert [q.line_id for q in s1.quotes] == [31, 33]
    assert s1.quotes[0].link.endswith("#t=0")
    assert (s2.guess, [q.text for q in s2.quotes]) == (None, ["Nice work."])
    assert view.parse_unnamed("No unnamed speakers found.") == []


ITEMS = """2 open action items (times are UTC). When answering, put the link next to each fact you use:
- #12 [open] Send Alice the revised spreadsheet (owner Me) due 2026-09-19 (Friday) — from "Q4 budget" 2026-09-15 10:00 http://h:8000/view/a?sig=x#t=40
- #13 [open] Pick up Aunt Rosa — from "Dinner plans" 2026-10-16 20:00 http://h:8000/view/b?sig=y#t=16"""


def test_parse_action_items():
    a, b = view.parse_action_items(ITEMS)
    assert (a.id, a.text, a.owner, a.due) == (
        12, "Send Alice the revised spreadsheet", "Me", "2026-09-19 (Friday)")
    assert (b.text, b.owner, b.due, b.link) == (
        "Pick up Aunt Rosa", None, None, "http://h:8000/view/b?sig=y#t=16")


# get_conversation / get_transcript as odysseus-sound's mcp_server.py prints them (fictional)
CONV = """"Alice hands over vendor contracts" — 2026-10-14 17:00–17:12 (Europe/Berlin), device omi
Speakers: Alice, Me. People: Alice, Me
Link: http://h:8000/view/c1?sig=0123456789abcdef0123456789abcdef
Summary: Alice moves to Berlin; the user takes over her vendor contracts.
* Alice leaves for the Berlin office in January
* The user owns the vendor contracts from November
Action #12 [open]: Send Alice the revised spreadsheet (owner Me) due 2026-10-23 (Friday) http://h:8000/view/c1?sig=0123456789abcdef0123456789abcdef#t=40
Action #13 [done]: Call the printer vendor http://h:8000/view/c1?sig=0123456789abcdef0123456789abcdef#t=56
Fact [Alice]: Alice moves to the Berlin office in January. http://h:8000/view/c1?sig=0123456789abcdef0123456789abcdef#t=48
When answering, put the link next to each fact you use."""

TRANSCRIPT = """"Alice hands over vendor contracts" 2026-10-14 17:00 http://h:8000/view/c1?sig=0123456789abcdef0123456789abcdef
#31 [0s 17:00:00] Alice: Morning. I have news: I'm moving to Berlin.
#32 [8s 17:00:08] Me: Wow, congratulations!
#33 [16s 17:00:16] Speaker 2: Who takes the vendor contracts?
… 5 more lines; call again with a later start_seconds.
When answering, put the link next to each fact you use."""


def test_parse_conversation():
    c = view.parse_conversation(CONV, "c1")
    assert (c.id, c.title, c.when) == (
        "c1", "Alice hands over vendor contracts",
        "2026-10-14 17:00–17:12 (Europe/Berlin), device omi")
    assert (c.speakers, c.people) == (["Alice", "Me"], ["Alice", "Me"])
    assert c.link == "http://h:8000/view/c1?sig=0123456789abcdef0123456789abcdef"
    assert c.summary == "Alice moves to Berlin; the user takes over her vendor contracts."
    assert len(c.key_points) == 2 and c.key_points[0].startswith("Alice leaves")
    a, b = c.action_items
    assert (a.id, a.status, a.text, a.owner, a.due, a.link[-5:]) == (
        12, "open", "Send Alice the revised spreadsheet", "Me", "2026-10-23 (Friday)", "#t=40")
    assert (b.id, b.status, b.text, b.owner, b.due) == (
        13, "done", "Call the printer vendor", None, None)
    assert [(f.subject, f.fact) for f in c.facts] == [
        ("Alice", "Alice moves to the Berlin office in January.")]


def test_parse_conversation_voice_note_and_empty():
    c = view.parse_conversation(
        '"Grocery list" (voice note) — 2026-10-16 08:00–08:01 (UTC), device omi, type note\n'
        "Speakers: —. People: —\nLink: http://h:8000/view/n1?sig=a\nSummary: (pending)\n"
        "some new line shape\nWhen answering, put the link next to each fact you use.", "n1")
    assert (c.title, c.when, c.speakers, c.people) == (
        "Grocery list", "2026-10-16 08:00–08:01 (UTC), device omi, type note", [], [])
    assert (c.summary, c.key_points, c.action_items, c.facts) == (
        "(pending)\nsome new line shape", [], [], [])
    assert view.parse_conversation("", "x").title == ""


def test_parse_transcript():
    t = view.parse_transcript(TRANSCRIPT)
    assert t.title == "Alice hands over vendor contracts"
    assert [(ln.id, ln.offset, ln.time, ln.speaker) for ln in t.lines] == [
        (31, 0, "17:00:00", "Alice"), (32, 8, "17:00:08", "Me"), (33, 16, "17:00:16", "Speaker 2")]
    assert t.lines[0].text == "Morning. I have news: I'm moving to Berlin."
    assert t.lines[2].link == "http://h:8000/view/c1?sig=0123456789abcdef0123456789abcdef#t=16"
    assert (t.more, t.next_start) == (True, 16)
    last = view.parse_transcript("\n".join(TRANSCRIPT.splitlines()[:3] + ["garbage", "x."]))
    assert (len(last.lines), last.more, last.next_start) == (2, False, None)


def test_parse_transcript_one_time_per_line():
    # newer modules print only the offset; the clock time comes from the header
    t = view.parse_transcript(
        '"Alice hands over vendor contracts" 2026-10-14 17:00:30 http://h:8000/view/c1?sig=a\n'
        "#31 [0s] Alice: Morning.\n#32 [45s] Me: Wow, congratulations!\n"
        "When answering, put the link next to each fact you use. To show the transcript, "
        "give the link above and a short summary; don't copy these lines into your answer.")
    assert t.title == "Alice hands over vendor contracts"
    assert [(ln.id, ln.offset, ln.time, ln.speaker, ln.text) for ln in t.lines] == [
        (31, 0, "17:00:30", "Alice", "Morning."), (32, 45, "17:01:15", "Me", "Wow, congratulations!")]
    assert t.lines[1].link == "http://h:8000/view/c1?sig=a#t=45"
    assert (t.more, t.next_start) == (False, None)


class FakeManager:
    _connections = {"srv1": {"name": "convo", "status": "connected"}}

    def __init__(self):
        self.calls = []
        self.items_json = None  # None: answer like an older module (text lines)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        tool = name.split("__")[-1]
        if tool == "update_action_item":
            return {"stdout": f"Action item #{args['item_id']} is now {args['status']}.",
                    "exit_code": 0}
        if tool in {"get_conversation", "get_transcript"} and args["conversation_id"] != "c1":
            return {"stdout": f"No conversation {args['conversation_id']!r}.", "exit_code": 0}
        if tool == "reassign_line":
            msg = (f"No line #{args['line_id']}." if args["line_id"] != 32 else
                   f"Line #32 (\"Wow, congratulations!\") moved from Me to {args['name']}; "
                   "the conversation's summary will be redone.")
            return {"stdout": msg, "exit_code": 0}
        if tool == "rename_speaker_everywhere":
            msg = (f"Renamed {args['old_name']} to {args['new_name']} in 3 lines across 1 "
                   "conversations; their summaries will be redone."
                   if args["old_name"] == "Alise" else f"No lines by {args['old_name']!r}. "
                   "Known speakers: Alice, Me.")
            return {"stdout": msg, "exit_code": 0}
        if tool == "list_action_items" and self.items_json is not None:
            return {"stdout": self.items_json, "exit_code": 0}
        out = {"search_conversations": CONVS, "list_unnamed_speakers": UNNAMED,
               "list_action_items": ITEMS, "get_conversation": CONV,
               "get_transcript": TRANSCRIPT,
               "name_speaker": "Renamed 4 lines from Speaker 1 to Ben; the summary will be redone."}
        return {"stdout": out[tool], "exit_code": 0}


@pytest.fixture
def client(monkeypatch):
    # The routes module's own require_admin: another test may swap core.middleware in
    # sys.modules, and then a fresh import names a different function.
    from routes.sound_module_routes import require_admin, setup_sound_module_routes

    manager = FakeManager()
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: manager)
    app = FastAPI()
    app.include_router(setup_sound_module_routes())
    app.dependency_overrides[require_admin] = lambda: None
    with TestClient(app) as c:
        c.manager = manager
        yield c


def test_routes_call_only_module_tools(client):
    r = client.get("/api/modules/sound/conversations", params={"since": "2026-10-01", "limit": 999})
    assert r.status_code == 200 and r.json()["conversations"][0]["id"] == "c1"
    name, args = client.manager.calls[-1]
    assert name == "mcp__srv1__search_conversations"
    assert args["since"] == "2026-10-01" and args["limit"] == 50
    assert "kind" not in args  # older module versions don't take it
    client.get("/api/modules/sound/conversations", params={"kind": "note"})
    assert client.manager.calls[-1][1]["kind"] == "note"
    client.get("/api/modules/sound/conversations", params={"kind": "bogus"})
    assert "kind" not in client.manager.calls[-1][1]
    assert len(client.get("/api/modules/sound/unnamed").json()["speakers"]) == 2

    r = client.post("/api/modules/sound/name-speaker",
                    json={"conversation_id": "c3", "speaker_label": "Speaker 1", "name": " Ben "})
    assert r.json() == {"ok": True, "message": "Renamed 4 lines from Speaker 1 to Ben; "
                                               "the summary will be redone."}
    assert client.manager.calls[-1] == ("mcp__srv1__name_speaker", {
        "conversation_id": "c3", "speaker_label": "Speaker 1", "name": "Ben",
        "remember_voice": True})
    assert client.post("/api/modules/sound/name-speaker",
                       json={"conversation_id": "c3", "speaker_label": "Speaker 1",
                             "name": ""}).status_code == 422


def test_action_items_are_live_from_the_module(client):
    r = client.get("/api/modules/sound/action-items")
    assert [i["id"] for i in r.json()["action_items"]] == [12, 13]
    assert client.manager.calls[-1] == ("mcp__srv1__list_action_items",
                                        {"status": "open", "format": "json"})
    r = client.post("/api/modules/sound/action-items/12", json={"status": "done"})
    assert r.json() == {"ok": True, "message": "Action item #12 is now done."}
    assert client.manager.calls[-1] == ("mcp__srv1__update_action_item",
                                        {"item_id": 12, "status": "done"})
    assert client.post("/api/modules/sound/action-items/12",
                       json={"status": "deleted"}).status_code == 422
    assert client.get("/api/modules/sound/action-items?status=bogus").status_code == 400


def test_module_not_connected(client):
    client.manager._connections = {}
    assert client.get("/api/modules/sound/unnamed").status_code == 503


def test_api_is_admin_only(monkeypatch):
    from routes.sound_module_routes import setup_sound_module_routes

    app = FastAPI()
    app.include_router(setup_sound_module_routes())
    monkeypatch.setattr("core.middleware.auth_disabled", lambda: False)
    with TestClient(app) as c:
        assert c.get("/api/modules/sound/unnamed").status_code == 403


def test_former_page_address_redirects(client):
    r = client.get("/barvis/sound", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/modules/sound"


def test_parse_voice_note_rows():
    # odysseus-sound marks voice notes after the title: "Title" (voice note)
    convs = view.parse_conversations(
        '- 2026-10-16 08:00–08:01 "Grocery list" (voice note) id n1; people: Me\n'
        "  Buy milk and eggs.\n  http://h:8000/view/n1?sig=a")
    assert [(c.id, c.title, c.people, c.link, c.voice_note) for c in convs] == [
        ("n1", "Grocery list", ["Me"], "http://h:8000/view/n1?sig=a", True)]
    items = view.parse_action_items(
        '- #20 [open] Buy milk due 2026-10-17 (tomorrow) — from "Grocery list" (voice note) '
        "2026-10-16 08:00 http://h:8000/view/n1?sig=a#t=3")
    assert [(i.id, i.text, i.due, i.conversation_title) for i in items] == [
        (20, "Buy milk", "2026-10-17 (tomorrow)", "Grocery list")]


def test_parse_action_item_text_with_due_word():
    a, b = view.parse_action_items(
        '- #21 [open] Confirm the due date with Ana (owner Me) due 2026-10-23 (Friday) — from '
        '"Invoices" 2026-10-16 08:00 http://h:8000/view/x?sig=a#t=3\n'
        '- #22 [open] Check the due date with Ana due next week — from '
        '"Invoices" 2026-10-16 08:00 http://h:8000/view/x?sig=a#t=9')
    assert (a.text, a.owner, a.due) == ("Confirm the due date with Ana", "Me",
                                        "2026-10-23 (Friday)")
    assert (b.text, b.owner, b.due) == ("Check the due date with Ana", None, "next week")


def test_conversation_and_transcript_routes(client):
    r = client.get("/api/modules/sound/conversations/c1")
    assert r.status_code == 200
    conv = r.json()["conversation"]
    assert conv["id"] == "c1" and conv["action_items"][0]["id"] == 12
    assert client.manager.calls[-1] == ("mcp__srv1__get_conversation", {"conversation_id": "c1"})

    r = client.get("/api/modules/sound/conversations/c1/transcript", params={"start_seconds": 8})
    body = r.json()
    assert [ln["id"] for ln in body["lines"]] == [31, 32, 33]
    assert (body["more"], body["next_start"]) == (True, 16)
    assert client.manager.calls[-1] == ("mcp__srv1__get_transcript", {
        "conversation_id": "c1", "start_seconds": 8, "max_lines": 200})

    assert client.get("/api/modules/sound/conversations/nope").status_code == 404
    assert client.get("/api/modules/sound/conversations/nope/transcript").status_code == 404
    n = len(client.manager.calls)
    assert client.get("/api/modules/sound/conversations/bad%20id").status_code == 400
    assert client.get("/api/modules/sound/conversations/" + "a" * 201).status_code == 400
    assert client.get("/api/modules/sound/conversations/a$b/transcript").status_code == 400
    assert len(client.manager.calls) == n  # nothing reached the module


def test_fix_speaker_routes(client):
    r = client.post("/api/modules/sound/lines/32", json={"name": " Alice "})
    assert r.json()["ok"] is True and r.json()["message"].startswith("Line #32")
    assert client.manager.calls[-1] == ("mcp__srv1__reassign_line",
                                        {"line_id": 32, "name": "Alice"})
    assert client.post("/api/modules/sound/lines/99", json={"name": "Alice"}).json() == {
        "ok": False, "message": "No line #99."}

    r = client.post("/api/modules/sound/rename-speaker",
                    json={"old_name": "Alise", "new_name": "Alice"})
    assert r.json()["ok"] is True
    assert client.manager.calls[-1] == ("mcp__srv1__rename_speaker_everywhere",
                                        {"old_name": "Alise", "new_name": "Alice"})
    r = client.post("/api/modules/sound/rename-speaker",
                    json={"old_name": "Zed", "new_name": "Alice"})
    assert r.json()["ok"] is False

    n = len(client.manager.calls)
    assert client.post("/api/modules/sound/lines/32", json={"name": "  "}).status_code == 400
    assert client.post("/api/modules/sound/lines/0", json={"name": "Alice"}).status_code == 400
    assert client.post("/api/modules/sound/rename-speaker",
                       json={"old_name": "Alise", "new_name": "x" * 101}).status_code == 400
    assert len(client.manager.calls) == n


def test_reassign_line_already_correct_is_ok():
    import asyncio

    async def call(tool, args):
        return f"Line #{args['line_id']} is already {args['name']}'s."

    assert asyncio.run(view.reassign_line(call, 5, "Ben")) == {
        "ok": True, "message": "Line #5 is already Ben's."}


# ------------------------------------------------- action items as JSON, filing (fictional)
ITEMS_JSON = [
    {"id": 12, "status": "open", "kind": "event", "text": "Meet Jason", "owner": "Me",
     "due": "Wednesday at 12 pm", "due_date": "2026-10-21", "due_time": "12:00",
     "duration_minutes": None, "location": "Cafe Nord", "time_unclear": False,
     "conversation_id": "n1", "conversation_title": "Jason meeting",
     "conversation_kind": "note", "conversation_at": "2026-10-20T09:00:00+00:00",
     "created_at": "2026-10-20T09:05:00+00:00", "timezone": "Europe/Berlin",
     "link": "http://h:8000/view/n1?sig=a#t=4"},
    {"id": 13, "status": "open", "kind": "task", "text": "Send Alice the budget",
     "owner": None, "due": "at 9", "due_date": "2026-10-22", "due_time": "09:00",
     "duration_minutes": None, "location": None, "time_unclear": True,
     "conversation_id": "c1", "conversation_title": "Q4 budget",
     "conversation_kind": "conversation", "conversation_at": "2026-10-19T15:00:00+00:00",
     "created_at": "2026-10-19T15:20:00+00:00", "timezone": "Europe/Berlin",
     "link": "http://h:8000/view/c1?sig=b#t=40"},
]


def test_parse_action_items_json():
    import json

    a, b = view.parse_action_items_result(json.dumps(ITEMS_JSON))
    assert (a.id, a.kind, a.text, a.due_date, a.due_time, a.voice_note, a.location, a.when) == (
        12, "event", "Meet Jason", "2026-10-21", "12:00", True, "Cafe Nord", "2026-10-20 11:00")  # Berlin
    assert (b.kind, b.time_unclear, b.voice_note, b.owner) == ("task", True, False, None)
    assert view.parse_action_items_result("[]") == []
    # junk rows and fields degrade instead of failing
    odd = view.parse_action_items_result(json.dumps(
        [{"id": "x"}, "nope", {"id": 5, "kind": "party", "due_date": "soon", "due_time": "9"}]))
    assert [(i.id, i.kind, i.due_date, i.due_time) for i in odd] == [(5, "task", None, None)]


def test_parse_action_items_text_fallback_new_shape():
    # odysseus-sound >= 0.2 text lines: kind word, time in the due part, [time unclear]
    a, b, c = view.parse_action_items_result(
        '- #12 [open] event: Meet Jason (owner Me) due 2026-10-21 12:00 (Wednesday at 12 pm) '
        '— from "Jason meeting" (voice note) 2026-10-20 09:00 http://h:8000/view/n1?sig=a#t=4\n'
        '- #13 [open] Send Alice the budget due 2026-10-22 09:00 (at 9) [time unclear] — from '
        '"Q4 budget" 2026-10-19 15:00 http://h:8000/view/c1?sig=b#t=40\n'
        '- #14 [open] reminder: Call Mom due today at 5 — from "Errands" (voice note) '
        '2026-10-20 08:00 http://h:8000/view/n2?sig=c#t=1')
    assert (a.kind, a.text, a.owner, a.due_date, a.due_time, a.voice_note) == (
        "event", "Meet Jason", "Me", "2026-10-21", "12:00", True)
    assert (b.kind, b.text, b.due_time, b.time_unclear, b.voice_note) == (
        "task", "Send Alice the budget", "09:00", True, False)
    assert (c.kind, c.due, c.due_date) == ("reminder", "today at 5", None)


def test_merge_filed_state():
    import json

    items = view.parse_action_items_result(json.dumps(ITEMS_JSON))
    view.merge_filed(items, {12: {"kind": "event", "target": "calendar", "ref": "uid-1",
                                  "at": "2026-10-20T09:06:00+00:00"},
                             "13": {"target": "todo", "ref": "note-9", "undone": True}})
    assert items[0].filed == {"target": "calendar", "ref": "uid-1", "open_url": "/#event-uid-1"}
    assert items[1].filed is None  # undone: shown as not filed
    view.merge_filed(items, {"13": {"kind": "task", "target": "todo", "ref": "note-9"}})
    assert items[1].filed["open_url"] == "/#open=notes&note=note-9"
    assert items[0].filed is None
    assert view.open_url("calendar", "bad uid<x>") is None


def test_check_move():
    assert view.check_move("event", "2026-10-21", "12:00") == ("event", "2026-10-21", "12:00")
    assert view.check_move("task", "", None) == ("task", None, None)
    assert view.check_move("task", "none", "none") == ("task", "none", "none")
    for bad in [("meeting", None, None), ("task", "2026-02-30", None), ("task", "21.10.", None),
                ("task", None, "25:00"), ("task", None, "9:00")]:
        with pytest.raises(ValueError):
            view.check_move(*bad)


@pytest.fixture
def filing(client, monkeypatch):
    """Fake src/module_action_items.py page helpers (the real ones file into Odysseus)."""
    import json

    from src import module_action_items as mai

    client.manager.items_json = json.dumps(ITEMS_JSON)
    state = {12: {"kind": "event", "target": "calendar", "ref": "uid-1", "at": "x"}}
    calls = []

    async def file_item(owner, item, call, *, target=None):
        calls.append(("file", owner, item["id"], target))
        state[item["id"]] = {"kind": item["kind"], "target": target, "ref": "note-7", "at": "y"}
        return {"target": target, "ref": "note-7", "open_url": "/#open=notes&note=note-7"}

    async def unfile_item(owner, item_id, call):
        calls.append(("undo", owner, item_id))
        state[item_id]["undone"] = True
        await call("update_action_item", {"item_id": item_id, "status": "dismissed"})
        return {"ok": True}

    async def move_item(owner, item_id, call, *, kind, due_date=None, due_time=None,
                        list_name=None):
        calls.append(("move", owner, item_id, kind, due_date, due_time)
                     + ((list_name,) if list_name else ()))
        if item_id == 99:
            raise LookupError("No action item #99.")
        if item_id == 98:
            raise RuntimeError("calendar store is down")
        state[item_id] = {"kind": kind, "target": "todo" if kind != "event" else "calendar",
                          "ref": "note-8", "at": "z"}
        if list_name and list_name != "none":
            state[item_id]["list"] = list_name.title()
        return {"target": state[item_id]["target"], "ref": "note-8", "open_url": None}

    def filed_state(owner):
        calls.append(("state", owner))
        return dict(state)

    def todo_lists(owner):
        return ["Packing", "Shopping"]

    for name, fn in [("file_item", file_item), ("unfile_item", unfile_item),
                     ("move_item", move_item), ("filed_state", filed_state),
                     ("todo_lists", todo_lists)]:
        monkeypatch.setattr(mai, name, fn, raising=False)
    client.filing_calls = calls
    return client


def test_action_items_route_merges_filed_state(filing):
    items = filing.get("/api/modules/sound/action-items").json()["action_items"]
    assert [(i["id"], i["kind"], i["voice_note"]) for i in items] == [
        (12, "event", True), (13, "task", False)]
    assert items[0]["filed"] == {"target": "calendar", "ref": "uid-1",
                                 "open_url": "/#event-uid-1"}
    assert items[1]["filed"] is None


def test_action_items_route_without_filing_helpers(client, monkeypatch):
    from src import module_action_items as mai

    monkeypatch.delattr(mai, "filed_state", raising=False)
    items = client.get("/api/modules/sound/action-items").json()["action_items"]
    assert [i["filed"] for i in items] == [None, None]  # text fallback, nothing filed
    assert client.post("/api/modules/sound/action-items/13/undo").status_code == 503


def test_file_suggestion_route(filing):
    r = filing.post("/api/modules/sound/action-items/13/file", json={"target": "todo"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["filed"]["ref"] == "note-7"
    assert body["item"]["id"] == 13 and body["item"]["filed"]["target"] == "todo"
    assert ("file", "", 13, "todo") in filing.filing_calls
    # filed already, unknown, bad target, bad id
    assert filing.post("/api/modules/sound/action-items/12/file",
                       json={"target": "calendar"}).status_code == 409
    assert filing.post("/api/modules/sound/action-items/77/file",
                       json={"target": "calendar"}).status_code == 404
    assert filing.post("/api/modules/sound/action-items/13/file",
                       json={"target": "inbox"}).status_code == 422
    assert filing.post("/api/modules/sound/action-items/0/file",
                       json={"target": "todo"}).status_code == 400


def test_move_route(filing):
    r = filing.post("/api/modules/sound/action-items/12/move",
                    json={"kind": "task", "due_date": "2026-10-22", "due_time": "08:30"})
    assert r.status_code == 200
    assert r.json()["item"]["filed"]["open_url"] == "/#open=notes&note=note-8"
    assert filing.filing_calls[-2] == ("move", "", 12, "task", "2026-10-22", "08:30")
    n = len(filing.filing_calls)
    for body in [{"kind": "meeting"}, {"kind": "task", "due_date": "2026-13-01"},
                 {"kind": "task", "due_time": "7pm"}]:
        assert filing.post("/api/modules/sound/action-items/12/move", json=body).status_code == 400
    assert filing.post("/api/modules/sound/action-items/12/move",
                       json={"kind": "task", "due_date": "x" * 11}).status_code == 422
    assert len(filing.filing_calls) == n  # nothing reached the helpers
    assert filing.post("/api/modules/sound/action-items/99/move",
                       json={"kind": "event"}).status_code == 404
    r = filing.post("/api/modules/sound/action-items/98/move", json={"kind": "event"})
    assert r.status_code == 502 and "calendar store" not in r.text


def test_undo_route(filing):
    r = filing.post("/api/modules/sound/action-items/12/undo")
    assert r.status_code == 200 and r.json()["ok"]
    assert ("undo", "", 12) in filing.filing_calls
    assert filing.manager.calls[-2] == ("mcp__srv1__update_action_item",
                                        {"item_id": 12, "status": "dismissed"})
    assert r.json()["item"]["filed"] is None


def test_filing_routes_need_the_module(filing):
    filing.manager._connections = {}
    assert filing.post("/api/modules/sound/action-items/13/file",
                       json={"target": "todo"}).status_code == 503


def test_filing_routes_are_admin_only(monkeypatch):
    from routes.sound_module_routes import setup_sound_module_routes

    app = FastAPI()
    app.include_router(setup_sound_module_routes())
    monkeypatch.setattr("core.middleware.auth_disabled", lambda: False)
    with TestClient(app) as c:
        for path, body in [("13/file", {"target": "todo"}), ("13/move", {"kind": "task"}),
                           ("13/undo", None)]:
            assert c.post(f"/api/modules/sound/action-items/{path}", json=body).status_code == 403


def test_move_onto_a_list_route(filing):
    r = filing.post("/api/modules/sound/action-items/13/move",
                    json={"kind": "task", "list_name": "  shopping  "})
    assert r.status_code == 200
    assert filing.filing_calls[-2] == ("move", "", 13, "task", None, None, "shopping")
    assert r.json()["item"]["filed"]["list"] == "Shopping"
    r = filing.post("/api/modules/sound/action-items/13/move",
                    json={"kind": "task", "list_name": "none"})
    assert r.status_code == 200 and "list" not in r.json()["item"]["filed"]
    assert filing.post("/api/modules/sound/action-items/13/move",
                       json={"kind": "task", "list_name": "x" * 41}).status_code == 400
    assert filing.post("/api/modules/sound/action-items/13/move",
                       json={"kind": "task", "list_name": "x" * 61}).status_code == 422
    assert filing.get("/api/modules/sound/todo-lists").json() == {
        "lists": ["Packing", "Shopping"]}


def test_check_list_name_and_json_list():
    assert view.check_list_name(None) is None and view.check_list_name("  ") is None
    assert view.check_list_name(" NONE ") == "none"
    assert view.check_list_name(" Hardware   store ") == "Hardware store"
    for bad in ("x" * 41, "milk\x07list"):
        with pytest.raises(ValueError):
            view.check_list_name(bad)
    item = view.item_from_json({"id": 5, "text": "Milk", "list_name": "shopping"})
    assert item.list_name == "shopping"
    assert view.filed_for({5: {"target": "todo", "ref": "n1", "list": "Shopping"}}, 5) == {
        "target": "todo", "ref": "n1", "open_url": "/#open=notes&note=n1", "list": "Shopping"}


def test_note_items_and_filed_notes():
    item = view.item_from_json({"id": 6, "text": "Battery is 3 mm", "kind": "note",
                                "note_title": "Battery", "list_name": "hardware"})
    assert (item.kind, item.note_title, item.list_name) == ("note", "Battery", "hardware")
    assert view.filed_for({6: {"target": "note", "ref": "n2", "notes": "Hardware notes"}},
                          6) == {"target": "note", "ref": "n2",
                                 "open_url": "/#open=notes&note=n2",
                                 "notes": "Hardware notes"}
    assert view.check_target("note") == "note"
    with pytest.raises(ValueError):  # nothing is moved to a note
        view.check_move("note", None, None)


def test_file_as_note_route(filing):
    r = filing.post("/api/modules/sound/action-items/13/file", json={"target": "note"})
    assert r.status_code == 200 and r.json()["message"] == "Added to notes."
    assert ("file", "", 13, "note") in filing.filing_calls


def test_unfile_todos_route(client, monkeypatch):
    from src import module_action_items as mai

    calls = []

    async def unfile_auto_todos(owner, call, *, apply=False, only=None, include_all=False):
        calls.append((owner, apply, only, include_all))
        return {"apply": apply, "removed": 1 if apply else 0,
                "todos": [{"id": 12, "title": "Call Mom", "text": "Call Mom",
                           "status": "open", "ref": "note-1"}]}

    monkeypatch.setattr(mai, "unfile_auto_todos", unfile_auto_todos)
    r = client.post("/api/modules/sound/unfile-todos", json={})
    assert r.status_code == 200 and r.json()["todos"][0]["id"] == 12
    assert r.json()["removed"] == 0 and calls[-1] == ("", False, None, False)
    r = client.post("/api/modules/sound/unfile-todos",
                    json={"apply": True, "ids": [12], "all": True})
    assert r.json()["removed"] == 1 and calls[-1] == ("", True, {12}, True)
    client.manager._connections = {}
    assert client.post("/api/modules/sound/unfile-todos", json={}).status_code == 503


def test_unfile_script_reports_through_the_route():
    import httpx

    from scripts import unfile_voice_note_todos as script

    seen = []

    def handler(request):
        seen.append((request.url.path, request.content))
        return httpx.Response(200, json={"ok": True, "apply": True, "removed": 1, "todos": [
            {"id": 12, "title": "Call Mom", "text": "Call Mom", "status": "open",
             "ref": "n"}]})

    args = script.parse_args(["--apply", "--only", "12", "--all"])
    with httpx.Client(base_url="http://hub", transport=httpx.MockTransport(handler)) as c:
        assert script.run(c, args) == 0
    assert seen == [("/api/modules/sound/unfile-todos", b'{"apply":true,"ids":[12],"all":true}')]
    assert script.report({"apply": False, "todos": [
        {"id": 12, "title": "Call Mom", "status": "open",
         "why": ["filed before explicit filing", "not explicit now"]}]}).splitlines() == [
        "1 to-dos would be removed (run again with --apply):",
        "- #12 [open] Call Mom (filed before explicit filing; not explicit now)"]
    assert script.parse_args([]).apply is False


def test_parse_action_items_result_skips_other_peoples_items():
    rows = [{"id": 1, "text": "Buy milk", "mine": True},
            {"id": 2, "text": "Let Shahyah know how it goes", "owner": "Hayden", "mine": False},
            {"id": 3, "text": "Call Mom"}]  # an older module sends no flag
    items = view.parse_action_items_result(json.dumps(rows))
    assert [i.id for i in items] == [1, 3]
