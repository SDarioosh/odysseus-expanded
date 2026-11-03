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
from fork.voice import openai_api, routes as voice_routes, sessions as voice_sessions
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
    return Brain(monkeypatch, data_dir)


def _token_request(owner="alice", scopes=("voice",)):
    req = fake_request(owner)
    req.state.api_token = True
    req.state.api_token_owner = owner
    req.state.api_token_scopes = list(scopes)
    return req


async def _voice(brain, sess, message, *, request=None, disabled=voice_routes.LEFT_OUT):
    events = brain_turn.read_only_turn(
        request or _token_request(sess.owner), sess=sess, session_id=sess.id,
        message=message, owner=sess.owner, instructions="Speak briefly.", disabled=disabled)
    assert events is not None
    out = []
    async for chunk in events:
        out.append(chunk)
    return out


def _offered(request) -> set[str]:
    return {t["function"]["name"] for t in request.get("tools") or []}


async def test_voice_offers_athena_reads_to_a_token_but_no_writes_and_no_email(brain):
    sess = brain.session()
    brain.llm.rounds([text("Nothing yet.")])
    await _voice(brain, sess, "What did Priya say?")
    offered = _offered(brain.llm.requests[0])
    # a bearer token's run, yet Athena's read tools are offered (as the owner)
    assert SEARCH in offered
    assert UPDATE not in offered and ERASE not in offered
    assert not offered & set(voice_routes.LEFT_OUT)
    assert "generate_image" not in offered
    # the voice instructions stand where a preset's text goes
    assert brain.llm.requests[0]["messages"][0]["content"].endswith("Speak briefly.")


async def test_a_write_the_model_calls_anyway_is_refused_never_asked(brain):
    sess = brain.session()
    brain.llm.rounds([calls((UPDATE, {"id": 3}))], [text("I can't change that by voice.")])
    chunks = await _voice(brain, sess, "Mark the cake to-do done")
    assert brain.ran == []  # upstream's runner never saw the call
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    assert not any(e.get("type") == "ask_user" or e.get("ask_user") for e in events)
    tool_msg = [m for m in brain.llm.requests[1]["messages"] if m.get("role") == "tool"][-1]
    assert "not available here" in tool_msg["content"]  # not offered, so not run
    assert brain_turn.pending_count() == 0


async def test_store_tools_run_their_reads_and_refuse_their_writes(brain):
    sess = brain.session()
    brain.llm.rounds(
        [calls(("manage_calendar", {"action": "list_events"}),
               ("manage_calendar", {"action": "create_event", "title": "Dentist"}))],
        [text("You have nothing on Tuesday.")])
    await _voice(brain, sess, "What's on Tuesday?")
    assert [r["tool"] for r in brain.ran] == ["manage_calendar"]
    assert "list_events" in brain.ran[0]["content"]
    refused = [m for m in brain.llm.requests[1]["messages"] if m.get("role") == "tool"][-1]
    assert "can only read" in refused["content"]
    assert brain_turn.pending_count() == 0


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
def api(monkeypatch):
    """The voice router on a bare app; the token is set by a header the fake auth reads."""
    voice_sessions.forget_all()
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

    def fake_turn(request, *, sess, session_id, message, owner, instructions, disabled,
                  services):
        turns.append({"sid": session_id, "message": message, "owner": owner,
                      "instructions": instructions, "disabled": disabled})

        async def events():
            yield 'data: {"type": "tool_start", "tool": "x"}\n\n'
            yield 'data: {"delta": "**Priya** brings the cake. "}\n\n'
            yield 'data: {"delta": "' + f"[transcript]({LINK})" + '"}\n\n'
            yield 'data: {"type": "metrics", "data": {"input_tokens": 9, "output_tokens": 4}}\n\n'
            yield "data: [DONE]\n\n"
        return events()

    monkeypatch.setattr("fork.brain.turn.read_only_turn", fake_turn)
    monkeypatch.setattr(voice_sessions, "default_model",
                        lambda owner: ("http://127.0.0.1:8082/v1/chat/completions", "local", {}))
    monkeypatch.setattr(voice_sessions, "_file_in_folder", lambda sid: None)
    app.include_router(voice_routes.setup_voice_routes())
    client = TestClient(app)
    client.turns = turns
    client.sessions = app.state.session_manager
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
    assert "read aloud" in turn["instructions"] and "send_email" in turn["disabled"]
    assert api.sessions.created[0].name == "Voice: Who brings the cake?"


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


def test_a_conversation_goes_stale_after_the_follow_up_window():
    voice_sessions.forget_all()
    voice_sessions.remember("alice", "id:x", "chat-1", now=1000.0)
    assert voice_sessions.remembered("alice", "id:x", now=1000.0 + 60) == "chat-1"
    assert voice_sessions.remembered("bob", "id:x", now=1000.0 + 60) is None
    late = 1000.0 + voice_sessions.FOLLOW_UP_SECONDS + 61
    assert voice_sessions.remembered("alice", "id:x", now=late) is None


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
