"""Fork: what the chat route asks the fork about each turn, in one place.

``routes/chat_routes.py`` decides per turn whether it runs in chat or agent mode,
what the request authority grants (what may run without approval) and which tools
the turn contract offers. The fork changes those decisions at fixed points of the
route; each point is one hook that calls one function here, in the route's order:

1. :func:`scope_tool_intent`, after ``_classify_tool_intent``: a chat-mode how/why
   question about servers stays a question (``chat_mode_scope.py``).
2. :func:`escalate_chat_mode`, after upstream's chat→agent escalation: a slash skill
   with tools, a module question or a personal-store lookup runs in agent mode.
3. :func:`extend_authority`, after the request authority (and the exact-approval
   authority) is built: ``web_search`` with the web toggle on, read actions of the
   personal stores, the calculator in Auto; or, on an approval continuation, the
   approved trusted-module tool again.
4. :func:`extend_contract`, after ``resolve_turn_contract``: a slash skill's tools, or
   the trusted modules' read tools for a module question; the calculator in Auto.
5. :func:`admit_offered_reads`, after the final turn policy: read actions of every
   personal store the agent turn offers.

Two more hooks stay where upstream reads their input and call their own file: the
Auto/Ask choice from the form (``auto_mode.remember_chat_mode``) and the warm tools of
a used trusted module (``fork/mcp/trust.py``, ``with_trusted_server_tools``).

The positions are set by what the route computes in between, so the hooks can't be
fewer without changing a decision: (1) must clear the tool intent before upstream's
escalation reads it, while (2) runs only when that escalation left the turn in chat
mode; (4) feeds the contract policy, scope denials and final policy that (5) reads.

Each function keeps the order and short-circuits of the hooks it replaces, so every
decision is the same (``tests/fork/test_turn_policy_characterization.py`` pins them
for a table of turns through the real route). The one difference: on an approval
continuation, (3) no longer computes the web, personal-read and calculator grants
that the route then replaced with the approval's own authority.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def scope_tool_intent(tool_intent: Any, *, chat_mode: Any, message: Any) -> Any:
    """``tool_intent``, or None when a chat-mode how/why question must not escalate."""
    from fork.routing.chat_mode_scope import keep_chat_for_question

    if chat_mode == "chat" and keep_chat_for_question(message, tool_intent):
        logger.info("chat stays chat: %s matched a how/why question", tool_intent.reason)
        return None
    return tool_intent


def needs_agent_mode(message: Any, owner: Any) -> bool:
    """True when a chat-mode ``message`` needs tools the fork routes.

    A slash-invoked skill counts when it declares tools. Any other message counts
    when it asks about a trusted module's data, else when it is a lookup in one of
    the owner's personal stores (checked in that order).
    """
    from fork.routing.skill_invocation import (
        parse_invoke_message,
        slash_skill_needs_tools,
    )

    invoked = parse_invoke_message(message)
    if invoked is not None:
        return slash_skill_needs_tools(invoked, owner)
    from fork.routing.module_turn_scope import module_turn_requested
    from fork.routing.personal_store_escalation import personal_store_turn

    return module_turn_requested(message) or personal_store_turn(message)


def escalate_chat_mode(chat_mode: Any, auto_escalated: bool, *, message: Any,
                       owner: Any) -> tuple[Any, bool]:
    """``(chat_mode, auto_escalated)``, promoted to agent mode by :func:`needs_agent_mode`."""
    if chat_mode == "chat" and needs_agent_mode(message, owner):
        return "agent", True
    return chat_mode, auto_escalated


def extend_authority(authority, *, exact_approval, session_id, web_search_enabled: bool,
                     client_runtime_context=None):
    """The turn's request authority with the fork's grants.

    On an approval continuation (``exact_approval``) the route resumes the approved
    request's own authority; only the approved trusted-module tool is added
    (``fork/mcp/trust.py``). Otherwise, in this order: ``web_search`` when the toggle
    is on and the words named no tool, the personal stores' read actions, and the
    calculator while the chat is in Auto.
    """
    if exact_approval is not None:
        from fork.mcp.trust import with_approved_tool_grant

        return with_approved_tool_grant(authority, exact_approval)
    from fork.routing.auto_mode import with_calculator_grant
    from fork.routing.personal_read_grants import with_personal_store_reads
    from fork.routing.web_turn_scope import with_unclassified_web_search

    authority = with_unclassified_web_search(
        authority, web_search_enabled=web_search_enabled,
        client_runtime_context=client_runtime_context)
    authority = with_personal_store_reads(authority)
    return with_calculator_grant(authority, session_id=session_id)


def scoped_contract(contract, *, message: Any, owner: Any, schemas, policy, manager=None):
    """The turn contract with a slash skill's tools, or a module question's.

    A slash-invoked skill pins its declared tools (``skill_invocation.py``); any other
    message may add the trusted modules' read tools (``module_turn_scope.py``).
    """
    from fork.routing.skill_invocation import parse_invoke_message, pin_invoked_skill

    invoked = parse_invoke_message(message)
    if invoked is None:
        from fork.routing.module_turn_scope import with_module_tools

        return with_module_tools(contract, message=message, schemas=schemas, manager=manager)
    return pin_invoked_skill(contract, invoked, owner=owner, schemas=schemas, policy=policy,
                             manager=manager)


def extend_contract(contract, *, message: Any, owner: Any, session_id: Any, schemas, policy,
                    manager=None):
    """The turn contract with the fork's tools: :func:`scoped_contract`, then, while
    the chat is in Auto, the calculator next to the turn's tools."""
    from fork.routing.auto_mode import with_calculator_offer

    contract = scoped_contract(contract, message=message, owner=owner, schemas=schemas,
                               policy=policy, manager=manager)
    return with_calculator_offer(contract, owner=owner, session_id=session_id)


def admit_offered_reads(authority, *, chat_mode: Any, contract, policy, disabled_tools,
                        owner: Any):
    """An agent turn's authority that also admits reads of every offered personal store."""
    if chat_mode != "agent":
        return authority
    from fork.routing.offered_reads import with_offered_read_grants

    return with_offered_read_grants(authority, contract, policy, disabled_tools, owner=owner)
