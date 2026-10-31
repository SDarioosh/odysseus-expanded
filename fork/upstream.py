"""The fork's reads of upstream's private names, in one place.

Upstream may rename or drop a ``_``-prefixed name in any sync. The fork reaches each one
through a small function here, so an upstream sync has one file to check, and
``tests/fork/test_fork_upstream.py`` fails loudly when a name is gone.

Every function imports lazily, at call time: no upstream module is loaded by importing
this one, and a test that patches the upstream attribute still reaches the call.

Not routed through here: ``fork/mcp/session_recovery.py`` and ``fork/mcp/stdio_owner.py``
are the bodies of hooks inside ``McpManager`` methods and work on its state directly
(:data:`MCP_MANAGER_INTERNALS` lists what they use; the test checks each still exists).
"""
from __future__ import annotations

import sys
from typing import Any

# McpManager attributes fork/mcp/session_recovery.py and fork/mcp/stdio_owner.py use.
MCP_MANAGER_INTERNALS: tuple[str, ...] = (
    "_connections", "_tools", "_sessions", "_stacks", "_connect_tasks",
    "_resource_connections", "_generation", "_owner_tasks", "_do_call", "_connect_stdio",
)


# ------------------------------------------------------------------ MCP manager state
def mcp_connections(manager: Any) -> dict:
    """``McpManager._connections``: ``{server_id: {"status", "name", ...}}``, or ``{}``."""
    return getattr(manager, "_connections", {}) or {}


def mcp_tools(manager: Any) -> dict:
    """``McpManager._tools``: ``{server_id: [tool dicts]}``, or ``{}``."""
    return getattr(manager, "_tools", {}) or {}


def load_mcp_disabled_map() -> Any:
    """``src.agent_loop._load_mcp_disabled_map()``: ``{server_id: [disabled tool names]}``."""
    from src.agent_loop import _load_mcp_disabled_map

    return _load_mcp_disabled_map()


# ------------------------------------------------------------------- auth and owners
def scope_owner(request: Any, scopes: Any) -> Any:
    """``routes.codex_routes._scope_owner``: the owner of an API token holding one of
    ``scopes`` (raises the route's 403 otherwise)."""
    from routes.codex_routes import _scope_owner

    return _scope_owner(request, scopes)


def authority_owner(owner: Any) -> Any:
    """``src.agent_runtime.authority._owner``: the owner as a request authority stores it."""
    from src.agent_runtime.authority import _owner

    return _owner(owner)


def private_action_reads() -> Any:
    """``src.tool_capabilities._PRIVATE_ACTION_READS``: ``{tool: read actions}``."""
    from src.tool_capabilities import _PRIVATE_ACTION_READS

    return _PRIVATE_ACTION_READS


# -------------------------------------------------------------------- models, hosts
def is_local_or_private_host(host: str) -> bool:
    """``src.model_context``'s own test for a host on this machine or network
    (``_LOCAL_HOSTS``, ``_is_private_ip_literal``, ``_in_tailscale_range``)."""
    from src.model_context import (
        _LOCAL_HOSTS,
        _in_tailscale_range,
        _is_private_ip_literal,
    )

    return host in _LOCAL_HOSTS or _is_private_ip_literal(host) or _in_tailscale_range(host)


def endpoint_enabled_models(endpoint: Any) -> Any:
    """``src.endpoint_resolver._endpoint_enabled_models(endpoint)``."""
    from src.endpoint_resolver import _endpoint_enabled_models

    return _endpoint_enabled_models(endpoint)


def resolve_model(spec: Any, owner: Any = None) -> Any:
    """``src.ai_interaction._resolve_model(spec, owner=owner)``."""
    from src.ai_interaction import _resolve_model

    return _resolve_model(spec, owner=owner)


def memory_vector() -> Any:
    """The memory index Odysseus loaded (``src.ai_interaction._memory_vector``), or None
    when ``src.ai_interaction`` isn't loaded (never imports it)."""
    ai = sys.modules.get("src.ai_interaction")
    return getattr(ai, "_memory_vector", None) if ai is not None else None


def vector_lanes(store: Any) -> list:
    """A ``MemoryVectorStore``'s embedding lanes (``_lanes``), or ``[]``."""
    return getattr(store, "_lanes", None) or []


def chat_stream_active() -> bool:
    """``src.interactive_gate._has_active_chat_stream()``: a chat or agent run is
    generating (False when upstream has no such check)."""
    from src import interactive_gate

    return bool(getattr(interactive_gate, "_has_active_chat_stream", lambda: False)())


# ------------------------------------------------------------------------- calendar
def expand_rrule(*args: Any, **kwargs: Any) -> Any:
    """``routes.calendar_routes._expand_rrule``: a recurring event's occurrences."""
    from routes.calendar_routes import _expand_rrule

    return _expand_rrule(*args, **kwargs)


def checkin_calendar_events(*args: Any, **kwargs: Any) -> Any:
    """``src.task_scheduler._checkin_calendar_events``: the owner's events in a window."""
    from src.task_scheduler import _checkin_calendar_events

    return _checkin_calendar_events(*args, **kwargs)


def resolve_task_timezone(*args: Any, **kwargs: Any) -> Any:
    """``src.task_scheduler._resolve_task_timezone``: a task's crew timezone."""
    from src.task_scheduler import _resolve_task_timezone

    return _resolve_task_timezone(*args, **kwargs)


def fetch_contacts(*args: Any, **kwargs: Any) -> Any:
    """``routes.contacts.contacts_routes._fetch_contacts``: the owner's contacts."""
    from routes.contacts.contacts_routes import _fetch_contacts

    return _fetch_contacts(*args, **kwargs)


# ------------------------------------------------------------------------ the brain
def chat_active_streams() -> dict:
    """``routes.chat_routes._active_streams``: ``{session_id: stream record}`` of the chat
    turns streaming now (background extraction waits while its chat is in it)."""
    from routes.chat_routes import _active_streams

    return _active_streams


# name -> (upstream module, private attribute) for every function above that imports one
PRIVATE_NAMES: dict[str, tuple[str, str]] = {
    "load_mcp_disabled_map": ("src.agent_loop", "_load_mcp_disabled_map"),
    "scope_owner": ("routes.codex_routes", "_scope_owner"),
    "authority_owner": ("src.agent_runtime.authority", "_owner"),
    "private_action_reads": ("src.tool_capabilities", "_PRIVATE_ACTION_READS"),
    "is_local_or_private_host:hosts": ("src.model_context", "_LOCAL_HOSTS"),
    "is_local_or_private_host:ip": ("src.model_context", "_is_private_ip_literal"),
    "is_local_or_private_host:tailscale": ("src.model_context", "_in_tailscale_range"),
    "endpoint_enabled_models": ("src.endpoint_resolver", "_endpoint_enabled_models"),
    "resolve_model": ("src.ai_interaction", "_resolve_model"),
    "memory_vector": ("src.ai_interaction", "_memory_vector"),
    "chat_stream_active": ("src.interactive_gate", "_has_active_chat_stream"),
    "expand_rrule": ("routes.calendar_routes", "_expand_rrule"),
    "checkin_calendar_events": ("src.task_scheduler", "_checkin_calendar_events"),
    "resolve_task_timezone": ("src.task_scheduler", "_resolve_task_timezone"),
    "fetch_contacts": ("routes.contacts.contacts_routes", "_fetch_contacts"),
    "chat_active_streams": ("routes.chat_routes", "_active_streams"),
}
