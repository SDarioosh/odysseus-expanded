"""Fork: the voice assistant (fork/voice/, the brain's read-only turn in fork/brain/):
spoken text, read-only tools, the OpenAI-shaped endpoint, conversations and the voice
token. Fake model, tools and sessions (tests/fork/brain_fakes.py); fictional data."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fork.brain import turn as brain_turn
from fork.voice import confirm, ledger, openai_api, routes as voice_routes
from fork.voice import sessions as voice_sessions
from fork.voice.speech import SpeechStream, to_speech
from tests.fork.brain_fakes import Brain, calls, fake_request, text

SEARCH = "mcp__convo__search_transcripts"
UPDATE = "mcp__convo__update_action_item"
ERASE = "mcp__convo__erase_fact"
LINK = "https://athena.example.test/view/12?sig=abc#t=34"


# --------------------------------------------------------------------- spoken text
def test_markdown_and_links_are_not_read_aloud():
    spoken, links = to_speech(
        "## Lunch with Priya\n"
        f"- **Priya** wants the *blue* option ([transcript]({LINK}))\n"
        "- Lee pays next time [1]\n\n"
        "See https://example.test/menu for the menu.")
    assert spoken == ("Lunch with Priya. Priya wants the blue option. Lee pays next time. "
                      "See for the menu.")
    assert links == [LINK, "https://example.test/menu"]


def test_a_link_with_a_real_name_keeps_the_name():
    spoken, links = to_speech("It's on [the cafe's page](https://cafe.example.test/).")
    assert spoken == "It's on the cafe's page."
    assert links == ["https://cafe.example.test/"]


def test_a_list_of_links_at_the_end_is_not_read_out():
    spoken, links = to_speech(
        f"Alice moves to Berlin in January. Links: [Q4 Budget Cut]({LINK}), "
        "[Office Move](https://athena.example.test/view/9?sig=x).")
    assert spoken == "Alice moves to Berlin in January."
    assert links == [LINK, "https://athena.example.test/view/9?sig=x"]
    spoken, links = to_speech("It rains today.\nSources: https://weather.example.test/x")
    assert spoken == "It rains today." and links == ["https://weather.example.test/x"]


def test_link_names_left_hanging_are_not_read_out():
    # what the sandbox's small model wrote (fictional data)
    spoken, links = to_speech(
        "This was later revised to a 10% cut. Links to the conversations: Initial budget "
        f"cut and office move: {LINK} Revised budget cut: https://athena.example.test/view/9")
    assert spoken == "This was later revised to a 10% cut."
    assert len(links) == 2
    assert to_speech("She said: yes.")[0] == "She said: yes."  # no links: untouched


def test_code_and_tables_are_left_out_or_flattened():
    spoken, _ = to_speech("Here:\n```\nrm -rf /\n```\n| Day | Time |\n|---|---|\n| Tue | 3 pm |")
    assert "rm" not in spoken and "|" not in spoken and "---" not in spoken
    assert "Tue, 3 pm." in spoken


def test_stream_speaks_finished_sentences_and_never_cuts_a_link():
    s = SpeechStream()
    assert s.feed("Priya said she'd") == ""
    assert s.feed(" bring the cake. And Lee") == "Priya said she'd bring the cake."
    assert s.feed(f" brings drinks ([transcript]({LINK[:20]}") == ""  # link still open
    assert s.feed(LINK[20:] + ")). Done") == " And Lee brings drinks."
    assert s.finish() == " Done."
    assert s.spoken == "Priya said she'd bring the cake. And Lee brings drinks. Done."
    assert s.links == [LINK]


# ---------------------------------------------------------------- OpenAI requests
def test_parse_takes_the_newest_question_and_the_conversation():
    q = openai_api.parse({
        "model": "odysseus", "stream": True, "user": "conv-7",
        "messages": [
            {"role": "system", "content": "You are Home Assistant. Devices: ..."},
            {"role": "user", "content": "What did Priya say?"},
            {"role": "assistant", "content": "She wants the blue option."},
            {"role": "user", "content": [{"type": "text", "text": "And Lee?"}]},
        ],
        "tools": [{"type": "function", "function": {"name": "HassTurnOn"}}],
    })
    assert (q.text, q.stream, q.conversation, q.first_question) == (
        "And Lee?", True, "conv-7", "What did Priya say?")


@pytest.mark.parametrize("body", [[], {}, {"messages": []},
                                  {"messages": [{"role": "system", "content": "x"}]},
                                  {"messages": [{"role": "user", "content": "x" * 2001}]}])
def test_parse_refuses_what_it_cannot_answer(body):
    with pytest.raises(openai_api.BadRequest):
        openai_api.parse(body)


def test_conversation_header_when_there_is_no_user_field():
    q = openai_api.parse({"messages": [{"role": "user", "content": "Hi"}]},
                         conversation_header="ha-1")
    assert q.conversation == "ha-1" and q.first_question is None and q.stream is False


# --------------------------------------------------------- the read-only brain turn
@pytest.fixture
def brain(monkeypatch, data_dir):
    confirm.forget_all()
    monkeypatch.setattr(confirm, "athena_item_title", _no_title)
    return Brain(monkeypatch, data_dir)


async def _no_title(item_id):
    return None


def _token_request(owner="alice", scopes=("voice",)):
    req = fake_request(owner)
    req.state.api_token = True
    req.state.api_token_owner = owner
    req.state.api_token_scopes = list(scopes)
    return req


async def _voice(brain, sess, message, *, request=None, personal=True, turn_id="turn-1"):
    """A voice turn the way the route starts one: the chat's prefix, the voice gate."""
    from fork.voice.chat_prefix import EMPTY
    from fork.voice.policy import VoiceGate

    gate = VoiceGate(owner=sess.owner, session_id=sess.id, turn_id=turn_id,
                     personal=personal)
    events = brain_turn.read_only_turn(
        request or _token_request(sess.owner), sess=sess, session_id=sess.id,
        message=message, owner=sess.owner, instructions="Speak briefly.", preset=EMPTY,
        gate=gate, shared_prefix=True, context_lines=("Asked from: Kitchen Echo, Kitchen",),
        no_memory=not personal)
    assert events is not None
    out = []
    async for chunk in events:
        out.append(chunk)
    return out


