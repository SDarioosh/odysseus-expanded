"""Fork: the brain's fixes from a bug hunt (2026-10-31): fetches of made-up paths, the
query guard's gaps, local host names, the final round, broken tool calls, Forget across
line breaks and a cached tool set after an admin's demotion. Fictional data only."""
from __future__ import annotations

import pytest

from fork.brain import query_guard as qg
from fork.brain import turn as brain_turn
from tests.fork.brain_fakes import Brain, answer_text, calls, of, search_output, text


@pytest.fixture
def brain(monkeypatch, data_dir):
    return Brain(monkeypatch, data_dir)


# ------------------------------------------------------------------ web_fetch
async def test_a_made_up_path_on_a_search_hits_site_asks(brain):
    sess = brain.session()
    brain.results["web_search"] = {
        "output": search_output("Gardening tips", "https://garden.example/tips"), "exit_code": 0}
    brain.llm.rounds(
        [calls(("web_search", {"query": "gardening tips"}))],
        [calls(("web_fetch", {"url": "https://garden.example/c/dentist-tuesday-wren-hale"}))])
    events = await brain.run(sess, "give me gardening tips")
    assert [r["tool"] for r in brain.ran] == ["web_search"]
    assert of(events, "ask_user")


async def test_the_search_hit_itself_still_runs_without_asking(brain):
    sess = brain.session()
    brain.results["web_search"] = {
        "output": search_output("Gardening tips", "https://garden.example/tips"), "exit_code": 0}
    brain.llm.rounds(
        [calls(("web_search", {"query": "gardening tips"}))],
        [calls(("web_fetch", {"url": "https://www.garden.example/tips/"}))],
        [text("Water in the morning.")])
    events = await brain.run(sess, "give me gardening tips")
    assert [r["tool"] for r in brain.ran] == ["web_search", "web_fetch"]
    assert not of(events, "ask_user")


# ---------------------------------------------------------------- query guard
NAMES = ["Wren Hale", "José García", "Jordan Vale", "Mo Ali", "Dörte Müller"]
TITLES = ["Chemo appointment Dr Okafor", "Lisbon offsite"]
PHONES = ["15550199123"]
STREETS = ["12 Elm Street"]


@pytest.fixture
def terms(monkeypatch):
    monkeypatch.setattr(qg, "_private_terms", lambda owner: (NAMES, TITLES))
    monkeypatch.setattr(qg, "_contact_details", lambda owner: (PHONES, STREETS))


@pytest.mark.parametrize("query, want", [
    ("Jose Garcia lawyer", "lawyer"),
    ("JOSÉ GARCÍA lawyer", "lawyer"),
    ("Doerte Mueller birthday", "birthday"),
    ("Wren_Hale birthday gift", "birthday gift"),
    ("Wren.Hale linkedin", "linkedin"),
    ("WrenHale instagram", "instagram"),
    ("Mo's surgery recovery time", "surgery recovery time"),
    ("Chemo appointment with Dr Okafor reviews", "with Dr reviews"),
    ("what to pack for the offsite in Lisbon", "what to pack for the in"),
    ("who owns 5550199123", "who owns"),
    ("who owns 555 019 9123", "who owns"),
    ("parking near 12 elm street", "parking near"),
    ("parking near Elm Street 12", "parking near"),
])
def test_guard_removes(terms, query, want):
    out, removed = qg.guard_query(query, "alice")
    assert removed > 0 and out == want, (query, out)


@pytest.mark.parametrize("query", [
    "Lisbon weather forecast today", "flights to Jordan in May", "Lisbon cheap eats",
    "Abbey Road 2019 remaster", "ISBN 9780140449136", "Route 66 diner", "AI news today",
    "Zürich train times", "Momo novel Michael Ende",
    "wren bird song", "Elm Street movie 1984",
])
def test_guard_keeps_ordinary_queries(terms, query):
    assert qg.guard_query(query, "alice") == (query, 0)


def test_contact_details_come_from_the_contacts(monkeypatch):
    rows = [{"name": "Wren Hale", "phones": ["+1 (555) 019-9123"],
             "address": "12 Elm Street, Springfield, 12345"}]
    monkeypatch.setattr("routes.contacts_routes._fetch_contacts", lambda force, owner: rows)
    phones, streets = qg._load_contact_details("alice")
    assert phones == ["15550199123"] and streets == ["12 Elm Street"]


