"""Fork: voice actions (fork/voice/actions.py): adding and completing items by voice,
filed exactly as a voice note's items are (fork/athena/action_items_filing.py), the
setting, the push, and the brain's allow-list for them. Real Odysseus notes and calendar
in a temp database, a fake Athena and a fake model; fictional data."""
from __future__ import annotations

import datetime
import json

import pytest

import core.database as cdb
from fork.athena import action_items as mai
from fork.athena.action_items_state import load_state, save_state
from fork.brain import turn as brain_turn
from fork.voice import actions as va
from fork.voice import openai_api, prompt as voice_prompt
from fork.voice.speech import SpeechStream, strip_offers, to_speech
from tests.fork.brain_fakes import Brain, calls, text
from tests.fork.test_voice_assistant import _token_request, api  # noqa: F401 -- fixture
from tests.helpers.sqlite_db import make_temp_sqlite

# Monday 20 October 2031, 07:00 UTC
NOW = datetime.datetime(2031, 10, 20, 7, 0, tzinfo=datetime.timezone.utc)
UPDATE = "mcp__convo__update_action_item"
ERASE = "mcp__convo__erase_fact"


@pytest.fixture
def pushes(data_dir, monkeypatch):
    from routes import note_routes

    sent = []

    async def fake_dispatch(title, body, key, owner="", settings_override=None, **kw):
        sent.append({"title": title, "body": body, "owner": owner})
        return {"ntfy_sent": True}

    monkeypatch.setattr(note_routes, "dispatch_reminder", fake_dispatch)
    monkeypatch.delenv("MODULE_NOTIFY_CHANNEL", raising=False)
    monkeypatch.setenv("TZ", "UTC")
    va._RECENT.clear()
    va._CLARIFY.clear()
    return sent


@pytest.fixture
def hub(pushes, monkeypatch):
    """A real Odysseus calendar and notes database in a temp file."""
    import routes.calendar_routes as cr
    from src.user_time import clear_user_time_context

    factory, engine, _tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(cr, "SessionLocal", factory, raising=False)
    monkeypatch.delenv("MODULE_DIGEST_HOUR", raising=False)
    clear_user_time_context()
    yield factory
    engine.dispose()


SKIP = {"id", "uid", "owner", "label", "content", "description", "calendar_id",
        "created_at", "updated_at", "last_modified", "dtstamp"}


def snapshot(factory, owner):
    """What was made for ``owner``, without ids, owners, labels and source lines."""
    def row(obj):
        return {c.name: getattr(obj, c.name) for c in obj.__table__.columns
                if c.name not in SKIP}
    with factory() as db:
        notes = [row(n) for n in db.query(cdb.Note).filter(cdb.Note.owner == owner).all()]
        events = [row(e) for e in db.query(cdb.CalendarEvent).join(cdb.CalendarCal)
                  .filter(cdb.CalendarCal.owner == owner).all()]
    return notes, events


def all_notes(factory, owner="alice"):
    with factory() as db:
        return db.query(cdb.Note).filter(cdb.Note.owner == owner).all()


def all_events(factory, owner="alice"):
    with factory() as db:
        return db.query(cdb.CalendarEvent).join(cdb.CalendarCal).filter(
            cdb.CalendarCal.owner == owner).all()


async def say_add(owner, said, **args):
    return await va.add(owner, args, said=said, now=NOW)


# ------------------------------------------------------- filed as a voice note is
CASES = [
    ("Remind me to call the dentist tomorrow at 9",
     dict(kind="reminder", text="call the dentist", due_date="2031-10-21", due_time="09:00")),
    ("Add a to-do to renew the passport on Friday",
     dict(kind="task", text="renew the passport", due_date="2031-10-24")),
    ("Add a task to fix the bike light",
     dict(kind="task", text="fix the bike light")),  # undated: the "Voice notes" list
    ("Add milk to my shopping list",
     dict(kind="task", text="milk", list_name="shopping")),
    ("Put lunch with Sam on Friday at noon on my calendar",
     dict(kind="event", text="Lunch with Sam", due_date="2031-10-24", due_time="12:00")),
    ("Put the school fair on Saturday on my calendar",
     dict(kind="event", text="School fair", due_date="2031-10-25")),  # all day
    ("Make a note that the garage code is changing",
     dict(kind="note", text="The garage code is changing", note_title="Garage code")),
    ("Add to my hardware notes that the soldering iron needs a new tip",
     dict(kind="note", text="The soldering iron needs a new tip", list_name="hardware")),
]