def _offered(request) -> set[str]:
    return {t["function"]["name"] for t in request.get("tools") or []}


def _tool_results(request) -> list[str]:
    return [m["content"] for m in request["messages"] if m.get("role") == "tool"]


async def test_voice_shares_the_chat_prefix_and_tools(brain):
    """Same system prompt and tools, in the same order, as a chat turn of the owner;
    the voice instructions come after the cached prefix, with the question."""
    chat, voice = brain.session(sid="chat-a"), brain.session(sid="voice-a")
    brain.llm.rounds([text("Hi.")], [text("Hi.")])
    await brain.run(chat, "What did Priya say?")
    await _voice(brain, voice, "What did Priya say?")
    chat_req, voice_req = brain.llm.requests
    assert [t["function"]["name"] for t in voice_req["tools"]] == \
        [t["function"]["name"] for t in chat_req["tools"]]
    assert voice_req["tools"] == chat_req["tools"]
    assert UPDATE in _offered(voice_req) and "send_email" in _offered(voice_req)
    cm, vm = chat_req["messages"], voice_req["messages"]
    assert vm[:-2] == cm[:-2]  # everything before the context message
    assert "Asked from: Kitchen Echo, Kitchen" in vm[-2]["content"]
    assert vm[-1]["content"].startswith("Speak briefly.") and \
        vm[-1]["content"].endswith("What did Priya say?")
    assert cm[-1]["content"] == "What did Priya say?"
    # the chat window's saved message is the plain question
    assert voice.history[0].content == "What did Priya say?"


async def test_voice_uses_the_chat_preset_text(brain, monkeypatch, data_dir):
    from src.preset_manager import PresetManager
    from fork.voice import chat_prefix

    pm = PresetManager(str(data_dir))
    pm.presets["custom"] = {**pm.presets["custom"], "enabled": True,
                            "system_prompt": "Call me Sam."}
    pm.save(pm.presets)
    preset = chat_prefix.default_preset()
    assert preset.system_prompt == "Call me Sam."
    pm.presets["custom"]["enabled"] = False
    pm.save(pm.presets)
    assert not chat_prefix.default_preset().system_prompt


async def test_a_disallowed_write_is_refused_never_asked(brain):
    sess = brain.session()
    brain.llm.rounds([calls((ERASE, {"fact_id": 3}),
                            ("manage_calendar", {"action": "delete_event", "uid": "x"}))],
                     [text("I can't do that by voice.")])
    chunks = await _voice(brain, sess, "Forget that fact and delete the dentist")
    assert brain.ran == []  # upstream's runner never saw the calls
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    assert not any(e.get("type") == "ask_user" or e.get("ask_user") for e in events)
    results = _tool_results(brain.llm.requests[1])
    assert "not available here" in results[0]  # not in the brain's set, chat or voice
    assert "can only read" in results[1]
    assert brain_turn.pending_count() == 0
    assert confirm.pending(sess.owner, sess.id) is None


async def test_email_is_listed_but_never_run(brain):
    sess = brain.session()
    brain.llm.rounds([calls(("list_emails", {}), ("send_email", {"to": "x@example.test"}))],
                     [text("No email by voice.")])
    await _voice(brain, sess, "Read my email")
    assert brain.ran == []
    assert all("email isn't available by voice" in r
               for r in _tool_results(brain.llm.requests[1]))


async def test_store_reads_run_and_an_allowed_write_is_only_proposed(brain):
    sess = brain.session()
    brain.llm.rounds(
        [calls(("manage_calendar", {"action": "list_events"}),
               ("manage_calendar", {"action": "create_event", "summary": "Dentist",
                                    "local_start": {"date": "2026-11-10", "time": "15:00"}}))],
        [text("You're free. Add Dentist on Tuesday at three to your calendar?")])
    await _voice(brain, sess, "Add the dentist on Tuesday at three")
    assert [r["tool"] for r in brain.ran] == ["manage_calendar"]
    assert "list_events" in brain.ran[0]["content"]
    proposed = _tool_results(brain.llm.requests[1])[-1]
    assert proposed == confirm.PROPOSED
    p = confirm.pending(sess.owner, sess.id)
    assert p is not None and [c.name for c in p.calls] == ["manage_calendar"]
    assert p.calls[0].change.kind == "event" and p.calls[0].change.where == "Calendar"
    assert brain_turn.pending_count() == 0


async def test_yes_runs_the_kept_call_exactly_with_an_exact_approval(brain):
    sess = brain.session()
    args = {"item_id": 3, "status": "done"}
    brain.llm.rounds([calls((UPDATE, args))], [text("Mark the cake to-do as done?")])
    await _voice(brain, sess, "Mark the cake to-do done")
    assert brain.ran == []
    p = confirm.take(sess.owner, sess.id)
    outcomes = await confirm.run(p)
    assert [o.ok for o in outcomes] == [True]
    assert len(brain.ran) == 1 and brain.ran[0]["tool"] == UPDATE
    assert json.loads(brain.ran[0]["content"]) == args
    assert brain.ran[0]["exact_approval"] is not None
    assert confirm.reply(outcomes) == "Done. Marked action item 3 done."


async def test_an_athena_update_other_than_done_is_refused(brain):
    sess = brain.session()
    brain.llm.rounds([calls((UPDATE, {"item_id": 3, "status": "done", "due_date": "none"}))],
                     [text("Can't.")])
    await _voice(brain, sess, "Mark it done and clear the date")
    assert "can only read" in _tool_results(brain.llm.requests[1])[0]
    assert confirm.pending(sess.owner, sess.id) is None


