"""Plain questions about module data reach the module's tools (src/module_turn_scope.py)."""

from __future__ import annotations

import asyncio

import pytest

from src import module_turn_scope as scope
from src.module_turn_scope import (
    module_cue,
    module_turn_requested,
    parse_people,
    refresh_known_people,
    with_module_tools,
)
from src.skill_invocation import INVOKE_PREFIX
from src.tool_policy import build_effective_tool_policy
from src.turn_contract import requested_capabilities, resolve_turn_contract

RO = {"readOnlyHint": True, "destructiveHint": False}
RW = {"readOnlyHint": False, "destructiveHint": False}
READS = ["search_transcripts", "search_conversations", "get_conversation", "list_people"]
WRITES = ["update_action_item", "name_speaker"]
PEOPLE = """Speakers:
- Me (owner): 120 lines in 8 conversations, last 2026-10-07 09:30
- Alice: 30 lines in 2 conversations, last 2026-10-02 10:00
- Speaker 2: 4 lines in 1 conversations, last 2026-10-07 09:30
Mentioned:
- Jonas: 1 conversations, last 2026-10-04
- Dr. Patel: 1 conversations, last 2026-09-30
"""


class FakeManager:
    def __init__(self, status="connected", people=PEOPLE, fail=False):
        self._tools = {
            "c0nv0": [{"name": n, "annotations": RO} for n in READS]
                     + [{"name": n, "annotations": RW} for n in WRITES],
            "other": [{"name": "search_web", "annotations": RO}],
        }
        self._connections = {"c0nv0": {"status": status, "name": "convo"},
                             "other": {"status": "connected", "name": "other"}}
        self.people, self.fail, self.calls = people, fail, 0

    def is_builtin(self, server_id):
        return False

    async def call_tool(self, name, args):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return {"stdout": self.people, "exit_code": 0}


def _q(tool, server="c0nv0"):
    return f"mcp__{server}__{tool}"


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": {}}}}


BUILTINS = ["ask_user", "update_plan", "bash", "python", "read_file", "web_search", "web_fetch",
            "manage_calendar", "manage_tasks", "send_email", "search_emails", "read_email"]
SCHEMAS = ([_schema(n) for n in BUILTINS] + [_schema(_q(t)) for t in READS + WRITES]
           + [_schema(_q("search_web", "other"))])
MODULE_READS = {_q(t) for t in READS}


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_TRUSTED_READONLY_MCP", "convo")
    monkeypatch.setattr(scope, "_people", frozenset())
    monkeypatch.setattr(scope, "_people_at", 0.0)


def _contract(message, families=None):
    caps = requested_capabilities(message) if families is None else families
    return resolve_turn_contract(capabilities=caps, schemas=SCHEMAS,
                                 policy=build_effective_tool_policy(), message=message)


def _scoped(message, families=None, manager=None):
    return with_module_tools(_contract(message, families), message=message, schemas=SCHEMAS,
                             manager=manager or FakeManager())


# --- cues -------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "What did Alice say about the budget in September?",
    "What did Tom say on October 3rd?",
    "What did Ben say in the standup on October 7th?",
    "What did Maria ask me to do last week?",
    "What happened in my conversations on September 30?",
    "Did anyone mention a marathon?",
    "What did I promise Ben?",
    "What did Dr. Patel tell me to avoid?",
    "Did my doctor tell me to rest?",
    "Which book did Jonas recommend?",
    "When is my next dentist appointment?",
    "How much will my rent be from January?",
    "Who is taking over Alice's vendor contracts?",
    "what action items do I have from the dinner planning?",
])
def test_conversation_questions_are_cues(message):
    assert module_cue(message)


@pytest.mark.parametrize("message", [
    "What is the capital of France?",
    "Write a python function that reverses a list",
    "Translate good morning into Spanish",
    "Summarize this article for me",
    "tell me why",
    "Tell me why the sky is blue",
    "tell me a joke",
])
def test_general_questions_are_not_cues(message):
    assert not module_cue(message, frozenset({"Alice"}))


def test_known_people_are_cues():
    people = parse_people(PEOPLE)
    assert people == {"Alice", "Jonas", "Dr. Patel"}  # not the owner or "Speaker 2"
    assert module_cue("What is Jonas allergic to?", people)
    assert module_cue("When exactly is alice moving to Berlin?", people)
    assert not module_cue("What is Jonas allergic to?", frozenset())
    assert not module_cue("Is Alicante nice in May?", people)


# --- chat → agent ------------------------------------------------------------

def test_cue_requests_a_module_turn():
    assert module_turn_requested("What did Alice say?", FakeManager())


def test_known_person_requests_a_module_turn_once_cached():
    manager = FakeManager()
    assert not module_turn_requested("What is Jonas allergic to?", manager)  # cache empty
    asyncio.run(refresh_known_people(manager))
    assert module_turn_requested("What is Jonas allergic to?", manager)
    assert asyncio.run(refresh_known_people(manager)) == parse_people(PEOPLE)
    assert manager.calls == 1  # cached for 10 minutes


def test_stale_cache_refreshes_in_the_background():
    manager = FakeManager()

    async def turn():
        first = module_turn_requested("What is Jonas allergic to?", manager)
        for _ in range(5):
            await asyncio.sleep(0)
        return first, module_turn_requested("What is Jonas allergic to?", manager)

    assert asyncio.run(turn()) == (False, True)


def test_no_module_turn_without_a_connected_trusted_server(monkeypatch):
    assert not module_turn_requested("What did Alice say?", FakeManager(status="error"))
    monkeypatch.setenv("ODYSSEUS_TRUSTED_READONLY_MCP", "")
    assert not module_turn_requested("What did Alice say?", FakeManager())