@pytest.mark.parametrize("said,args", CASES, ids=[c[1]["kind"] + ":" + c[1]["text"][:12]
                                                  for c in CASES])
async def test_each_item_is_filed_as_the_same_sentence_in_a_voice_note(hub, said, args):
    target = mai.default_target({**args, "conversation_kind": "note"})
    athena_row = {"id": 12, **args, "conversation_kind": "note", "status": "open"}
    await mai._file_one("athena-owner", athena_row, target, now_utc=NOW)
    result = await say_add("voice-owner", said, **args)
    assert result["exit_code"] == 0, result
    assert snapshot(hub, "voice-owner") == snapshot(hub, "athena-owner")
    assert snapshot(hub, "voice-owner") != ([], [])


async def test_undated_task_goes_on_the_voice_notes_list(hub):
    res = await say_add("alice", "add a task to fix the bike light", kind="task",
                        text="fix the bike light")
    (note,) = all_notes(hub)
    assert note.title == "Voice notes" and note.label == va.LABEL
    assert [i["text"] for i in json.loads(note.items)] == ["fix the bike light"]
    assert res["output"] == "Added 'fix the bike light' to your Voice notes list."


async def test_named_list_and_named_notes_get_one_line_each(hub):
    await say_add("alice", "add milk to my shopping list", kind="task", text="milk",
                  list_name="shopping")
    await say_add("alice", "and eggs to my shopping list", kind="task", text="eggs",
                  list_name="my shopping list")
    res = await say_add("alice", "add to my hardware notes: order flux", kind="note",
                        text="order flux", list_name="hardware")
    lists = {n.title: n for n in all_notes(hub)}
    assert [i["text"] for i in json.loads(lists["Shopping"].items)] == ["milk", "eggs"]
    assert lists["Hardware notes"].content == "- order flux"
    assert res["output"] == "Added 'order flux' to your Hardware notes."


async def test_reminder_and_event_replies_and_due_times(hub):
    res = await say_add("alice", "remind me to call the dentist tomorrow at 9",
                        kind="reminder", text="call the dentist", due_date="tomorrow",
                        due_time="9am")
    assert res["output"] == "Added 'call the dentist' to your to-dos, due tomorrow at 9 a.m."
    (todo,) = all_notes(hub)
    assert todo.due_date is not None and todo.content == va.SOURCE
    res = await say_add("alice", "put lunch with Sam on Friday at noon on my calendar",
                        kind="event", text="Lunch with Sam", due_date="Friday",
                        due_time="noon")
    assert res["output"] == "Put 'Lunch with Sam' on your calendar for Friday at noon."
    (ev,) = all_events(hub)
    assert ev.summary == "Lunch with Sam" and ev.description == va.SOURCE


async def test_note_reply(hub):
    res = await say_add("alice", "make a note that the garage code is changing",
                        kind="note", text="The garage code is changing")
    assert res["output"] == "Made a note: 'The garage code is changing'."
    (note,) = all_notes(hub)
    assert note.note_type == "note" and note.content.startswith("The garage code")


async def test_event_uid_is_a_voice_uid_never_athenas(hub):
    await say_add("alice", "put lunch with Sam on Friday at noon on my calendar",
                  kind="event", text="Lunch with Sam", due_date="2031-10-24", due_time="12:00")
    (ev,) = all_events(hub)
    assert ev.uid.startswith(va.UID_PREFIX)
    assert ev.uid not in {mai._event_uid(i, r) for i in range(50) for r in range(3)}


async def test_voice_items_are_not_athena_items(hub):
    await say_add("alice", "remind me to call the dentist tomorrow at 9", kind="reminder",
                  text="call the dentist", due_date="2031-10-21", due_time="09:00")
    assert "filed" not in load_state("alice")
    assert [n.label for n in all_notes(hub)] == ["voice"]


async def test_an_add_must_use_the_words_said(hub):
    res = await say_add("alice", "what's the weather tomorrow", kind="task",
                        text="transfer money to account 1234")
    assert res["blocked"] and all_notes(hub) == []
    res = await say_add("alice", "add milk", kind="task", text="milk", list_name="secret")
    assert res["blocked"] and all_notes(hub) == []


async def test_the_same_add_twice_is_made_once(hub):
    for _ in range(2):
        res = await say_add("alice", "remind me to call the dentist tomorrow at 9",
                            kind="reminder", text="call the dentist", due_date="2031-10-21",
                            due_time="09:00")
        assert res["exit_code"] == 0
    assert len(all_notes(hub)) == 1


