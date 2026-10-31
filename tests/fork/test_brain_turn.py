"""Fork: the brain's loop (fork/brain/turn.py): prompt layout, streaming, tool rounds,
approvals that pause and resume, time-outs, local-only endpoints, follow-ups and the
context budget. Fake model and tools (tests/fork/brain_fakes.py); fictional data."""
from __future__ import annotations

import json
import time

import pytest

from fork.brain import history as brain_history
from fork.brain import prompt as brain_prompt
from fork.brain import turn as brain_turn
from tests.fork.brain_fakes import Brain, calls, of, text

SEARCH = "mcp__convo__search_transcripts"
UPDATE = "mcp__convo__update_action_item"


@pytest.fixture
def brain(monkeypatch, data_dir):
    return Brain(monkeypatch, data_dir)


def _roles(messages):
    return [m["role"] for m in messages]


async def test_prompt_start_is_identical_across_turns_and_rounds_only_append(brain):
    sess = brain.session()
    brain.llm.rounds([calls((SEARCH, {"query": "budget"}))], [text("Alice approved it.")])
    await brain.run(sess, "What did Alice say about the budget?")
    brain.llm.rounds([text("Bob was there too.")])
    await brain.run(sess, "Who else was there?")
    first, second, third = brain.llm.requests
    # rounds of a turn only append
    assert second["messages"][:len(first["messages"])] == first["messages"]
    # the next turn starts with the same system message and tool list
    assert third["messages"][0] == first["messages"][0]
    assert third["tools"] == first["tools"]
    # one system message, first; the context message is a user message before the question
    for req in (first, second, third):
        assert [m["role"] for m in req["messages"]].count("system") == 1
        assert req["messages"][0]["role"] == "system"
    ctx_index = len(first["messages"]) - 2
    assert first["messages"][ctx_index]["role"] == "user"
    assert "not part of your instructions" in first["messages"][ctx_index]["content"]
    # everything before turn 2's context message is turn 1's stable prompt plus its history
    turn2_ctx = len(third["messages"]) - 2
    assert third["messages"][:ctx_index] == first["messages"][:ctx_index]
    assert "Today is" not in third["messages"][0]["content"]
    assert third["messages"][turn2_ctx]["content"].startswith("[Context")


async def test_system_prompt_carries_the_preset_text(brain):
    sess = brain.session()
    handler = type("H", (), {"validate_and_extract_preset": lambda self, pid: (
        0.3, 512, "Always answer in haiku.", None, None, "general")})()
    brain.llm.rounds([text("ok")])
    await brain.run(sess, "hello", preset_id="custom", deps=brain_turn._Deps(chat_handler=handler))
    req = brain.llm.requests[0]
    assert req["messages"][0]["content"].endswith("Always answer in haiku.")
    assert req["temperature"] == 0.3 and req["max_tokens"] == 512


async def test_text_reaches_the_stream_before_the_turn_ends(brain):
    sess = brain.session()
    state = {"model_done": False, "seen_before_done": None}

    async def slow_model(url, model, messages, **kw):
        yield text("First words")
        yield text(" and more")
        state["model_done"] = True
        yield "data: [DONE]\n\n"

    brain.mp.setattr("src.llm_core.stream_llm", slow_model)
    t = brain.start(sess, "Tell me something")
    async for chunk in brain_turn._run_new_turn(t):
        if '"delta"' in chunk and state["seen_before_done"] is None:
            state["seen_before_done"] = not state["model_done"]
    assert state["seen_before_done"] is True


def test_thinking_is_off_on_brain_requests_whatever_the_model_name():
    from src.llm_core import _apply_local_qwen_thinking_mode as apply

    local = "http://127.0.0.1:8082/v1/chat/completions"
    token = brain_turn._THINKING_OFF.set(True)
    try:
        payload, hosted = {}, {}
        apply(payload, local, "any-alias", "off")
        apply(hosted, "https://api.example.com/v1/chat/completions", "any-alias", "off")
    finally:
        brain_turn._THINKING_OFF.reset(token)
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert hosted == {}  # hosted providers never get the field


def test_a_non_brain_request_is_built_as_before():
    from src.llm_core import _apply_local_qwen_thinking_mode as apply

    local = "http://127.0.0.1:8082/v1/chat/completions"
    unknown, qwen = {}, {}
    apply(unknown, local, "any-alias", "off")
    apply(qwen, local, "qwen3.5-35b", "on")
    assert unknown == {}  # an unrecognised name gets nothing outside a brain turn
    assert qwen == {"chat_template_kwargs": {"enable_thinking": True}}
    assert brain_turn.apply_brain_thinking({}) is False
    payload = {"x": 1}
    brain_turn.apply_brain_thinking(payload)
    assert payload == {"x": 1}


