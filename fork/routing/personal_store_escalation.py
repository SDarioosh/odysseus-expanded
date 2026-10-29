"""Fork: a chat-mode lookup in one of the owner's stores runs in agent mode.

The web UI starts every chat in chat mode. Upstream promotes a chat-mode turn
to agent mode only when ``src/action_intents.py`` recognises an *action* it
lists (add to calendar, send an email, set a reminder, run a command ...).
It has no contacts pattern at all and no pattern for a plain lookup in a
personal store, so "list the contact for Alex Example", "what are my tasks for
today?" or "what is in my notes about the trip" never left chat mode: the turn
got no tools, and the model answered from pinned memory or made the record up
("Based on the manage_contacts tool ..."), with no tool card to show that
nothing ran.

Agent mode already knows these turns: the turn-contract classifier
(``requested_capabilities`` in ``src/turn_contract.py``) assigns them a family
and offers that store's tool. :func:`personal_store_turn` reuses it: when the
message falls in one of the personal-store families below, the turn runs in
agent mode, where the contract offers the store's tool and
``src/personal_read_grants.py`` admits its reads.

Scope: contacts, calendar, tasks, notes and memory, the stores whose reads the
fork grants on every turn, found by the classifier or named as a tool in the
message ("use the manage_contacts tool ..."). The weak ``unknown`` family (an
action word or a lookup word with no store named) and every other family are
left to upstream. A message that says "no tools" stays in chat mode, and so
does a slash command (``src/skill_invocation.py`` handles those). The hook sees
the message only, so a bare follow-up ("and his email?") in chat mode is not
promoted; the escalated turn's pill shows agent mode and the user can switch.

Off with ``ODYSSEUS_PERSONAL_STORE_ESCALATION=0``. **No new hook:** the chat
route already hands a message without a slash command to
``invoked_skill_needs_tools`` (``src/skill_invocation.py``), which calls this.
"""

from __future__ import annotations

import os
import re
from typing import Any

PERSONAL_STORE_FAMILIES = frozenset({"contacts", "calendar", "tasks", "notes", "memory"})

# The store tool named outright ("use the manage_contacts tool ..."), with the
# plural and spacing a user types; the classifier only knows the exact names.
_TOOL_NAME = re.compile(
    r"\bmanage[_ -]?(?P<store>contacts?|calendars?|tasks?|notes?|memory|memories)\b", re.I)
_TOOL_FAMILY = {"contact": "contacts", "calendar": "calendar", "task": "tasks",
                "note": "notes", "memor": "memory"}

# Same opt-out upstream applies to the agent-mode contract
# (``_explicitly_denies_tool_use`` in routes/chat_routes.py).
_NO_TOOLS = re.compile(
    r"\b(?:no\s+tools?|do\s+not\s+use\s+(?:any\s+)?tools?|don'?t\s+use\s+(?:any\s+)?tools?)\b",
    re.I,
)


def enabled() -> bool:
    return os.environ.get("ODYSSEUS_PERSONAL_STORE_ESCALATION", "1").strip().lower() not in {
        "0", "false", "no", "off"}


def personal_store_families(message: Any, history: Any = ()) -> frozenset[str]:
    """The personal-store families the agent-mode classifier finds in ``message``."""
    if not isinstance(message, str) or not message.strip():
        return frozenset()
    from src.turn_contract import requested_capabilities

    families = set(requested_capabilities(message, list(history or ()))) & PERSONAL_STORE_FAMILIES
    for match in _TOOL_NAME.finditer(message):
        store = match["store"].lower()
        families.update(f for key, f in _TOOL_FAMILY.items() if store.startswith(key))
    return frozenset(families)


def personal_store_turn(message: Any, history: Any = ()) -> bool:
    """True when a chat-mode ``message`` should run in agent mode for a store lookup."""
    if not enabled() or not isinstance(message, str) or not message.strip():
        return False
    if _NO_TOOLS.search(message):
        return False
    from src.skill_invocation import parse_invoke_message

    if parse_invoke_message(message) is not None:
        return False
    return bool(personal_store_families(message, history))