def test_guard_cost_stays_small_with_many_contacts(monkeypatch):
    import time
    names = [f"Given{i} Family{i}" for i in range(500)]
    monkeypatch.setattr(qg, "_private_terms", lambda owner: (names, TITLES))
    monkeypatch.setattr(qg, "_contact_details", lambda owner: (PHONES * 50, STREETS * 50))
    qg.guard_query("warm up", "alice")
    start = time.perf_counter()
    for _ in range(20):
        qg.guard_query("best ramen near Lisbon open late tonight", "alice")
    assert (time.perf_counter() - start) / 20 < 0.25


# ------------------------------------------------------------- local endpoints
@pytest.mark.parametrize("url, local", [
    ("http://llama-server:8080/v1", True), ("http://gpu-box.local:8080/v1", True),
    ("http://server.tail1234.ts.net:8080/v1", True), ("http://[fd7a:115c:a1e0::1]:8080/v1", True),
    ("http://[fe80::1]:8080/v1", True), ("http://ollama:11434", True),
    ("http://router.lan:8080/v1", True),
    ("https://api.openai.com/v1", False), ("http://8.8.8.8/v1", False),
    ("http://[2001:4860:4860::8888]/v1", False), ("https://llm.example.com/v1", False),
    ("http://local.example.com/v1", False),
])
def test_private_host_names_count_as_local(monkeypatch, url, local):
    monkeypatch.setattr("fork.upstream.configured_endpoint_kind", lambda u: "auto")
    assert brain_turn.is_local(url) is local


def test_a_private_host_name_set_to_api_is_still_refused(monkeypatch):
    monkeypatch.setattr("fork.upstream.configured_endpoint_kind", lambda u: "api")
    assert brain_turn.is_local("http://llama-server:8080/v1") is False


# ------------------------------------------------------------------- the loop
async def test_calls_in_the_final_round_never_run(brain):
    brain.mp.setenv("FORK_BRAIN_MAX_ROUNDS", "1")
    sess = brain.session()
    brain.llm.rounds([calls(("web_search", {"query": "rust news"}))],
                     [text("Best I can say."), calls(("web_search", {"query": "more news"}))])
    await brain.run(sess, "loop")
    assert len(brain.llm.requests) == 2
    assert [r["content"] for r in brain.ran] == ['{"query": "rust news"}']
    assert sess.history[-1].content == "Best I can say."


@pytest.mark.parametrize("broken", [
    '<tool_call>{"name": "web_search", "arguments": {"query": "x"</tool_call>',
    '<tool_call>{"name": "send_email", "arguments": {"to": "a@b.example"}}</tool_call>',
])
async def test_a_broken_tool_call_is_tried_again_and_never_shown(brain, broken):
    sess = brain.session()
    brain.llm.rounds([text("Let me look. " + broken)], [text("Here it is.")])
    events = await brain.run(sess, "find it")
    assert len(brain.llm.requests) == 2
    assert brain.llm.requests[1]["messages"][-1]["content"] == brain_turn.BROKEN_CALL_NOTE
    shown = answer_text(events)
    assert "tool_call" not in shown and "Here it is." in shown
    assert "tool_call" not in sess.history[-1].content


@pytest.mark.parametrize("reply", [
    "In JavaScript, write <function name> like this: function f() {}. Done.",
    "Use the <parameter> element inside <invoke>. That is all.",
    "The tag [TOOL_CALL] is a marker. End.",
])
async def test_ordinary_text_with_brackets_is_an_answer(brain, reply):
    sess = brain.session()
    brain.llm.rounds([text(reply)])
    events = await brain.run(sess, "explain")
    assert len(brain.llm.requests) == 1
    assert answer_text(events) == reply and sess.history[-1].content == reply


async def test_a_demoted_admin_is_no_longer_offered_mcp_tools(brain):
    sess = brain.session()
    brain.llm.rounds([text("hi")], [text("ok")])
    await brain.run(sess, "hello")
    assert any(t["function"]["name"].startswith("mcp__")
               for t in brain.llm.requests[0]["tools"])
    brain.mp.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: False)
    await brain.run(sess, "search my transcripts")
    assert not any(t["function"]["name"].startswith("mcp__")
                   for t in brain.llm.requests[1].get("tools", []))


# -------------------------------------------------------------------- Forget
def test_forget_finds_a_fact_split_across_lines(data_dir):
    from fork.brain import history as h
    msgs = [{"role": "tool", "name": "mcp__convo__get_transcript", "tool_call_id": "c1",
             "content": "[00:12] Wren: I am moving to\nLisbon in May next year"}]
    h.save_turn("chat-9", "m1", msgs, owner="alice")
    assert h.forget_texts("alice", ["I am moving to Lisbon in May next year"]) == 1
    assert h.exchanges("chat-9") == {}
