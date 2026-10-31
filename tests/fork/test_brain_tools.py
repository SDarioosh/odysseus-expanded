"""Fork: the brain's tool runs (fork/brain/tools.py): the request authority, owner
isolation through upstream's real runner, what asks and what runs, search_beliefs."""
from __future__ import annotations

import pytest

from fork.brain import tools as brain_tools
from tests.fork.brain_fakes import Brain, calls, of, text


@pytest.fixture
def brain(monkeypatch, data_dir):
    return Brain(monkeypatch, data_dir, admins=("alice", "bob"))


def _memories(tmp_path):
    from src.memory import MemoryManager

    (tmp_path / "mem").mkdir()
    manager = MemoryManager(str(tmp_path / "mem"))
    alice = manager.add_entry("Alice's locker code is 4417", owner="alice")
    bob = manager.add_entry("Bob's locker is number 12", owner="bob")
    manager.save([alice, bob])
    return manager


async def test_another_owners_memory_is_never_returned(brain, monkeypatch, tmp_path):
    """Upstream's real runner, through the brain's authority, for bob's turn."""
    manager = _memories(tmp_path)
    monkeypatch.setattr("src.ai_interaction._memory_manager", manager)
    brain.mp.setattr("src.tool_execution.execute_tool_block", brain.real_execute)
    monkeypatch.setattr("src.tool_execution.owner_is_admin_or_single_user",
                        lambda owner: owner in ("alice", "bob"))
    sess = brain.session(sid="chat-bob", owner="bob")
    brain.llm.rounds([calls(("manage_memory", {"action": "list"}))], [text("ok")])
    from fork.brain import turn as brain_turn

    events = await brain.run(sess, "what's my locker?",
                             deps=brain_turn._Deps(memory_manager=manager))
    output = of(events, "tool_output")[0]["output"]
    assert "Bob's locker" in output and "4417" not in output
    # the context message recalls bob's memories only
    context = brain.llm.requests[0]["messages"][-2]["content"]
    assert "4417" not in context


async def test_beliefs_and_recall_are_asked_for_the_turns_owner(brain, monkeypatch):
    asked = []

    def fake_search(owner, query, **kw):
        asked.append(owner)
        return [{"subject_display": "Jonas", "text": "is allergic to nuts", "link": "L"}]

    monkeypatch.setattr("fork.beliefs.search.search_beliefs", fake_search)
    sess = brain.session(sid="chat-bob", owner="bob")
    brain.llm.rounds([calls(("search_beliefs", {"query": "Jonas allergy"}))], [text("Nuts.")])
    events = await brain.run(sess, "What is Jonas allergic to?")
    assert asked == ["bob", "bob"]  # context recall, then the tool
    assert "Jonas: is allergic to nuts (L)" in of(events, "tool_output")[0]["output"]
    assert "is allergic to nuts" in brain.llm.requests[0]["messages"][-2]["content"]


def test_authority_admits_reads_of_the_set_and_no_writes():
    from src.agent_runtime.authority import ExactOperation

    run = brain_tools.build_run(
        request_text="hello", owner="alice", session_id="c1", history=[],
        offered=("web_search", "web_fetch", "manage_calendar", "manage_contact",
                 "list_emails", "send_email"), disabled=set())
    permits = run.authority.permits
    assert permits(ExactOperation.normalize("web_search", '{"query": "x"}'))
    assert permits(ExactOperation.normalize("manage_calendar", '{"action": "list_events"}'))
    assert permits(ExactOperation.normalize("manage_contact", '{"action": "search"}'))
    assert permits(ExactOperation.normalize("list_emails", "{}"))
    assert not permits(ExactOperation.normalize("manage_calendar", '{"action": "create_event"}'))
    assert not permits(ExactOperation.normalize("send_email", "{}"))
    assert not permits(ExactOperation.normalize("bash", "ls"))
    assert run.authority.bound_to(owner="alice", session_id="c1")


def test_disabled_tools_stay_denied():
    from src.agent_runtime.authority import ExactOperation

    run = brain_tools.build_run(request_text="hello", owner="alice", session_id="c1",
                                history=[], offered=("web_search",), disabled={"web_search"})
    assert not run.authority.permits(ExactOperation.normalize("web_search", '{"query": "x"}'))


def test_privileges_and_the_global_list_disable_tools(monkeypatch):
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda o: True)
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: ["get_weather"] if key == "disabled_tools" else default)
    off = brain_tools.turn_disabled_tools(None, "alice", privileges={"can_manage_memory": False})
    assert {"manage_memory", "get_weather"} <= off
    assert "bash" in brain_tools.turn_disabled_tools(None, "alice", delegated=True)


def test_writes_and_gate_refusals_ask_reads_run(monkeypatch):
    run = brain_tools.build_run(request_text="hello", owner="alice", session_id="c1",
                                history=[], offered=("manage_notes", "web_search"), disabled=set())
    assert brain_tools.needs_approval(run, "manage_notes", '{"action": "search"}') is None
    assert brain_tools.needs_approval(run, "manage_notes", '{"action": "add"}')
    assert brain_tools.needs_approval(run, "web_search", '{"query": "x"}') is None
    # after outside content upstream's gate asks for private reads too (Ask mode)
    run.security.external_untrusted_context_seen = True
    run.security._fork_auto_mode = False  # the chat is in Ask
    assert brain_tools.needs_approval(run, "manage_notes", '{"action": "search"}')


def test_trim_cuts_at_a_line_and_says_so():
    rows = "\n".join(f"row {i}" for i in range(5000))
    out = brain_tools.trim(rows, 100)
    assert out.splitlines()[0] == "row 0" and "more characters not shown" in out
    assert brain_tools.trim("short") == "short"