async def test_stream_llm_gets_the_session_and_thinking_off(brain):
    sess = brain.session()
    brain.llm.rounds([text("hi")])
    await brain.run(sess, "hello")
    req = brain.llm.requests[0]
    assert req["session_id"] == sess.id and req["thinking_mode"] == "off"
    assert req["url"] == sess.endpoint_url


async def test_a_non_local_endpoint_is_refused(brain):
    sess = brain.session(url="https://api.openai.example/v1")
    events = await brain.run(sess, "hello")
    assert brain.llm.requests == []
    assert of(events, "final_response")[0]["content"] == brain_turn.LOCAL_ONLY
    assert sess.history[-1].content == brain_turn.LOCAL_ONLY


async def test_reads_run_and_events_have_the_shapes_the_window_and_bakeoff_read(brain):
    sess = brain.session()
    brain.llm.rounds([calls((SEARCH, {"query": "budget"}), ("web_search", {"query": "x"}))],
                     [text("Answer.")])
    events = await brain.run(sess, "What did Alice say about the budget?")
    outs = of(events, "tool_output")
    assert [o["tool"] for o in outs] == [SEARCH, "web_search"]
    assert all(o["execution_attempted"] is True and o["blocked"] is False for o in outs)
    assert [s["tool"] for s in of(events, "tool_start")] == [SEARCH, "web_search"]
    assert of(events, "metrics")[0]["data"]["tool_calls"] == 2
    assert of(events, "message_saved")[0]["id"] == sess.history[-1].metadata["_db_id"]
    assert events[-1] == {"type": "[DONE]"}
    assert sess.history[-1].content == "Answer."
    assert [m.role for m in sess.history] == ["user", "assistant"]
    assert brain.post, "upstream's after-turn jobs run"


async def test_every_call_goes_through_execute_tool_block_with_a_request_authority(brain):
    from src.agent_runtime.authority import RequestAuthority
    from src.tool_capabilities import ToolRunSecurityContext

    sess = brain.session()
    brain.llm.rounds([calls((SEARCH, {"query": "q"}), ("manage_notes", {"action": "search",
                                                                         "query": "trip"}))],
                     [text("ok")])
    await brain.run(sess, "my notes about the trip and what Alice said")
    assert [r["tool"] for r in brain.ran] == [SEARCH, "manage_notes"]
    for r in brain.ran:
        assert isinstance(r["request_authority"], RequestAuthority)
        assert r["request_authority"].bound_to(owner="alice", session_id=sess.id)
        assert isinstance(r["security_context"], ToolRunSecurityContext)
        assert r["owner"] == "alice" and r["session_id"] == sess.id
        assert r["exact_approval"] is None


async def test_a_tool_outside_the_set_is_refused_without_running(brain):
    sess = brain.session()
    brain.llm.rounds([calls(("bash", {"command": "ls"}))], [text("I can't.")])
    events = await brain.run(sess, "list files")
    assert brain.ran == []
    out = of(events, "tool_output")[0]
    assert out["blocked"] is True and out["execution_attempted"] is False


async def test_text_format_tool_calls_are_parsed(brain):
    sess = brain.session()
    brain.llm.rounds([text("<tool_call>\n<function=web_search>\n<parameter=query>\nlisbon "
                           "weather\n</parameter>\n</function>\n</tool_call>")],
                     [text("Sunny.")])
    await brain.run(sess, "weather in lisbon?")
    assert brain.ran[0]["tool"] == "web_search"
    assert json.loads(brain.ran[0]["content"]) == {"query": "lisbon weather"}
    assert sess.history[-1].content == "Sunny."


async def test_max_rounds_then_one_final_round_without_tools(brain):
    brain.mp.setenv("FORK_BRAIN_MAX_ROUNDS", "2")
    sess = brain.session()
    brain.llm.rounds([calls(("web_search", {"query": "a"}))],
                     [calls(("web_search", {"query": "b"}))],
                     [text("Best answer.")])
    await brain.run(sess, "search a lot")
    assert len(brain.llm.requests) == 3
    last = brain.llm.requests[-1]
    assert "tools" not in last and last["tool_choice_none"] is True
    assert last["messages"][-1] == {"role": "user", "content": brain_turn.FINAL_ROUND_NOTE}
    assert sess.history[-1].content == "Best answer."


