"""Fork: a failed approved web read doesn't end the turn.

When the owner approves a gated action, the agent loop runs that one sealed
action first. If it fails, upstream ends the turn with "The approved <tool>
action could not run: <error>" so the model can't improvise a different action
in place of the one approved. For a write that is right. For a web read it
threw the run away: a product comparison had three searches and a fetched page
in hand, the approved second fetch got HTTP 429 from the shop, and the owner
got the rate-limit error as the answer.

:func:`recover_failed_web_read` says when the loop should go on instead: the
sealed action was a web read (``web_fetch``, ``web_search``, ``pdf_extract``,
``youtube_tool``) and it failed on the network or the site, not because a
policy, the gate or the server refused it. The failed result goes to the model
like any tool result, with a note to answer from what it already has or try
another source. Every later call still goes through the normal gates.

Hook: ``src/agent_loop.py``, the exact-approval continuation.
"""

from __future__ import annotations

from typing import Any

WEB_READ_TOOLS = frozenset({"web_fetch", "web_search", "pdf_extract", "youtube_tool"})

RECOVERY_NOTE = (
    "This source could not be read. Don't retry the same address. Answer from "
    "the evidence already gathered in this conversation, or read another source "
    "from the search results, and say which source could not be read."
)


def _refused(result: dict) -> bool:
    """A policy, gate or approval refused the call; it never reached the web."""
    return bool(
        result.get("blocked")
        or result.get("approval_required")
        or result.get("policy")
        or result.get("denied")
    )


def recover_failed_web_read(tool_name: Any, result: Any) -> tuple[Any, bool]:
    """``(result, True)`` when the turn should continue after this failed read.

    The returned result carries :data:`RECOVERY_NOTE` in its error. Anything
    else comes back unchanged with ``False``, so upstream's handling stays.
    """
    if tool_name not in WEB_READ_TOOLS or not isinstance(result, dict):
        return result, False
    if _refused(result):
        return result, False
    error = str(result.get("error") or "").strip()
    if not error:
        return result, False
    return {**result, "error": f"{error}\n\n{RECOVERY_NOTE}"}, True
