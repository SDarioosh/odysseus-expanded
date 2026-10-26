"""Plain questions about a module's data reach the module's tools.

``/sound`` and ``/context`` pin a module's tools for one message
(``src/skill_invocation.py``), but a plain question ("What did Alice say about
the budget?") in "Auto" got none of them:

* in chat mode (the web UI default) the lexical tool-intent classifier sees no
  tool request, so the turn never leaves chat mode and has no tools at all;
* in agent mode the turn contract offers only tool families recognized from the
  message words, and no family contains MCP tools, so a question with no family
  gets just the core tools (bash, python, read_file, web) and the model never
  sees the module.

Two small steps fix that, both for servers in ``ODYSSEUS_TRUSTED_READONLY_MCP``
only. Both run from the existing slash-skill hooks in routes/chat_routes.py
(``invoked_skill_needs_tools`` and ``pin_skill_contract`` in
``src/skill_invocation.py`` hand a message without a slash command to this
file), so this feature adds no lines to upstream files:

* :func:`module_turn_requested`: a chat-mode message with a module cue runs in
  agent mode. A cue is conversation vocabulary ("said", "told me", "meeting",
  "promised", "action items" ...), "my ..." questions, or the name of a person
  the module knows (its ``list_people`` tool, cached).
* :func:`with_module_tools`: when the contract found no family, or only personal
  stores (calendar, tasks, notes, memory, contacts), or the message has a cue,
  the trusted servers' **read-only** tools are added to what the contract
  offers. Nothing is removed from the offer, except that a cue on a turn with no
  family drops bash/python/read_file, which small models otherwise drift to.

Only offers change. Trusted read-only tools are already granted on every turn
(``src/mcp_trust.py``); write tools aren't added here (a follow-up gets them back
through ``with_trusted_server_tools``). Policy, per-tool disables and
``disable_mcp`` still apply: added names must already be executable in the
contract. Slash invocations are left to ``pin_skill_contract``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import replace
from typing import Any

logger = logging.getLogger(__name__)

# Families a recording can answer as well: "when is my dentist appointment?"
# may be in the calendar or only in a conversation.
PERSONAL_FAMILIES = frozenset({"calendar", "tasks", "notes", "memory", "contacts", "unknown"})
# Families that are only allowed together with a cue.
CUE_FAMILIES = PERSONAL_FAMILIES | {"search_browser"}
# Dropped from a cue turn with no family; small models pick them over the module.
DRIFT_TOOLS = frozenset({"bash", "python", "read_file"})

_CONVERSATION_RE = re.compile(r"\bconversations?\b", re.I)
# Words that point at the hub's own chats rather than recordings.
_CHAT_RE = re.compile(
    r"\b(?:chats?|sessions?|threads?|chat\s+history)\b"
    r"|\b(?:this|our|current)\s+(?:\w+\s+)?conversations?\b"
    r"|\bconversations?\s+(?:with|between)\s+(?:you|us|odysseus|the\s+(?:assistant|ai|bot|model))\b"
    r"|\bwe\s+(?:had|have|talked|discussed|chatted)\b",
    re.I,
)

_CUE_RE = re.compile(
    r"\b("
    r"conversations?|transcripts?|recordings?|recorded|meetings?|stand-?ups?|1[:-]on[:-]1|"
    r"one-on-one|calls? with|chat(?:ted)? with|talk(?:ed|ing)? (?:to|with|about)|"
    r"discuss(?:ed|ion)?|mention(?:ed)?|said|says?|told|ask(?:ed)? me|"
    r"promis(?:e|ed)|recommend(?:ed)?|agreed|action items?|to-?dos? from|digest|speakers?|"
    r"heard|who is taking|who's taking"
    r")\b"
    # Dots allow titles: "did Dr. Patel tell".
    r"|\b(?:did|has|have|was|were)\s+[\w.]+(?:\s+[\w.]+)?\s+"
    r"(?:say|tell|ask|mention|promise|recommend|suggest)\b"
    r"|\bmy\s+(?:next|last|mom|mum|dad|mother|father|sister|brother|wife|husband|partner|"
    r"boss|rent|lease|landlord|doctor|dentist|appointments?|plans?)\b",
    re.I,
)

# People the trusted modules know, so "What is Jonas allergic to?" is a cue.
_PEOPLE_TTL = 600.0
_PEOPLE_TIMEOUT = 2.0
_people: frozenset[str] = frozenset()
_people_at = 0.0
_SKIP_NAMES = {"me", "unknown"}
_SPEAKER_LABEL = re.compile(r"^speaker\s*\d+$", re.I)
_PERSON_LINE = re.compile(r"^-\s+(?P<name>[^:(]+?)\s*(?:\(|:)")


def parse_people(text: str) -> frozenset[str]:
    """Names from a ``list_people`` result (``- Name (owner): ...`` lines)."""
    names = set()
    for line in (text or "").splitlines():
        m = _PERSON_LINE.match(line.strip())
        if not m:
            continue
        name = m["name"].strip()
        if (len(name) < 2 or name.casefold() in _SKIP_NAMES
                or _SPEAKER_LABEL.match(name) or "(owner)" in line):
            continue
        names.add(name)
    return frozenset(names)


def _manager():
    from src.mcp_trust import _manager as manager

    return manager()


def _people_tools(manager) -> list[str]:
    from src.mcp_trust import trusted_readonly_mcp_tools

    return sorted(n for n in trusted_readonly_mcp_tools(manager) if n.endswith("__list_people"))


async def refresh_known_people(manager=None, *, now: float | None = None) -> frozenset[str]:
    """People named by the trusted modules' ``list_people``, cached for 10 minutes.

    Errors and slow servers leave the previous list (or none); a cue then just
    falls back to the words in the message.
    """
    global _people, _people_at
    now = time.monotonic() if now is None else now
    if _people_at and now - _people_at < _PEOPLE_TTL:
        return _people
    manager = manager if manager is not None else _manager()
    if manager is None:
        return _people
    _people_at = now
    names: set[str] = set()
    for tool in _people_tools(manager):
        try:
            res = await asyncio.wait_for(manager.call_tool(tool, {}), _PEOPLE_TIMEOUT)
        except Exception:
            logger.debug("module people lookup failed: %s", tool, exc_info=True)
            continue
        if isinstance(res, dict) and not res.get("error") and not res.get("exit_code"):
            names |= parse_people(str(res.get("stdout") or ""))
    if names:
        _people = frozenset(names)
    return _people


def _names_re(names: frozenset[str]) -> re.Pattern | None:
    parts = sorted({re.escape(n) for n in names if n}, key=len, reverse=True)
    if not parts:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?:'s)?(?!\w)", re.I)


def module_cue(message: Any, people: frozenset[str] | None = None) -> bool:
    """True when the message reads like a question about recorded conversations."""
    if not isinstance(message, str) or not message.strip():
        return False
    if _CUE_RE.search(message):
        return True
    names_re = _names_re(_people if people is None else people)
    return bool(names_re and names_re.search(message))


def recorded_conversation(message: Any) -> bool:
    """True when "conversation" means a recording, not a chat with the assistant."""
    return (isinstance(message, str) and bool(_CONVERSATION_RE.search(message))
            and not _CHAT_RE.search(message))


def _is_slash(message: Any) -> bool:
    from src.skill_invocation import parse_invoke_message

    return parse_invoke_message(message) is not None


def known_people(manager=None) -> frozenset[str]:
    """The cached people list; a stale or empty cache is refreshed in the background.

    The chat route's hooks are synchronous, so the lookup never blocks a turn: the
    first question after startup (or after 10 minutes) uses the words alone.
    """
    if not _people_at or time.monotonic() - _people_at >= _PEOPLE_TTL:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and not _refresh_running:
            _start_refresh(loop, manager)
    return _people


_refresh_running = False


def _start_refresh(loop, manager) -> None:
    global _refresh_running
    _refresh_running = True

    async def run():
        global _refresh_running
        try:
            await refresh_known_people(manager)
        finally:
            _refresh_running = False

    loop.create_task(run())


def module_turn_requested(message: Any, manager=None) -> bool:
    """A plain (non-slash) message with a module cue while a trusted module is connected."""
    from src.mcp_trust import trusted_readonly_mcp_tools

    if not isinstance(message, str) or _is_slash(message):
        return False
    manager = manager if manager is not None else _manager()
    if manager is None or not trusted_readonly_mcp_tools(manager):
        return False
    return module_cue(message, known_people(manager))


def with_module_tools(contract, *, message: Any, schemas, manager=None):
    """``contract`` with the trusted modules' read-only tools added, or unchanged."""
    from src.mcp_trust import trusted_readonly_mcp_tools

    if contract is None or not isinstance(message, str) or _is_slash(message):
        return contract
    if contract.required_read_operation is not None or contract.unavailable:
        return contract
    manager = manager if manager is not None else _manager()
    families = set(contract.capabilities)
    cue = module_cue(message, known_people(manager))
    chat_tools: frozenset[str] = frozenset()
    if "sessions" in families and recorded_conversation(message):
        from src.turn_contract import FAMILY_TOOLS

        families.discard("sessions")
        chat_tools = FAMILY_TOOLS.get("sessions", frozenset()) - set(contract.required)
    if not families <= (CUE_FAMILIES if cue else PERSONAL_FAMILIES):
        return contract
    module_tools = {n for n in trusted_readonly_mcp_tools(manager) if n in contract.executable}
    if not module_tools:
        return contract
    offered = (set(contract.offered) - chat_tools) | module_tools | ({"ask_user"} & contract.executable)
    if cue and not (families - {"unknown"}):
        offered -= DRIFT_TOOLS - set(contract.required)
    inventory = {s["function"]["name"]: s for s in schemas or ()
                 if isinstance(s.get("function"), dict) and s["function"].get("name")}
    offered = {n for n in offered if n in inventory}
    if offered == set(contract.offered):
        return contract
    logger.info("[module-scope] added=%s dropped=%s", sorted(offered - set(contract.offered)),
                sorted(set(contract.offered) - offered))
    return replace(contract, offered=frozenset(offered),
                   schema_json=tuple(json.dumps(inventory[n], sort_keys=True)
                                     for n in sorted(offered)))
