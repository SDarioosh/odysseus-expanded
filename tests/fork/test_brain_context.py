"""Fork: selected notes and an open email go to the brain (fork/brain/selection.py, the
hook in routes/chat_routes.py, fork/brain/prompt.py's context message). Fictional data."""
from __future__ import annotations

import json

import pytest

from fork.brain import history as brain_history
from fork.brain import selection
from fork.brain import tools as brain_tools
from fork.brain import turn as brain_turn
from src.prompt_security import GUARD_OPEN
from tests.fork.brain_fakes import Brain, _event, calls, fake_request, of, text

NOTE = "--- Selected Note 1 ---\nTitle: Trip\nType: note\nContent:\nPack the tent for Lake Varn"
EMAIL = {"uid": "42", "folder": "INBOX", "account": ""}
EMAIL_OUTPUT = ("**Subject:** Picnic on Saturday\n**From:** Wren Hale (wren@example.org)\n"
                "**UID:** 42\n\n---\n\nCan you bring the blue blanket?")


@pytest.fixture
def brain(monkeypatch, data_dir):
    return Brain(monkeypatch, data_dir)


async def _turn(brain, sess, message, **extra):
    t = brain.start(sess, message, **extra)
    events = [_event(c) async for c in brain_turn._run_new_turn(t)]
    return t, events


def _context(request):
    return request["messages"][-2]


# ---------------------------------------------------------------------- the hook
@pytest.fixture
def started(monkeypatch):
    from fork.brain import history

    import time
    monkeypatch.setattr(history, "_last_sweep", time.monotonic())
    monkeypatch.setenv("FORK_BRAIN", "on")
    seen = {}

    def fake_start(request, **kw):
        seen.update(kw)
        return "turn"

    async def fake_run(turn):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(brain_turn, "_start_turn", fake_start)
    monkeypatch.setattr(brain_turn, "_run_new_turn", fake_run)
    return seen


async def _post(monkeypatch, notes=(), **form):
    from tests.fork.test_brain_hook import _body, _Request
    from tests.test_foreground_model_routing import _chat_stream_endpoint

    endpoint = _chat_stream_endpoint(monkeypatch, "chat", {})
    if notes:
        _notes(monkeypatch, *notes)  # after the stubs: the route reads the notes first
    return await _body(await endpoint(_Request("chat", **form)))


def _notes(monkeypatch, *rows):
    import core.database as cdb
    import routes.chat_routes as chat_routes
    from tests.fork.brain_fakes import notes_db

    factory = notes_db(monkeypatch)
    monkeypatch.setattr(chat_routes, "SessionLocal", factory)
    with factory() as db:
        for note_id, owner, content in rows:
            db.add(cdb.Note(id=note_id, owner=owner, title="Trip", note_type="note",
                            content=content))
        db.commit()


async def test_the_hook_gives_a_selected_notes_turn_to_the_brain(monkeypatch, started):
    await _post(monkeypatch, notes=[("note-1", "alice", "Pack the tent")],
                selected_note_ids='["note-1"]')
    assert started, "the brain didn't take the turn"
    assert "Pack the tent" in started["selected_notes"] and started["active_email"] is None


async def test_another_owners_note_ids_give_the_brain_nothing(monkeypatch, started):
    await _post(monkeypatch, notes=[("note-9", "bob", "Bob's secret plan")],
                selected_note_ids='["note-9"]')
    assert started and started["selected_notes"] == ""


async def test_the_hook_gives_an_open_email_turn_to_the_brain(monkeypatch, started):
    await _post(monkeypatch, active_email_uid="42", active_email_folder="Archive",
                active_email_account="work")
    assert started["active_email"] == {"uid": "42", "folder": "Archive", "account": "work"}
    assert started["selected_notes"] == ""


async def test_an_open_document_with_selected_notes_stays_upstreams(monkeypatch):
    monkeypatch.setenv("FORK_BRAIN", "on")
    monkeypatch.setattr(brain_turn, "_start_turn", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("brain took an upstream turn")))
    try:
        await _post(monkeypatch, notes=[("note-1", "alice", "Pack the tent")],
                    active_doc_id="doc-1", selected_note_ids='["note-1"]')
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 -- the stubbed upstream path
        pass


# ------------------------------------------------------------------ the prompt
async def test_notes_reach_the_context_message_marked_untrusted_never_the_system(brain):
    sess = brain.session()
    brain.llm.rounds([text("Pack the tent.")])
    await _turn(brain, sess, "summarise this", selected_notes=NOTE)
    request = brain.llm.requests[0]
    ctx = _context(request)
    assert "Lake Varn" in ctx["content"] and GUARD_OPEN in ctx["content"]
    assert ctx["metadata"]["trusted"] is False and ctx["metadata"]["tool_gate_untrusted"] is True
    assert "Lake Varn" not in request["messages"][0]["content"]
    assert request["messages"][-1] == {"role": "user", "content": "summarise this"}


