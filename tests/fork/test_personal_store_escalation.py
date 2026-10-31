"""A chat-mode lookup in a personal store runs in agent mode (src/personal_store_escalation.py)."""

from __future__ import annotations

import json

import pytest

from src.personal_store_escalation import personal_store_families, personal_store_turn
from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint

LOOKUPS = [
    "list the contact for Alex Example",
    "show my contacts",
    "do I have a contact for Alex Example?",
    "what are my tasks for today?",
    "what is in my notes about the trip",
    "what's on my calendar tomorrow?",
    "what do you remember about my allergies?",
    "use the manage_contacts tool to retrieve the info about Alex Example and give it to me",
    "call manage_contact with action list",
    "check manage-notes for the trip",
]
PLAIN = [
    "",
    "hello",
    "What is the capital of France?",
    "how do contacts work in odysseus",
    "list the contact for Alex Example, no tools please",
    "/sound what did Alice say?",
]


@pytest.mark.parametrize("message", LOOKUPS)
def test_store_lookups_escalate(message):
    assert personal_store_turn(message)


@pytest.mark.parametrize("message", PLAIN)
def test_other_messages_stay_in_chat_mode(message):
    assert not personal_store_turn(message)


def test_families_are_limited_to_personal_stores():
    assert personal_store_families("list the contact for Alex Example") == {"contacts"}
    assert personal_store_families("use the manage_contacts tool for Alex Example") == {"contacts"}
    assert personal_store_families("manage_memory and manage_tasks please") == {"memory", "tasks"}
    # The weak "unknown" family (a lookup word with no store named) is not enough.
    assert not personal_store_families("retrieve the info about Alex Example and give it to me")
    assert not personal_store_turn("retrieve the info about Alex Example and give it to me")


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_PERSONAL_STORE_ESCALATION", "0")
    assert not personal_store_turn("list the contact for Alex Example")


def test_slash_hook_hands_plain_messages_here():
    from src.skill_invocation import invoked_skill_needs_tools

    assert invoked_skill_needs_tools("list the contact for Alex Example", "alice")
    assert not invoked_skill_needs_tools("What is the capital of France?", "alice")


async def _chat_turn(monkeypatch, message):
    """Send ``message`` through the real chat route in chat mode; return (chunks, agent kwargs)."""
    from routes import chat_routes
    from src import tool_security

    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    monkeypatch.setattr(chat_routes, "coerce_message_and_session",
                        lambda *args, **kwargs: (message, "session-1"))
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    agent_kwargs = []

    async def capture_agent(*args, **kwargs):
        agent_kwargs.append(kwargs)
        yield 'data: {"delta":"agent ran"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_agent_loop", capture_agent)
    request = _RouteRequest("chat")
    request._form["message"] = message
    request._form["compare_mode"] = "false"
    response = await endpoint(request)
    assert response.status_code == 200
    chunks = [chunk async for chunk in response.body_iterator]
    return chunks, agent_kwargs, captured


def _turn_mode(chunks):
    for chunk in chunks:
        if chunk.startswith("data: "):
            try:
                event = json.loads(chunk[6:])
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "turn_mode":
                return event
    return None


@pytest.mark.asyncio
async def test_chat_mode_contact_lookup_runs_in_agent_mode_with_manage_contact(monkeypatch):
    chunks, agent_kwargs, captured = await _chat_turn(
        monkeypatch, "list the contact for Alex Example")
    mode = _turn_mode(chunks)
    assert mode == {"type": "turn_mode", "mode": "agent", "auto_escalated": True}
    assert len(agent_kwargs) == 1 and "chat" not in captured
    contract = agent_kwargs[0]["turn_contract"]
    assert contract is not None and contract.capabilities == {"contacts"}
    assert "manage_contact" in contract.offered
    assert "manage_contact" in agent_kwargs[0]["forced_tools"]
    assert not agent_kwargs[0]["tool_policy"].blocks("manage_contact")


@pytest.mark.asyncio
async def test_chat_mode_plain_question_stays_in_chat_mode(monkeypatch):
    chunks, agent_kwargs, captured = await _chat_turn(monkeypatch, "What is the capital of France?")
    assert _turn_mode(chunks)["mode"] == "chat"
    assert not agent_kwargs and "chat" in captured


@pytest.mark.asyncio
async def test_kill_switch_keeps_chat_mode(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_PERSONAL_STORE_ESCALATION", "0")
    chunks, agent_kwargs, captured = await _chat_turn(
        monkeypatch, "list the contact for Alex Example")
    assert _turn_mode(chunks)["mode"] == "chat"
    assert not agent_kwargs and "chat" in captured