async def test_private_answers_refuse_personal_tools_and_keep_the_web(brain):
    sess = brain.session()
    brain.llm.rounds([calls((SEARCH, {"query": "cake"}), ("get_weather", {"location": "Lisbon"}))],
                     [text("It's sunny.")])
    await _voice(brain, sess, "Who brings the cake and how's the weather?", personal=False)
    assert [r["tool"] for r in brain.ran] == ["get_weather"]
    from fork.voice.policy import PRIVATE_REPLY
    assert PRIVATE_REPLY in _tool_results(brain.llm.requests[1])[0]


async def test_reads_run_and_the_answer_is_saved_in_the_chat(brain):
    sess = brain.session()
    brain.llm.rounds([calls((SEARCH, {"query": "cake"}))],
                     [text(f"Priya brings the cake. [transcript]({LINK})")])
    await _voice(brain, sess, "Who brings the cake?")
    assert [r["tool"] for r in brain.ran] == [SEARCH]
    assert [m.role for m in sess.history] == ["user", "assistant"]
    assert sess.history[0].content == "Who brings the cake?"
    # after-turn jobs run (title, totals), but no memories are taken from a voice turn
    assert brain.post and brain.post[-1][1]["allow_background_extraction"] is False


async def test_an_ordinary_chat_turn_still_asks_before_a_write(brain):
    sess = brain.session()
    brain.llm.rounds([calls((UPDATE, {"id": 3}))])
    events = await brain.run(sess, "Mark the cake to-do done")
    assert any(e.get("type") == "ask_user" for e in events)


def test_no_voice_turn_when_the_brain_is_off(brain, monkeypatch):
    monkeypatch.setenv("FORK_BRAIN", "off")
    sess = brain.session()
    assert brain_turn.read_only_turn(_token_request(), sess=sess, session_id=sess.id,
                                     message="Hi", owner="alice") is None


# ---------------------------------------------------------------- the endpoint
class FakeSessions:
    def __init__(self):
        self.sessions = {}
        self.created = []

    def create_session(self, session_id, name, url, model, owner=None, headers=None, **kw):
        sess = SimpleNamespace(id=session_id, name=name, owner=owner, endpoint_url=url,
                               model=model, headers=headers or {}, history=[])
        self.sessions[session_id] = sess
        self.created.append(sess)
        return sess

    def get_session(self, session_id):
        return self.sessions[session_id]


@pytest.fixture
def api(monkeypatch, data_dir):
    """The voice router on a bare app; the token is set by a header the fake auth reads.
    ``client.answer``: what the fake brain turn says; ``client.on_turn(kw)``: called with
    the turn's arguments (to keep a proposal, as the voice gate would)."""
    import sys

    voice_sessions.forget_all()
    confirm.forget_all()
    # no satellite module unless a test adds one (the HUB-SATELLITE stream's file)
    monkeypatch.setitem(sys.modules, "fork.voice.satellite", None)
    # The owner's zone comes from the database; a first import of core.database here
    # would bind it to this test's empty data_dir for every later test.
    monkeypatch.setattr("fork.common.local_time.owner_timezone_name", lambda owner: None)
    app = FastAPI()
    app.state.session_manager = FakeSessions()
    turns = []

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        request.state.api_token_owner = "alice" if scopes is not None else None
        request.state.api_token_scopes = (scopes or "").split(",") if scopes else []
        request.state.current_user = "api" if scopes is not None else "alice"
        return await call_next(request)

    def fake_turn(request, *, sess, session_id, message, owner, instructions, services,
                  **kw):
        turns.append({"sid": session_id, "message": message, "owner": owner,
                      "instructions": instructions, **kw})
        if client.on_turn:
            client.on_turn(turns[-1])
        said = client.answer

        async def events():
            yield 'data: {"type": "tool_start", "tool": "x"}\n\n'
            yield 'data: {"type": "agent_step", "round": 2}\n\n'
            for piece in said:
                yield "data: " + json.dumps({"delta": piece}) + "\n\n"
            yield 'data: {"type": "metrics", "data": {"input_tokens": 9, "output_tokens": 4}}\n\n'
            yield "data: [DONE]\n\n"
        return events()

    monkeypatch.setattr("fork.brain.turn.read_only_turn", fake_turn)
    monkeypatch.setattr("fork.voice.chat_prefix.default_preset",
                        lambda: SimpleNamespace(system_prompt="", temperature=None,
                                                max_tokens=None, character_name=None))
    monkeypatch.setattr(voice_sessions, "default_model",
                        lambda owner: ("http://127.0.0.1:8082/v1/chat/completions", "local", {}))
    monkeypatch.setattr(voice_sessions, "_file_in_folder", lambda sid: None)
    monkeypatch.setattr(voice_routes, "_save_exchange", lambda *a: turns.append({"saved": a[2:]}))
    app.include_router(voice_routes.setup_voice_routes())
    client = TestClient(app)
    client.turns = turns
    client.sessions = app.state.session_manager
    client.answer = ["**Priya** brings the cake. ", f"[transcript]({LINK})"]
    client.on_turn = None
    return client


VOICE = {"x-test-scopes": "voice"}
URL = voice_routes.PREFIX + "/chat/completions"
MODELS = voice_routes.PREFIX + "/models"
ASK = {"model": "odysseus", "messages": [{"role": "user", "content": "Who brings the cake?"}]}


def test_only_a_voice_token_may_ask(api):
    assert api.post(URL, json=ASK).status_code == 403  # a browser login
    assert api.post(URL, json=ASK,
                    headers={"x-test-scopes": "chat"}).status_code == 403
    assert api.get(MODELS).status_code == 403
    assert api.turns == []


def test_models_lists_odysseus(api):
    assert api.get(MODELS, headers=VOICE).json()["data"][0]["id"] == "odysseus"


def test_a_bad_request_gets_an_openai_error(api):
    res = api.post(URL, json={"messages": []}, headers=VOICE)
    assert res.status_code == 400 and "messages" in res.json()["error"]["message"]