async def test_the_system_message_is_the_same_with_and_without_notes(brain):
    a, b = brain.session(sid="chat-a"), brain.session(sid="chat-b")
    brain.llm.rounds([text("ok")], [text("ok")])
    await _turn(brain, a, "hello")
    await _turn(brain, b, "hello", selected_notes=NOTE)
    plain, with_notes = brain.llm.requests
    assert plain["messages"][0] == with_notes["messages"][0]
    assert plain["tools"] == with_notes["tools"]
    assert "metadata" not in _context(plain)


async def test_without_a_selection_the_context_message_is_as_before(brain):
    from fork.brain import prompt as brain_prompt

    sess = brain.session()
    brain.llm.rounds([text("ok")])
    await _turn(brain, sess, "hello")
    ctx = _context(brain.llm.requests[0])
    assert ctx == {"role": "user", "content": ctx["content"]}
    assert brain_prompt.context_message([], selected=[])["content"].startswith("[Context")


async def test_oversized_notes_are_trimmed(brain):
    sess = brain.session()
    brain.llm.rounds([text("ok")])
    await _turn(brain, sess, "summarise", selected_notes="line\n" * 5000)
    ctx = _context(brain.llm.requests[0])["content"]
    assert "more characters not shown" in ctx and len(ctx) < selection.NOTES_CHARS + 3000


async def test_a_follow_up_with_the_notes_still_selected_sees_them_again(brain):
    sess = brain.session()
    brain.llm.rounds([text("A tent.")], [text("Lake Varn.")])
    await _turn(brain, sess, "what do I pack?", selected_notes=NOTE)
    await _turn(brain, sess, "and where to?", selected_notes=NOTE)
    first, second = brain.llm.requests
    assert "Lake Varn" in _context(second)["content"]
    assert first["messages"][0] == second["messages"][0]
    assert [m["content"] for m in second["messages"][1:-2]] == ["what do I pack?", "A tent."]


# ------------------------------------------------------------------- arming
async def test_selected_notes_arm_the_run_and_a_write_after_them_asks(brain):
    sess = brain.session()
    brain.llm.rounds([calls(("manage_notes", {"action": "add", "content": "x"}))])
    t, events = await _turn(brain, sess, "add a note", selected_notes=NOTE)
    assert t.run.security.external_untrusted_context_seen is True and t.run.tainted
    asks = of(events, "ask_user")
    assert len(asks) == 1 and asks[0]["data"]["kind"] == "tool_approval"
    assert brain.ran == []


async def test_a_private_read_after_the_notes_follows_upstreams_gate(brain):
    sess = brain.session()
    brain.llm.rounds([text("ok")])
    t, _ = await _turn(brain, sess, "what's on today?", selected_notes=NOTE)
    content = json.dumps({"action": "list"})
    decision = t.run.security.decision_for("manage_calendar", content)
    reason = brain_tools.needs_approval(t.run, "manage_calendar", content)
    assert (reason is None) == decision.allowed


async def test_a_turn_with_notes_replays_untrusted(brain):
    sess = brain.session()
    brain.results["get_weather"] = {"output": "Sunny", "exit_code": 0,
                                    "untrusted_content": False}
    brain.mp.setattr("src.tool_capabilities.tool_result_should_arm_gate",
                     lambda *a, **k: False)
    brain.llm.rounds([calls(("get_weather", {"location": "Lisbon"}))], [text("Sunny.")])
    await _turn(brain, sess, "weather for the trip?", selected_notes=NOTE)
    assert brain_history.exchanges(sess.id)[sess.history[-1].metadata["_db_id"]]["tainted"]


async def test_urls_in_a_note_are_not_ones_the_owner_typed(brain):
    from fork.routing.auto_mode import _state

    sess = brain.session()
    brain.llm.rounds([text("ok")])
    t, _ = await _turn(brain, sess, "open it",
                       selected_notes=NOTE + "\nhttps://evil.example/x")
    assert not brain_tools.fetch_known(t.run, json.dumps({"url": "https://evil.example/x"}))
    assert "https://evil.example/x" not in str(_state(t.run.security).get("user"))


