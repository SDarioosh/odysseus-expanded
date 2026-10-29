"""Slash-invoked skills bring their declared tools to that turn.

``/<skill> <request>`` in the chat box calls ``POST /api/skills/<name>/invoke``
(routes/skills_routes.py), which returns a composed message:

    Apply the skill below to my request, following its Procedure / ...
    --- BEGIN SKILL ---
    <SKILL.md>
    --- END SKILL ---
    Request: <request>

and the frontend sends that as an ordinary chat message. In routed tool modes
("Auto"), the turn contract (routes/chat_routes.py) then picks tools from the
message words, and ``turn_contract.offered`` replaces everything the agent loop
added for matched skills. So a skill's ``requires_toolsets`` reached the model
only when the SKILL.md text happened to name the tool, and the SKILL.md text
could itself be misread (for example as a "search skills" request).

:func:`pin_skill_contract` recognizes the composed message server-side, looks
the skill up in the sender's own invokable skills (the same ``index_for`` list
the invoke route uses), and resolves the turn contract from the skill's
declared tools only (plus ``ask_user``/``update_plan``). It looks only at the
current message and keeps no state, so the next message without a slash
command is routed normally. Policy, disabled tools and approvals still apply:
the contract is resolved by the same ``resolve_turn_contract``.

Why recognize the message instead of carrying the skill name in a request
field: it works for every client that sends the invoke route's message (the
web UI, the PWA, API callers, evals) without a frontend change, and the trust
decision comes from the owner's skill store, not from the message text. A
user who pastes the format by hand gets exactly what typing the slash command
would give them.

Plain module questions: the same two hooks also cover a message with no slash
command that asks about a trusted module's data ("What did Alice say?"); see
``src/module_turn_scope.py``. Sharing the hooks keeps the fork's footprint in
routes/chat_routes.py unchanged.

Managed skills: skills an admin creates through configuration (source
``managed``, e.g. ``scripts/modules_setup.py``) are slash-invokable without the
LLM audit that "Auto-approve skills" requires for learned skills.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from src.skill_mcp_toolsets import expand_toolsets

logger = logging.getLogger(__name__)

# Must match the message built by invoke_skill in routes/skills_routes.py
# (tests/test_skill_mcp_toolsets.py checks that the route still builds it).
INVOKE_PREFIX = (
    "Apply the skill below to my request, following its Procedure / Pitfalls / Verification.\n\n"
    "--- BEGIN SKILL ---\n"
)
END_MARKER = "\n--- END SKILL ---\n\n"
MANAGED_SOURCE = "managed"
PINNED_CONTROL_TOOLS = frozenset({"ask_user", "update_plan"})

_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class InvokedSkill:
    name: str
    request: str


def parse_invoke_message(message: object) -> InvokedSkill | None:
    """The skill name and request of a composed slash message, else None.

    Browsers submit a textarea's line breaks as CRLF (HTML form encoding), so the
    web UI's copy of the message arrives with ``\r\n``; normalize before matching.
    """
    text = str(message or "").replace("\r\n", "\n")
    if not text.startswith(INVOKE_PREFIX):
        return None
    body = text[len(INVOKE_PREFIX):]
    end = body.rfind(END_MARKER)
    if end < 0 or not body.startswith("---\n"):
        return None
    front_end = body.find("\n---\n", 4)
    if front_end < 0 or front_end > end:
        return None
    match = _NAME_RE.search(body[4:front_end])
    if not match:
        return None
    name = match.group(1).strip().strip("\"'")
    rest = body[end + len(END_MARKER):]
    request = rest[len("Request:"):].strip() if rest.startswith("Request:") else rest.strip()
    return InvokedSkill(name=name, request=request) if name else None


def is_managed_skill(skill: dict) -> bool:
    return str((skill or {}).get("source") or "") == MANAGED_SOURCE


def _invokable_skill(name: str, owner):
    from services.memory.skills import SkillsManager
    from src.constants import DATA_DIR

    manager = SkillsManager(DATA_DIR)
    if name not in {s.get("name") for s in manager.index_for(owner=owner)}:
        return None
    return next((s for s in manager.load(owner=owner) if s.get("name") == name), None)


def invoked_skill_needs_tools(message, owner) -> bool:
    """True when ``message`` slash-invokes one of ``owner``'s skills that declares tools.

    Without a slash command it answers whether the message is a plain question
    about a trusted module (``src/module_turn_scope.py``) or a lookup in one of
    the owner's personal stores (``src/personal_store_escalation.py``).

    The chat route uses this to run such a turn in agent mode: in chat mode the turn
    contract (and so :func:`pin_skill_contract`) never runs, and the lexical tool-intent
    classifier doesn't see a tool request in the composed skill message, so the model
    would answer with no tools at all.
    """
    invoked = parse_invoke_message(message)
    if invoked is None:
        from src.module_turn_scope import module_turn_requested
        from src.personal_store_escalation import personal_store_turn
        return module_turn_requested(message) or personal_store_turn(message)
    try:
        skill = _invokable_skill(invoked.name, owner)
    except Exception:
        logger.debug("slash skill lookup failed", exc_info=True)
        return False
    return bool((skill or {}).get("requires_toolsets"))


def pin_skill_contract(contract, *, message, owner, schemas, policy, manager=None):
    """Return a contract offering the invoked skill's tools, or ``contract`` unchanged.

    A message without a slash command goes to
    :func:`src.module_turn_scope.with_module_tools` (plain module questions).
    Unchanged when the skill isn't one
    the owner may invoke, or the skill declares no ``requires_toolsets``.
    An MCP entry that matches no connected, enabled server is reported as
    unavailable (the turn then says which one) instead of being dropped.
    """
    invoked = parse_invoke_message(message)
    if invoked is None:
        from src.module_turn_scope import with_module_tools
        return with_module_tools(contract, message=message, schemas=schemas, manager=manager)
    try:
        skill = _invokable_skill(invoked.name, owner)
    except Exception:
        logger.debug("slash skill lookup failed", exc_info=True)
        return contract
    required = list((skill or {}).get("requires_toolsets") or [])
    if not required:
        return contract
    from src.turn_contract import resolve_turn_contract

    names, missing = expand_toolsets(required, manager)
    pinned = resolve_turn_contract(
        capabilities=(),
        schemas=schemas,
        policy=policy,
        required_tools=missing,
        selected_tools=names | PINNED_CONTROL_TOOLS,
    )
    logger.info(
        "[skill-pin] /%s requires=%s offered=%s unavailable=%s",
        invoked.name, required, sorted(pinned.offered), sorted(pinned.unavailable),
    )
    return pinned
