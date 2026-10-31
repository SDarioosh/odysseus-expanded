"""Fork: the brain's fixed tool set (docs/fork/brain.md, "Tool set").

The set is decided here, never re-picked per message: the groups named by
``FORK_BRAIN_TOOL_GROUPS``, in :data:`GROUPS` order, each tool in its listed order. The
schemas come from upstream's ``src/tool_schemas.py`` (``FUNCTION_TOOL_SCHEMAS``), from the
MCP manager for Athena's tools (server ``convo``, or ``athena``: ``fork/athena/client.py``)
and from :data:`SEARCH_BELIEFS_SCHEMA` for the fork's own ``search_beliefs``.

The first resolved set of a chat is kept for that chat (:func:`for_chat`), so every
message of a chat is offered the same tools in the same order; a slash skill
(``/athena``, ``/context``) adds its declared tools for that one turn
(:func:`with_skill_tools`).

Left out whatever the groups say: tools the owner may not use (upstream's
``blocked_tools_for_owner``: non-admins), tools disabled for the turn (privileges, the
admin's global list), and everything in :data:`NEVER` (shell, files, model serving,
endpoints, tokens, MCP, settings, webhooks, ``api_call``, ``app_api``; deep research).

Read or write (:func:`is_read`) reuses upstream's and the fork's classification: the read
actions of the personal-store tools (``fork/routing/personal_read_grants.py``, upstream's
``_PRIVATE_ACTION_READS``) and the trusted read-only MCP tools (``fork/mcp/trust.py``).
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

SEARCH_BELIEFS = "search_beliefs"
ATHENA_SERVER_KEY = "convo"  # fork/athena/client.server_id also finds "athena"

# group -> tools, in the order they are offered
GROUPS: dict[str, tuple[str, ...]] = {
    "web": ("web_search", "web_fetch", "get_weather"),
    "athena": ("search_conversations", "search_transcripts", "get_conversation",
               "get_transcript", "list_action_items", "list_facts", "get_daily_digest",
               "list_people", "update_action_item"),
    # No manage_tasks: that is upstream's scheduled-prompt tool. The owner's to-dos,
    # reminders and checklists are notes (manage_notes).
    "workspace": ("manage_calendar", "manage_notes", "resolve_contact", "manage_contact",
                  "manage_memory"),
    "beliefs": (SEARCH_BELIEFS,),
    "chats": ("search_chats",),
    "email": ("list_email_accounts", "list_emails", "search_emails", "read_email",
              "draft_email", "draft_email_reply", "send_email", "reply_to_email"),
    "images": ("generate_image",),
}
ATHENA_GROUP = "athena"

# Tools that only read (built-in names). Athena's tools are read when the trusted
# read-only MCP rule says so; the personal stores below read or write by action.
READ_TOOLS = frozenset({
    "web_search", "web_fetch", "get_weather", "resolve_contact", "search_chats",
    "list_email_accounts", "list_emails", "search_emails", "read_email", SEARCH_BELIEFS,
})
ACTION_TOOLS = frozenset({
    "manage_calendar", "manage_tasks", "manage_notes", "manage_contact", "manage_memory",
})

NEVER = frozenset({
    "bash", "host_shell", "python", "read_file", "write_file", "edit_file", "apply_patch",
    "grep", "glob", "ls", "get_workspace", "manage_bg_jobs", "download_model",
    "serve_model", "serve_preset", "stop_served_model", "cancel_download",
    "list_served_models", "manage_endpoints", "manage_tokens", "manage_mcp",
    "manage_settings", "manage_webhooks", "api_call", "app_api", "trigger_research",
    "manage_research",
})

SEARCH_BELIEFS_SCHEMA = {
    "type": "function",
    "function": {
        "name": SEARCH_BELIEFS,
        "description": (
            "Search what is known about the owner and the people in their life: facts "
            "confirmed across recorded conversations, notes and memory (where someone "
            "lives or works, preferences, relationships, plans). Returns the best "
            "matches with links to their sources; cite the link next to each fact."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, in a few words."},
                "limit": {"type": "integer", "description": "Most results (1-10, default 5)."},
            },
            "required": ["query"],
        },
    },
}

_CHAT_SETS: OrderedDict[str, tuple[dict, ...]] = OrderedDict()
_CHAT_SETS_MAX = 512
_LOCK = threading.Lock()


def _manager(manager: Any = None) -> Any:
    if manager is not None:
        return manager
    try:
        from src.tool_execution import get_mcp_manager
        return get_mcp_manager()
    except Exception:  # noqa: BLE001 -- no MCP: the built-in tools only
        return None


def athena_server(manager: Any = None) -> str | None:
    """The connected Athena MCP server's id, or None."""
    from fork.athena.client import server_id
    from fork.common import settings

    manager = _manager(manager)
    if manager is None:
        return None
    return server_id(manager, settings.athena_server() or ATHENA_SERVER_KEY)


