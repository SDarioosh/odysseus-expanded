"""Fork: the brain runs its tool calls through upstream's tool runner
(docs/fork/brain.md, "Running tools safely").

Every built-in and MCP call goes through upstream's ``execute_tool_block``
(``src/tool_execution.py``) with a request authority from ``create_request_authority``
(``src/agent_runtime/authority.py``) and a ``ToolRunSecurityContext``
(``src/tool_capabilities.py``), built from the same inputs ``chat_stream`` uses: the
owner, the chat, the request text, the chat's history, the turn's tool policy and
whether a bearer token drives the request. So owner scoping, admin-only tools, the public
user policy, path denial and the evidence ledger all stay upstream's.

What the brain adds is which calls run without asking and which ask:

- **Reads run:** the authority also grants the read tools of the brain's set and the
  read actions of the personal stores it offers (``toolset.is_read``), the way
  ``fork/routing/offered_reads.py`` does for an agent turn. Athena's ``readOnlyHint``
  tools are granted by upstream's authority already (the fork's trusted read-only MCP
  rule, ``fork/mcp/trust.py``).
- **Writes ask:** a write never runs on the model's word. :func:`needs_approval` says so
  for every write, and for any call upstream's approval gate refuses
  (``ToolRunSecurityContext.decision_for``); the loop then files an exact approval in
  upstream's ``tool_approval_store`` (:func:`create_approval`) and runs the call only with
  the consumed ``ExactToolApproval`` (exact admission in ``execute_tool_block``).

The one tool upstream doesn't have, ``search_beliefs``, runs here, owner-scoped
(``fork/beliefs/search.py``). Before ``web_search`` runs, its query passes through
``fork/brain/query_guard.py``; when nothing public is left, the search isn't run and the
model is told why.

Results are trimmed before they enter the prompt (:func:`trim`); the chat window gets
them in full (:func:`result_text`).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any

from fork.brain import toolset

logger = logging.getLogger(__name__)

PROMPT_RESULT_CHARS = 6000  # a tool result in the prompt, web pages included
WINDOW_RESULT_CHARS = 20000
EMPTY_QUERY_RESULT = (
    "The search was not run: after removing private details (names, contact details, "
    "event titles) nothing was left to search for. Search only for the public part of "
    "the question, without names or personal details, or answer without the web.")


@dataclass
class ToolRun:
    """What every call of one brain turn runs with."""
    owner: Any
    session_id: str
    authority: Any
    security: Any
    policy: Any
    disabled: frozenset[str]
    offered: tuple[str, ...]
    request_text: str = ""
    external_seen: bool = False
    seen: dict = field(default_factory=dict)


def turn_disabled_tools(request: Any, owner: Any, *, privileges: dict | None = None,
                        delegated: bool = False) -> set[str]:
    """The tools this request may not use, from the same inputs ``chat_stream`` reads:
    the user's privileges, the admin's global list, the owner's public-user policy and,
    for a bearer token, the delegated-credential list."""
    from src.tool_security import blocked_tools_for_owner, delegated_credential_blocked_tools

    disabled = set(blocked_tools_for_owner(owner))
    if delegated:
        disabled |= delegated_credential_blocked_tools()
    privs = privileges or {}
    if privs:
        if not privs.get("can_use_documents", True):
            disabled |= {"manage_documents", "create_document", "edit_document",
                         "update_document", "suggest_document"}
        if not privs.get("can_generate_images", True):
            disabled |= {"generate_image", "edit_image", "ai_edit_image"}
        if not privs.get("can_manage_memory", True):
            disabled |= {"manage_memory", "manage_skills"}
        if not privs.get("can_use_research", True):
            disabled |= {"trigger_research", "manage_research"}
    try:
        from src.settings import get_setting
        global_disabled = get_setting("disabled_tools", [])
        if isinstance(global_disabled, list):
            disabled |= {str(t) for t in global_disabled}
    except Exception:  # noqa: BLE001
        logger.debug("brain: global disabled tools unavailable", exc_info=True)
    return disabled


def _with_read_grants(authority: Any, offered: tuple[str, ...]) -> Any:
    """``authority`` that also admits the reads of the offered tools (nothing else)."""
    from src.agent_runtime.authority import ExactOperation, OperationGrant, canonical_tool

    from fork.routing.personal_read_grants import store_read_actions

    if getattr(authority, "block_all", False):
        return authority
    grants = {g.tool: g for g in authority.grants}
    for name in offered:
        if name.startswith("mcp__") or name == toolset.SEARCH_BELIEFS:
            continue  # MCP reads: upstream's trusted read-only grant; beliefs: run here
        tool = canonical_tool(name)
        if tool in authority.denied:
            continue
        if name in toolset.ACTION_TOOLS:
            reads = frozenset(store_read_actions(tool))
            if not reads:
                continue
            current = grants.get(tool)
            if current is None:
                grants[tool] = OperationGrant(tool, reads)
            elif current.inputs is not None:
                actions = set()
                for value in current.inputs:
                    try:
                        actions.add(ExactOperation.normalize(tool, value).action)
                    except (ValueError, TypeError):
                        actions.add(None)
                if actions <= reads:
                    grants[tool] = OperationGrant(tool, reads)
            elif current.actions is not None:
                grants[tool] = OperationGrant(tool, current.actions | reads)
        elif name in toolset.READ_TOOLS and tool not in grants:
            grants[tool] = OperationGrant(tool)
    # None asks the authority's own constructor to seal the backends and owned scopes
    # of every grant again (it does so from the grants, for this owner and chat).
    return replace(authority, grants=tuple(grants.values()), backend_resources=None,
                   owned_scopes=None)


def build_run(*, request_text: str, owner: Any, session_id: str, history: list,
              offered: tuple[str, ...], disabled: set[str], delegated: bool = False,
              prompt_messages: list[dict] | None = None, authority: Any = None) -> ToolRun:
    """The authority, security context and policy for one brain turn.

    ``authority``: the sealed authority of a paused turn (an approval resume); otherwise
    a new one is created from ``request_text``.
    """
    from src.agent_runtime.authority import create_request_authority
    from src.tool_capabilities import ToolRunSecurityContext
    from src.tool_policy import build_effective_tool_policy

    from fork.routing.auto_mode import bind_run

    policy = build_effective_tool_policy(disabled_tools=disabled, last_user_message=request_text)
    if authority is None:
        authority = create_request_authority(
            request_text, owner=owner, session_id=session_id, workspace=None,
            history=history, policy=policy)
        authority = _with_read_grants(authority, offered).restrict(policy)
    security = ToolRunSecurityContext(delegated_credential=bool(delegated))
    bind_run(security, owner=owner, session_id=session_id)
    security.observe_messages(prompt_messages or ())
    return ToolRun(owner=owner, session_id=str(session_id), authority=authority,
                   security=security, policy=policy, disabled=frozenset(policy.all_disabled_names()),
                   offered=tuple(offered), request_text=request_text)


# ------------------------------------------------------------------ deciding to ask
def needs_approval(run: ToolRun, name: str, content: str) -> str | None:
    """Why this call must ask the owner first, or None when it runs directly."""
    if name not in run.offered:
        return None  # refused by execute(), never asked
    decision = run.security.decision_for(name, content)
    if not decision.allowed:
        if run.security.delegated_credential:
            return None  # a token can't approve; execute_tool_block refuses it
        return decision.reason or "This action needs your approval."
    if not toolset.is_read(name, content):
        return "This changes your data, so it needs your approval."
    return None


def create_approval(run: ToolRun, name: str, content: str, *, continuation_query: str) -> Any:
    """File an exact approval for this call in upstream's store; returns the pending one."""
    from src.tool_approvals import tool_approval_store
    from src.tool_capabilities import capabilities_for_action

    return tool_approval_store.create(
        owner=run.owner, session_id=run.session_id, origin_run_id=run.security.run_id,
        tool_name=name, content=content, workspace=None,
        external_untrusted_context_seen=run.security.external_untrusted_context_seen,
        selected_tools=set(run.offered) | {name},
        continuation_query=continuation_query,
        capabilities=capabilities_for_action(name, content),
        request_text=run.request_text, request_authority=run.authority)