async def test_writes_ask_and_an_approved_write_resumes_without_a_second_model_call(brain):
    sess = brain.session()
    brain.llm.rounds([text("Marking it done."), calls((UPDATE, {"id": 7}))])
    events = await brain.run(sess, "mark action item 7 done")
    asks = of(events, "ask_user")
    assert len(asks) == 1 and asks[0]["data"]["kind"] == "tool_approval"
    approval_id = asks[0]["data"]["approval_id"]
    out = of(events, "tool_output")[0]
    assert out["execution_attempted"] is False and out["ask_user"]["approval_id"] == approval_id
    assert brain.ran == []  # nothing ran on the model's word
    assert brain_turn.pending_count() == 1
    paused = sess.history[-1]
    assert paused.metadata[brain_prompt.PAUSED_FLAG] is True
    assert paused.metadata["tool_events"][-1]["ask_user"]["approval_id"] == approval_id
    assert len(brain.llm.requests) == 1

    brain.llm.rounds([text("Done, item 7 is closed.")])
    resumed = await brain.resume(approval_id, sess)
    # the approved call ran with upstream's exact approval, then ONE new round
    assert [r["tool"] for r in brain.ran] == [UPDATE]
    assert brain.ran[0]["exact_approval"].pending.approval_id == approval_id
    assert len(brain.llm.requests) == 2
    follow = brain.llm.requests[1]["messages"]
    assert follow[:len(brain.llm.requests[0]["messages"])] == brain.llm.requests[0]["messages"]
    assert follow[-2]["tool_calls"][0]["function"]["name"] == UPDATE
    assert follow[-1]["role"] == "tool"
    out = of(resumed, "tool_output")[0]
    assert out["approved"] is True and out["execution_attempted"] is True
    assert sess.history[-1].content == "Done, item 7 is closed."
    assert brain_turn.pending_count() == 0


async def test_upstream_gate_and_write_rule_build_a_real_exact_approval(brain):
    from src.tool_approvals import tool_approval_store

    sess = brain.session()
    brain.llm.rounds([calls(("manage_calendar", {"action": "create_event", "summary": "Lunch"}))])
    events = await brain.run(sess, "put lunch on my calendar")
    approval_id = of(events, "ask_user")[0]["data"]["approval_id"]
    pending = tool_approval_store.peek(approval_id)
    assert pending.owner == "alice" and pending.session_id == sess.id
    assert pending.tool_name == "manage_calendar"
    assert pending.continuation_query == "put lunch on my calendar"


async def test_timeout_ends_the_turn(brain):
    from src.tool_approvals import tool_approval_store

    sess = brain.session()
    brain.llm.rounds([calls((UPDATE, {"id": 7}))])
    events = await brain.run(sess, "close item 7")
    approval_id = of(events, "ask_user")[0]["data"]["approval_id"]
    assert brain_turn.expire_pending(now=time.time() + 5) == 0
    assert brain_turn.expire_pending(now=time.time() + brain_turn.PENDING_SECONDS + 1) == 1
    assert brain_turn.pending_count() == 0
    assert sess.history[-1].content == brain_turn.NOT_APPROVED
    assert tool_approval_store.peek(approval_id) is None  # a late answer can't run it
    assert brain.ran == []


async def test_deny_drops_the_paused_turn_quietly(brain):
    from src.tool_approvals import tool_approval_store

    sess = brain.session()
    brain.llm.rounds([calls((UPDATE, {"id": 7}))])
    events = await brain.run(sess, "close item 7")
    approval_id = of(events, "ask_user")[0]["data"]["approval_id"]
    tool_approval_store.consume(approval_id, decision="deny", owner="alice", session_id=sess.id)
    before = len(sess.history)
    assert brain_turn.expire_pending() == 0
    assert brain_turn.pending_count() == 0 and len(sess.history) == before


async def test_one_pending_approval_per_chat(brain):
    sess = brain.session()
    brain.llm.rounds([calls((UPDATE, {"id": 7}))])
    await brain.run(sess, "close item 7")
    assert brain_turn.pending_count() == 1
    brain_turn.drop_pending_for_chat(owner="alice", session_id=sess.id)
    assert brain_turn.pending_count() == 0


async def test_another_owner_cannot_resume_a_paused_turn(brain):
    sess = brain.session()
    brain.llm.rounds([calls((UPDATE, {"id": 7}))])
    events = await brain.run(sess, "close item 7")
    approval_id = of(events, "ask_user")[0]["data"]["approval_id"]
    assert brain_turn.take_pending(approval_id, owner="mallory", session_id=sess.id) is None
    assert brain_turn.take_pending(approval_id, owner="alice", session_id="other") is None
    assert brain_turn.pending_count() == 1