def test_answer_is_spoken_text_with_the_links_apart(api):
    res = api.post(URL, json=ASK, headers=VOICE)
    body = res.json()
    assert res.status_code == 200 and body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant",
                                             "content": "Priya brings the cake."}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}
    assert body["odysseus"]["links"] == [LINK]
    turn = api.turns[0]
    assert turn["owner"] == "alice" and turn["message"] == "Who brings the cake?"
    assert "read aloud" in turn["instructions"] and turn["shared_prefix"] is True
    assert "disabled" not in turn  # email is listed like in chat, refused at execution
    assert turn["gate"].personal is True and turn["no_memory"] is False
    assert body["odysseus"]["turn_id"]
    assert api.sessions.created[0].name == "Voice: Who brings the cake?"


def test_a_turn_saves_how_long_each_step_took(api):
    from fork.voice import timings

    api.post(URL, json=ASK, headers=VOICE)
    api.post(URL, json={**ASK, "stream": True}, headers=VOICE)
    found = timings.rows("alice")
    assert len(found) == 2
    for row in found:
        hub = row["hub"]
        assert {"ready", "first_token", "first_text", "first_spoken", "done"} <= set(hub["marks"])
        assert [t["name"] for t in hub["tools"]] == ["x"] and hub["outcome"] == "answered"
        assert row["turn_id"] and not row["device"]
    assert "Priya" not in json.dumps(found)  # numbers only, never what was said


def test_streamed_answer_is_openai_chunks(api):
    res = api.post(URL, json={**ASK, "stream": True}, headers=VOICE)
    lines = [l[6:] for l in res.text.split("\n\n") if l.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(l) for l in lines[:-1]]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    said = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert said == "Priya brings the cake."
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["odysseus"]["links"] == [LINK]


def test_a_follow_up_continues_the_chat_and_a_new_conversation_does_not(api):
    follow = {"user": "ha-conv-1", **ASK}
    api.post(URL, json=follow, headers=VOICE)
    api.post(URL, json=follow, headers=VOICE)
    api.post(URL, json={"user": "ha-conv-2", **ASK}, headers=VOICE)
    api.post(URL, json=ASK, headers=VOICE)  # no conversation: new chat
    sids = [t["sid"] for t in api.turns]
    assert sids[0] == sids[1] and len(set(sids)) == 3


def _ha_turns(*said):
    """A request as Home Assistant's agent sends it: no ``user`` field, the earlier turns
    of its conversation in ``messages``."""
    messages = [{"role": "system", "content": "You are Home Assistant."}]
    for i, text_ in enumerate(said):
        if i:
            messages.append({"role": "assistant", "content": "Earlier answer?"})
        messages.append({"role": "user", "content": text_})
    return {"model": "odysseus", "stream": True, "messages": messages}


def test_the_first_follow_up_of_a_conversation_continues_its_chat(api):
    """Home Assistant names a conversation only by its earlier turns: the reply to the
    first answer (the second request) must land in the first question's chat."""
    api.post(URL, json=_ha_turns("Who brings the cake?"), headers=VOICE)
    api.post(URL, json=_ha_turns("Who brings the cake?", "And the drinks?"), headers=VOICE)
    api.post(URL, json=_ha_turns("Who brings the cake?", "And the drinks?", "Thanks"),
             headers=VOICE)
    api.post(URL, json=_ha_turns("Who brings the cake?"), headers=VOICE)  # a new one
    sids = [t["sid"] for t in api.turns]
    assert sids[0] == sids[1] == sids[2] and sids[3] != sids[0]
    assert len(api.sessions.created) == 2


def test_a_yes_in_the_first_follow_up_answers_the_kept_change(api, monkeypatch):
    async def fake_run(p):
        return [confirm.Outcome(c.change, True) for c in p.calls]
    monkeypatch.setattr(confirm, "run", fake_run)
    _propose(api)
    ask = "Add the dentist on Tuesday at three"
    api.post(URL, json={**_ha_turns(ask), "stream": False}, headers=VOICE)
    api.on_turn = None
    body = api.post(URL, json={**_ha_turns(ask, "Yes."), "stream": False},
                    headers=VOICE).json()
    assert body["choices"][0]["message"]["content"] == "Done. Added Dentist to your calendar."


def test_a_conversation_goes_stale_after_the_follow_up_window():
    voice_sessions.forget_all()
    voice_sessions.remember("alice", "id:x", "chat-1", now=1000.0)
    assert voice_sessions.remembered("alice", "id:x", now=1000.0 + 60) == "chat-1"
    assert voice_sessions.remembered("bob", "id:x", now=1000.0 + 60) is None
    late = 1000.0 + voice_sessions.FOLLOW_UP_SECONDS + 61
    assert voice_sessions.remembered("alice", "id:x", now=late) is None


def test_a_whole_answer_leaves_out_what_was_said_before_a_tool_call():
    answer = openai_api.Answer(streaming=False)
    for chunk in ('data: {"delta": "Let me search for that."}\n\n',
                  'data: {"type": "tool_start", "tool": "x"}\n\n',
                  'data: {"type": "agent_step", "round": 2}\n\n',
                  'data: {"delta": "Priya brings the cake."}\n\n'):
        assert answer.feed(chunk) == ""
    answer.finish()
    assert answer.speech.spoken == "Priya brings the cake."


def test_no_answer_and_failures_are_spoken():
    answer = openai_api.Answer()
    answer.feed("event: error\ndata: {}\n\n")
    assert answer.finish() == "Sorry, something went wrong while answering."
    answer = openai_api.Answer()
    answer.feed('data: {"type": "ask_user", "data": {}}\n\n')
    assert answer.finish().startswith("I can't make changes by voice.")


def test_the_voice_routes_are_outside_the_request_timeout():
    """A CPU answer takes minutes: app.py's 45 s timeout must leave the prefix alone."""
    from pathlib import Path

    app = (Path(__file__).resolve().parents[2] / "app.py").read_text(encoding="utf-8")
    block = app[app.index("_TIMEOUT_EXEMPT_PREFIXES = ("):]
    block = block[:block.index(")")]
    exempt = re.findall(r'"(/[^"]*)"', block)
    assert any(voice_routes.PREFIX.startswith(p) for p in exempt)


