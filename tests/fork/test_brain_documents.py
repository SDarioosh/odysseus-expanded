"""Fork: the brain's documents group (fork/brain/documents.py, toolset.py, tools.py,
turn.py): stored editor documents read without asking and changed only on approval, a
new document created through upstream's real runner and opened in the editor, and
``search_documents`` over the owner's personal library: owner-isolated, untrusted,
offered only where upstream would let the user read documents. Fictional data only."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fork.brain import documents as brain_documents
from fork.brain import prompt as brain_prompt
from fork.brain import tools as brain_tools
from fork.brain import toolset
from fork.brain import turn as brain_turn
from tests.fork.brain_fakes import Brain, calls, fake_request, of, text

SEARCH_DOCS = brain_documents.SEARCH_DOCUMENTS


@pytest.fixture
def brain(monkeypatch, data_dir):
    return Brain(monkeypatch, data_dir)


class FakeRag:
    """``rag_manager.search`` over fictional passages ``(owner, filename, text, similarity)``.
    ``leaky``: the store ignores the owner filter (the brain must still drop them)."""

    def __init__(self, rows, *, leaky=False):
        self.rows, self.leaky, self.asked = list(rows), leaky, []

    def search(self, query, k=5, owner=None):
        self.asked.append({"query": query, "k": k, "owner": owner})
        rows = [r for r in self.rows if self.leaky or owner is None or r[0] == owner]
        return [{"id": f"c{i}", "document": t, "similarity": s,
                 "metadata": {"filename": f, "owner": o}}
                for i, (o, f, t, s) in enumerate(rows)][:k]


LIBRARY = [
    ("alice", "boat-insurance.pdf", "Policy KX-4471 covers the sailboat Petrel until March.", 0.82),
    ("alice", "recipes.txt", "Lentil soup: two cups of red lentils.", 0.20),  # under the threshold
    ("bob", "bob-salary.txt", "Bob's salary review: confidential.", 0.95),
]


def rag_request(rag, user="alice"):
    request = fake_request(user)
    request.app.state.personal_docs_manager = SimpleNamespace(rag_manager=rag)
    return request


def offered(brain, i=0):
    return toolset.names(brain.llm.requests[i].get("tools") or ())


# ---------------------------------------------------------------------- tool set
def test_documents_is_a_default_group():
    from fork.brain.settings import DEFAULT_GROUPS
    from fork.common import settings

    assert "documents" in DEFAULT_GROUPS
    assert "documents" in settings.BRAIN_TOOL_GROUPS.default.split(",")
    assert toolset.GROUPS["documents"] == ("manage_documents", "create_document", SEARCH_DOCS)


@pytest.mark.parametrize(("name", "args", "read"), [
    ("manage_documents", {"action": "list", "search": "trip"}, True),
    ("manage_documents", {"action": "read", "document_id": "d1"}, True),
    ("manage_documents", {"action": "delete", "document_id": "d1"}, False),
    ("manage_documents", {"action": "tidy"}, False),
    ("create_document", {"title": "T", "content": "x"}, False),
    (SEARCH_DOCS, {"query": "insurance"}, True),
])
def test_document_reads_run_and_writes_ask(name, args, read):
    assert toolset.is_read(name, json.dumps(args)) is read


async def test_a_listing_runs_without_asking_and_a_delete_asks(brain):
    sess = brain.session()
    brain.llm.rounds([calls(("manage_documents", {"action": "list", "search": "trip"}))],
                     [text("You have one.")])
    events = await brain.run(sess, "which documents do I have about the trip?")
    assert [r["tool"] for r in brain.ran] == ["manage_documents"] and not of(events, "ask_user")
    brain.llm.rounds([calls(("manage_documents", {"action": "delete", "document_id": "d1"}))])
    events = await brain.run(sess, "delete it")
    assert [r["tool"] for r in brain.ran] == ["manage_documents"]  # nothing more ran
    assert of(events, "ask_user")[0]["data"]["action"]["tool"] == "manage_documents"


async def test_a_delete_without_a_document_id_is_refused_not_asked(brain):
    """Upstream's delete falls back to the most recent document; the owner's approval
    card must name the one it deletes."""
    sess = brain.session()
    brain.llm.rounds([calls(("manage_documents", {"action": "delete"}))], [text("Which one?")])
    events = await brain.run(sess, "delete my document")
    assert brain.ran == [] and not of(events, "ask_user")
    assert "document_id" in of(events, "tool_output")[0]["output"]


def test_the_schema_budget_stays_small():
    """Upstream's two document schemas are offered with short descriptions (their
    parameters stay upstream's); the whole group costs under 650 tokens (most of it upstream's language list)."""
    schemas = {s["function"]["name"]: s for s in brain_documents.schemas()}
    assert set(schemas) == {"manage_documents", "create_document", SEARCH_DOCS}
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    upstream = {s["function"]["name"]: s for s in FUNCTION_TOOL_SCHEMAS}
    for name in ("manage_documents", "create_document"):
        assert schemas[name]["function"]["parameters"] == upstream[name]["function"]["parameters"]
    assert len(json.dumps(list(schemas.values()))) // 4 < 650


# ------------------------------------------------------------------ create_document
def _real_documents(brain):
    """Upstream's real runner on a real (temp file) database, the chat owned by alice."""
    import core.database as cdb
    import src.database as sdb
    from tests.fork.brain_fakes import notes_db

    factory = notes_db(brain.mp)
    brain.mp.setattr(sdb, "SessionLocal", factory)
    brain.mp.setattr("src.tool_execution.execute_tool_block", brain.real_execute)
    brain.mp.setattr("src.tool_execution.owner_is_admin_or_single_user",
                     lambda owner: owner in ("alice",))
    with factory() as db:
        db.add(cdb.Session(id="chat-1", owner="alice", name="t", model="m",
                           endpoint_url="http://127.0.0.1:8082/v1"))
        db.commit()
    return factory