async def test_follow_ups_see_the_earlier_turns_tool_results(brain):
    sess = brain.session()
    brain.mp.setitem(brain.results, SEARCH, {"output": "Conversation 12: Alice, budget "
                                             "approved (link)", "exit_code": 0})
    brain.llm.rounds([calls((SEARCH, {"query": "budget"}))], [text("Alice approved it.")])
    await brain.run(sess, "What did Alice say about the budget?")
    brain.llm.rounds([text("Conversation 12.")])
    await brain.run(sess, "Which conversation was that?")
    msgs = brain.llm.requests[-1]["messages"]
    roles = _roles(msgs)
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user", "user"]
    assert "Conversation 12: Alice" in msgs[3]["content"]
    assert msgs[2]["tool_calls"][0]["function"]["name"] == SEARCH
    assert msgs[4]["content"] == "Alice approved it."


async def test_tool_results_are_trimmed_in_the_prompt_but_whole_in_the_window(brain):
    sess = brain.session()
    page = "line of a long page\n" * 2000
    brain.results["web_fetch"] = {"output": page, "exit_code": 0}
    brain.llm.rounds([calls(("web_fetch", {"url": "https://example.org/a"}))], [text("ok")])
    events = await brain.run(sess, "read https://example.org/a")
    in_prompt = brain.llm.requests[1]["messages"][-1]["content"]
    assert len(in_prompt) < 6200 and "more characters not shown" in in_prompt
    assert len(of(events, "tool_output")[0]["output"]) > 6200


async def test_context_over_budget_drops_old_tool_results_first(brain):
    sess = brain.session()
    big = "word " * 3000  # ~3.8k tokens
    brain.results[SEARCH] = {"output": big, "exit_code": 0}
    brain.llm.rounds([calls((SEARCH, {"query": "a"}))], [text("First answer.")])
    await brain.run(sess, "first question")
    # a window whose budget fits the prompt only without the old result
    req = brain.llm.requests[1]
    full = brain_turn._estimate(req["messages"], tuple(req["tools"]))
    brain.window = int((full - 200) / brain_turn.CONTEXT_BUDGET)
    brain.llm.rounds([text("Second answer.")])
    await brain.run(sess, "second question")
    msgs = brain.llm.requests[-1]["messages"]
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == brain_turn.DROPPED_RESULT
    assert [m["content"] for m in msgs if m["role"] == "user"][0] == "first question"
    assert len(brain.llm.requests) == 3  # no summary call was needed


def test_drop_old_tool_results_keeps_the_newest():
    msgs = [{"role": "system", "content": "s"},
            {"role": "tool", "content": "a " * 4000},
            {"role": "tool", "content": "b " * 4000},
            {"role": "tool", "content": "c " * 4000}]
    assert brain_turn.drop_old_tool_results(msgs, 2500, turn_start=1) is True
    assert msgs[1]["content"] == brain_turn.DROPPED_RESULT
    assert msgs[2]["content"] == brain_turn.DROPPED_RESULT
    assert msgs[3]["content"].startswith("c ")


async def test_still_over_budget_summarises_older_turns_once(brain):
    sess = brain.session()
    long = "talk " * 1500
    for i in range(3):
        brain.llm.rounds([text(f"Answer {i}: {long}")])
        await brain.run(sess, f"question {i} {long}")
    brain.window = 4000
    brain.llm.rounds([text("A short summary of earlier turns.")], [text("Fourth answer.")])
    await brain.run(sess, "question 3")
    summary_req, answer_req = brain.llm.requests[-2:]
    assert summary_req["messages"][0]["content"] == brain_turn.SUMMARY_PROMPT
    assert "tools" not in summary_req
    stored = brain_history.summary(sess.id)
    assert stored and stored[1] == "A short summary of earlier turns."
    assert "summary of the earlier part" in answer_req["messages"][1]["content"]
    assert answer_req["messages"][0]["role"] == "system"


async def test_web_search_queries_go_through_the_query_guard(brain):
    seen = []

    def guard(query, owner):
        seen.append((query, owner))
        return ("lisbon weather", 1)

    brain.mp.setattr("fork.brain.query_guard.guard_query", guard)
    sess = brain.session()
    brain.llm.rounds([calls(("web_search", {"query": "lisbon weather Wren Hale meeting"}))],
                     [text("Sunny.")])
    await brain.run(sess, "will it rain during my meeting with Wren?")
    assert seen == [("lisbon weather Wren Hale meeting", "alice")]
    assert json.loads(brain.ran[0]["content"])["query"] == "lisbon weather"