# ------------------------------------------------------------------ the voice token
def test_voice_is_an_allowed_token_scope():
    from routes.api_token_routes import ALLOWED_SCOPES

    assert "voice" in ALLOWED_SCOPES


def test_the_settings_card_makes_a_voice_only_token(monkeypatch):
    from core.middleware import require_admin

    made = []
    monkeypatch.setattr("fork.athena.phone_pairing.create_api_token",
                        lambda owner, name, scopes: made.append((owner, name, scopes))
                        or {"id": "t1", "token": "ody_fake"})
    monkeypatch.setattr("src.auth_helpers.get_current_user", lambda request: "alice")
    app = FastAPI()
    app.dependency_overrides[require_admin] = lambda: None
    app.include_router(voice_routes.setup_voice_routes())
    res = TestClient(app).post("/api/fork/voice/token")
    assert res.status_code == 200 and res.json()["token"] == "ody_fake"
    owner, name, scopes = made[0]
    assert owner == "alice" and scopes == ["voice"] and name.startswith("Voice assistant ")


# --------------------------------------------------- the turn ledger and its feeds
ACTIONS = voice_routes.PREFIX + "/actions"
LATEST = voice_routes.PREFIX + "/turns/latest"
YES = {"user": "ha-1", "model": "odysseus", "messages": [{"role": "user", "content": "Yes."}]}
DENTIST = {"user": "ha-1", "model": "odysseus",
           "messages": [{"role": "user", "content": "Add the dentist on Tuesday at three"}]}


def test_the_feeds_need_a_voice_token(api):
    for url in (ACTIONS, LATEST):
        assert api.get(url).status_code == 403
        assert api.get(url, headers={"x-test-scopes": "chat"}).status_code == 403
    assert api.get(ACTIONS, headers=VOICE).json() == {"actions": []}
    assert api.get(LATEST, headers=VOICE).json() == {"turn": None}


def test_a_turn_is_recorded_and_served_as_the_latest(api):
    body = api.post(URL, json=ASK, headers=VOICE).json()
    turn = api.get(LATEST, headers=VOICE).json()["turn"]
    assert turn["id"] == body["odysseus"]["turn_id"]
    assert turn["question"] == "Who brings the cake?"
    assert turn["answer"] == "Priya brings the cake."
    assert turn["links"] == [{"url": LINK, "label": None}]
    assert turn["actions"] == [] and turn["awaiting_confirmation"] is None
    assert turn["device"] is None and turn["speaker"] is None
    assert turn["session_id"] == body["odysseus"]["session_id"]
    assert turn["started_at"].endswith("+00:00") and turn["ended_at"]
    future = (ledger.time.time() + 60) * 1000
    assert api.get(LATEST, params={"since": int(future)}, headers=VOICE).json() == {"turn": None}


def test_a_streamed_turn_is_recorded_too(api):
    res = api.post(URL, json={**ASK, "stream": True}, headers=VOICE)
    last = json.loads([l[6:] for l in res.text.split("\n\n") if l.startswith("data: {")][-1])
    turn = api.get(LATEST, headers=VOICE).json()["turn"]
    assert turn["id"] == last["odysseus"]["turn_id"] and turn["answer"] == "Priya brings the cake."


def test_the_ledger_is_owner_scoped_and_newest_first(data_dir):
    t = ledger.start_turn("alice", "q", now=1000.0)
    ledger.add_action(t, "alice", action="created", kind="todo", title="Old", where="To-dos",
                      now=1001.0)
    ledger.add_action(t, "alice", action="completed", kind="action_item", title="New",
                      where="Athena", source="athena", now=1002.0)
    ledger.add_action(t, "bob", action="created", kind="note", title="Bob's", where="Notes",
                      now=1003.0)
    rows = ledger.actions("alice", since_ms=0, limit=8)
    assert [r["title"] for r in rows] == ["New", "Old"]
    assert set(rows[0]) == {"id", "at", "action", "kind", "title", "detail", "due",
                            "all_day", "where", "source", "turn_id"}
    assert rows[0]["at"] == "1970-01-01T00:16:42+00:00" and rows[0]["source"] == "athena"
    assert [r["title"] for r in ledger.actions("alice", since_ms=1001500)] == ["New"]
    assert len(ledger.actions("alice", limit=1)) == 1
    with pytest.raises(ValueError):
        ledger.add_action(t, "alice", action="deleted", kind="todo", title="x", where="y")
    # pruned after 7 days
    ledger.start_turn("alice", "later", now=1000.0 + ledger.KEEP_SECONDS + 10)
    assert ledger.actions("alice") == []


@pytest.mark.parametrize("said", [
    "Remind me to call Sam tomorrow at five", "Add milk to my to-dos for Friday",
    "Remind me to stretch in 20 minutes", "Pay rent on the 1st", "Call Lee at 3pm",
    "Water the plants every morning", "Buy a gift before Christmas", "Dentist at 9:30",
    "Remind me on May 3 to renew", "Remind me tonight"])
def test_words_that_say_when(said):
    assert confirm.says_when(said)


@pytest.mark.parametrize("said", [
    "Add buy milk to my to-dos", "Make a todo to call the plumber", "Remind me to call Sam",
    "Add a to-do: pick up the dry cleaning", "I may need batteries, add them to my to-dos"])
def test_words_that_say_no_when(said):
    assert not confirm.says_when(said)


