"""Fork: read-only access to the owner's own stores without exact wording.

Upstream grants a tool only when the words of the message name it (request
authority). For the owner's personal stores that is too strict: "notate that
Katherine is my fiance" or a pronoun follow-up got no grant, the model's
``manage_contact`` call was blocked as "outside server request authority", and
a small local model could not recover.

``with_personal_store_reads`` adds a grant for the *read* actions only (list,
search, find, view...) of the personal-store tools below. Writes and deletes
keep their existing gate: wording that names them, or an approval.

Off with ``ODYSSEUS_PERSONAL_READ_GRANTS=0``. Policy and per-tool disables still
win (denied tools are skipped here, and the caller applies ``restrict`` later).
Hook: ``routes/chat_routes.py``, after the request authority is created.
"""

from __future__ import annotations

import os
from dataclasses import replace

# The read actions come from src/tool_capabilities.py, the single source of
# truth, so a new action there is never granted here by accident.
PERSONAL_STORE_TOOLS = (
    "manage_contact",
    "manage_notes",
    "manage_calendar",
    "manage_tasks",
    "manage_memory",
    "manage_documents",
    "manage_research",
    "manage_skills",
)


def enabled() -> bool:
    return os.environ.get("ODYSSEUS_PERSONAL_READ_GRANTS", "1").strip().lower() not in {
        "0", "false", "no", "off"}


def with_personal_store_reads(authority):
    """Add read-only grants for the personal-store tools to ``authority``."""
    if authority is None or not enabled() or getattr(authority, "block_all", False):
        return authority
    from src.agent_runtime.authority import OperationGrant, canonical_tool
    from src.agent_runtime.resources import seal_owned_scopes
    from src.tool_capabilities import _PRIVATE_ACTION_READS

    grants = {g.tool: g for g in authority.grants}
    added = []
    for name in PERSONAL_STORE_TOOLS:
        tool = canonical_tool(name)
        reads = _PRIVATE_ACTION_READS.get(tool)
        if not reads or tool in authority.denied:
            continue
        current = grants.get(tool)
        if current is None:
            grants[tool] = OperationGrant(tool, frozenset(reads))
            added.append(tool)
        elif current.actions is not None and current.inputs is None:
            grants[tool] = OperationGrant(tool, current.actions | frozenset(reads))
        # An unrestricted or input-pinned grant already came from the words.
    scopes = {(s.namespace, s.owner, s.thread_id): s for s in authority.owned_scopes or ()}
    for scope in seal_owned_scopes(authority.owner, authority.session_id, added):
        scopes.setdefault((scope.namespace, scope.owner, scope.thread_id), scope)
    return replace(authority, grants=tuple(grants.values()), owned_scopes=tuple(scopes.values()))