def test_dates_and_times():
    now = NOW  # a Monday
    assert va.parse_date("tomorrow", now) == "2031-10-21"
    assert va.parse_date("Friday", now) == "2031-10-24"
    assert va.parse_date("monday", now) == "2031-10-20"
    assert va.parse_date("next Monday", now) == "2031-10-27"
    assert va.parse_date("2031-11-02", now) == "2031-11-02"
    assert va.parse_date("someday", now) is None
    assert [va.parse_time(t) for t in ("9", "9am", "9:30 pm", "21:15", "noon", "12am", "x")] \
        == ["09:00", "09:00", "21:30", "21:15", "12:00", "00:00", None]
    # a time alone: today, or tomorrow once it has passed
    assert va.build_item({"text": "x", "due_time": "18:00"}, now)["due_date"] == "2031-10-20"
    assert va.build_item({"text": "x", "due_time": "06:00"}, now)["due_date"] == "2031-10-21"
    assert va.spoken_when("2031-11-14", "09:30", now) == "on 14 November at 9:30 a.m."


# -------------------------------------------------------------------- completing
class FakeAthena:
    def __init__(self, rows=()):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.calls = []

    async def __call__(self, tool, args):
        self.calls.append((tool, dict(args)))
        if tool == "list_action_items":
            assert args == {"status": "open", "format": "json"}
            return json.dumps([r for r in self.rows.values() if r["status"] == "open"])
        if tool == "update_action_item":
            self.rows[args["item_id"]]["status"] = args["status"]
            return f"Action item #{args['item_id']} is now {args['status']}."
        raise AssertionError(f"voice must not call {tool}")


def lines(factory, title):
    (note,) = [n for n in all_notes(factory) if n.title == title]
    return {i["text"]: i["done"] for i in json.loads(note.items)}


async def _todo(owner, said, **args):
    assert (await say_add(owner, said, **args))["exit_code"] == 0


async def test_complete_one_odysseus_todo(hub, pushes):
    await _todo("alice", "remind me to send the revised spreadsheet to Alice tomorrow",
                kind="reminder", text="send the revised spreadsheet to Alice",
                due_date="tomorrow")
    pushes.clear()
    athena = FakeAthena()
    res = await va.complete("alice", {"description": "revised spreadsheet"},
                            said="mark the revised spreadsheet to-do as done", call=athena)
    assert res["output"] == "Marked 'send the revised spreadsheet to Alice' done."
    assert lines(hub, "send the revised spreadsheet to Alice") == {
        "send the revised spreadsheet to Alice": True}
    assert pushes == [{"title": "Voice assistant", "owner": "alice",
                       "body": "Voice: Marked 'send the revised spreadsheet to Alice' done."}]


async def test_complete_a_list_line(hub):
    for item in ("milk", "eggs"):
        await _todo("alice", f"add {item} to my shopping list", kind="task", text=item,
                    list_name="shopping")
    res = await va.complete("alice", {"description": "milk"}, said="tick off milk",
                            call=FakeAthena())
    assert res["output"] == "Marked 'milk' done."
    assert lines(hub, "Shopping") == {"milk": True, "eggs": False}


async def test_complete_an_athena_item(hub):
    athena = FakeAthena([{"id": 7, "status": "open", "text": "Book the restaurant"},
                         {"id": 8, "status": "open", "text": "Review Ben's pull request"}])
    res = await va.complete("alice", {"description": "the restaurant"},
                            said="I've booked the restaurant, mark it done", call=athena)
    assert res["output"] == "Marked 'Book the restaurant' done."
    assert ("update_action_item", {"item_id": 7, "status": "done"}) in athena.calls
    assert athena.rows[8]["status"] == "open"


async def test_two_matches_change_nothing_and_ask_which(hub, pushes):
    await _todo("alice", "add a task to call the dentist", kind="task",
                text="call the dentist")
    athena = FakeAthena([{"id": 7, "status": "open", "text": "Call the plumber"}])
    pushes.clear()
    res = await va.complete("alice", {"description": "call"}, said="mark the call done",
                            chat="c1", call=athena)
    assert res["output"] == "Which one: 'call the dentist' on Voice notes or 'Call the plumber'?"
    assert res["changed"] is False and pushes == []
    assert lines(hub, "Voice notes") == {"call the dentist": False}
    assert [c for c in athena.calls if c[0] == "update_action_item"] == []
    # the answer names an option: it counts as said for the next turn of that chat
    res = await va.complete("alice", {"description": "Call the plumber"},
                            said="the second one", chat="c1", call=athena)
    assert res["output"] == "Marked 'Call the plumber' done."
    assert athena.rows[7]["status"] == "done"


