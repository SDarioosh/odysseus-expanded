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
- **Fetches of known pages only:** ``web_fetch`` has no standing grant. A fetch runs
  without asking only when every URL is one the owner typed in this message, or a ranked
  web-search hit of this chat, exactly (also the hits of earlier turns, kept in
  ``history.py``; not other pages on a hit's site); it then gets a grant for that one exact
  call (:func:`_with_fetch_grant`). Any other URL asks, since a URL the model makes up
  can carry data out.
- **Writes ask:** a write never runs on the model's word. :func:`needs_approval` says so
  for every write, and for any call upstream's approval gate refuses
  (``ToolRunSecurityContext.decision_for``); the loop then files an exact approval in
  upstream's ``tool_approval_store`` (:func:`create_approval`) and runs the call only with
  the consumed ``ExactToolApproval`` (exact admission in ``execute_tool_block``, which
  takes one only for an armed run: the approval is filed armed and the loop arms the run
  before the approved call; arming only adds gates).
- **Outside content arms the gate:** the run's security context is armed when the route
  says the turn follows outside content, when an earlier exchange put back into the
  prompt was untrusted (``history.py``'s flag; the replayed results carry upstream's
  untrusted marking), and by upstream itself when a result of this turn is. ``tainted``
  records the last for this turn's exchange.

The tools upstream doesn't have run here, owner-scoped: ``search_beliefs``
(``fork/beliefs/search.py``) and ``search_documents`` (the owner's document library,
``fork/brain/documents.py``). A result of theirs with content is outside content: it arms
the run like an untrusted result of upstream's runner (:func:`distrust`). A
``create_document`` call runs in upstream's line format and its result names the new
document for the model (``documents.py``). Before ``web_search`` runs, its query passes
through
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

from fork.brain import documents as brain_documents
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
    owner_urls: frozenset[str] = frozenset()  # URLs the owner typed in this message
    tainted: bool = False  # a result of this turn was untrusted
    search_hits: set[str] = field(default_factory=set)  # ranked hits of this turn
    rag: Any = None  # the document library's rag_manager, when there is one


def turn_disabled_tools(request: Any, owner: Any, *, privileges: dict | None = None,
                        delegated: bool = False, rag: Any = None,
                        use_rag: bool = True) -> set[str]:
    """The tools this request may not use, from the same inputs ``chat_stream`` reads:
    the user's privileges, the admin's global list, the owner's public-user policy and,
    for a bearer token, the delegated-credential list. ``rag``: the document library
    (none: no ``search_documents``); ``use_rag``: the chat's documents toggle."""
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
    # Beliefs are memory: whoever upstream's policy keeps from manage_memory (non-admin
    # users, API tokens, the privilege, the global list) doesn't get search_beliefs.
    if "manage_memory" in disabled:
        disabled.add(toolset.SEARCH_BELIEFS)
    # The library is documents: whoever upstream keeps from manage_documents (the
    # document privilege, non-admin users, API tokens, the global list) doesn't get
    # search_documents; nor a chat with the documents toggle off, nor a hub without one.
    if "manage_documents" in disabled or rag is None or not use_rag:
        disabled.add(toolset.SEARCH_DOCUMENTS)
    return disabled


def _with_read_grants(authority: Any, offered: tuple[str, ...]) -> Any:
    """``authority`` that also admits the reads of the offered tools (nothing else)."""
    from src.agent_runtime.authority import ExactOperation, OperationGrant, canonical_tool

    from fork.routing.personal_read_grants import store_read_actions

    if getattr(authority, "block_all", False):
        return authority
    grants = {g.tool: g for g in authority.grants}
    for name in offered:
        if name.startswith("mcp__") or name in toolset.FORK_TOOLS:
            continue  # MCP reads: upstream's trusted read-only grant; fork tools: run here
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
        elif name in toolset.READ_TOOLS and tool not in grants and name != "web_fetch":
            grants[tool] = OperationGrant(tool)
    # web_fetch: no standing grant, not even one upstream's words gave (see the module doc)
    grants.pop(canonical_tool("web_fetch"), None)
    # None asks the authority's own constructor to seal the backends and owned scopes
    # of every grant again (it does so from the grants, for this owner and chat).
    return replace(authority, grants=tuple(grants.values()), backend_resources=None,
                   owned_scopes=None)


def build_run(*, request_text: str, owner: Any, session_id: str, history: list,
              offered: tuple[str, ...], disabled: set[str], delegated: bool = False,
              prompt_messages: list[dict] | None = None, authority: Any = None,
              external_seen: bool = False, chat_search_hits=(), rag: Any = None) -> ToolRun:
    """The authority, security context and policy for one brain turn.

    ``authority``: the sealed authority of a paused turn (an approval resume); otherwise
    a new one is created from ``request_text``. ``external_seen``: the turn follows
    outside content (the route's flag, an untrusted earlier exchange): the security
    context starts armed. ``chat_search_hits``: the ranked web-search hits of this
    chat's earlier turns, which ``web_fetch`` may read. ``rag``: the document library
    ``search_documents`` reads.
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
    security = ToolRunSecurityContext(external_untrusted_context_seen=bool(external_seen),
                                      delegated_credential=bool(delegated))
    bind_run(security, owner=owner, session_id=session_id)
    security.observe_messages(prompt_messages or ())
    _seed_search_hits(security, chat_search_hits)
    from fork.routing.auto_mode import _urls_in
    return ToolRun(owner=owner, session_id=str(session_id), authority=authority,
                   security=security, policy=policy, disabled=frozenset(policy.all_disabled_names()),
                   offered=tuple(offered), request_text=request_text,
                   external_seen=bool(security.external_untrusted_context_seen),
                   owner_urls=frozenset(_urls_in(request_text or "", bare=True)), rag=rag)


def _seed_search_hits(security: Any, hits) -> None:
    """Earlier turns' ranked hits count as this run's (``fork/routing/auto_mode.py``'s
    per-run URL state, read by ``web_turn_scope._search_hit`` and the auto-mode gate)."""
    from fork.routing.auto_mode import _add, _state, normalize_url

    urls = {u for u in (normalize_url(h) for h in hits or ()) if u}
    if urls:
        state = _state(security)
        _add(state.setdefault("search", set()), urls)
        _add(state["result"], urls)


# ------------------------------------------------------------------ web addresses
FETCH_ASKS = ("This opens a web address that is neither in your message nor a search "
              "result of this chat, and an address can carry data out, so it needs your "
              "approval.")


def fetch_known(run: ToolRun, content: Any) -> bool:
    """True when every URL of this ``web_fetch`` is one the owner typed in this message
    or a ranked web-search hit of this chat, exactly (``www.`` and a trailing ``/``
    aside). Not another page on a hit's site (``fork/routing/web_turn_scope.py`` allows
    that for upstream's agent): its path is made up by the model, and a page the search
    found could have asked for one that carries private data out."""
    from fork.routing.auto_mode import _host, _loose, _state, fetch_urls, normalize_url, public_host

    urls = fetch_urls(content)
    if not urls:
        return False
    own = {_loose(u) for u in run.owner_urls}
    hits = {_loose(u) for u in (_state(run.security).get("search") or ())}
    for url in urls:
        normalized = normalize_url(url)
        if normalized is None:
            return False
        loose = _loose(normalized)
        if loose in own:
            continue
        if not (public_host(_host(normalized)) and loose in hits):
            return False
    return True


def _with_fetch_grant(authority: Any, content: str) -> Any:
    """``authority`` plus a grant for this one exact ``web_fetch`` call (the shape of
    ``web_turn_scope.with_search_hit_fetch``); unchanged when it can't be granted."""
    from src.agent_runtime.authority import (
        ExactOperation, OperationGrant, RequestAuthority, canonical_tool,
    )

    if not isinstance(authority, RequestAuthority) or authority.block_all:
        return authority
    fetch = canonical_tool("web_fetch")
    try:
        operation = ExactOperation.normalize(fetch, content)
    except (ValueError, TypeError):
        return authority
    if authority.restricted(operation) or authority.permits(operation):
        return authority
    from src.agent_runtime.remote_resources import resolve_backend
    try:
        backend = resolve_backend(fetch, admission=True, owner=authority.owner)
    except (ValueError, TypeError, AttributeError):
        return authority
    backends = authority.backend_resources
    if backends is not None and backend not in backends:
        backends = (*backends, backend)
    grant = OperationGrant(fetch, inputs=frozenset({operation.input}))
    return replace(authority, grants=(*authority.grants, grant), backend_resources=backends)


# ------------------------------------------------------------------ deciding to ask
def needs_approval(run: ToolRun, name: str, content: str) -> str | None:
    """Why this call must ask the owner first, or None when it runs directly."""
    if name not in run.offered or brain_documents.refusal(name, content):
        return None  # refused by execute(), never asked
    if name == "web_fetch" and not run.security.delegated_credential \
            and not fetch_known(run, content):
        # Checked before upstream's gate, so neither "allow for this task" nor an
        # unarmed run lets a made-up address through. A token run isn't asked: it has
        # no grant for the call and execute_tool_block refuses it.
        return FETCH_ASKS
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
        # Armed: execute_tool_block takes an exact approval only for an armed run and an
        # armed record (the loop arms the run before the approved call). Arming only
        # adds gates, and every brain approval is a call the owner reviewed.
        external_untrusted_context_seen=True,
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
    # beliefs come from recorded conversations, notes and memory: outside content, as
    # upstream counts Athena's and the notes' results
    return f"search_beliefs: {len(lines)} result(s)", {"output": output, "exit_code": 0,
                                                       "untrusted_content": True}


def distrust(run: ToolRun, source: str) -> None:
    """A fork tool returned outside content: the run is armed, as upstream's runner arms
    it for an untrusted result, and this turn's exchange is replayed untrusted."""
    run.tainted = True
    run.security.external_untrusted_context_seen = True
    if source not in run.security.external_sources:
        run.security.external_sources.append(source)


TIME_FILTERS = frozenset({"day", "week", "month", "year"})
EMPTY_LOCATION_RESULT = (
    "The weather was not looked up: after removing private details (names, contact "
    "details, addresses) no place was left. Ask for a city or region only.")


def _args(content: str, key: str) -> dict:
    try:
        args = json.loads(content or "{}")
    except ValueError:
        args = {key: content}
    return args if isinstance(args, dict) else {key: str(args)}


async def _guard(text: str, owner: Any) -> str:
    from fork.brain.query_guard import guard_query  # lazy: its own file

    cleaned, removed = await asyncio.to_thread(guard_query, text, owner)
    if removed:
        logger.info("brain: web query guard removed %d term(s)", removed)
    return str(cleaned or "").strip()


async def _guarded_web_search(run: ToolRun, content: str) -> tuple[str, dict | None]:
    """``(content, None)`` with the cleaned query, or ``(content, result)`` when the
    search must not run. The call is always written again with only the known fields
    (``query``, and ``time_filter`` when valid), so no other key can carry text past
    the guard into upstream's fallbacks."""
    args = _args(content, "query")
    query = str(args.get("query") or args.get("command") or "")
    cleaned = await _guard(query, run.owner)
    if not cleaned:
        return content, {"error": EMPTY_QUERY_RESULT, "exit_code": 1, "blocked": True,
                         "policy": "web_query_privacy"}
    out = {"query": cleaned}
    time_filter = str(args.get("time_filter") or "").strip().lower()
    if time_filter in TIME_FILTERS:
        out["time_filter"] = time_filter
    return json.dumps(out, ensure_ascii=False), None


async def _guarded_weather(run: ToolRun, content: str) -> tuple[str, dict | None]:
    """``get_weather``'s location through the same guard; written again as
    ``{"location": ...}`` only."""
    args = _args(content, "location")
    cleaned = await _guard(str(args.get("location") or ""), run.owner)
    if not cleaned:
        return content, {"error": EMPTY_LOCATION_RESULT, "exit_code": 1, "blocked": True,
                         "policy": "web_query_privacy"}
    return json.dumps({"location": cleaned}, ensure_ascii=False), None


async def execute(run: ToolRun, name: str, content: str, *, exact_approval: Any = None,
                  progress_cb=None) -> tuple[str, str, dict]:
    """Run one call; returns ``(content as run, description, result)``."""
    from src.tool_execution import execute_tool_block
    from src.tool_types import ToolBlock

    refused = None if name in run.offered else f"Tool '{name}' is not available here."
    refused = refused or brain_documents.refusal(name, content)
    if refused:
        desc, result = _blocked(name, refused)
        return content, desc, result
    if name in toolset.FORK_TOOLS:
        if name == toolset.SEARCH_BELIEFS:
            desc, result = await _search_beliefs(run, content)
        else:
            desc, result = await brain_documents.search(run.owner, run.rag, content)
        if result.pop("untrusted_content", False):
            distrust(run, name)
        return content, desc, result
    if name in ("web_search", "get_weather"):
        guard = _guarded_web_search if name == "web_search" else _guarded_weather
        content, refused = await guard(run, content)
        if refused is not None:
            return content, f"{name}: NOT RUN", refused
    authority = run.authority
    if name == "web_fetch" and exact_approval is None and fetch_known(run, content):
        authority = _with_fetch_grant(authority, content)
    desc, result = await execute_tool_block(
        ToolBlock(name, content), session_id=run.session_id, disabled_tools=set(run.disabled),
        owner=run.owner, progress_cb=progress_cb, workspace=None, tool_policy=run.policy,
        security_context=run.security, exact_approval=exact_approval,
        client_runtime_context=None, request_authority=authority)
    if not isinstance(result, dict):
        result = {"output": str(result or "")}
    brain_documents.describe(name, result)
    run.security.observe_tool_result(name, result, content)
    from src.tool_capabilities import tool_result_should_arm_gate
    if tool_result_should_arm_gate(name, result, content):
        run.tainted = True
    if name == "web_search":
        from fork.routing.auto_mode import _search_hits
        run.search_hits |= _search_hits(result)
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
