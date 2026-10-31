"""Fork: the brain's fixed tool set (fork/brain/toolset.py): order, groups, the same set
for every message of a chat, slash skills, non-admin owners, read vs write."""
from __future__ import annotations

import pytest

from fork.brain import toolset
from tests.fork.brain_fakes import ATHENA, Brain, athena_manager, calls, text

SEARCH = f"mcp__{ATHENA}__search_transcripts"


@pytest.fixture
def brain(monkeypatch, data_dir):
    return Brain(monkeypatch, data_dir)


def test_fixed_order_and_never_the_excluded_tools(brain):
    names = toolset.names(toolset.resolve(owner="alice"))
    assert names[:3] == ["web_search", "web_fetch", "get_weather"]
    assert names.index(SEARCH) < names.index("manage_calendar") < names.index("search_beliefs")
    assert names.index("search_chats") < names.index("list_email_accounts")
    for never in ("bash", "python", "read_file", "trigger_research", "manage_settings",
                  "manage_mcp", "api_call", "app_api", "generate_image",
                  f"mcp__{ATHENA}__erase_fact"):
        assert never not in names
    assert toolset.names(toolset.resolve(owner="alice")) == names  # deterministic


def test_groups_follow_the_setting(brain, monkeypatch):
    monkeypatch.setenv("FORK_BRAIN_TOOL_GROUPS", "athena, web")
    names = toolset.names(toolset.resolve(owner="alice"))
    assert names[0] == "web_search"  # groups keep their fixed order
    assert set(names) == {"web_search", "web_fetch", "get_weather", SEARCH,
                          f"mcp__{ATHENA}__get_conversation",
                          f"mcp__{ATHENA}__update_action_item"}


def test_the_same_set_for_every_message_of_a_chat(brain, monkeypatch):
    first = toolset.for_chat("chat-1", owner="alice")
    # the module disconnects: the chat keeps its set
    monkeypatch.setattr("src.tool_execution.get_mcp_manager", lambda: None)
    assert toolset.for_chat("chat-1", owner="alice") == first
    assert SEARCH not in toolset.names(toolset.for_chat("chat-2", owner="alice"))


async def test_the_same_tools_reach_the_model_on_every_turn(brain):
    sess = brain.session()
    for q in ("What did Alice say?", "what is 2+2", "search the web for llama.cpp"):
        brain.llm.rounds([text("ok")])
        await brain.run(sess, q)
    tools = [r["tools"] for r in brain.llm.requests]
    assert tools[0] == tools[1] == tools[2]


def _skill(monkeypatch, toolsets):
    monkeypatch.setattr("fork.routing.skill_invocation._invokable_skill",
                        lambda name, owner: {"name": name, "requires_toolsets": toolsets})


def _slash(name, request):
    from fork.routing.skill_invocation import INVOKE_PREFIX

    return (INVOKE_PREFIX + f"---\nname: {name}\n---\n\n## Procedure\n\n1. Look.\n\n"
            f"--- END SKILL ---\n\nRequest: {request}")


async def test_slash_skills_add_their_tools_for_that_turn(brain, monkeypatch):
    _skill(monkeypatch, ["mcp:convo", "manage_documents", "bash"])
    sess = brain.session()
    brain.llm.rounds([text("ok")], [text("ok")])
    await brain.run(sess, _slash("athena", "What did Alice say?"))
    await brain.run(sess, "and then?")
    with_skill = toolset.names(brain.llm.requests[0]["tools"])
    without = toolset.names(brain.llm.requests[1]["tools"])
    assert f"mcp__{ATHENA}__erase_fact" in with_skill and "manage_documents" in with_skill
    assert "bash" not in with_skill  # never, whatever a skill declares
    assert with_skill[:len(without)] == without  # the chat's set first, unchanged
    assert f"mcp__{ATHENA}__erase_fact" not in without


def test_a_non_admin_gets_no_admin_or_private_store_tools(brain):
    names = toolset.names(toolset.resolve(owner="bob"))
    assert "web_search" in names and "search_beliefs" in names
    for blocked in ("manage_calendar", "manage_memory", "manage_tasks", "search_chats",
                    "list_emails", "send_email", SEARCH):
        assert blocked not in names


async def test_a_non_admin_cannot_reach_admin_only_tools_even_if_offered(brain, monkeypatch):
    """Upstream's runner refuses them; the brain adds no rule of its own."""
    from fork.brain import tools as brain_tools

    brain.mp.setattr("src.tool_execution.execute_tool_block", brain.real_execute)
    monkeypatch.setattr("src.tool_execution.owner_is_admin_or_single_user",
                        lambda owner: owner == "alice")
    run = brain_tools.build_run(request_text="change the settings and my calendar",
                                owner="bob", session_id="chat-b", history=[],
                                offered=("manage_settings", "manage_calendar"), disabled=set())
    for name, content in (("manage_settings", '{"action": "list"}'),
                          ("manage_calendar", '{"action": "list_events"}')):
        _, desc, result = await brain_tools.execute(run, name, content)
        assert "BLOCKED" in desc and result.get("exit_code") == 1, (name, result)


def test_read_or_write():
    assert toolset.is_read("web_search")
    assert toolset.is_read("manage_calendar", '{"action": "list_events"}')
    assert toolset.is_read("manage_tasks", "{}")  # upstream's default action: list
    assert not toolset.is_read("manage_calendar", '{"action": "create_event"}')
    assert not toolset.is_read("manage_notes", '{"action": "add", "content": "x"}')
    assert not toolset.is_read("send_email", "{}")
    assert not toolset.is_read("draft_email", "{}")


def test_mcp_reads_follow_the_trusted_read_only_rule(brain, monkeypatch):
    assert toolset.is_read(SEARCH)
    assert not toolset.is_read(f"mcp__{ATHENA}__update_action_item")
    monkeypatch.delenv("ODYSSEUS_TRUSTED_READONLY_MCP")
    assert not toolset.is_read(SEARCH)  # an untrusted server's tools ask


def test_athena_found_under_its_new_name(brain, monkeypatch):
    manager = athena_manager()
    manager._tools = {"athena": manager._tools.pop(ATHENA)}
    manager._connections = {"athena": {"status": "connected", "name": "athena"}}
    monkeypatch.setattr("src.tool_execution.get_mcp_manager", lambda: manager)
    assert "mcp__athena__search_transcripts" in toolset.names(toolset.resolve(owner="alice"))


async def test_brain_calls_only_offered_tools_for_a_non_admin(brain):
    sess = brain.session(owner="bob")
    brain.llm.rounds([calls(("manage_calendar", {"action": "list_events"}))], [text("no")])
    await brain.run(sess, "what's on my calendar?")
    assert brain.ran == []