async def test_no_match_says_so(hub):
    await _todo("alice", "add milk to my shopping list", kind="task", text="milk",
                list_name="shopping")
    res = await va.complete("alice", {"description": "the tax return"},
                            said="mark the tax return done", call=FakeAthena())
    assert res["output"] == "I couldn't find an open to-do matching 'the tax return'."
    assert lines(hub, "Shopping") == {"milk": False}


async def test_a_filed_athena_item_is_one_item(hub):
    """A voice-note item the job filed as a to-do: the to-do is ticked and the module told
    at once; it isn't offered twice."""
    athena = FakeAthena([{"id": 12, "status": "open", "text": "Water the plants",
                          "kind": "task", "conversation_kind": "note"}])
    entry = await mai._file_one("alice", athena.rows[12], "todo", now_utc=NOW)
    save_state("alice", {"filed": {"12": entry}})
    res = await va.complete("alice", {"description": "water the plants"},
                            said="I watered the plants, mark it done", call=athena)
    assert res["output"] == "Marked 'Water the plants' done."
    assert lines(hub, "Voice notes") == {"Water the plants": True}
    assert athena.rows[12]["status"] == "done"


async def test_a_complete_must_use_the_words_said(hub):
    await _todo("alice", "add milk to my shopping list", kind="task", text="milk",
                list_name="shopping")
    res = await va.complete("alice", {"description": "milk"}, said="what's on today",
                            call=FakeAthena())
    assert res["blocked"] and lines(hub, "Shopping") == {"milk": False}


# ---------------------------------------------------------------- setting, push
def test_setting_defaults_on_and_saves(data_dir):
    assert va.settings() == {"actions": True, "push": True}
    assert isinstance(va.for_turn(), va.VoiceActions)
    assert va.save(actions=False) == {"actions": False, "push": True}
    assert va.for_turn() is None
    assert va.save(push=False, actions=True) == {"actions": True, "push": False}


async def test_push_is_not_sent_when_its_setting_is_off(hub, pushes):
    va.save(push=False)
    await _todo("alice", "add milk to my shopping list", kind="task", text="milk",
                list_name="shopping")
    assert pushes == []
    va.save(push=True)
    await _todo("alice", "add eggs to my shopping list", kind="task", text="eggs",
                list_name="shopping")
    assert pushes[-1]["body"] == "Voice: Added 'eggs' to your Shopping list."


def test_route_off_is_read_only_as_before_and_on_offers_actions(api):
    ask = {"model": "odysseus", "messages": [{"role": "user", "content": "Hi"}]}
    va.save(actions=False)
    assert api.post("/api/chat/voice/v1/chat/completions", json=ask,
                    headers={"x-test-scopes": "voice"}).status_code == 200
    off = api.turns[-1]
    assert off["actions"] is None and off["instructions"] == voice_prompt.INSTRUCTIONS
    va.save(actions=True)
    api.post("/api/chat/voice/v1/chat/completions", json=ask,
             headers={"x-test-scopes": "voice"})
    on = api.turns[-1]
    assert isinstance(on["actions"], va.VoiceActions)
    assert on["instructions"] == voice_prompt.ACTION_INSTRUCTIONS


def test_settings_route_is_admin_only(api):
    from core.middleware import require_admin

    assert api.get("/api/fork/voice/settings").status_code == 403
    api.app.dependency_overrides[require_admin] = lambda: None
    assert api.get("/api/fork/voice/settings").json() == {"actions": True, "push": True}
    assert api.put("/api/fork/voice/settings", json={"push": False}).json() == {
        "actions": True, "push": False}


# ------------------------------------------------------- in the brain's voice turn
@pytest.fixture
def brain(monkeypatch, data_dir, pushes):
    b = Brain(monkeypatch, data_dir)
    import routes.calendar_routes as cr
    monkeypatch.setattr(cr, "SessionLocal", cdb.SessionLocal, raising=False)
    return b