# ----------------------------------------------------------------------- running
def _blocked(name: str, reason: str) -> tuple[str, dict]:
    return f"{name}: BLOCKED", {"error": reason, "exit_code": 1, "blocked": True}


async def _search_beliefs(run: ToolRun, content: str) -> tuple[str, dict]:
    from fork.beliefs.search import search_beliefs

    try:
        args = json.loads(content or "{}")
    except ValueError:
        args = {}
    query = str((args or {}).get("query") or "").strip() if isinstance(args, dict) else ""
    if not query:
        return "search_beliefs: no query", {"error": "Give a query.", "exit_code": 1}
    try:
        limit = max(1, min(10, int(args.get("limit") or 5)))
    except (TypeError, ValueError):
        limit = 5
    hits = await asyncio.to_thread(search_beliefs, run.owner, query, k=limit)
    lines = []
    for b in hits:
        name = b.get("subject_display") or b.get("subject") or ""
        text = " ".join(str(b.get("shown_text") or b.get("text") or "").split())
        link = str(b.get("link") or "").strip()
        lines.append("- " + (f"{name}: " if name else "") + text + (f" ({link})" if link else ""))
    output = "\n".join(lines) if lines else "No matching facts."
    return f"search_beliefs: {len(lines)} result(s)", {"output": output, "exit_code": 0}