def test_an_unsaid_due_date_is_dropped_from_a_to_do():
    call = json.dumps({"action": "add", "title": "Buy milk", "due_date": "tomorrow at 9am"})
    out = json.loads(confirm.without_unsaid_due("manage_notes", call, "Add buy milk to my to-dos"))
    assert out == {"action": "add", "title": "Buy milk", "note_type": "checklist"}
    kept = confirm.without_unsaid_due("manage_notes", call, "Remind me tomorrow to buy milk")
    assert kept == call
    items = json.dumps({"action": "add", "title": "Shop", "due_date": "today",
                        "checklist_items": [{"text": "eggs"}]})
    assert json.loads(confirm.without_unsaid_due("manage_notes", items, "Make a shopping todo")) \
        == {"action": "add", "title": "Shop", "checklist_items": [{"text": "eggs"}]}
    other = json.dumps({"action": "update", "id": "ab", "due_date": "today"})
    assert confirm.without_unsaid_due("manage_notes", other, "x") == other
    event = json.dumps({"action": "create_event", "summary": "x", "due_date": "today"})
    assert confirm.without_unsaid_due("manage_calendar", event, "x") == event


async def test_a_to_do_keeps_no_due_date_the_owner_never_said(brain):
    sess = brain.session()
    brain.llm.rounds(
        [calls(("manage_notes", {"action": "add", "title": "Buy milk",
                                 "due_date": "tomorrow at 9am"}))],
        [text("Add Buy milk to your to-dos?")])
    await _voice(brain, sess, "Make a todo to buy milk")
    p = confirm.pending(sess.owner, sess.id)
    assert p is not None and len(p.calls) == 1
    assert "due_date" not in json.loads(p.calls[0].content)
    change = p.calls[0].change
    assert (change.kind, change.due, change.question) == \
        ("todo", None, "Add Buy milk to your to-dos?")


async def test_a_date_said_in_the_previous_question_counts(brain):
    from fork.voice.policy import VoiceGate

    run = SimpleNamespace(request_text="Actually call it pick up the cake")
    gate = VoiceGate(owner="alice", session_id="s-1", turn_id="t-1",
                     earlier="Remind me to get the cake tomorrow at noon")
    call = json.dumps({"action": "add", "title": "Pick up the cake", "due_date": "tomorrow noon"})
    await gate(run, "manage_notes", call)
    p = confirm.pending("alice", "s-1")
    assert p is not None and p.calls[0].content == call and p.calls[0].change.kind == "reminder"


def _propose(api, change=None):
    """The fake turn keeps a proposal, as the voice gate does for an allowed write."""
    change = change or confirm.Change("created", "event", "Dentist", "Calendar",
                                      "Added Dentist to your calendar.", due="2026-11-10T15:00:00+00:00",
                                      question="Add Dentist to your calendar?")

    def keep(kw):
        gate = kw["gate"]
        confirm.keep("alice", kw["sid"], gate.turn_id, SimpleNamespace(), "manage_calendar",
                     '{"action": "create_event", "summary": "Dentist"}', change)
    api.on_turn = keep


@pytest.mark.parametrize("said", [["Sure."], ["Add Dentist on Tuesday at three to your calendar?"]])
def test_a_proposal_ends_with_a_confirmation_question(api, said):
    _propose(api)
    api.answer = said
    body = api.post(URL, json=DENTIST, headers=VOICE).json()
    spoken = body["choices"][0]["message"]["content"]
    assert spoken.endswith("?")
    turn = api.get(LATEST, headers=VOICE).json()["turn"]
    assert turn["awaiting_confirmation"] == spoken.split(". ")[-1]
    assert api.get(ACTIONS, headers=VOICE).json() == {"actions": []}  # not run


@pytest.mark.parametrize("said", ["It is sunny. Would you like to hear more?",
                                  "Add milk to your shopping list?"])
def test_the_spoken_question_is_the_kept_calls_own(api, said):
    """A "yes" must answer the question built from the stored call: a question the model
    wrote (an offer, or one naming another change) is replaced, never kept."""
    _propose(api)
    api.answer = [said]
    body = api.post(URL, json=DENTIST, headers=VOICE).json()
    spoken = body["choices"][0]["message"]["content"]
    assert spoken.endswith("Add Dentist to your calendar?")
    assert "hear more" not in spoken and "milk" not in spoken


def test_several_kept_changes_are_each_named():
    p = confirm.Proposal("alice", "s", "t", None)
    for q in ("Add Dentist to your calendar?", "Set a reminder: call Sam?"):
        p.calls.append(confirm.Call("x", "{}", confirm.Change("created", "event", "x", "y",
                                                              "z", question=q)))
    assert p.question() == "Add Dentist to your calendar, and set a reminder: call Sam?"


def test_event_and_reminder_questions_say_when():
    assert confirm._spoken_when("2026-11-10T15:00:00+00:00") == " on Tuesday, November 10 at 3:00 PM"
    assert confirm._spoken_when("2026-11-10", all_day=True) == " on Tuesday, November 10"
    assert confirm._spoken_when(None) == ""
    assert confirm._spoken_when("next week") == " (next week)"


def test_yes_runs_the_proposal_without_the_model(api, monkeypatch):
    _propose(api)
    api.post(URL, json=DENTIST, headers=VOICE)
    ran = []

    async def fake_run(p):
        ran.append(p)
        return [confirm.Outcome(c.change, True) for c in p.calls]
    monkeypatch.setattr(confirm, "run", fake_run)
    api.on_turn = None
    body = api.post(URL, json=YES, headers=VOICE).json()
    assert body["choices"][0]["message"]["content"] == "Done. Added Dentist to your calendar."
    assert len([t for t in api.turns if "message" in t]) == 1  # no model turn for the yes
    assert len(ran) == 1 and ran[0].calls[0].name == "manage_calendar"
    actions = api.get(ACTIONS, headers=VOICE).json()["actions"]
    assert [(a["action"], a["kind"], a["title"], a["where"], a["source"]) for a in actions] == [
        ("created", "event", "Dentist", "Calendar", "odysseus")]
    assert actions[0]["due"] == "2026-11-10T15:00:00+00:00" and actions[0]["all_day"] is False
    turn = api.get(LATEST, headers=VOICE).json()["turn"]
    assert turn["question"] == "Yes." and turn["actions"] == actions
    assert actions[0]["turn_id"] == body["odysseus"]["turn_id"]
    # used up: a second yes is a new question
    api.answer = ["Yes what."]
    api.post(URL, json=YES, headers=VOICE)
    assert len([t for t in api.turns if "message" in t]) == 2