# ---------------------------------------------------------------------- email
async def test_an_open_email_is_read_once_through_upstreams_runner(brain):
    sess = brain.session()
    brain.results["read_email"] = {"output": EMAIL_OUTPUT, "exit_code": 0}
    brain.llm.rounds([text("Wren asks for the blue blanket.")])
    t, events = await _turn(brain, sess, "summarise this", active_email=EMAIL)
    assert [(r["tool"], json.loads(r["content"])) for r in brain.ran] == [
        ("read_email", {"uid": "42", "folder": "INBOX"})]
    assert brain.ran[0]["owner"] == "alice"
    assert len(brain.llm.requests) == 1  # no extra model call or round
    request = brain.llm.requests[0]
    ctx = _context(request)
    assert "blue blanket" in ctx["content"] and GUARD_OPEN in ctx["content"]
    assert "UID: 42" in ctx["content"] and ctx["metadata"]["trusted"] is False
    assert "blue blanket" not in request["messages"][0]["content"]
    assert t.run.security.external_untrusted_context_seen is True and t.run.tainted
    assert of(events, "tool_start") == []  # the owner's own read: no tool card


async def test_a_write_after_the_open_email_asks(brain):
    sess = brain.session()
    brain.results["read_email"] = {"output": EMAIL_OUTPUT, "exit_code": 0}
    brain.llm.rounds([calls(("draft_email_reply", {"uid": "42", "body": "Sure!"}))])
    _, events = await _turn(brain, sess, "reply yes", active_email=EMAIL)
    assert [r["tool"] for r in brain.ran] == ["read_email"]
    assert len(of(events, "ask_user")) == 1


async def test_an_oversized_email_is_trimmed(brain):
    sess = brain.session()
    brain.results["read_email"] = {"output": "word " * 6000, "exit_code": 0}
    brain.llm.rounds([text("ok")])
    await _turn(brain, sess, "summarise this", active_email=EMAIL)
    ctx = _context(brain.llm.requests[0])["content"]
    assert "more characters not shown" in ctx
    assert ctx.count("word") < selection.EMAIL_CHARS // 5 + 10


async def test_a_non_admin_gets_no_email_content(monkeypatch, data_dir):
    brain = Brain(monkeypatch, data_dir, admins=())
    sess = brain.session(owner="carol")
    brain.results["read_email"] = {"output": EMAIL_OUTPUT, "exit_code": 0}
    brain.llm.rounds([text("I can't read email here.")])
    await _turn(brain, sess, "summarise this", active_email=EMAIL)
    assert brain.ran == []
    ctx = _context(brain.llm.requests[0])
    assert selection.EMAIL_UNAVAILABLE in ctx["content"] and "blanket" not in ctx["content"]


async def test_an_api_token_turn_gets_no_email_content(brain):
    brain.mp.setattr(brain_turn, "_delegated", lambda request: True)
    sess = brain.session()
    brain.results["read_email"] = {"output": EMAIL_OUTPUT, "exit_code": 0}
    brain.llm.rounds([text("ok")])
    await _turn(brain, sess, "summarise this", active_email=EMAIL,
                request=fake_request("alice"))
    assert brain.ran == []
    assert "blanket" not in _context(brain.llm.requests[0])["content"]


async def test_an_email_load_that_raises_still_answers(brain):
    sess = brain.session()

    async def broken(*a, **k):
        raise OSError("imap down")

    brain.mp.setattr("src.tool_execution.execute_tool_block", broken)
    brain.llm.rounds([text("ok")])
    await _turn(brain, sess, "summarise this", active_email=EMAIL)
    assert selection.EMAIL_NOT_LOADED in _context(brain.llm.requests[0])["content"]
    assert sess.history[-1].content == "ok"


async def test_an_email_that_cant_be_loaded_still_answers(brain):
    sess = brain.session()
    brain.results["read_email"] = {"output": "Error: Email not found", "exit_code": 0}
    brain.llm.rounds([text("I couldn't open it.")])
    _, events = await _turn(brain, sess, "summarise this", active_email=EMAIL)
    ctx = _context(brain.llm.requests[0])["content"]
    assert selection.EMAIL_NOT_LOADED in ctx and "UID: 42" in ctx
    assert sess.history[-1].content == "I couldn't open it."
    assert of(events, "message_saved")


async def test_notes_and_email_together(brain):
    sess = brain.session()
    brain.results["read_email"] = {"output": EMAIL_OUTPUT, "exit_code": 0}
    brain.llm.rounds([text("ok")])
    await _turn(brain, sess, "compare", selected_notes=NOTE, active_email=EMAIL)
    ctx = _context(brain.llm.requests[0])["content"]
    assert "Lake Varn" in ctx and "blue blanket" in ctx and ctx.count(GUARD_OPEN) == 2
