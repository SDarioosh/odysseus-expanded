"""MCP tools in a skill's ``requires_toolsets``.

A skill's ``requires_toolsets`` names the tools it needs. Built-in tools are
named directly (``manage_notes``). This module adds two forms for MCP tools:

* ``mcp:<server>``: every enabled tool of a connected MCP server, where
  ``<server>`` is the server's name or id (case-insensitive);
* ``mcp__<server_id>__<tool>``: one qualified MCP tool.

Both expand only to tools of *connected* servers that are not disabled
(Settings > MCP per-tool toggles). Expansion never grants anything: the
expanded names still go through tool policy, the turn contract and request
authority, so write tools keep their normal approval.

Callers use:

* :func:`expand_toolsets` where a skill's tools are added to a turn;
* :func:`with_mcp_toolsets` where ``requires_toolsets`` is checked against an
  "available tools" set (skill matching, the skill index), so a skill that
  needs ``mcp:<server>`` is visible only while that server is usable.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)

MCP_TOOLSET_PREFIX = "mcp:"
MCP_TOOL_PREFIX = "mcp__"


def is_mcp_toolset(entry: object) -> bool:
    text = str(entry or "").strip()
    return text.startswith((MCP_TOOLSET_PREFIX, MCP_TOOL_PREFIX))


def _manager():
    try:
        from src.tool_execution import get_mcp_manager
        return get_mcp_manager()
    except (ImportError, AttributeError):
        return None


def _disabled_map() -> dict:
    try:
        from src.agent_loop import _load_mcp_disabled_map
        return _load_mcp_disabled_map() or {}
    except Exception:
        logger.debug("MCP disabled map unavailable", exc_info=True)
        return {}


def enabled_mcp_tools(manager=None, disabled_map: dict | None = None) -> dict[str, set[str]]:
    """``{server_id: {qualified tool names}}`` for connected servers' enabled tools."""
    manager = manager if manager is not None else _manager()
    if manager is None:
        return {}
    disabled_map = _disabled_map() if disabled_map is None else disabled_map
    connections = getattr(manager, "_connections", {}) or {}
    out: dict[str, set[str]] = {}
    for server_id, tools in (getattr(manager, "_tools", {}) or {}).items():
        if (connections.get(server_id) or {}).get("status") != "connected":
            continue
        disabled = set((disabled_map or {}).get(server_id) or ())
        names = {
            f"{MCP_TOOL_PREFIX}{server_id}__{tool['name']}"
            for tool in tools or []
            if isinstance(tool, dict) and tool.get("name") and tool["name"] not in disabled
        }
        if names:
            out[server_id] = names
    return out


def _server_keys(manager, server_ids: Iterable[str]) -> dict[str, set[str]]:
    """casefolded server id or name -> server ids."""
    connections = getattr(manager, "_connections", {}) or {}
    keys: dict[str, set[str]] = {}
    for server_id in server_ids:
        name = str((connections.get(server_id) or {}).get("name") or "").strip()
        for key in {str(server_id), name}:
            if key:
                keys.setdefault(key.casefold(), set()).add(server_id)
    return keys


def expand_toolsets(
    entries: Iterable[object] | None,
    manager=None,
    *,
    enabled: dict[str, set[str]] | None = None,
) -> tuple[set[str], set[str]]:
    """Expand ``requires_toolsets`` entries to tool names.

    Returns ``(names, missing)``: built-in names pass through unchanged; MCP
    entries become qualified names of enabled tools. ``missing`` holds the MCP
    entries that matched nothing (server unknown, disconnected, or every tool
    disabled), so the caller can report them instead of silently dropping them.
    """
    names: set[str] = set()
    missing: set[str] = set()
    entries = [str(e).strip() for e in (entries or []) if str(e or "").strip()]
    if not any(is_mcp_toolset(e) for e in entries):
        return set(entries), missing
    manager = manager if manager is not None else _manager()
    if enabled is None:
        enabled = enabled_mcp_tools(manager) if manager is not None else {}
    all_enabled = set().union(*enabled.values()) if enabled else set()
    keys = _server_keys(manager, enabled) if manager is not None else {}
    for entry in entries:
        if entry.startswith(MCP_TOOLSET_PREFIX):
            key = entry[len(MCP_TOOLSET_PREFIX):].strip().casefold()
            found = set()
            for server_id in keys.get(key, ()):
                found |= enabled.get(server_id, set())
            if found:
                names |= found
            else:
                missing.add(entry)
        elif entry.startswith(MCP_TOOL_PREFIX):
            if entry in all_enabled:
                names.add(entry)
            else:
                missing.add(entry)
        else:
            names.add(entry)
    return names, missing


def expand_skill_toolsets(entries: Iterable[object] | None, manager=None) -> set[str]:
    """Like :func:`expand_toolsets`, names only (for existing call sites)."""
    return expand_toolsets(entries, manager)[0]


def with_mcp_toolsets(available: Iterable[str], manager=None) -> set[str]:
    """``available`` plus every MCP toolset entry that is currently usable.

    Adds ``mcp:<server id>``, ``mcp:<server name>`` (as written and casefolded)
    and each enabled qualified tool name, so ``all(t in available for t in
    requires_toolsets)`` works unchanged for MCP entries.
    """
    out = set(available or ())
    manager = manager if manager is not None else _manager()
    if manager is None:
        return out
    try:
        enabled = enabled_mcp_tools(manager)
    except Exception:
        logger.debug("MCP toolset availability skipped", exc_info=True)
        return out
    connections = getattr(manager, "_connections", {}) or {}
    for server_id, names in enabled.items():
        out |= names
        name = str((connections.get(server_id) or {}).get("name") or "").strip()
        for key in {str(server_id), name}:
            if key:
                out.add(MCP_TOOLSET_PREFIX + key)
                out.add(MCP_TOOLSET_PREFIX + key.casefold())
    return out