async def _voice(sess, message, *, actions):
    events = brain_turn.read_only_turn(
        _token_request(sess.owner), sess=sess, session_id=sess.id, message=message,
        owner=sess.owner, instructions="Speak briefly.", actions=actions)
    assert events is not None
    return [c async for c in events]


def _offered(request) -> set[str]:
    return {t["function"]["name"] for t in request.get("tools") or []}


def _tool_messages(request):
    return [m["content"] for m in request["messages"] if m.get("role") == "tool"]


async def test_off_offers_no_actions_and_refuses_them(brain):
    sess = brain.session()
    brain.llm.rounds([calls((va.ADD, {"kind": "task", "text": "milk"}))],
                     [text("I can't make changes by voice.")])
    await _voice(sess, "add milk to my shopping list", actions=None)
    assert not _offered(brain.llm.requests[0]) & va.NAMES
    assert "not available here" in _tool_messages(brain.llm.requests[1])[-1]


async def test_on_offers_the_two_actions_and_no_other_write(brain):
    sess = brain.session()
    brain.llm.rounds([text("Hello.")])
    await _voice(sess, "hello", actions=va.VoiceActions(now=NOW))
    offered = _offered(brain.llm.requests[0])
    assert va.NAMES <= offered
    assert not {UPDATE, ERASE, "generate_image", "send_email"} & offered


async def test_an_action_runs_without_asking_and_other_writes_stay_refused(brain):
    owner = "alice"
    sess = brain.session()
    brain.llm.rounds(
        [calls((UPDATE, {"item_id": 3, "status": "done"}),
               ("manage_calendar", {"action": "create_event", "title": "Dentist"}),
               (va.ADD, {"kind": "task", "text": "oat milk brain test",
                         "list_name": "shopping"}))],
        [text("Added 'oat milk brain test' to your Shopping list. Would you like anything else?")])
    chunks = await _voice(sess, "add oat milk brain test to my shopping list",
                          actions=va.VoiceActions(now=NOW))
    results = _tool_messages(brain.llm.requests[1])
    assert "not available here" in results[0]  # never offered
    assert "can only read" in results[1]  # a store's write: refused
    assert results[2] == "Added 'oat milk brain test' to your Shopping list."
    assert brain.ran == []  # upstream's runner ran none of them
    assert brain_turn.pending_count() == 0
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    assert not any(e.get("type") == "ask_user" or e.get("ask_user") for e in events)
    from fork.athena.action_items_lists import find_list
    assert "oat milk brain test" in find_list(owner, "shopping").items
    # the whole answer, as Home Assistant gets it, ends without the offer
    answer = openai_api.Answer(streaming=False)

    async def replay():
        for c in chunks:
            yield c
    await openai_api.collect(replay(), answer)
    assert answer.speech.spoken == "Added 'oat milk brain test' to your Shopping list."


# ------------------------------------------------------------------- spoken text
def test_any_link_lead_in_and_its_link_names_are_not_read():
    spoken, links = to_speech(
        "Alice cut the budget by 15%. Links to the conversations: "
        "[Q4 Budget Cut](https://athena.example.test/view/1) and "
        "[Alice's Office Move](https://athena.example.test/view/2).")
    assert spoken == "Alice cut the budget by 15%."
    assert len(links) == 2
    assert to_speech("Links to the past are fun: yes.")[0] == "Links to the past are fun: yes."


def test_label_only_parentheses_go_with_their_link():
    for answer in ("It will rain in Lisbon (Web [source](https://w.example.test/a)).",
                   "It will rain in Lisbon (Web https://w.example.test/a)."):
        assert to_speech(answer)[0] == "It will rain in Lisbon."
    assert to_speech("Lunch (with Sam) at noon.")[0] == "Lunch (with Sam) at noon."


def test_trailing_offers_and_questions_are_dropped_but_not_which_one():
    assert strip_offers("Marked 'milk' done. Would you like anything else?") \
        == "Marked 'milk' done."
    assert strip_offers("It's Paris. Let me know if you need more.") == "It's Paris."
    assert strip_offers("It's sunny. Shall I check tomorrow too?") == "It's sunny."
    assert strip_offers("I found two. Which one: 'a' or 'b'?") == \
        "I found two. Which one: 'a' or 'b'?"
    assert strip_offers("Would you like tea?") == "Would you like tea?"  # never empty
    s = SpeechStream()
    assert s.feed("It's sunny. ") == "It's sunny."
    assert s.feed("Anything else I can do?") == ""
    assert s.finish() == "" and s.spoken == "It's sunny."