async def _guarded_web_search(run: ToolRun, content: str) -> tuple[str, dict | None]:
    """``(content, None)`` with the cleaned query, or ``(content, result)`` when the
    search must not run."""
    try:
        args = json.loads(content or "{}")
    except ValueError:
        args = {"query": content}
    if not isinstance(args, dict):
        args = {"query": str(args)}
    query = str(args.get("query") or args.get("command") or "")
    from fork.brain.query_guard import guard_query  # lazy: its own file

    cleaned, removed = await asyncio.to_thread(guard_query, query, run.owner)
    if removed:
        logger.info("brain: web query guard removed %d term(s)", removed)
    if not str(cleaned or "").strip():
        return content, {"error": EMPTY_QUERY_RESULT, "exit_code": 1, "blocked": True,
                         "policy": "web_query_privacy"}
    if cleaned != query:
        args["query"] = cleaned
        args.pop("command", None)
        content = json.dumps(args, ensure_ascii=False)
    return content, None


async def execute(run: ToolRun, name: str, content: str, *, exact_approval: Any = None,
                  progress_cb=None) -> tuple[str, str, dict]:
    """Run one call; returns ``(content as run, description, result)``."""
    from src.tool_execution import execute_tool_block
    from src.tool_types import ToolBlock

    if name not in run.offered:
        desc, result = _blocked(name, f"Tool '{name}' is not available here.")
        return content, desc, result
    if name == toolset.SEARCH_BELIEFS:
        desc, result = await _search_beliefs(run, content)
        return content, desc, result
    if name == "web_search":
        content, refused = await _guarded_web_search(run, content)
        if refused is not None:
            return content, "web_search: NOT RUN", refused
    desc, result = await execute_tool_block(
        ToolBlock(name, content), session_id=run.session_id, disabled_tools=set(run.disabled),
        owner=run.owner, progress_cb=progress_cb, workspace=None, tool_policy=run.policy,
        security_context=run.security, exact_approval=exact_approval,
        client_runtime_context=None, request_authority=run.authority)
    if not isinstance(result, dict):
        result = {"output": str(result or "")}
    run.security.observe_tool_result(name, result, content)
    return content, desc, result


# ----------------------------------------------------------------------- results
def result_text(result: dict) -> str:
    """The text of a tool result, the way upstream's agent shows it."""
    for key in ("output", "stdout", "response", "results", "content"):
        value = result.get(key)
        if value not in (None, "", [], {}):
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if result.get("error"):
        return "Error: " + str(result["error"])
    return "(no output)"


def trim(text: str, limit: int = PROMPT_RESULT_CHARS) -> str:
    """``text`` cut to ``limit`` characters at a line break where it can be (long lists
    keep their first rows), with a note saying how much was left out."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = limit
    left = len(text) - cut
    return text[:cut].rstrip() + f"\n[… {left} more characters not shown]"
