"""Trusted read-only MCP servers.

By default every third-party MCP tool is an "unknown" capability: the first
call in a turn needs user approval, and request authority never grants MCP
tools, so after that approved call any further MCP call in the same turn is
blocked. That is the right default for arbitrary servers, but it makes a
local knowledge server (search, then open the hit, then cite it) unusable.

An admin can name servers in ``ODYSSEUS_TRUSTED_READONLY_MCP`` (comma
separated server names or ids). For those servers only, tools that the server
itself declares ``readOnlyHint: true`` (and not ``destructiveHint: true``) are:

* granted by request authority for every turn, and
* allowed by the post-external-context approval gate.

Everything else is unchanged: write tools on a trusted server, and every tool
on any other server, keep the normal approval flow. There is no name
heuristic here; a tool without an explicit read-only annotation is not trusted.
Per-tool disables, tool policy and ``disable_mcp`` still apply, because the
grant goes through ``RequestAuthority.restrict`` like any other.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

TRUSTED_READONLY_MCP_ENV = "ODYSSEUS_TRUSTED_READONLY_MCP"


def trusted_readonly_server_keys() -> frozenset[str]:
    raw = os.environ.get(TRUSTED_READONLY_MCP_ENV, "")
    return frozenset(part.strip().casefold() for part in raw.split(",") if part.strip())


def _annotation(tool: dict, key: str) -> Any:
    ann = tool.get("annotations")
    if ann is None:
        return None
    if isinstance(ann, dict):
        return ann.get(key)
    return getattr(ann, key, None)


def tool_declares_read_only(tool: dict) -> bool:
    """Strict: only an explicit ``readOnlyHint: true`` counts."""
    return (_annotation(tool, "readOnlyHint") is True
            and _annotation(tool, "destructiveHint") is not True)


def _manager():
    try:
        from src.tool_execution import get_mcp_manager
        return get_mcp_manager()
    except (ImportError, AttributeError):
        return None


def _trusted_server_ids(manager, keys: frozenset[str]) -> Iterable[str]:
    connections = getattr(manager, "_connections", {}) or {}
    for server_id in (getattr(manager, "_tools", {}) or {}):
        if manager.is_builtin(server_id):
            continue
        conn = connections.get(server_id) or {}
        if conn.get("status") != "connected":
            continue
        name = str(conn.get("name") or "").strip().casefold()
        if str(server_id).casefold() in keys or (name and name in keys):
            yield server_id


def trusted_readonly_mcp_tools(manager=None) -> frozenset[str]:
    """Qualified ``mcp__<server_id>__<tool>`` names admitted without approval."""
    keys = trusted_readonly_server_keys()
    if not keys:
        return frozenset()
    manager = manager if manager is not None else _manager()
    if manager is None:
        return frozenset()
    names = set()
    for server_id in _trusted_server_ids(manager, keys):
        for tool in manager._tools.get(server_id, []):
            if tool_declares_read_only(tool):
                names.add(f"mcp__{server_id}__{tool['name']}")
    return frozenset(names)


def is_trusted_readonly_mcp_tool(tool_name: Any, manager=None) -> bool:
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return False
    if not trusted_readonly_server_keys():
        return False
    return tool_name.strip() in trusted_readonly_mcp_tools(manager)


def with_trusted_server_tools(names: Iterable[str], manager=None,
                              enabled: dict[str, set[str]] | None = None) -> frozenset[str]:
    """``names`` plus every enabled tool of each trusted server already in it.

    Upstream restores only the exact tools that ran earlier in a chat ("warm
    tools") on a follow-up turn. For a module server that hides its write
    tools: after ``/sound`` searched a conversation, "Speaker 2 was Kath" got
    the search tools back but never ``name_speaker``. A trusted module is one
    surface, so a used tool brings back its server's other enabled tools.
    This only offers them: write tools still need approval, and policy,
    per-tool disables and ``disable_mcp`` still filter the turn.
    """
    names = frozenset(str(n) for n in names or () if n)
    keys = trusted_readonly_server_keys()
    if not keys or not any(n.startswith("mcp__") for n in names):
        return names
    manager = manager if manager is not None else _manager()
    if manager is None:
        return names
    if enabled is None:
        from src.skill_mcp_toolsets import enabled_mcp_tools
        enabled = enabled_mcp_tools(manager)
    out = set(names)
    for server_id in _trusted_server_ids(manager, keys):
        if any(n.startswith(f"mcp__{server_id}__") for n in names):
            out |= enabled.get(server_id, set())
    return frozenset(out)


def is_trusted_server_tool(tool_name: Any, manager=None) -> bool:
    """A ``mcp__<server_id>__<tool>`` name on a connected trusted server."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return False
    keys = trusted_readonly_server_keys()
    if not keys:
        return False
    manager = manager if manager is not None else _manager()
    if manager is None:
        return False
    return any(tool_name.startswith(f"mcp__{server_id}__")
               for server_id in _trusted_server_ids(manager, keys))


def with_approved_tool_grant(authority, exact_approval, manager=None):
    """Let an approved trusted-module tool run again in the resumed task.

    The chat card's "allow" covers the rest of the resumed task (upstream sets
    ``allow_remaining_actions``, which lifts the approval gate), but request
    authority still admits only the one exact approved call: built-in tools
    get grants from the request text, MCP tools never do. So "Speaker 1 and 3
    were Marisol, speaker 2 was Kath" renamed Speaker 1 after the owner
    approved, then hard-blocked the next ``name_speaker`` calls ("outside
    server request authority"). For a tool on a trusted module server only,
    the resumed task now gets a grant for that same tool. Other tools get
    nothing, and ``restrict`` (policy, per-tool disables, ``disable_mcp``)
    still applies to every call.

    The authority also lists the external backends it may reach, sealed from
    the original request's grants, so the tool's MCP backend is added there
    too. If that backend can't be resolved for this owner, nothing is granted.
    """
    if authority is None or exact_approval is None:
        return authority
    if not getattr(exact_approval, "allow_remaining_actions", False):
        return authority
    tool = str(getattr(exact_approval.pending, "tool_name", "") or "").strip()
    if not is_trusted_server_tool(tool, manager):
        return authority
    from dataclasses import replace

    from src.agent_runtime.authority import OperationGrant, canonical_tool
    from src.agent_runtime.remote_resources import resolve_backend
    try:
        backend = resolve_backend(tool, admission=True, owner=authority.owner)
    except (ValueError, TypeError, AttributeError):
        return authority
    grants = authority.grants
    if not any(g.tool == canonical_tool(tool) and g.actions is None and g.inputs is None
               for g in grants):
        grants = (*grants, OperationGrant(canonical_tool(tool)))
    backends = authority.backend_resources
    if backends is not None and backend not in backends:
        backends = (*backends, backend)
    if grants is authority.grants and backends is authority.backend_resources:
        return authority
    return replace(authority, grants=grants, backend_resources=backends)


def ran_trusted_server_tool(tool_events: Iterable[Any], manager=None) -> bool:
    """True when this turn ran a tool of a trusted module server successfully.

    After a module search the model sometimes ends the turn with no text (a
    thinking model spends its whole reply on reasoning). The chat then shows
    the route's placeholder "Done." instead of the answer. Upstream already
    has a one-shot, tool-free "your last response was empty, answer now"
    recovery, but only for unattended runs. The agent loop also uses it for
    interactive turns when this returns True. A turn that stopped on a
    question or approval card is waiting for the owner, so it doesn't count.
    """
    events = [e for e in tool_events or () if isinstance(e, dict)]
    if any(e.get("ask_user") for e in events):
        return False
    for event in events:
        if event.get("error") or event.get("exit_code") not in (None, 0):
            continue
        if is_trusted_server_tool(str(event.get("tool") or "").strip(), manager):
            return True
    return False