def test_no_drops_the_proposal_and_anything_else_is_a_new_question(api, monkeypatch):
    monkeypatch.setattr(confirm, "run", lambda p: pytest.fail("must not run"))
    _propose(api)
    api.post(URL, json=DENTIST, headers=VOICE)
    no = {**YES, "messages": [{"role": "user", "content": "No, never mind."}]}
    body = api.post(URL, json=no, headers=VOICE).json()
    assert body["choices"][0]["message"]["content"] == "Okay, I won't."
    assert confirm.pending("alice", body["odysseus"]["session_id"]) is None
    api.post(URL, json=DENTIST, headers=VOICE)
    api.on_turn = None
    other = {**YES, "messages": [{"role": "user", "content": "What's the weather?"}]}
    body = api.post(URL, json=other, headers=VOICE).json()
    assert api.turns[-1]["message"] == "What's the weather?"
    assert confirm.pending("alice", body["odysseus"]["session_id"]) is None


def test_a_proposal_expires(api):
    _propose(api)
    body = api.post(URL, json=DENTIST, headers=VOICE).json()
    sid = body["odysseus"]["session_id"]
    later = ledger.time.time() + confirm.PROPOSAL_SECONDS + 1
    assert confirm.pending("alice", sid, now=later) is None


REMIND = {"user": "ha-1", "model": "odysseus", "messages": [
    {"role": "user", "content": "Make a reminder to feed the dogs today at 5 p.m."}]}


def _asked_without_calling(api):
    """The model's first answer asked in its own words and called no tool: the voice
    chat ends with that question and no change is kept."""
    api.answer = ["Add a reminder to feed the dogs today at five to your list?"]
    body = api.post(URL, json=REMIND, headers=VOICE).json()
    sess = api.sessions.sessions[body["odysseus"]["session_id"]]
    sess.history = [SimpleNamespace(role="user", content=REMIND["messages"][0]["content"]),
                    SimpleNamespace(role="assistant", content=api.answer[0])]
    assert confirm.pending("alice", sess.id) is None
    return sess


def test_a_yes_with_nothing_kept_tells_the_model_to_call_the_tool(api):
    """Seen on the Echo: the model asked without calling the tool, so every "yes" went
    back to it and it asked the same question again, without end."""
    _asked_without_calling(api)
    body = api.post(URL, json=YES, headers=VOICE).json()
    assert confirm.UNHELD_NOTE in api.turns[-1]["context_lines"]
    # it only asked again: the question is replaced, so the next yes can't loop
    assert body["choices"][0]["message"]["content"] == confirm.UNHELD
    assert api.get(LATEST, headers=VOICE).json()["turn"]["awaiting_confirmation"] is None


def test_a_yes_with_nothing_kept_then_asks_the_kept_changes_question(api):
    sess = _asked_without_calling(api)
    _propose(api, confirm.Change("created", "reminder", "Feed the dogs", "To-dos",
                                 "I'll remind you: Feed the dogs.",
                                 question="Set a reminder: Feed the dogs at 5:00 PM?"))
    body = api.post(URL, json=YES, headers=VOICE).json()
    assert body["choices"][0]["message"]["content"] == \
        "Set a reminder: Feed the dogs at 5:00 PM?"
    assert confirm.pending("alice", sess.id) is not None


def test_a_new_question_after_an_unanswered_one_gets_no_note(api):
    _asked_without_calling(api)
    api.answer = ["It is sunny."]
    other = {**YES, "messages": [{"role": "user", "content": "What's the weather?"}]}
    api.post(URL, json=other, headers=VOICE)
    assert confirm.UNHELD_NOTE not in api.turns[-1]["context_lines"]


def test_the_voice_instructions_say_to_call_the_tool_before_asking():
    from fork.voice.prompt import INSTRUCTIONS

    assert "Call the tool first" in INSTRUCTIONS and "manage_notes" in INSTRUCTIONS


def test_end_with_statement_replaces_the_last_questions():
    from fork.voice.speech import end_with_statement

    assert end_with_statement("Okay. Add it? Sure?", "Not done.") == "Okay. Not done."
    assert end_with_statement("Add it?", "Not done.") == "Not done."
    assert end_with_statement("Fine.", "Not done.") == "Fine. Not done."


@pytest.mark.parametrize("text_, kind", [
    ("Yes.", "yes"), ("yeah, do it!", "yes"), ("Sure", "yes"), ("Okay, go ahead.", "yes"),
    ("Correct", "yes"), ("Please do", "yes"), ("No.", "no"), ("Cancel", "no"),
    ("Never mind", "no"), ("Don't", "no"), ("No, make it Wednesday", "other"),
    ("What's on Tuesday?", "other"), ("", "other")])
def test_reply_kind(text_, kind):
    assert confirm.reply_kind(text_) == kind


@pytest.mark.parametrize("said, spoken", [
    (["Paris is the capital. Anything else?"], "Paris is the capital."),
    (["It rains. ", "Would you like more details?"], "It rains."),
    (["Anything else?"], "Anything else."),
    # a real question keeps its "?": Home Assistant then listens for the answer
    (["Is it Paris?"], "Is it Paris?"),
    (["You have two notes about it. ", "Do you want me to read them?"],
     "You have two notes about it. Do you want me to read them?"),
    (["Nothing in your calendar. Want me to check the transcripts?"],
     "Nothing in your calendar. Want me to check the transcripts?"),
])
def test_a_real_question_ends_with_a_question_mark_and_an_offer_is_dropped(api, said, spoken):
    api.answer = said
    body = api.post(URL, json=ASK, headers=VOICE).json()
    assert body["choices"][0]["message"]["content"] == spoken
    res = api.post(URL, json={**ASK, "stream": True}, headers=VOICE)
    chunks = [json.loads(l[6:]) for l in res.text.split("\n\n") if l.startswith("data: {")]
    assert "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks) == spoken


