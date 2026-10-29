"""Fork: keep web lookups working when the web toggle is on.

Upstream decides which tools a turn may run from the words of the message
(request authority). Two gaps made web lookups fail in chat:

1. ``model_lookup_wants_web``: "search mistral 7b vs qwen3.5-9b" (and even
   "search the web for ...") matched the Hugging Face model-discovery rule, so
   the turn was granted ``search_hf_models`` only. A comparison or an explicit
   web word asks for information about the models, not for a catalog entry,
   so the rule now leaves those clauses to the normal web routing.
   Hook: ``src/turn_contract.py`` (model-discovery rule).

2. ``with_unclassified_web_search``: a message with no action words
   ("mistral 7b vs qwen3.5-9b") got no tool grants at all, yet with the web
   toggle on the model was shown ``web_search``, called it, and was blocked
   ("The exact operation is outside server request authority"). When the user
   has turned web search on and the words name no tool, the turn now gets a
   ``web_search`` grant. Only ``web_search``: no fetch or browser, so a search
   result can't steer the model to an arbitrary URL. Toggle off, policy and
   per-tool disables still win.
   Hook: ``routes/chat_routes.py``, after the request authority is created.

3. ``with_search_hit_fetch``: a turn that may search could not read any page
   the search found. "Compare the fanatik nex e1 pro and e1 max" got
   ``web_search`` from (2), the search returned the product pages, and every
   ``web_fetch`` of them was blocked as outside request authority, so the
   answer rested on snippets. Now a turn that holds ``web_search`` may also
   ``web_fetch`` a page this run's search returned: a ranked hit (``www.``
   and a trailing ``/`` don't count), or a page with no query string and a
   short path on a hit's site (on GitHub only inside the same owner/repo).
   Links inside fetched pages don't count, so a page can't steer the model to
   a new site. The grant covers that one exact call. Policy, per-tool
   disables and the approval gate still apply.
   Hook: ``src/tool_execution.py``, where request authority admits a call.
"""

from __future__ import annotations

import re
from dataclasses import replace

# Words that turn "search <model> ..." into a question about the model rather
# than a request for a Hugging Face catalog entry.
_WEB_OR_COMPARISON = re.compile(
    r"\b(?:vs\.?|versus|compar\w*|benchmarks?|web|internet|online|google)\b",
    re.I,
)

# Tools that never need more than the turn's own words to be useful.
_ALWAYS_GRANTED = frozenset({"ask_user", "update_plan"})


def model_lookup_wants_web(clause: str) -> bool:
    """True when a model-discovery clause asks for web information instead."""
    return bool(_WEB_OR_COMPARISON.search(str(clause or "")))


def _granted_tools(authority) -> set[str]:
    return {g.tool for g in getattr(authority, "grants", ()) or ()}


def with_unclassified_web_search(authority, *, web_search_enabled: bool,
                                 client_runtime_context=None):
    """Grant ``web_search`` to a turn whose words named no tool, if web is on."""
    if authority is None or not web_search_enabled:
        return authority
    from src.agent_runtime.authority import OperationGrant, canonical_tool
    from fork.mcp.trust import trusted_readonly_mcp_tools

    tool = canonical_tool("web_search")
    granted = _granted_tools(authority)
    trusted = {canonical_tool(name) for name in trusted_readonly_mcp_tools()}
    if granted - _ALWAYS_GRANTED - trusted:
        return authority  # the words already chose the turn's tools
    if tool in getattr(authority, "denied", frozenset()) or getattr(authority, "block_all", False):
        return authority
    from src.agent_runtime.remote_resources import resolve_backend
    try:
        backend = resolve_backend(tool, context=client_runtime_context,
                                  admission=True, owner=authority.owner)
    except (ValueError, TypeError, AttributeError):
        return authority
    backends = authority.backend_resources
    if backends is not None and backend not in backends:
        backends = (*backends, backend)
    return replace(authority, grants=(*authority.grants, OperationGrant(tool)),
                   backend_resources=backends)


def _search_hit(security_context, url: str) -> bool:
    """``url`` is a web_search hit of this run, or a short clean page on a hit's site."""
    from urllib.parse import urlsplit

    from fork.routing.auto_mode import (
        _MAX_SEARCH_SITE_PATH, _host, _loose, _state, normalize_url, public_host, same_site,
    )
    normalized = normalize_url(url)
    if normalized is None or not public_host(_host(normalized)):
        return False
    hits = _state(security_context).get("search") or set()
    if normalized in hits or _loose(normalized) in {_loose(u) for u in hits}:
        return True
    parts = urlsplit(normalized)
    return (not parts.query and len(parts.path) <= _MAX_SEARCH_SITE_PATH
            and any(same_site(normalized, u) for u in hits))


def with_search_hit_fetch(authority, operation, security_context,
                          client_runtime_context=None):
    """``(authority, True)`` widened for this one ``web_fetch`` of search hits.

    Anything else comes back unchanged with ``False``, so upstream's denial stays.
    """
    from src.agent_runtime.authority import OperationGrant, RequestAuthority, canonical_tool
    from src.tool_capabilities import ToolRunSecurityContext

    fetch = canonical_tool("web_fetch")
    search = canonical_tool("web_search")
    if (not isinstance(authority, RequestAuthority)
            or not isinstance(security_context, ToolRunSecurityContext)
            or getattr(operation, "tool", None) != fetch
            or authority.restricted(operation)
            or search in authority.denied
            or search not in _granted_tools(authority)):
        return authority, False
    from fork.routing.auto_mode import fetch_urls
    urls = fetch_urls(operation.input)
    if not urls or not all(_search_hit(security_context, u) for u in urls):
        return authority, False
    from src.agent_runtime.remote_resources import resolve_backend
    try:
        backend = resolve_backend(fetch, context=client_runtime_context,
                                  admission=True, owner=authority.owner)
    except (ValueError, TypeError, AttributeError):
        return authority, False
    backends = authority.backend_resources
    if backends is not None and backend not in backends:
        backends = (*backends, backend)
    grant = OperationGrant(fetch, inputs=frozenset({operation.input}))
    grants = tuple(g for g in authority.grants if g.tool != fetch) + (grant,)
    return replace(authority, grants=grants, backend_resources=backends), True
