"""Fork: a question with a document open in the editor is the brain's; a change to it
stays upstream's (fork/brain/open_document.py, the hook in routes/chat_routes.py).
Fictional data."""
from __future__ import annotations

import pytest

from fork.brain import open_document
from fork.brain import turn as brain_turn
from src.prompt_security import GUARD_OPEN
from tests.fork.brain_fakes import Brain, _event, notes_db, text

PLAN = "Garden plan\n\nPlant the tomatoes along the south fence of Lake Varn cottage."


@pytest.fixture
def brain(monkeypatch, data_dir):
    return Brain(monkeypatch, data_dir)


@pytest.fixture
def docs(monkeypatch):
    import core.database as cdb

    factory = notes_db(monkeypatch)
    with factory() as db:
        db.add(cdb.Document(id="doc-1", owner="alice", title="Garden plan",
                            language="markdown", current_content=PLAN))
        db.add(cdb.Document(id="doc-9", owner="bob", title="Bob's diary",
                            language="markdown", current_content="Bob's secret plan"))
        db.commit()
    return factory


async def _turn(brain, sess, message, **extra):
    t = brain.start(sess, message, **extra)
    events = [_event(c) async for c in brain_turn._run_new_turn(t)]
    return t, events


# ---------------------------------------------------------------------- the hook
@pytest.fixture
def started(monkeypatch):
    import time

    from fork.brain import history

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


async def _post(monkeypatch, **form):
    from tests.fork.test_brain_hook import _body, _Request
    from tests.test_foreground_model_routing import _chat_stream_endpoint

    endpoint = _chat_stream_endpoint(monkeypatch, "chat", {})
    return await _body(await endpoint(_Request("chat", **form)))


@pytest.mark.parametrize("message", ["what does this say about tomatoes?",
                                     "summarise it", "is there a date in here?"])
async def test_a_question_with_a_document_open_is_the_brains(monkeypatch, started, message):
    await _post(monkeypatch, active_doc_id="doc-1", active_doc_state="visible",
                message=message)
    assert started, "the brain didn't take the turn"
    assert started["active_doc_id"] == "doc-1"


@pytest.mark.parametrize("message", ["make it shorter", "rewrite it"])
async def test_an_edit_of_the_open_document_goes_to_upstream(monkeypatch, started, message):
    from tests.fork.brain_fakes import fake_request

    sess = type("S", (), {"history": [], "owner": "alice"})()
    out = await brain_turn.brain_response(fake_request("alice"), sess=sess,
                                          session_id="s1", message=message, owner="alice",
                                          active_doc_id="doc-1")
    assert out is None and not started
    out = await brain_turn.brain_response(fake_request("alice"), sess=sess,
                                          session_id="s1", message=message, owner="alice")
    assert out is not None and started  # without a document open it is the brain's


@pytest.mark.parametrize("message", ["make it shorter", "fix the typos",
                                     "rewrite the second paragraph", "add a title",
                                     "translate this to Spanish"])
def test_edits_of_the_open_document_are_upstreams(message):
    assert open_document.wants_edit(message)


@pytest.mark.parametrize("message", ["what does this say?", "summarise it",
                                     "who is mentioned?", "when is the deadline?"])
def test_questions_are_not_edits(message):
    assert not open_document.wants_edit(message)


# ------------------------------------------------------------------ the prompt
async def test_the_open_document_reaches_the_context_message_marked_untrusted(brain, docs):
    sess = brain.session()
    brain.llm.rounds([text("Along the south fence.")])
    t, _ = await _turn(brain, sess, "where do the tomatoes go?", active_doc_id="doc-1")
    request = brain.llm.requests[0]
    ctx = request["messages"][-2]
    assert "south fence" in ctx["content"] and "Title: Garden plan" in ctx["content"]
    assert GUARD_OPEN in ctx["content"] and ctx["metadata"]["trusted"] is False
    assert "south fence" not in request["messages"][0]["content"]
    assert t.run.security.external_untrusted_context_seen is True


async def test_another_owners_document_gives_the_brain_nothing(brain, docs):
    sess = brain.session()
    brain.llm.rounds([text("I don't see a document.")])
    await _turn(brain, sess, "what does this say?", active_doc_id="doc-9")
    ctx = brain.llm.requests[0]["messages"][-2]["content"]
    assert "secret plan" not in ctx and open_document.DOCUMENT_LABEL not in ctx


async def test_an_unknown_document_gives_the_brain_nothing(brain, docs):
    assert await open_document.item("doc-404", "alice") is None
    assert await open_document.item("", "alice") is None


async def test_an_oversized_document_is_trimmed(monkeypatch, docs):
    import core.database as cdb

    with docs() as db:
        db.add(cdb.Document(id="doc-2", owner="alice", title="Long", language="text",
                            current_content="word " * 6000))
        db.commit()
    _, body = await open_document.item("doc-2", "alice")
    assert "more characters not shown" in body
    assert len(body) < open_document.DOCUMENT_CHARS + 500