async def test_an_approved_create_document_creates_the_owners_document_and_opens_it(brain):
    import core.database as cdb

    factory = _real_documents(brain)
    sess = brain.session()
    brain.llm.rounds([calls(("create_document", {
        "title": "Packing list", "language": "markdown",
        "content": "# Packing list\n- tent\n- stove"}))])
    events = await brain.run(sess, "write me a packing list document")
    card = of(events, "ask_user")[0]["data"]
    assert card["action"]["tool"] == "create_document"
    with factory() as db:
        assert db.query(cdb.Document).count() == 0  # nothing ran on the model's word
    brain.llm.rounds([text("It's open in the editor.")])
    resumed = await brain.resume(card["approval_id"], sess, decision="approve")
    with factory() as db:
        docs = [(d.title, d.owner, d.language, d.current_content, d.session_id)
                for d in db.query(cdb.Document).all()]
    assert docs == [("Packing list", "alice", "markdown", "# Packing list\n- tent\n- stove",
                     "chat-1")]
    kinds = [e.get("type") for e in resumed]
    # upstream's order: the editor stream first, then the saved document, then the card
    assert kinds.index("doc_stream_open") < kinds.index("doc_stream_delta") \
        < kinds.index("doc_update") < kinds.index("tool_output")
    opened = of(resumed, "doc_stream_open")[0]
    assert (opened["title"], opened["language"]) == ("Packing list", "markdown")
    update = of(resumed, "doc_update")[0]
    out = of(resumed, "tool_output")[0]
    assert update["doc_id"] == out["doc_id"] and update["content"].startswith("# Packing")
    assert out["document_title"] == "Packing list" and out["document_action"] == "create"
    # the model is told what was created, not "(no output)"
    tool_msg = [m for m in brain.llm.requests[-1]["messages"] if m.get("role") == "tool"][-1]
    assert "Packing list" in tool_msg["content"] and update["doc_id"] in tool_msg["content"]