async def test_an_empty_guarded_query_is_not_searched_and_the_model_is_told(brain):
    brain.mp.setattr("fork.brain.query_guard.guard_query", lambda q, o: ("", 3))
    sess = brain.session()
    brain.llm.rounds([calls(("web_search", {"query": "Wren Hale"}))], [text("I won't search names.")])
    events = await brain.run(sess, "search Wren Hale")
    assert brain.ran == []
    out = of(events, "tool_output")[0]
    assert out["blocked"] is True and out["execution_attempted"] is False
    told = brain.llm.requests[1]["messages"][-1]
    assert told["role"] == "tool" and "search was not run" in told["content"]


async def test_a_model_error_is_passed_on_and_the_stream_ends(brain):
    sess = brain.session()
    brain.llm.script.append(['event: error\ndata: {"error": "boom", "status": 503}\n\n'])
    events = await brain.run(sess, "hello")
    assert events[-2]["type"] == "raw" and "boom" in events[-2]["chunk"]
    assert events[-1] == {"type": "[DONE]"}


# ---------------------------------------------------- upstream's first-event watchdog
class _HttpResponse:
    status_code = 200

    def __init__(self, lines, hang):
        self.lines, self.hang = lines, hang

    async def aiter_lines(self):
        if self.hang:
            import asyncio

            await asyncio.sleep(5)
        for line in self.lines:
            yield line

    async def aread(self):
        return b""


class _HttpClient:
    """``src.llm_core._get_http_client()`` faked: each request answers the next script
    entry, ``None`` for a server that accepts the request and then sends nothing."""

    is_closed = False

    def __init__(self, script):
        self.script, self.payloads = list(script), []

    def stream(self, method, url, json=None, headers=None, timeout=None):
        from contextlib import asynccontextmanager

        self.payloads.append(json)
        lines = self.script.pop(0)

        @asynccontextmanager
        async def cm():
            yield _HttpResponse(lines or [], hang=lines is None)
        return cm()


def _sse_line(delta=None, finish=None):
    choice = {"delta": delta or {}}
    if finish:
        choice["finish_reason"] = finish
    return "data: " + json.dumps({"choices": [choice]})


TOOL_ROUND = [
    _sse_line({"tool_calls": [{"index": 0, "id": "c1", "function": {
        "name": "web_search", "arguments": '{"query": "lisbon weather"}'}}]}),
    _sse_line(finish="tool_calls"), "data: [DONE]"]
TEXT_ROUND = [_sse_line({"content": "Sunny today."}), _sse_line(finish="stop"), "data: [DONE]"]


async def test_a_watchdog_retry_runs_no_tool_twice_and_repeats_no_text(brain):
    """A local server silent past the first-event budget: upstream's stream_llm drops
    the request and retries once before it has passed anything on, so the brain sees one
    clean round (the real stream_llm, the HTTP client faked)."""
    brain.mp.setenv("ODYSSEUS_FIRST_TOKEN_TIMEOUT", "0.05")
    brain.mp.setattr("src.llm_core.stream_llm", brain.real_stream_llm)
    client = _HttpClient([None, TOOL_ROUND, TEXT_ROUND])
    brain.mp.setattr("src.llm_core._get_http_client", lambda: client)
    sess = brain.session()
    events = await brain.run(sess, "weather in lisbon?")
    assert len(client.payloads) == 3  # silent, retried, then the answer round
    assert [r["tool"] for r in brain.ran] == ["web_search"]
    assert [e["delta"] for e in events if "delta" in e] == ["Sunny today."]
    assert sess.history[-1].content == "Sunny today."
    # and the real requests had thinking off for an unrecognised model name
    assert brain.session().model == "local-model"
    assert all(p["chat_template_kwargs"] == {"enable_thinking": False}
               for p in client.payloads)
    assert client.payloads[0]["tools"]


async def test_a_server_silent_twice_ends_the_turn_with_the_error(brain):
    brain.mp.setenv("ODYSSEUS_FIRST_TOKEN_TIMEOUT", "0.05")
    brain.mp.setattr("src.llm_core.stream_llm", brain.real_stream_llm)
    client = _HttpClient([None, None])
    brain.mp.setattr("src.llm_core._get_http_client", lambda: client)
    sess = brain.session()
    events = await brain.run(sess, "weather in lisbon?")
    assert brain.ran == []
    assert any(e["type"] == "raw" and "504" in e["chunk"] for e in events)
    assert events[-1] == {"type": "[DONE]"}
