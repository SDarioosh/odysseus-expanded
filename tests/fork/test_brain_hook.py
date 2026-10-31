"""Fork: the brain's hook in ``chat_stream`` (routes/chat_routes.py -> fork/brain/turn.py).

Drives the real ``/api/chat_stream`` route with the stubs of
``tests/test_foreground_model_routing.py``: off, the hook changes nothing; on, a normal
chat turn gets the brain's stream and upstream's paths never run; ``/research`` and an
approval that isn't the brain's stay upstream's."""
from __future__ import annotations

import json

import pytest

from fork.brain import turn as brain_turn
from tests.test_foreground_model_routing import _chat_stream_endpoint, _RouteRequest


class _Request(_RouteRequest):
    def __init__(self, mode="chat", **form):
        super().__init__(mode)
        self._form["compare_mode"] = "false"
        self._form.update(form)


@pytest.fixture(autouse=True)
def no_sweep(monkeypatch):
    """The hook's history sweep reads the real data dir and chat database: not here."""
    import time

    from fork.brain import history

    monkeypatch.setattr(history, "_last_sweep", time.monotonic())


@pytest.fixture
def spy(monkeypatch):
    """Records each call of the hook and its answer."""
    seen = []
    original = brain_turn.brain_response

    async def recording(*args, **kwargs):
        result = await original(*args, **kwargs)
        seen.append((kwargs, result))
        return result

    monkeypatch.setattr(brain_turn, "brain_response", recording)
    return seen


@pytest.fixture
def no_brain_turns(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the brain must not take this turn")

    monkeypatch.setattr(brain_turn, "_start_turn", refuse)
    monkeypatch.setattr(brain_turn, "take_pending", refuse)
    monkeypatch.setattr(brain_turn, "expire_pending", refuse)


async def _body(response) -> str:
    out = []
    async for chunk in response.body_iterator:
        out.append(chunk if isinstance(chunk, str) else chunk.decode())
    return "".join(out)


@pytest.mark.parametrize("mode", ["chat", "agent"])
async def test_off_the_hook_is_inert(monkeypatch, spy, no_brain_turns, mode):
    monkeypatch.setenv("FORK_BRAIN", "off")
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, mode, captured)
    response = await endpoint(_Request(mode))
    await _body(response)
    selected = ("https://selected.example/v1", "selected-model",
                {"Authorization": "Bearer selected"})
    assert (captured.get("chat") == [selected]) if mode == "chat" else (
        captured["agent"]["primary"] == selected)
    assert [result for _, result in spy] == [None]


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "OFF"])
async def test_off_values_never_reach_the_brain(monkeypatch, no_brain_turns, value):
    monkeypatch.setenv("FORK_BRAIN", value)
    assert await brain_turn.brain_response(object(), sess=None, session_id="s", message="hi",
                                           owner="alice") is None


@pytest.mark.parametrize("value", [None, "", "on", "yes"])
def test_the_brain_is_on_unless_switched_off(monkeypatch, value):
    from fork.brain import settings as brain_settings

    if value is None:
        monkeypatch.delenv("FORK_BRAIN", raising=False)
    else:
        monkeypatch.setenv("FORK_BRAIN", value)
    assert brain_settings.enabled() is True


async def test_on_a_normal_turn_gets_the_brains_stream(monkeypatch, spy):
    monkeypatch.setenv("FORK_BRAIN", "on")
    started = {}

    def fake_start(request, **kw):
        started.update(kw)
        return "turn"

    async def fake_run(turn):
        yield f"data: {json.dumps({'delta': 'from the brain'})}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(brain_turn, "_start_turn", fake_start)
    monkeypatch.setattr(brain_turn, "_run_new_turn", fake_run)
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    response = await endpoint(_Request("chat"))
    body = await _body(response)
    assert "from the brain" in body
    assert "chat" not in captured and "agent" not in captured  # upstream never ran
    assert response.headers["X-Odysseus-Run-Id"]
    assert response.media_type == "text/event-stream"
    assert started["message"] == "hello" and started["owner"] == "alice"
    assert started["session_id"] == "session-1"


async def test_research_stays_upstreams(monkeypatch, spy, no_brain_turns):
    monkeypatch.setenv("FORK_BRAIN", "on")
    monkeypatch.setattr(brain_turn, "expire_pending", lambda now=None: 0)
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    try:
        response = await endpoint(_Request("chat", use_research="true"))
        await _body(response)
    except Exception:  # noqa: BLE001 -- the stubbed research service; the hook is what counts
        pass
    kwargs, result = spy[0]
    assert kwargs["upstream_turn"] is True and result is None


@pytest.mark.parametrize("form", [{"plan_mode": "true"}, {"incognito": "true"},
                                  {"compare_mode": "true"}])