async def test_a_failed_create_sends_no_editor_events(brain):
    sess = brain.session()
    brain.results["create_document"] = {"error": "Cannot create document", "exit_code": 1}
    brain.llm.rounds([calls(("create_document", {"title": "T", "content": "x"}))])
    events = await brain.run(sess, "write a doc")
    approval_id = of(events, "ask_user")[0]["data"]["approval_id"]
    brain.llm.rounds([text("That failed.")])
    resumed = await brain.resume(approval_id, sess, decision="approve")
    assert not of(resumed, "doc_stream_open") and not of(resumed, "doc_update")


def test_native_create_arguments_become_upstreams_line_format():
    content = brain_documents.call_content(
        "create_document", json.dumps({"title": "Plan", "language": "markdown",
                                       "content": "Day 1"}))
    assert content == "Plan\nmarkdown\nDay 1"
    # other tools and text-format calls are left as they are
    assert brain_documents.call_content("manage_notes", '{"action": "list"}') == '{"action": "list"}'
    assert brain_documents.call_content("create_document", "Plan\nDay 1") == "Plan\nDay 1"


# ---------------------------------------------------------------- search_documents
async def test_search_documents_is_owner_scoped_and_above_the_threshold(brain):
    rag = FakeRag(LIBRARY, leaky=True)
    sess = brain.session()
    brain.llm.rounds([calls((SEARCH_DOCS, {"query": "boat insurance"}))], [text("KX-4471.")])
    await brain.run(sess, "what's my boat insurance policy number?", request=rag_request(rag))
    assert rag.asked == [{"query": "boat insurance", "k": 5, "owner": "alice"}]
    tool_msg = [m for m in brain.llm.requests[1]["messages"] if m.get("role") == "tool"][-1]
    body = tool_msg["content"]
    assert "boat-insurance.pdf" in body and "KX-4471" in body
    assert "bob" not in body.casefold() and "lentil" not in body.casefold()
    assert brain.ran == []  # runs in fork code, not upstream's runner


async def test_another_owners_library_never_comes_back(brain):
    rag = FakeRag(LIBRARY, leaky=True)
    sess = brain.session(owner="bob", sid="chat-b")
    brain.llm.rounds([calls((SEARCH_DOCS, {"query": "insurance policy"}))], [text("ok")])
    await brain.run(sess, "find my insurance", request=rag_request(rag, "bob"),
                    owner="bob")
    tool_msg = [m for m in brain.llm.requests[1]["messages"] if m.get("role") == "tool"][-1]
    assert "KX-4471" not in tool_msg["content"] and "Petrel" not in tool_msg["content"]


async def test_results_are_trimmed(brain):
    long = "word " * 5000
    rag = FakeRag([("alice", "long.txt", long, 0.9)] * 5)
    sess = brain.session()
    brain.llm.rounds([calls((SEARCH_DOCS, {"query": "word", "limit": 50}))], [text("ok")])
    await brain.run(sess, "search my documents for word", request=rag_request(rag))
    assert rag.asked[0]["k"] == brain_documents.MAX_RESULTS
    tool_msg = [m for m in brain.llm.requests[1]["messages"] if m.get("role") == "tool"][-1]
    assert len(tool_msg["content"]) <= brain_tools.PROMPT_RESULT_CHARS + 100


async def test_library_results_arm_the_gate_and_are_replayed_untrusted(brain):
    rag = FakeRag(LIBRARY)
    sess = brain.session()
    brain.llm.rounds([calls((SEARCH_DOCS, {"query": "boat"}))], [text("KX-4471.")])
    t = brain.start(sess, "boat policy?", request=rag_request(rag))
    [_ async for _ in brain_turn._run_new_turn(t)]
    assert t.run.tainted is True and t.run.security.external_untrusted_context_seen is True
    assert SEARCH_DOCS in t.run.security.external_sources
    brain.llm.rounds([text("Until March.")])
    t2 = brain.start(sess, "until when?", request=rag_request(rag))
    [_ async for _ in brain_turn._run_new_turn(t2)]
    replayed = [m for m in brain.llm.requests[-1]["messages"] if m.get("role") == "tool"]
    assert replayed and all(m.get("metadata", {}).get("tool_gate_untrusted") for m in replayed)
    assert replayed[0]["metadata"]["source"] == SEARCH_DOCS
    assert t2.run.security.external_untrusted_context_seen is True