def _athena_schemas(manager: Any) -> dict[str, dict]:
    """``{bare tool name: OpenAI schema}`` of the Athena server's enabled tools."""
    sid = athena_server(manager)
    if not sid:
        return {}
    try:
        from fork.upstream import load_mcp_disabled_map
        disabled = load_mcp_disabled_map()
    except Exception:  # noqa: BLE001
        disabled = {}
    prefix = f"mcp__{sid}__"
    out = {}
    for schema in manager.get_all_openai_schemas(disabled):
        name = schema.get("function", {}).get("name", "")
        if name.startswith(prefix):
            out[name[len(prefix):]] = schema
    return out


def _builtin_schemas() -> dict[str, dict]:
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    return {s["function"]["name"]: s for s in FUNCTION_TOOL_SCHEMAS}


def resolve(*, owner: Any, disabled: set[str] | frozenset[str] = frozenset(),
            groups: tuple[str, ...] | None = None, manager: Any = None) -> tuple[dict, ...]:
    """The tool schemas for ``owner``, in the fixed order (see the module doc)."""
    from src.tool_security import blocked_tools_for_owner

    if groups is None:
        from fork.brain.settings import tool_groups
        groups = tool_groups()
    blocked = set(blocked_tools_for_owner(owner)) | set(disabled or ())
    builtin = _builtin_schemas()
    manager = _manager(manager)
    out: list[dict] = []
    for group in GROUPS:
        if group not in groups:
            continue
        if group == ATHENA_GROUP:
            athena = _athena_schemas(manager) if manager is not None else {}
            for bare in GROUPS[group]:
                schema = athena.get(bare)
                if schema is None:
                    continue
                name = schema["function"]["name"]
                if name in blocked or owner_blocks_mcp(owner):
                    continue
                out.append(schema)
            continue
        for name in GROUPS[group]:
            if name in NEVER or name in blocked:
                continue
            if name == SEARCH_BELIEFS:
                out.append(SEARCH_BELIEFS_SCHEMA)
            elif name in builtin:
                out.append(builtin[name])
    return tuple(out)


def owner_blocks_mcp(owner: Any) -> bool:
    """Non-admin owners never reach MCP tools (upstream's public-user policy)."""
    from src.tool_security import is_public_blocked_tool, owner_is_admin_or_single_user
    return not owner_is_admin_or_single_user(owner) and is_public_blocked_tool("mcp__x__y")


