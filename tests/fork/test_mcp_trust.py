"""Admin-trusted read-only MCP servers (src/mcp_trust.py)."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src import mcp_trust
from src.agent_runtime.authority import ExactOperation, create_request_authority
from src.tool_capabilities import ToolRunSecurityContext
from src.tool_policy import ToolPolicy


class FakeManager:
    def __init__(self):
        self._connections = {
            "srv1": {"status": "connected", "name": "convo"},
            "srv2": {"status": "connected", "name": "other"},
        }
        ro = {"readOnlyHint": True}
        self._tools = {
            "srv1": [
                {"name": "search_transcripts", "annotations": ro},
                # pydantic-style annotations object, as the mcp client returns
                {"name": "get_transcript", "annotations": SimpleNamespace(readOnlyHint=True,
                                                                          destructiveHint=None)},
                {"name": "name_speaker", "annotations": {"readOnlyHint": False}},
                {"name": "list_guess"},  # no annotation: name heuristic must not apply
                {"name": "wipe", "annotations": {"readOnlyHint": True, "destructiveHint": True}},
            ],
            "srv2": [{"name": "search_web", "annotations": ro}],
        }

    def is_builtin(self, server_id):
        return False


@pytest.fixture
def manager(monkeypatch):
    m = FakeManager()
    monkeypatch.setattr(mcp_trust, "_manager", lambda: m)
    return m


SEARCH = "mcp__srv1__search_transcripts"


def test_off_by_default(manager, monkeypatch):
    monkeypatch.delenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, raising=False)
    assert mcp_trust.trusted_readonly_mcp_tools() == frozenset()
    assert not mcp_trust.is_trusted_readonly_mcp_tool(SEARCH)


@pytest.mark.parametrize("value", ["convo", "CONVO", "srv1", " other , convo "])
def test_only_declared_read_only_tools_on_named_servers(manager, monkeypatch, value):
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, value)
    tools = mcp_trust.trusted_readonly_mcp_tools()
    assert SEARCH in tools and "mcp__srv1__get_transcript" in tools
    for name in ("name_speaker", "list_guess", "wipe"):
        assert f"mcp__srv1__{name}" not in tools
    assert ("mcp__srv2__search_web" in tools) == ("other" in value)


def test_disconnected_server_is_not_trusted(manager, monkeypatch):
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    manager._connections["srv1"]["status"] = "error"
    assert mcp_trust.trusted_readonly_mcp_tools() == frozenset()


def test_request_authority_grants_trusted_tools_only(manager, monkeypatch):
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    auth = create_request_authority("What did Mike say last week?", owner="alice",
                                    session_id="s", policy=ToolPolicy())
    assert auth.permits(ExactOperation.normalize(SEARCH, '{"speaker":"Mike"}'))
    assert not auth.permits(ExactOperation.normalize(
        "mcp__srv1__name_speaker", '{"name":"Mike"}'))


def test_request_authority_still_honours_disables(manager, monkeypatch):
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    auth = create_request_authority("What did Mike say?", owner="alice", session_id="s",
                                    policy=ToolPolicy())
    op = ExactOperation.normalize(SEARCH, "{}")
    assert not auth.restrict(disabled_tools={SEARCH}).permits(op)
    assert not auth.restrict(ToolPolicy(disable_mcp=True)).permits(op)


def test_gate_admits_trusted_reads_after_external_context(manager, monkeypatch):
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    ctx = ToolRunSecurityContext()
    ctx.observe_tool_result(SEARCH, {"output": "untrusted transcript text"}, "{}")
    assert ctx.external_untrusted_context_seen
    assert ctx.decision_for(SEARCH, "{}").allowed
    # Write tools on the trusted server and tools on other servers stay gated.
    assert not ctx.decision_for("mcp__srv1__name_speaker", "{}").allowed
    assert not ctx.decision_for("mcp__srv2__search_web", "{}").allowed


def test_web_disabled_shortcut_yields_to_trusted_tools(manager, monkeypatch):
    from src.agent_loop import WEB_TOOL_NAMES, _web_search_unavailable_for_turn
    disabled = set(WEB_TOOL_NAMES) | {"private_browser"}
    monkeypatch.delenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, raising=False)
    baseline = _web_search_unavailable_for_turn({"web"}, disabled, "What's the latest on the move?",
                                                None, None)
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    assert _web_search_unavailable_for_turn({"web"}, disabled, "What's the latest on the move?",
                                            None, None) is False
    # Disabling the trusted tools restores the original behaviour.
    trusted = mcp_trust.trusted_readonly_mcp_tools()
    assert _web_search_unavailable_for_turn({"web"}, disabled | trusted,
                                            "What's the latest on the move?", None, None) == baseline


def _mcp_capture(tool):
    from src.agent_runtime.effect_adapters import DispatchCapture
    from src.agent_runtime.remote_resources import BoundBackendOperation
    from src.agent_runtime.resources import ExternalResource
    resource = ExternalResource("mcp", "endpoint", "srv1", tool, "incarnation")
    return DispatchCapture(backend=BoundBackendOperation(resource, "req", "alice", "s", tool, "{}"))


def test_trusted_read_is_not_an_unverified_external_effect(manager, monkeypatch):
    """Otherwise the answer gets 'External operation ... not independently verified'."""
    from src.agent_runtime.effect_adapters import classify
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    assert classify(_mcp_capture(SEARCH)) is None
    assert classify(_mcp_capture("mcp__srv1__name_speaker"))["external"] is True
    monkeypatch.delenv(mcp_trust.TRUSTED_READONLY_MCP_ENV)
    assert classify(_mcp_capture(SEARCH))["external"] is True


@pytest.fixture
def no_disables(monkeypatch):
    from src import skill_mcp_toolsets
    disabled = {}
    monkeypatch.setattr(skill_mcp_toolsets, "_disabled_map", lambda: disabled)
    return disabled


def test_follow_up_gets_the_used_trusted_servers_other_tools(manager, monkeypatch, no_disables):
    """'Speaker 2 was Kath' after a /sound search must be offered name_speaker."""
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    warm = mcp_trust.with_trusted_server_tools({SEARCH, "manage_notes"})
    assert {"mcp__srv1__name_speaker", "mcp__srv1__get_transcript", "manage_notes"} <= warm
    assert "mcp__srv2__search_web" not in warm


def test_follow_up_expansion_respects_trust_and_disables(manager, monkeypatch, no_disables):
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "other")
    assert mcp_trust.with_trusted_server_tools({SEARCH}) == {SEARCH}
    monkeypatch.delenv(mcp_trust.TRUSTED_READONLY_MCP_ENV)
    assert mcp_trust.with_trusted_server_tools({SEARCH}) == {SEARCH}
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    no_disables["srv1"] = ["name_speaker"]
    assert "mcp__srv1__name_speaker" not in mcp_trust.with_trusted_server_tools({SEARCH})
    # Nothing from the server ran yet: nothing is added.
    assert mcp_trust.with_trusted_server_tools({"manage_notes"}) == {"manage_notes"}


def test_follow_up_contract_offers_write_tool_but_does_not_grant_it(manager, monkeypatch,
                                                                    no_disables):
    from src.turn_contract import resolve_turn_contract
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    schemas = [{"type": "function", "function": {"name": f"mcp__srv1__{t['name']}",
                                                 "parameters": {"type": "object"}}}
               for t in manager._tools["srv1"]]
    message = 'Speaker 1 and 3 was "Marisol", speaker 2 was "Kath"'
    contract = resolve_turn_contract(
        capabilities=(), schemas=schemas, policy=ToolPolicy(), message=message,
        warm_tools=mcp_trust.with_trusted_server_tools({SEARCH}))
    assert "mcp__srv1__name_speaker" in contract.offered
    auth = create_request_authority(message, owner="alice", session_id="s", policy=ToolPolicy())
    assert not auth.permits(ExactOperation.normalize("mcp__srv1__name_speaker", "{}"))


def _approval(tool, allow_remaining=True):
    return SimpleNamespace(pending=SimpleNamespace(tool_name=tool),
                           allow_remaining_actions=allow_remaining)


RENAME = "mcp__srv1__name_speaker"


def _rename(label, name):
    return ExactOperation.normalize(RENAME, f'{{"speaker_label":"{label}","name":"{name}"}}')


@pytest.fixture
def backends(manager, monkeypatch):
    """MCP backend identities, as McpManager.resource_identity returns them."""
    from src import tool_execution
    from src.agent_runtime.resources import ExternalResource

    def identity(qualified):
        _, server, tool = qualified.split("__", 2)
        if server not in manager._tools:
            return None
        return ExternalResource("mcp", f"endpoint:{server}", server, tool, "incarnation")

    manager.resource_identity = identity
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: manager)
    return manager


def test_approved_trusted_tool_runs_again_in_the_resumed_task(backends, monkeypatch):
    """Approving one name_speaker lets the other speakers be renamed in that task."""
    from src.agent_runtime.authority import RequestAuthority
    from src.agent_runtime.remote_resources import bind_backend_for_operation
    from src.agent_runtime.resources import ResourceIdentityError
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    # A resumed authority: inherited, backends sealed from the original request.
    base = replace(RequestAuthority.empty(owner="alice", session_id="s"), inherited=True)
    assert not base.permits(_rename("Speaker 3", "Marisol"))
    with pytest.raises(ResourceIdentityError):
        bind_backend_for_operation(base, _rename("Speaker 3", "Marisol"))
    auth = mcp_trust.with_approved_tool_grant(base, _approval(RENAME))
    assert auth.permits(_rename("Speaker 3", "Marisol"))
    assert auth.permits(_rename("Speaker 2", "Kath"))
    # The tool's MCP backend is in scope too ("External backend is outside
    # sealed request scope" on the server before this).
    bound = bind_backend_for_operation(auth, _rename("Speaker 2", "Kath"))
    assert bound.resource.tool_id == "name_speaker"
    with pytest.raises(ResourceIdentityError):
        bind_backend_for_operation(auth, ExactOperation.normalize("mcp__srv2__search_web", "{}"))
    # Only the approved tool; other tools on the server or elsewhere get nothing.
    assert not auth.permits(ExactOperation.normalize("mcp__srv1__wipe", "{}"))
    assert not auth.permits(ExactOperation.normalize("mcp__srv2__search_web", "{}"))
    # Policy and per-tool disables still win.
    assert not auth.restrict(disabled_tools={RENAME}).permits(_rename("Speaker 2", "Kath"))
    assert not auth.restrict(ToolPolicy(disable_mcp=True)).permits(_rename("Speaker 2", "Kath"))
    assert mcp_trust.with_approved_tool_grant(auth, _approval(RENAME)) is auth


def test_no_grant_without_trust_or_continuation(backends, monkeypatch):
    from src.agent_runtime.authority import RequestAuthority
    base = RequestAuthority.empty(owner="alice", session_id="s")
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    for approval in (_approval(RENAME, allow_remaining=False),
                     _approval("mcp__srv2__search_web"), _approval("bash"), None):
        assert mcp_trust.with_approved_tool_grant(base, approval) is base
    monkeypatch.delenv(mcp_trust.TRUSTED_READONLY_MCP_ENV)
    assert mcp_trust.with_approved_tool_grant(base, _approval(RENAME)) is base
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "convo")
    backends._connections["srv1"]["status"] = "error"
    assert mcp_trust.with_approved_tool_grant(base, _approval(RENAME)) is base
    # A backend that can't be resolved (or belongs to another owner) gets nothing.
    backends._connections["srv1"]["status"] = "connected"
    backends.resource_identity = lambda qualified: None
    assert mcp_trust.with_approved_tool_grant(base, _approval(RENAME)) is base
