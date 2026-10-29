"""An empty reply after module tools gets one answer round (src/mcp_trust.py).

The owner asked "/sound what did I speak about last night around 7pm?", the
model ran search_transcripts and get_conversation, then returned no text, and
the chat showed only "Done.".
"""
import json

import pytest

import src.agent_loop as al
from src import mcp_trust
from tests.test_agent_rounds_exhausted import _collect, _patch_common, _types
from tests.fork.test_mcp_trust import SEARCH, FakeManager


@pytest.fixture
def trusted(monkeypatch):
    m = FakeManager()
    monkeypatch.setattr(mcp_trust, "_manager", lambda: m)
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    return m


def test_helper_needs_a_successful_trusted_tool(trusted):
    ok = {"tool": SEARCH, "output": "[]", "exit_code": 0}
    assert mcp_trust.ran_trusted_server_tool([ok])
    assert mcp_trust.ran_trusted_server_tool([{"tool": "bash", "exit_code": 0}, ok])
    assert not mcp_trust.ran_trusted_server_tool([{**ok, "exit_code": 1}])
    assert not mcp_trust.ran_trusted_server_tool([{**ok, "error": "boom"}])
    assert not mcp_trust.ran_trusted_server_tool([{"tool": "mcp__srv2__search_web"}])
    assert not mcp_trust.ran_trusted_server_tool([])
    # Waiting on an approval card: nothing to answer yet.
    pending = {"tool": "mcp__srv1__name_speaker",
               "ask_user": {"kind": "tool_approval"}}
    assert not mcp_trust.ran_trusted_server_tool([ok, pending])


def test_helper_is_off_without_trusted_servers(trusted, monkeypatch):
    monkeypatch.delenv(mcp_trust.TRUSTED_READONLY_MCP_ENV)
    assert not mcp_trust.ran_trusted_server_tool([{"tool": SEARCH, "exit_code": 0}])


def _run_web_chat(monkeypatch, module_tool_ran):
    """A web-chat turn (no unattended context): one tool, then an empty reply.

    Pushing a real MCP call through the loop needs a connected server, its
    schemas and a sealed backend; the helper is tested above, so the loop
    test stubs its verdict and checks the hook.
    """
    _patch_common(monkeypatch)
    import src.llm_core as llm_core

    synthesis_calls = []
    rounds = []
    seen = []

    async def _no_compaction(*args, **kwargs):
        return args[3], 0, False

    async def _fake_stream(_candidates, messages, **kwargs):
        rounds.append(messages)
        if len(rounds) == 1:
            yield "data: " + json.dumps({
                "type": "tool_calls",
                "calls": [{"id": "c1", "name": "inspect_media",
                           "arguments": json.dumps({"path": "/workspace/a.mp4"})}],
            }) + "\n\n"
        yield "data: [DONE]\n\n"

    async def _fake_synthesis(*args, **kwargs):
        synthesis_calls.append(kwargs)
        return "You talked with Mara about the trip [link](http://x/view/1#t=5)."

    def _verdict(tool_events, manager=None):
        seen.append(list(tool_events))
        return module_tool_ran

    monkeypatch.setattr(al, "maybe_compact", _no_compaction, raising=False)
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr(llm_core, "llm_call_async", _fake_synthesis, raising=False)
    monkeypatch.setattr(mcp_trust, "ran_trusted_server_tool", _verdict)

    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "qwen3.5-35b",
        [{"role": "user", "content": "what did I speak about last night around 7pm?"}],
        max_rounds=5,
        relevant_tools={"inspect_media"},
        forced_tools={"inspect_media"},
        workspace="/workspace",
        owner="admin",
    )))
    assert seen and seen[-1], "hook did not see the turn's tool events"
    return events, synthesis_calls


def test_empty_reply_after_module_tool_is_answered(monkeypatch):
    events, synthesis_calls = _run_web_chat(monkeypatch, module_tool_ran=True)

    assert len(synthesis_calls) == 1
    prompt = str(synthesis_calls[0]["messages"][-1]["content"]).lower()
    assert "last response was empty" in prompt
    assert "do not call" in prompt
    assert synthesis_calls[0]["thinking_mode"] == "off"
    final = next(e for e in events if e.get("type") == "final_response"
                 and e.get("fallback") == "unattended_empty_recovery")
    assert final["content"].startswith("You talked with Mara")


def test_other_tools_keep_upstream_behaviour(monkeypatch):
    _events, synthesis_calls = _run_web_chat(monkeypatch, module_tool_ran=False)
    assert synthesis_calls == []