def for_chat(session_id: str, *, owner: Any, disabled=frozenset(), manager: Any = None,
             groups: tuple[str, ...] | None = None) -> tuple[dict, ...]:
    """This chat's tool set: resolved on its first brain turn, then the same each turn.

    Kept in memory per chat (a restart resolves it again, from the same settings). A
    tool disabled since (privileges, global list) is dropped from the kept set; a set
    that came out empty (MCP not yet connected) is resolved again next turn. MCP tools
    are dropped for an owner who may not use them any more (no longer an admin).
    """
    key = str(session_id or "")
    with _LOCK:
        kept = _CHAT_SETS.get(key)
        if kept is not None:
            _CHAT_SETS.move_to_end(key)
    if kept is None:
        kept = resolve(owner=owner, disabled=disabled, manager=manager, groups=groups)
        if kept and key:
            with _LOCK:
                _CHAT_SETS[key] = kept
                while len(_CHAT_SETS) > _CHAT_SETS_MAX:
                    _CHAT_SETS.popitem(last=False)
    off = set(disabled or ())
    # checked every turn: an owner who lost admin since the set was kept loses MCP too
    no_mcp = any(s["function"]["name"].startswith("mcp__") for s in kept) \
        and owner_blocks_mcp(owner)
    return tuple(s for s in kept if s["function"]["name"] not in off
                 and not (no_mcp and s["function"]["name"].startswith("mcp__")))


def forget_chat(session_id: str) -> None:
    with _LOCK:
        _CHAT_SETS.pop(str(session_id or ""), None)


def names(schemas) -> list[str]:
    return [s["function"]["name"] for s in schemas]


def with_skill_tools(schemas: tuple[dict, ...], message: Any, *, owner: Any,
                     disabled=frozenset(), manager: Any = None) -> tuple[dict, ...]:
    """``schemas`` plus the tools a slash-invoked skill declares, for this turn only.

    Uses the fork's ``requires_toolsets`` handling (``fork/routing/skill_invocation.py``,
    ``skill_mcp_toolsets.py``); :data:`NEVER`, owner blocks and disables still apply.
    """
    from fork.routing.skill_invocation import parse_invoke_message
    invoked = parse_invoke_message(message)
    if invoked is None:
        return schemas
    try:
        from fork.routing.skill_invocation import _invokable_skill
        from fork.routing.skill_mcp_toolsets import expand_toolsets
        skill = _invokable_skill(invoked.name, owner)
        required = list((skill or {}).get("requires_toolsets") or [])
        if not required:
            return schemas
        manager = _manager(manager)
        wanted, _missing = expand_toolsets(required, manager)
    except Exception:  # noqa: BLE001 -- a broken skill adds nothing
        logger.debug("brain: slash skill tools unavailable", exc_info=True)
        return schemas
    from src.tool_security import blocked_tools_for_owner
    blocked = set(blocked_tools_for_owner(owner)) | set(disabled or ())
    have = set(names(schemas))
    extra: list[dict] = []
    builtin = _builtin_schemas()
    mcp = {}
    if manager is not None and any(str(n).startswith("mcp__") for n in wanted):
        try:
            from fork.upstream import load_mcp_disabled_map
            mcp = {s["function"]["name"]: s
                   for s in manager.get_all_openai_schemas(load_mcp_disabled_map())}
        except Exception:  # noqa: BLE001
            mcp = {}
    for name in sorted(wanted):
        if name in have or name in NEVER or name in blocked:
            continue
        if name.startswith("mcp__"):
            if owner_blocks_mcp(owner) or name not in mcp:
                continue
            extra.append(mcp[name])
        elif name in builtin and name not in {"ask_user", "update_plan"}:
            extra.append(builtin[name])
    return tuple(schemas) + tuple(extra)


def _action(name: str, content: Any) -> str | None:
    from src.agent_runtime.authority import ExactOperation
    try:
        return ExactOperation.normalize(name, content if isinstance(content, str) else "{}").action
    except (ValueError, TypeError):
        return None


def is_read(name: str, content: Any = "{}") -> bool:
    """True when this call only reads (runs without asking); else it is a write."""
    if name in READ_TOOLS:
        return True
    if name in ACTION_TOOLS:
        from fork.routing.personal_read_grants import store_read_actions
        return _action(name, content) in store_read_actions(name)
    if name.startswith("mcp__"):
        from fork.mcp.trust import is_trusted_readonly_mcp_tool
        return is_trusted_readonly_mcp_tool(name)
    return False
