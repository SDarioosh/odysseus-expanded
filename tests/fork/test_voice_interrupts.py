"""Fork: a satellite's barge-in ends the voice turn it interrupted (fork/voice/interrupts.py,
``POST …/satellite/interrupt`` in fork/voice/satellite_routes.py, the hook in
fork/voice/routes.py). Fake brain turn that waits on a slow model; fictional data."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from fork.voice import confirm, interrupts, ledger, openai_api, satellite, satellite_routes
from fork.voice import routes as voice_routes
from fork.voice import sessions as voice_sessions

VOICE = {"x-test-scopes": "voice"}
ASK = {"model": "odysseus", "messages": [{"role": "user", "content": "What did Priya say?"}]}
URL = voice_routes.PREFIX + "/chat/completions"
TURN = voice_routes.PREFIX + "/satellite/turn"
INTERRUPT = voice_routes.PREFIX + "/satellite/interrupt"
LATEST = voice_routes.PREFIX + "/turns/latest"
ECHO = {"device_id": "echo-kitchen", "device_name": "Kitchen Echo", "area": "Kitchen",
        "wake_word": "hey_athena"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    satellite.forget_all()
    interrupts.forget_all()
    confirm.forget_all()
    voice_sessions.forget_all()
    monkeypatch.delenv("FORK_VOICE_PRIVATE", raising=False)
    yield
    satellite.forget_all()
    interrupts.forget_all()


class FakeSessions:
    def __init__(self):
        self.sessions = {}

    def create_session(self, session_id, name, url, model, owner=None, headers=None, **kw):
        sess = SimpleNamespace(id=session_id, name=name, owner=owner, endpoint_url=url,
                               model=model, headers=headers or {}, history=[])
        self.sessions[session_id] = sess
        return sess

    def get_session(self, session_id):
        return self.sessions[session_id]


@pytest.fixture
def hub(monkeypatch, data_dir):
    """The voice and satellite routers on one app. The fake brain turn says "Let me
    look." and then waits for the model (``hub.model_done``) before the answer; it records
    whether it was closed before the model answered (``hub.closed``)."""
    app = FastAPI()
    app.state.session_manager = FakeSessions()
    state = SimpleNamespace(started=asyncio.Event(), model_done=asyncio.Event(), closed=[],
                            turns=0, preamble=False)

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        request.state.api_token_owner = request.headers.get("x-test-owner", "alice") \
            if scopes is not None else None
        request.state.api_token_scopes = scopes.split(",") if scopes else []
        request.state.current_user = "api" if scopes is not None else "alice"
        return await call_next(request)

    def fake_turn(request, **kw):
        state.turns += 1

        async def events():
            try:
                if state.preamble:
                    yield 'data: {"delta": "Let me look. "}\n\n'
                yield 'data: {"type": "tool_start", "tool": "x"}\n\n'
                state.started.set()
                await state.model_done.wait()
                yield 'data: {"type": "agent_step", "round": 2}\n\n'
                yield 'data: {"delta": "Priya said the budget is fine."}\n\n'
                yield "data: [DONE]\n\n"
            finally:
                state.closed.append(not state.model_done.is_set())
        return events()

    async def no_identify(audio, **_):
        return None

    monkeypatch.setattr("fork.brain.turn.read_only_turn", fake_turn)
    monkeypatch.setattr("fork.voice.chat_prefix.default_preset",
                        lambda: SimpleNamespace(system_prompt="", temperature=None,
                                                max_tokens=None, character_name=None))
    monkeypatch.setattr(voice_sessions, "default_model",
                        lambda owner: ("http://127.0.0.1:8082/v1/chat/completions", "local", {}))
    monkeypatch.setattr(voice_sessions, "_file_in_folder", lambda sid: None)
    monkeypatch.setattr(voice_routes, "_save_exchange", lambda *a: None)
    monkeypatch.setattr(satellite, "identify", no_identify)
    app.include_router(voice_routes.setup_voice_routes())
    app.include_router(satellite_routes.setup_satellite_routes())
    state.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://hub")
    return state


async def _post_context(hub, device="echo-kitchen", owner="alice") -> str:
    res = await hub.client.post(TURN, data={**ECHO, "device_id": device},
                                headers={**VOICE, "x-test-owner": owner})
    assert res.status_code == 200
    return res.json()["context_id"]


async def _interrupt(hub, device="echo-kitchen", owner="alice", context_id=None):
    data = {"device_id": device}
    if context_id is not None:
        data["context_id"] = context_id
    res = await hub.client.post(INTERRUPT, data=data,
                                headers={**VOICE, "x-test-owner": owner})
    assert res.status_code == 200
    return res.json()


async def _ask(hub, **extra):
    return await hub.client.post(URL, json={**ASK, **extra}, headers=VOICE)


# ------------------------------------------------------------------- the endpoint
async def test_a_barge_in_ends_the_running_turn_with_an_empty_answer(hub):
    await _post_context(hub)
    ask = asyncio.create_task(_ask(hub))
    await asyncio.wait_for(hub.started.wait(), 5)
    assert await _interrupt(hub) == {"ok": True, "cut": 1, "marked": 0}
    res = await asyncio.wait_for(ask, 5)  # at once, not after the model
    body = res.json()
    assert res.status_code == 200
    # empty: Home Assistant speaks nothing and ends its run
    assert body["choices"][0]["message"] == {"role": "assistant", "content": ""}
    assert hub.closed == [True]  # the turn (and its model call) was closed early
    turn = ledger.latest_turn("alice")
    assert turn["answer"] == "" and turn["question"] == "What did Priya say?"
    assert interrupts.running_count() == 0


async def test_a_streamed_turn_ends_with_what_was_already_said(hub):
    hub.preamble = True
    await _post_context(hub)
    ask = asyncio.create_task(_ask(hub, stream=True))
    await asyncio.wait_for(hub.started.wait(), 5)
    await _interrupt(hub)
    res = await asyncio.wait_for(ask, 5)
    lines = [l[6:] for l in res.text.split("\n\n") if l.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(l) for l in lines[:-1]]
    said = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert said == "Let me look."  # no apology, no "I couldn't find an answer"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert hub.closed == [True]


async def test_the_context_posted_after_ha_asked_still_finds_the_turn(hub):
    # Home Assistant's question came before the Echo's post: the turn has no context.
    ask = asyncio.create_task(_ask(hub))
    await asyncio.wait_for(hub.started.wait(), 5)
    await _post_context(hub)
    assert await _interrupt(hub) == {"ok": True, "cut": 1, "marked": 0}
    assert (await asyncio.wait_for(ask, 5)).json()["choices"][0]["message"]["content"] == ""
    # the context went to that turn: the next question can't claim it
    assert await satellite.claim("alice", wait_seconds=0) is None


async def test_a_late_post_is_only_adopted_right_after_the_question(hub, monkeypatch):
    monkeypatch.setattr(interrupts, "ADOPT_SECONDS", 0.0)
    ask = asyncio.create_task(_ask(hub))
    await asyncio.wait_for(hub.started.wait(), 5)
    await _post_context(hub)  # too late: another turn's
    assert await _interrupt(hub) == {"ok": True, "cut": 0, "marked": 1}
    hub.model_done.set()
    res = await asyncio.wait_for(ask, 5)
    assert res.json()["choices"][0]["message"]["content"] == "Priya said the budget is fine."


async def test_a_long_turn_is_still_found(hub, monkeypatch):
    # a barge-in after a long "Thinking…": the context's claim window has long passed
    await _post_context(hub)
    ask = asyncio.create_task(_ask(hub))
    await asyncio.wait_for(hub.started.wait(), 5)
    later = satellite._now() + 120
    monkeypatch.setattr(satellite, "_now", lambda: later)
    assert await _interrupt(hub) == {"ok": True, "cut": 1, "marked": 0}
    assert (await asyncio.wait_for(ask, 5)).json()["choices"][0]["message"]["content"] == ""


async def test_a_barge_in_before_ha_asked_empties_that_turn(hub):
    await _post_context(hub)
    assert await _interrupt(hub) == {"ok": True, "cut": 0, "marked": 1}
    res = await asyncio.wait_for(_ask(hub), 5)
    assert res.json()["choices"][0]["message"]["content"] == ""
    assert hub.turns == 0  # the brain never ran


async def test_the_next_question_is_answered(hub):
    await _post_context(hub)
    ask = asyncio.create_task(_ask(hub))
    await asyncio.wait_for(hub.started.wait(), 5)
    await _interrupt(hub)
    await asyncio.wait_for(ask, 5)
    hub.model_done.set()  # the model answers at once now
    await _post_context(hub)
    res = await asyncio.wait_for(_ask(hub), 5)
    assert res.json()["choices"][0]["message"]["content"] == "Priya said the budget is fine."


async def test_other_devices_owners_and_turns_without_a_device_are_left_alone(hub):
    # a turn without any satellite context (HA's text Assist, another client)
    ask = asyncio.create_task(_ask(hub))
    await asyncio.wait_for(hub.started.wait(), 5)
    assert await _interrupt(hub) == {"ok": True, "cut": 0, "marked": 0}
    assert await _interrupt(hub, device="echo-bedroom") == {"ok": True, "cut": 0, "marked": 0}
    assert await _interrupt(hub, owner="bob") == {"ok": True, "cut": 0, "marked": 0}
    hub.model_done.set()
    res = await asyncio.wait_for(ask, 5)
    assert res.json()["choices"][0]["message"]["content"] == "Priya said the budget is fine."
    assert hub.closed == [False]


async def test_an_interrupted_turn_keeps_no_change_waiting_for_a_yes(hub, monkeypatch):
    await _post_context(hub)
    ask = asyncio.create_task(_ask(hub))
    await asyncio.wait_for(hub.started.wait(), 5)
    # as the voice gate would (fork/voice/policy.py): this turn kept a change for a "yes"
    with ledger._connect() as db:
        running = db.execute("SELECT id, session_id FROM turns WHERE ended_at IS NULL").fetchone()
    monkeypatch.setattr(confirm, "pending", lambda owner, sid, **kw: SimpleNamespace(
        turn_id=running["id"], question=lambda: "Add Buy milk to your to-dos?"))
    dropped = []
    monkeypatch.setattr(confirm, "drop", lambda owner, sid: dropped.append(sid))
    await _interrupt(hub)
    body = (await asyncio.wait_for(ask, 5)).json()
    assert body["choices"][0]["message"]["content"] == ""  # the question was never heard
    assert dropped == [running["session_id"]]
    assert ledger.latest_turn("alice")["awaiting_confirmation"] is None


# ------------------------------------------------- the context id (POST …/satellite/turn)
async def _latest(hub, **params):
    res = await hub.client.get(LATEST, params=params, headers=VOICE)
    assert res.status_code == 200
    return res.json()["turn"]


async def test_the_latest_turn_by_its_context_id(hub):
    ctx = await _post_context(hub)
    ask = asyncio.create_task(_ask(hub))
    await asyncio.wait_for(hub.started.wait(), 5)
    assert await _latest(hub, context_id=ctx) is None  # still running
    hub.model_done.set()
    await asyncio.wait_for(ask, 5)
    turn = await _latest(hub, context_id=ctx)
    assert turn["context_id"] == ctx and turn["answer"] == "Priya said the budget is fine."
    # the id wins over since (a time after the turn would find none)
    future = int((ledger.time.time() + 60) * 1000)
    assert (await _latest(hub, context_id=ctx, since=future))["id"] == turn["id"]
    assert await _latest(hub, since=future) is None
    assert (await _latest(hub))["id"] == turn["id"]  # without an id: as before
    assert await _latest(hub, context_id="0" * 32) is None  # unknown id
    other = await hub.client.get(LATEST, params={"context_id": ctx},
                                 headers={**VOICE, "x-test-owner": "bob"})
    assert other.json() == {"turn": None}  # another owner's context


async def test_a_turn_without_a_context_has_no_context_id(hub):
    hub.model_done.set()
    await asyncio.wait_for(_ask(hub), 5)
    assert (await _latest(hub))["context_id"] is None


async def test_an_adopted_context_is_stored_on_its_turn(hub):
    from fork.voice import timings

    ask = asyncio.create_task(_ask(hub))  # Home Assistant asked before the Echo posted
    await asyncio.wait_for(hub.started.wait(), 5)
    ctx = await _post_context(hub)
    hub.model_done.set()
    await asyncio.wait_for(ask, 5)
    turn = await _latest(hub, context_id=ctx)
    assert turn is not None and turn["context_id"] == ctx
    [row] = timings.rows("alice")
    assert row["context_id"] == ctx and row["device_id"] == "echo-kitchen"
    assert row["turn_id"] == turn["id"]


async def test_a_barge_in_with_the_context_id_cuts_that_turn(hub):
    ctx = await _post_context(hub)
    ask = asyncio.create_task(_ask(hub))
    await asyncio.wait_for(hub.started.wait(), 5)
    assert await _interrupt(hub, context_id=ctx) == {"ok": True, "cut": 1, "marked": 0}
    assert (await asyncio.wait_for(ask, 5)).json()["choices"][0]["message"]["content"] == ""


async def test_a_barge_in_naming_an_older_context_leaves_the_running_turn(hub):
    old = await _post_context(hub)
    assert await satellite.claim("alice", wait_seconds=0) is not None  # its turn is over
    await _post_context(hub)
    ask = asyncio.create_task(_ask(hub))
    await asyncio.wait_for(hub.started.wait(), 5)
    # same device, but the id names the earlier context: not device-wide
    assert await _interrupt(hub, context_id=old) == {"ok": True, "cut": 0, "marked": 0}
    hub.model_done.set()
    res = await asyncio.wait_for(ask, 5)
    assert res.json()["choices"][0]["message"]["content"] == "Priya said the budget is fine."


async def test_a_barge_in_with_an_unknown_context_id_works_by_device(hub):
    await _post_context(hub)
    ask = asyncio.create_task(_ask(hub))
    await asyncio.wait_for(hub.started.wait(), 5)
    assert await _interrupt(hub, context_id="0" * 32) == {"ok": True, "cut": 1, "marked": 0}
    assert (await asyncio.wait_for(ask, 5)).json()["choices"][0]["message"]["content"] == ""


async def test_a_barge_in_with_the_id_before_ha_asked_marks_that_context(hub):
    ctx = await _post_context(hub)
    assert await _interrupt(hub, context_id=ctx) == {"ok": True, "cut": 0, "marked": 1}
    res = await asyncio.wait_for(_ask(hub), 5)
    assert res.json()["choices"][0]["message"]["content"] == ""
    assert hub.turns == 0
    assert (await _latest(hub, context_id=ctx))["answer"] == ""


# ------------------------------------------------------------------- the route
async def test_the_interrupt_route_needs_a_voice_token_and_a_device(hub):
    res = await hub.client.post(INTERRUPT, data={"device_id": "echo-kitchen"})
    assert res.status_code == 403
    res = await hub.client.post(INTERRUPT, data={"device_id": "echo-kitchen"},
                                headers={"x-test-scopes": "chat"})
    assert res.status_code == 403
    res = await hub.client.post(INTERRUPT, data={"device_id": " "}, headers=VOICE)
    assert res.status_code == 422
    res = await hub.client.post(INTERRUPT, json={"device_id": "echo-kitchen"}, headers=VOICE)
    assert res.status_code == 415
    res = await hub.client.post(INTERRUPT, data={"device_id": "x" * 200}, headers=VOICE)
    assert res.status_code == 422
    res = await hub.client.post(INTERRUPT, data={"device_id": "echo-kitchen",
                                                 "context_id": "x" * 200}, headers=VOICE)
    assert res.status_code == 422


# ------------------------------------------------------------------- the pieces
async def test_until_stopped_closes_the_turn_when_stopped():
    closed = []
    stop = asyncio.Event()

    async def turn():
        try:
            yield "one"
            await asyncio.sleep(30)
            yield "two"
        finally:
            closed.append(True)

    got = []

    async def read():
        async for chunk in interrupts.until_stopped(turn(), stop):
            got.append(chunk)

    task = asyncio.create_task(read())
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert got == ["one"] and closed == [True]


async def test_until_stopped_passes_a_whole_turn_through():
    async def turn():
        for x in ("a", "b", "c"):
            yield x
    assert [c async for c in interrupts.until_stopped(turn(), asyncio.Event())] == ["a", "b", "c"]


def test_an_interrupted_answer_says_nothing_more():
    a = openai_api.Answer(streaming=False)
    a.feed('data: {"delta": "Half an ans"}\n\n')
    a.interrupt()
    assert a.finish() == "" and a.speech.spoken == ""
    b = openai_api.Answer(streaming=False)
    b.interrupt()
    assert b.finish() == ""  # not voice_prompt.NO_ANSWER_REPLY


async def test_a_marked_context_is_claimed_as_interrupted():
    satellite.register("alice", device_id="echo-kitchen")
    assert satellite.interrupt_unclaimed("alice", "echo-kitchen") == 1
    ctx = await satellite.claim("alice")
    assert ctx is not None and ctx.interrupted is True
    satellite.register("alice", device_id="echo-kitchen")
    ctx = await satellite.claim("alice")
    assert ctx.interrupted is False


def test_an_old_ledger_without_the_context_column_still_works(data_dir):
    import sqlite3

    from fork.common import sqlite as common_sqlite

    old = ledger.SCHEMA  # the columns added later are not in it
    path = ledger.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as raw:
        raw.executescript(old)
        raw.execute("INSERT INTO turns (id, owner, started_at, ended_at, question, answer)"
                    " VALUES ('t0', 'alice', ?, ?, 'Old question', 'Old answer')",
                    (ledger.time.time(), ledger.time.time()))
    common_sqlite._ready.clear()  # as after an upgrade: a new process opens the old file
    turn = ledger.latest_turn("alice")
    assert turn["id"] == "t0" and turn["context_id"] is None
    new = ledger.start_turn("alice", "New question", context_id="ctx-1")
    ledger.finish_turn(new, answer="New answer")
    assert ledger.latest_turn("alice", context_id="ctx-1")["id"] == new