# ------------------------------------------------------------ the satellite's seam
def _satellite(monkeypatch, *, allowed: bool):
    import sys

    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Speaker:
        verified: bool
        is_owner: bool
        name: str | None
        score: float | None

    @dataclass(frozen=True)
    class Ctx:
        context_id: str
        device_id: str
        device_name: str
        area: str
        wake_word: str
        started_at_ms: int
        speaker: Speaker | None

    claimed = []

    async def claim(owner, *, wait_seconds=3.0):
        claimed.append(owner)
        return Ctx("c1", "echo-1", "Kitchen Echo", "Kitchen", "hey athena", 1,
                   Speaker(True, allowed, "Sam" if allowed else None, 0.7))

    fake = SimpleNamespace(claim=claim, personal_allowed=lambda ctx: allowed,
                           PRIVATE_REPLY="Owner only.")
    monkeypatch.setitem(sys.modules, "fork.voice.satellite", fake)
    import fork.voice
    monkeypatch.setattr(fork.voice, "satellite", fake, raising=False)
    return claimed


def test_without_the_satellite_module_every_speaker_is_answered(api):
    api.post(URL, json=ASK, headers=VOICE)
    kw = api.turns[-1]
    assert kw["gate"].personal is True and kw["context_lines"] == ("",)


def test_the_satellite_context_goes_into_the_turn(api, monkeypatch):
    claimed = _satellite(monkeypatch, allowed=True)
    api.post(URL, json=ASK, headers=VOICE)
    kw = api.turns[-1]
    assert claimed == ["alice"]
    assert kw["context_lines"] == ("Asked from: Kitchen Echo, Kitchen",)
    assert kw["gate"].personal is True and kw["no_memory"] is False
    turn = api.get(LATEST, headers=VOICE).json()["turn"]
    assert turn["device"] == {"id": "echo-1", "name": "Kitchen Echo", "area": "Kitchen"}
    assert turn["speaker"] == {"verified": True, "is_owner": True, "name": "Sam"}


def test_a_voice_that_isnt_the_owners_gets_no_personal_answers(api, monkeypatch):
    _satellite(monkeypatch, allowed=False)
    api.post(URL, json=ASK, headers=VOICE)
    kw = api.turns[-1]
    assert kw["gate"].personal is False and kw["gate"].private_reply == "Owner only."
    assert kw["no_memory"] is True  # nothing recalled into the context either
    # and a yes from that voice doesn't run a kept change
    monkeypatch.setattr(confirm, "run", lambda p: pytest.fail("must not run"))
    _propose(api)
    api.post(URL, json=DENTIST, headers=VOICE)
    body = api.post(URL, json=YES, headers=VOICE).json()
    assert body["choices"][0]["message"]["content"] == "Owner only."


# --------------------------------------------------------- what may be proposed
async def test_the_allow_list_describes_its_calls(monkeypatch, data_dir):
    from tests.fork.brain_fakes import notes_db

    monkeypatch.setattr(confirm, "athena_item_title", _no_title)
    monkeypatch.setattr("fork.brain.toolset.athena_server", lambda manager=None: "convo")
    d = confirm.describe
    todo = await d("alice", "manage_notes", json.dumps(
        {"action": "add", "title": "Call the dentist",
         "checklist_items": [{"text": "Call the dentist"}]}))
    assert (todo.kind, todo.where, todo.title) == ("todo", "To-dos", "Call the dentist")
    note = await d("alice", "manage_notes", json.dumps(
        {"action": "create", "title": "Gift ideas", "content": "A blue scarf"}))
    assert (note.kind, note.where, note.detail) == ("note", "Notes", "A blue scarf")
    event = await d("alice", "manage_calendar", json.dumps(
        {"action": "create_event", "summary": "Picnic", "local_start": {"date": "2026-11-07"}}))
    assert (event.kind, event.due, event.all_day) == ("event", "2026-11-07", True)
    done = await d("alice", UPDATE, json.dumps({"item_id": 4, "status": "done"}))
    assert (done.action, done.kind, done.source) == ("completed", "action_item", "athena")
    for name, args in (("manage_notes", {"action": "delete", "id": "abc"}),
                       ("manage_calendar", {"action": "delete_event", "uid": "u"}),
                       ("manage_calendar", {"action": "update_event", "uid": "u"}),
                       (UPDATE, {"item_id": 4, "status": "dismissed"}),
                       ("manage_memory", {"action": "add", "text": "x"}),
                       ("send_email", {"to": "x@example.test"}),
                       ("generate_image", {"prompt": "x"})):
        assert await d("alice", name, json.dumps(args)) is None, (name, args)

    # a list item: only an update that keeps every line and adds
    from core.database import Note
    factory = notes_db(monkeypatch)
    db = factory()
    db.add(Note(id="list1234", owner="alice", title="Shopping list", note_type="checklist",
                items=json.dumps([{"text": "Milk", "done": True}, {"text": "Bread", "done": False}])))
    db.commit()
    db.close()
    keep = {"action": "update", "id": "list1234", "checklist_items": [
        {"text": "Milk", "done": True}, {"text": "Bread", "done": False}, {"text": "Eggs"}]}
    item = await d("alice", "manage_notes", json.dumps(keep))
    assert (item.kind, item.title, item.where) == ("list_item", "Eggs", "Shopping list")
    assert item.check() is True
    drops = {"action": "update", "id": "list1234", "checklist_items": [{"text": "Eggs"}]}
    assert await d("alice", "manage_notes", json.dumps(drops)) is None
    rename = {**keep, "title": "Groceries"}
    assert await d("alice", "manage_notes", json.dumps(rename)) is None
    assert await d("bob", "manage_notes", json.dumps(keep)) is None  # not bob's list
