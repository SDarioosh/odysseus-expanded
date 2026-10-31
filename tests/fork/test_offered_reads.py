"""Offered personal stores are readable on the turn (src/offered_reads.py)."""
import json
from dataclasses import replace
from types import SimpleNamespace

from src.agent_runtime.authority import (
    ExactOperation,
    RequestAuthority,
    create_request_authority,
)
from src.agent_runtime.remote_resources import bind_backend_for_operation
from src.offered_reads import (
    OFFERED_READ_TOOLS,
    offered_read_tools,
    with_offered_read_grants,
)
from src.tool_policy import ToolPolicy


def op(tool, **args):
    return ExactOperation.normalize(tool, json.dumps(args))


def contract(*names):
    return SimpleNamespace(offered=frozenset(names))


LIST_EVENTS = op("manage_calendar", action="list_events", start="2026-10-19", end="2026-10-20")
CREATE_EVENT = op("manage_calendar", action="create_event", title="x", start="2026-10-20T10:00")
LIST_TASKS = op("manage_tasks", action="list")
DELETE_TASK = op("manage_tasks", action="delete", task_id="t1")


def tasks_authority():
    # "what are my tasks for today?" grants manage_tasks only.
    return create_request_authority("what are my tasks for today?", owner="alice", session_id="s")


def test_upstream_grants_only_the_words():
    auth = tasks_authority()
    assert auth.permits(LIST_TASKS)
    assert not auth.permits(LIST_EVENTS)


def test_offered_tool_gets_reads_not_writes():
    auth = with_offered_read_grants(
        tasks_authority(), contract("manage_tasks", "manage_calendar", "ask_user"))
    assert auth.permits(LIST_EVENTS)
    assert auth.permits(op("manage_calendar", action="list_calendars"))
    assert not auth.permits(CREATE_EVENT)
    assert not auth.permits(op("manage_calendar", action="delete_event", event_id="e1"))
    # The tool the words named keeps its full grant.
    assert auth.permits(LIST_TASKS) and auth.permits(DELETE_TASK)


def test_tools_not_offered_get_nothing():
    auth = with_offered_read_grants(tasks_authority(), contract("manage_tasks"))
    assert not auth.permits(LIST_EVENTS)
    # Admin and session tools are never widened, even when offered.
    auth = with_offered_read_grants(tasks_authority(), contract("manage_settings", "manage_tokens"))
    assert not auth.permits(op("manage_settings", action="list"))
    assert not auth.permits(op("manage_tokens", action="list"))


def test_pinned_exact_read_widens_to_reads_only():
    # "list my tasks" pins {"action":"list"} exactly.
    auth = create_request_authority("list my tasks", owner="alice", session_id="s")
    assert not auth.permits(op("manage_tasks", action="list", status="active"))
    auth = with_offered_read_grants(auth, contract("manage_tasks"))
    assert auth.permits(op("manage_tasks", action="list", status="active"))
    assert not auth.permits(DELETE_TASK)


def test_stale_denial_from_resumed_task_lifts_for_reads_only():
    # A resumed task carries the first turn's denials, even for a tool it had
    # a full grant for; it comes back for reads only.
    base = with_offered_read_grants(tasks_authority(), contract("manage_calendar"))
    base = replace(base, grants=tuple(
        g if g.tool != "manage_calendar" else replace(g, actions=None) for g in base.grants),
        denied=base.denied | {"manage_calendar"})
    assert not base.permits(LIST_EVENTS)
    auth = with_offered_read_grants(base, None)
    assert "manage_calendar" not in auth.denied
    assert auth.permits(LIST_EVENTS)
    assert not auth.permits(CREATE_EVENT)


def test_without_contract_policy_and_disables_still_win(monkeypatch):
    assert offered_read_tools(None) == OFFERED_READ_TOOLS
    assert "manage_calendar" not in offered_read_tools(None, disabled_tools={"manage_calendar"})
    policy = ToolPolicy(disabled_tools=frozenset({"manage_notes"}))
    assert "manage_notes" not in offered_read_tools(None, policy)
    assert offered_read_tools(None, ToolPolicy(block_all_tool_calls=True)) == frozenset()
    from src import tool_security
    monkeypatch.setattr(tool_security, "blocked_tools_for_owner", lambda owner: {"manage_memory"})
    assert "manage_memory" not in offered_read_tools(None, owner="bob")
    # A per-tool disable applied at execution still blocks the read.
    auth = with_offered_read_grants(tasks_authority(), None)
    assert auth.permits(LIST_EVENTS)
    assert not auth.restrict(disabled_tools={"manage_calendar"}).permits(LIST_EVENTS)


def test_block_all_is_untouched():
    auth = replace(tasks_authority(), block_all=True)
    assert with_offered_read_grants(auth, contract("manage_calendar")) is auth


def test_inherited_authority_reaches_backend_and_owned_scope():
    base = replace(RequestAuthority.empty(owner="alice", session_id="s"), inherited=True)
    auth = with_offered_read_grants(base, contract("manage_calendar", "manage_notes"))
    assert auth.permits(LIST_EVENTS)
    bound = bind_backend_for_operation(auth, LIST_EVENTS)
    assert bound.resource.tool_id == "manage_calendar"
    assert any(s.namespace == "notes" for s in auth.owned_scopes)


def test_nothing_offered_returns_same_authority():
    auth = tasks_authority()
    assert with_offered_read_grants(auth, contract("ask_user")) is auth
    assert with_offered_read_grants(None, contract("manage_calendar")) is None