async def test_an_empty_query_or_no_hit_says_so(brain):
    rag = FakeRag(LIBRARY)
    sess = brain.session()
    brain.llm.rounds([calls((SEARCH_DOCS, {"query": "  "}), (SEARCH_DOCS, {"query": "zebra"}))],
                     [text("Nothing.")])
    t = brain.start(sess, "zebra?", request=rag_request(FakeRag([])))
    [_ async for _ in brain_turn._run_new_turn(t)]
    msgs = [m["content"] for m in brain.llm.requests[1]["messages"] if m.get("role") == "tool"]
    assert "query" in msgs[0].casefold() and "no matching" in msgs[1].casefold()
    assert rag.asked == []
    assert t.run.tainted is False  # nothing came back: nothing to distrust


# ---------------------------------------------------------------------- who gets it
async def test_offered_with_a_library_for_an_admin(brain):
    sess = brain.session()
    brain.llm.rounds([text("ok")])
    await brain.run(sess, "hello", request=rag_request(FakeRag(LIBRARY)))
    assert {"manage_documents", "create_document", SEARCH_DOCS} <= set(offered(brain))


async def test_not_offered_without_a_rag_manager(brain):
    sess = brain.session()
    brain.llm.rounds([text("ok")], [text("ok")])
    await brain.run(sess, "hello")  # no personal_docs_manager at all
    request = rag_request(None)
    await brain.run(sess, "hello", request=request)
    assert SEARCH_DOCS not in offered(brain, 0) and SEARCH_DOCS not in offered(brain, 1)
    assert "manage_documents" in offered(brain, 0)  # the editor documents stay


@pytest.mark.parametrize("case", ["privilege", "token", "non_admin", "global", "rag_off"])
def test_search_documents_follows_upstreams_document_policy(monkeypatch, case):
    """Where upstream would refuse manage_documents (the document privilege, an API
    token, a non-admin user, the admin's global list), or the chat's documents toggle
    is off, search_documents isn't offered either."""
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user",
                        lambda o: case != "non_admin")
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: (
        ["manage_documents"] if case == "global" and key == "disabled_tools" else default))
    off = brain_tools.turn_disabled_tools(
        None, "alice", privileges={"can_use_documents": case != "privilege"},
        delegated=case == "token", rag=FakeRag(LIBRARY), use_rag=case != "rag_off")
    assert SEARCH_DOCS in off


def test_search_documents_stays_for_an_admin_with_a_library(monkeypatch):
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda o: True)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    assert SEARCH_DOCS not in brain_tools.turn_disabled_tools(None, "alice",
                                                              rag=FakeRag(LIBRARY))
    assert SEARCH_DOCS in brain_tools.turn_disabled_tools(None, "alice", rag=None)


async def test_the_documents_toggle_off_drops_the_library_for_that_turn(brain):
    sess = brain.session()
    brain.llm.rounds([text("ok")])
    t = brain_turn._start_turn(rag_request(FakeRag(LIBRARY)), sess=sess, session_id=sess.id,
                               message="hello", owner="alice", preset_id=None,
                               deps=brain_turn._Deps(), use_rag="false")
    [_ async for _ in brain_turn._run_new_turn(t)]
    assert SEARCH_DOCS not in offered(brain) and "manage_documents" in offered(brain)


async def test_a_token_turn_gets_no_document_tools_but_may_create(brain):
    sess = brain.session()
    brain.mp.setattr("src.auth_helpers.is_delegated_credential", lambda request: True)
    brain.llm.rounds([text("ok")])
    await brain.run(sess, "hello", request=rag_request(FakeRag(LIBRARY)))
    names = offered(brain)
    assert "manage_documents" not in names and SEARCH_DOCS not in names
    assert "create_document" in names  # upstream lets a token create (it still asks)


# --------------------------------------------------------------------- the prompt
def test_the_prompt_says_when_to_search_documents_and_to_cite_them():
    p = brain_prompt.SYSTEM_PROMPT
    assert "search_documents" in p and "manage_documents" in p and "create_document" in p
    assert "title or filename" in p
