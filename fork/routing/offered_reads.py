"""Fork: let the model read every personal store it is offered in a turn.

Upstream builds request authority from the words of one message: "what are my
tasks for today?" grants ``manage_tasks`` and nothing else. The model is often
shown more tools than that: the turn contract adds tools that ran earlier in
the chat, and the run that resumes after the chat approval card, like a
"Regular tools" model, has no contract and sees every enabled tool. So the
model was shown ``manage_calendar``, called it, and got "The exact operation
is disabled by user or server request authority policy" or "... outside
server request authority". Several tools in a row failed after the first.

``with_offered_read_grants`` closes that gap for reads only: for each personal
store tool offered on the turn, the authority also admits that tool's
read-only actions (upstream's ``_PRIVATE_ACTION_READS``: ``list_events``,
``list``, ``search``, ``view`` ...). Writes (create, edit, delete, run) still
need the request words or an approval. ``offered_read_tools`` says what counts
as offered; user, owner, global, incognito and plan-mode disables still win.
Hook: ``routes/chat_routes.py``, after the final turn policy.
"""

from __future__ import annotations

from dataclasses import replace

# Personal stores whose read actions are safe to admit whenever offered. Admin
# and session tools (settings, tokens, MCP, endpoints, sessions) stay out.
OFFERED_READ_TOOLS = frozenset({
    "manage_calendar",
    "manage_tasks",
    "manage_notes",
    "manage_memory",
    "manage_documents",
    "manage_skills",
})


def read_actions(tool: str) -> frozenset[str]:
    from src.tool_capabilities import _PRIVATE_ACTION_READS
    if tool not in OFFERED_READ_TOOLS:
        return frozenset()
    return frozenset(_PRIVATE_ACTION_READS.get(tool, ()))


def _merged_grant(grant, reads):
    """One grant admitting what ``grant`` did plus ``reads``, or None to keep it."""
    from src.agent_runtime.authority import ExactOperation, OperationGrant
    if grant.actions is None and grant.inputs is None:
        return None  # already unrestricted
    if grant.inputs is not None:
        # A pinned exact read ("list my tasks" -> {"action":"list"}). Widen it
        # to the read actions only when every pinned input is itself a read.
        actions = set()
        for value in grant.inputs:
            try:
                actions.add(ExactOperation.normalize(grant.tool, value).action)
            except (ValueError, TypeError):
                return None
        if not actions <= reads:
            return None
        if grant.actions is not None and not actions <= grant.actions:
            return None
        return OperationGrant(grant.tool, frozenset(reads))
    if reads <= grant.actions:
        return None
    return OperationGrant(grant.tool, grant.actions | reads)


def offered_read_tools(turn_contract, tool_policy=None, disabled_tools=(), owner=None) -> frozenset[str]:
    """Personal store tools the model is offered on this turn.

    With a turn contract, the tools it offers (resolution already applied user,
    owner and global disables). Without one (the turn resumed after an approval
    card, or a "Regular tools" model) the model sees every enabled tool, so
    every personal store tool that the turn's policy, disabled set and the
    owner's restrictions leave on.
    """
    from src.agent_runtime.authority import canonical_tool
    if turn_contract is not None:
        return frozenset(canonical_tool(str(n)) for n in turn_contract.offered) & OFFERED_READ_TOOLS
    off = {canonical_tool(str(n)) for n in disabled_tools or ()}
    if tool_policy is not None:
        if tool_policy.block_all_tool_calls:
            return frozenset()
        off |= {canonical_tool(n) for n in tool_policy.all_disabled_names()}
    if owner:
        from src.tool_security import blocked_tools_for_owner
        off |= {canonical_tool(n) for n in blocked_tools_for_owner(owner)}
    return OFFERED_READ_TOOLS - off


def with_offered_read_grants(authority, turn_contract, tool_policy=None, disabled_tools=(), owner=None):
    """Return ``authority`` that also admits read actions of offered personal tools."""
    from src.agent_runtime.authority import OperationGrant, RequestAuthority
    if not isinstance(authority, RequestAuthority) or authority.block_all:
        return authority
    tools = sorted(offered_read_tools(turn_contract, tool_policy, disabled_tools, owner))
    if not tools:
        return authority
    by_tool = {g.tool: g for g in authority.grants}
    added = []
    for tool in tools:
        reads = read_actions(tool)
        if not reads:
            continue
        current = by_tool.get(tool)
        if current is None or tool in authority.denied:
            # A denied tool comes back for its reads only, whatever it had.
            by_tool[tool] = OperationGrant(tool, reads)
        else:
            merged = _merged_grant(current, reads)
            if merged is None:
                continue
            by_tool[tool] = merged
        added.append(tool)
    if not added:
        return authority
    grants = tuple(by_tool[g.tool] for g in authority.grants) + tuple(
        by_tool[t] for t in added if t not in {g.tool for g in authority.grants})
    # A resumed task carries the first message's turn denials; this turn
    # offers these tools, so that stale denial no longer applies.
    denied = authority.denied - frozenset(added)
    # Inherited authorities only reach backends and owned records sealed in
    # them, so seal the added tools' local backends and owned scopes too.
    from src.agent_runtime.remote_resources import seal_backends
    from src.agent_runtime.resources import seal_owned_scopes
    backends = authority.backend_resources
    if backends is not None:
        extra = [b for b in seal_backends(added, owner=authority.owner) if b not in backends]
        backends = (*backends, *extra)
    owned = authority.owned_scopes
    if owned is not None:
        extra = [s for s in seal_owned_scopes(authority.owner, authority.session_id, added)
                 if s not in owned]
        owned = (*owned, *extra)
    return replace(authority, grants=grants, denied=denied,
                   backend_resources=backends, owned_scopes=owned)