def test_no_module_turn_for_slash_or_plain_questions():
    manager = FakeManager()
    assert not module_turn_requested(SLASH, manager)
    assert not module_turn_requested("What is the capital of France?", manager)


def test_people_lookup_failure_is_quiet():
    manager = FakeManager(fail=True)
    assert asyncio.run(refresh_known_people(manager)) == frozenset()
    assert not module_turn_requested("What is Jonas allergic to?", manager)


def test_slash_hooks_hand_plain_questions_to_this_module(monkeypatch):
    from src import skill_invocation

    manager = FakeManager()
    monkeypatch.setattr(scope, "_manager", lambda: manager)
    assert skill_invocation.invoked_skill_needs_tools("What did Alice say?", "owner")
    assert not skill_invocation.invoked_skill_needs_tools("Capital of France?", "owner")
    message = "What did Alice say about the budget?"
    contract = skill_invocation.pin_skill_contract(
        _contract(message, set()), message=message, owner="owner", schemas=SCHEMAS,
        policy=build_effective_tool_policy(), manager=manager)
    assert MODULE_READS <= contract.offered


# --- contract -----------------------------------------------------------------

def _invariants(contract):
    assert contract.required <= contract.offered <= contract.executable
    assert {s["function"]["name"] for s in contract.schemas()} == set(contract.offered)


def test_cue_with_no_family_offers_module_reads_and_drops_drift_tools():
    contract = _scoped("What did Alice say about the budget in September?", families=set())
    _invariants(contract)
    assert MODULE_READS <= contract.offered
    assert not {_q(t) for t in WRITES} & contract.offered  # writes come back on follow-ups
    assert _q("search_web", "other") not in contract.offered  # untrusted server
    assert not {"bash", "python", "read_file"} & contract.offered


def test_personal_family_keeps_its_tools_and_adds_module_reads():
    contract = _scoped("When is my next dentist appointment?", families={"calendar"})
    _invariants(contract)
    assert "manage_calendar" in contract.offered
    assert MODULE_READS <= contract.offered


def test_no_family_without_cue_adds_module_reads_but_keeps_core():
    contract = _scoped("How tall is the tallest building?", families=set())
    _invariants(contract)
    assert MODULE_READS <= contract.offered
    assert {"bash", "python", "web_search"} <= contract.offered


def test_web_family_needs_a_cue():
    plain = _contract("search the web for tallest buildings", {"search_browser"})
    assert _scoped("search the web for tallest buildings", {"search_browser"}) is not plain
    assert not MODULE_READS & _scoped("search the web for tallest buildings",
                                      {"search_browser"}).offered
    assert MODULE_READS <= _scoped("What's the latest on what Alice said?",
                                   {"search_browser"}).offered


def test_other_families_are_left_alone():
    contract = _contract("email Alice the report", {"email"})
    assert with_module_tools(contract, message="email Alice the report", schemas=SCHEMAS,
                             manager=FakeManager()) is contract


SLASH = (INVOKE_PREFIX + "---\nname: sound\nrequires_toolsets: [\"mcp:convo\"]\n---\n\n"
         "## Procedure\n\n1. Search the recordings.\n\n--- END SKILL ---\n\n"
         "Request: What did Alice say?")


def test_slash_invocation_is_left_to_the_skill_pin():
    message = SLASH
    contract = _contract(message, set())
    assert with_module_tools(contract, message=message, schemas=SCHEMAS,
                             manager=FakeManager()) is contract


def test_disabled_module_tools_are_not_added():
    message = "What did Alice say?"
    policy = build_effective_tool_policy(disabled_tools={_q("search_transcripts")})
    contract = resolve_turn_contract(capabilities=set(), schemas=SCHEMAS, policy=policy,
                                     message=message)
    scoped = with_module_tools(contract, message=message, schemas=SCHEMAS, manager=FakeManager())
    _invariants(scoped)
    assert _q("search_transcripts") not in scoped.offered
    assert _q("search_conversations") in scoped.offered


def test_no_trusted_server_leaves_the_contract(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_TRUSTED_READONLY_MCP", "")
    contract = _contract("What did Alice say?", set())
    assert with_module_tools(contract, message="What did Alice say?", schemas=SCHEMAS,
                             manager=FakeManager()) is contract


# --- "conversation" means a recording, "chat" means chat history -----------

CHAT_TOOLS = ["list_sessions", "manage_session", "create_session", "send_to_session",
              "chat_with_model", "pipeline"]
SESSION_SCHEMAS = SCHEMAS + [_schema(n) for n in CHAT_TOOLS]


def _scoped_sessions(message):
    contract = resolve_turn_contract(capabilities=requested_capabilities(message),
                                     schemas=SESSION_SCHEMAS,
                                     policy=build_effective_tool_policy(), message=message)
    return with_module_tools(contract, message=message, schemas=SESSION_SCHEMAS,
                             manager=FakeManager())


@pytest.mark.parametrize("message", [
    "what is my last conversation?",
    "What was my most recent conversation about?",
    "show my recent conversations",
    "find the conversation about the budget",
])
def test_conversation_questions_get_module_reads_not_chat_history(message):
    assert "sessions" in requested_capabilities(message)
    contract = _scoped_sessions(message)
    _invariants(contract)
    assert MODULE_READS <= contract.offered
    assert not set(CHAT_TOOLS) & contract.offered


@pytest.mark.parametrize("message", [
    "list my recent chats",
    "what is my last chat session?",
    "what did we discuss in our last conversation?",
    "rename this conversation to Budget",
])
def test_chat_history_questions_keep_session_tools(message):
    contract = _scoped_sessions(message)
    assert "list_sessions" in contract.offered or "manage_session" in contract.offered
    assert not MODULE_READS & contract.offered