async def test_other_upstream_turns_stay_upstreams(monkeypatch, spy, form):
    monkeypatch.setenv("FORK_BRAIN", "on")
    monkeypatch.setattr(brain_turn, "_start_turn", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("brain took an upstream turn")))
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    try:
        await _body(await endpoint(_Request("chat", **form)))
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001
        pass
    assert spy and spy[0][1] is None and spy[0][0]["upstream_turn"] is True


async def test_an_approval_that_isnt_the_brains_stays_upstreams(monkeypatch, spy):
    from src.tool_capabilities import capabilities_for_action

    import routes.chat_routes as chat_routes

    monkeypatch.setenv("FORK_BRAIN", "on")
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured)
    content = '{"action": "add", "content": "x"}'
    pending = chat_routes.tool_approval_store.create(
        owner="alice", session_id="session-1", origin_run_id="run-1",
        tool_name="manage_notes", content=content, workspace=None,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("manage_notes", content))
    request = _Request("agent", tool_approval_id=pending.approval_id,
                       tool_approval_decision="approve")
    await _body(await endpoint(request))
    kwargs, result = spy[0]
    assert kwargs["approval_id"] == pending.approval_id and result is None
    assert captured["exact_approval"].pending == pending  # upstream's agent resumed it


async def test_the_hook_takes_only_its_own_approvals(monkeypatch):
    monkeypatch.setenv("FORK_BRAIN", "on")
    assert brain_turn.takes_turn(message="hi", upstream_turn=False)
    assert not brain_turn.takes_turn(message="hi", upstream_turn=True)
    assert not brain_turn.takes_turn(message="   ", upstream_turn=False)
    assert await brain_turn.brain_response(object(), sess=None, session_id="s", message="",
                                           owner="alice", approval_id="unknown",
                                           exact_approval=object()) is None


@pytest.mark.parametrize("attachments", ['["0123456789abcdef0123456789abcdef.png"]',
                                         '["0123456789abcdef0123456789abcdef.txt"]'])
async def test_attachments_the_brain_cant_read_stay_upstreams(monkeypatch, spy, attachments):
    """Through the real route: an attachment id that doesn't resolve to a text file of
    the owner (here: no upload in the index) leaves the turn to upstream, which ran.
    Readable text and PDF attachments: tests/fork/test_brain_attachments.py."""
    monkeypatch.setenv("FORK_BRAIN", "on")
    monkeypatch.setattr(brain_turn, "_start_turn", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("brain took an upstream turn")))
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    await _body(await endpoint(_Request("chat", attachments=attachments)))
    kwargs, result = spy[0]
    assert result is None and kwargs["attachments"] and kwargs["upstream_turn"] is False
    assert "chat" in captured  # upstream's chat path answered


async def test_turns_with_an_open_document_stay_upstreams(monkeypatch, spy):
    """An open document stays upstream's; selected notes and an open email are the
    brain's since 2026-10-31 (tests/fork/test_brain_context.py)."""
    monkeypatch.setenv("FORK_BRAIN", "on")
    monkeypatch.setattr(brain_turn, "_start_turn", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("brain took an upstream turn")))
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    try:
        await _body(await endpoint(_Request("chat", active_doc_id="doc-1")))
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001
        pass
    assert spy and spy[0][1] is None and spy[0][0]["upstream_turn"] is True


@pytest.mark.parametrize("use_rag", ["true", "false"])
async def test_the_documents_toggle_reaches_the_brain(monkeypatch, spy, use_rag):
    """With search_documents in the brain's set, document RAG asked for is no reason to
    hand the turn to upstream; the toggle goes to the brain (off: no library search)."""
    monkeypatch.setenv("FORK_BRAIN", "on")
    started = {}

    def fake_start(request, **kw):
        started.update(kw)
        return "turn"

    async def fake_run(turn):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(brain_turn, "_start_turn", fake_start)
    monkeypatch.setattr(brain_turn, "_run_new_turn", fake_run)
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", {})
    await _body(await endpoint(_Request("chat", use_rag=use_rag)))
    kwargs, result = spy[0]
    assert result is not None and kwargs["upstream_turn"] is False
    assert started["use_rag"] == use_rag


async def test_the_hook_passes_memory_consent_and_the_outside_content_flag(monkeypatch, spy):
    monkeypatch.setenv("FORK_BRAIN", "on")
    started = {}

    def fake_start(request, **kw):
        started.update(kw)
        return "turn"

    async def fake_run(turn):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(brain_turn, "_start_turn", fake_start)
    monkeypatch.setattr(brain_turn, "_run_new_turn", fake_run)
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", {})
    await _body(await endpoint(_Request("chat")))  # the stub form sends no_memory=true
    kwargs, _ = spy[0]
    assert kwargs["no_memory"] is True and kwargs["external_untrusted_context_seen"] is False
    assert started["no_memory"] is True and started["external_seen"] is False
