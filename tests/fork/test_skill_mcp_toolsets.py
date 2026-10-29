"""MCP entries in skill requires_toolsets, and slash-invoked skills pinning their tools.

Covers src/skill_mcp_toolsets.py, src/skill_invocation.py and their hooks in
services/memory/skills.py (index_for) and routes/skills_routes.py (invoke message).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.memory.skills as skills_mod
from services.memory.skills import SkillsManager
from src import skill_invocation
from src.skill_invocation import (
    INVOKE_PREFIX,
    invoked_skill_needs_tools,
    is_managed_skill,
    parse_invoke_message,
    pin_skill_contract,
)
from src.skill_mcp_toolsets import expand_toolsets, with_mcp_toolsets
from src.tool_policy import build_effective_tool_policy
from src.turn_contract import resolve_turn_contract

CONVO = ["search_transcripts", "search_conversations", "get_conversation", "update_action_item"]


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": {}}}}


class FakeManager:
    def __init__(self, status="connected"):
        self._tools = {
            "8bd5fb02": [{"name": n} for n in CONVO],
            "notes01": [{"name": "search_notes"}],
        }
        self._connections = {
            "8bd5fb02": {"status": status, "name": "convo"},
            "notes01": {"status": "connected", "name": "Notes"},
        }

    def is_builtin(self, server_id):
        return False


def _q(tool, server="8bd5fb02"):
    return f"mcp__{server}__{tool}"


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    # Per-tool MCP disables normally come from the database.
    monkeypatch.setattr("src.skill_mcp_toolsets._disabled_map", dict)


# --- expansion -------------------------------------------------------------

def test_server_entry_expands_by_name_or_id_case_insensitive():
    mgr = FakeManager()
    for entry in ("mcp:convo", "mcp:CONVO", "mcp:8bd5fb02"):
        names, missing = expand_toolsets([entry], mgr)
        assert names == {_q(t) for t in CONVO} and missing == set()


def test_builtins_pass_through_and_qualified_tools_are_checked():
    names, missing = expand_toolsets(
        ["manage_notes", _q("get_conversation"), _q("nope")], FakeManager())
    assert names == {"manage_notes", _q("get_conversation")}
    assert missing == {_q("nope")}


def test_disabled_tools_and_disconnected_servers_are_not_expanded(monkeypatch):
    monkeypatch.setattr("src.skill_mcp_toolsets._disabled_map",
                        lambda: {"8bd5fb02": {"update_action_item"}})
    names, _ = expand_toolsets(["mcp:convo"], FakeManager())
    assert _q("update_action_item") not in names
    names, missing = expand_toolsets(["mcp:convo"], FakeManager(status="error"))
    assert names == set() and missing == {"mcp:convo"}


def test_with_mcp_toolsets_adds_usable_server_keys_only():
    avail = with_mcp_toolsets({"manage_notes"}, FakeManager(status="error"))
    assert "mcp:convo" not in avail and "mcp:notes" in avail and "mcp:Notes" in avail
    assert "manage_notes" in avail and _q("search_notes", "notes01") in avail


# --- skill visibility and matching ----------------------------------------

def _skills(tmp_path, monkeypatch, auto_approve=True):
    monkeypatch.setattr(skills_mod, "_approval_policy", lambda owner: (auto_approve, 0.85))
    sm = SkillsManager(str(tmp_path))
    sm.add_skill(name="sound", description="recorded conversations", owner="alice",
                 requires_toolsets=["mcp:convo"], procedure=["search the recordings"],
                 status="published", source="managed", confidence=1.0)
    sm.add_skill(name="handmade", description="unaudited user skill", owner="alice",
                 procedure=["x"], status="published", source="user")
    return sm


def test_index_for_gates_mcp_requirements_on_server_availability(tmp_path, monkeypatch):
    sm = _skills(tmp_path, monkeypatch)
    up = with_mcp_toolsets({"ask_user"}, FakeManager())
    down = with_mcp_toolsets({"ask_user"}, FakeManager(status="error"))
    assert "sound" in {s["name"] for s in sm.index_for(owner="alice", active_toolsets=list(up))}
    assert "sound" not in {s["name"] for s in sm.index_for(owner="alice", active_toolsets=list(down))}


def test_managed_skills_skip_the_audit_gate(tmp_path, monkeypatch):
    sm = _skills(tmp_path, monkeypatch, auto_approve=True)
    names = {s["name"] for s in sm.index_for(owner="alice")}
    assert "sound" in names          # managed: no audit needed
    assert "handmade" not in names   # unchanged upstream rule for other skills
    sm.set_audit("sound", "needs_work", owner="alice")
    assert "sound" in {s["name"] for s in sm.index_for(owner="alice")}
    assert is_managed_skill({"source": "managed"}) and not is_managed_skill({"source": "user"})


def test_relevant_skills_match_with_mcp_toolsets(tmp_path, monkeypatch):
    sm = _skills(tmp_path, monkeypatch)
    found = sm.get_relevant_skills("search the recordings sound", skills=sm.load(owner="alice"),
                                   threshold=0.1,
                                   available_toolsets=with_mcp_toolsets(set(), FakeManager()))
    assert [s["name"] for s in found] == ["sound"]


# --- the invoke message -----------------------------------------------------

def test_invoke_route_message_is_recognized(tmp_path, monkeypatch):
    """Round trip through the real /invoke route, so a format change upstream fails here."""
    from routes import skills_routes

    sm = _skills(tmp_path, monkeypatch)
    monkeypatch.setattr(skills_routes, "get_current_user", lambda request: "alice")
    app = FastAPI()
    app.include_router(skills_routes.setup_skills_routes(sm))
    r = TestClient(app).post("/api/skills/sound/invoke",
                             json={"request": "what did I tell Emily last Wednesday?"})
    assert r.status_code == 200
    message = r.json()["message"]
    assert message.startswith(INVOKE_PREFIX)
    invoked = parse_invoke_message(message)
    assert invoked is not None
    assert (invoked.name, invoked.request) == ("sound", "what did I tell Emily last Wednesday?")


@pytest.mark.parametrize("text", [
    "what did Alice say?",
    "Apply the skill below to my request",           # prefix only
    INVOKE_PREFIX + "no frontmatter\n--- END SKILL ---\n\nRequest: x",
    INVOKE_PREFIX + "---\nname: sound\n---\nbody without end marker",
])
def test_other_messages_are_not_invocations(text):
    assert parse_invoke_message(text) is None


# --- pinning the turn contract ---------------------------------------------

def _message(name="sound", request="What did Alice say about the budget?"):
    return (INVOKE_PREFIX + f"---\nname: {name}\nrequires_toolsets: [\"mcp:convo\"]\n---\n\n"
            "## Procedure\n\n1. Search the recordings.\n\n--- END SKILL ---\n\n"
            f"Request: {request}")


SCHEMAS = ([_schema(n) for n in ("ask_user", "update_plan", "manage_calendar", "python",
                                  "manage_skills", "read_file")]
           + [_schema(_q(t)) for t in CONVO])


def _routed(message):
    """Roughly what the route classifier offers for an untagged turn."""
    return resolve_turn_contract(capabilities={"calendar"}, schemas=SCHEMAS,
                                 policy=build_effective_tool_policy(), message=message)


@pytest.fixture
def owner_skills(monkeypatch):
    skills = {"sound": {"name": "sound", "requires_toolsets": ["mcp:convo"]},
              "plain": {"name": "plain", "requires_toolsets": []}}
    monkeypatch.setattr(skill_invocation, "_invokable_skill",
                        lambda name, owner: skills.get(name))
    return skills


def _pin(message, manager=None):
    contract = _routed(message)
    return contract, pin_skill_contract(contract, message=message, owner="alice", schemas=SCHEMAS,
                                        policy=build_effective_tool_policy(),
                                        manager=manager or FakeManager())


def test_slash_skill_turn_offers_only_its_tools(owner_skills):
    _, pinned = _pin(_message())
    assert pinned.offered == {_q(t) for t in CONVO} | {"ask_user", "update_plan"}
    assert not pinned.unavailable
    # schemas match offered, so the agent loop sends exactly these
    assert {s["function"]["name"] for s in pinned.schemas()} == set(pinned.offered)


def test_untagged_turn_is_unchanged(owner_skills):
    before, after = _pin("What did Alice say about the budget?")
    assert after is before


def test_earlier_slash_message_does_not_carry_over(owner_skills):
    """Only the current message counts: the pin takes no history, keeps no state."""
    _pin(_message())                                    # turn 1: /sound ...
    before, after = _pin("And what about October?")     # turn 2: no slash command
    assert after is before
    assert not any(n.startswith("mcp__") for n in after.offered)


def test_skill_without_toolsets_or_unknown_skill_is_unchanged(owner_skills):
    for msg in (_message(name="plain"), _message(name="not-mine")):
        before, after = _pin(msg)
        assert after is before


def test_disconnected_module_is_reported_unavailable(owner_skills):
    _, pinned = _pin(_message(), manager=FakeManager(status="error"))
    assert not pinned.offered
    assert "mcp:convo" in pinned.unavailable


def test_policy_still_applies_to_pinned_tools(owner_skills):
    message = _message()
    pinned = pin_skill_contract(
        _routed(message), message=message, owner="alice", schemas=SCHEMAS,
        policy=build_effective_tool_policy(disabled_tools={_q("update_action_item")}),
        manager=FakeManager())
    assert _q("update_action_item") not in pinned.offered
    assert _q("search_transcripts") in pinned.offered


def test_write_tools_keep_approval(monkeypatch):
    """Pinning offers write tools; trust (no approval) stays limited to readOnlyHint tools."""
    from src.mcp_trust import is_trusted_readonly_mcp_tool

    mgr = FakeManager()
    mgr._tools["8bd5fb02"] = [
        {"name": "search_transcripts", "annotations": {"readOnlyHint": True}},
        {"name": "update_action_item", "annotations": {"readOnlyHint": False}},
    ]
    monkeypatch.setenv("ODYSSEUS_TRUSTED_READONLY_MCP", "convo")
    assert is_trusted_readonly_mcp_tool(_q("search_transcripts"), mgr)
    assert not is_trusted_readonly_mcp_tool(_q("update_action_item"), mgr)


def test_slash_skill_with_tools_needs_agent_mode(owner_skills):
    """Chat mode never builds a turn contract, so the route must switch to agent mode."""
    assert invoked_skill_needs_tools(_message(), "alice")
    assert not invoked_skill_needs_tools(_message(name="plain"), "alice")
    assert not invoked_skill_needs_tools(_message(name="not-mine"), "alice")
    assert not invoked_skill_needs_tools("What did Alice say about the budget?", "alice")


def test_chat_route_escalates_slash_skills_to_agent_mode():
    """The hook sits in the chat→agent escalation, before the turn contract is built."""
    import inspect

    import routes.chat_routes as chat_routes

    src = inspect.getsource(chat_routes)
    hook = src.index("invoked_skill_needs_tools(message, effective_user(request))")
    assert src.index("auto_escalated = False") < hook < src.index("pin_skill_contract(_turn_contract")


def test_crlf_message_from_the_web_ui_is_recognized(owner_skills):
    """Browsers send a textarea's line breaks as CRLF; the UI's /sound must still match."""
    crlf = _message().replace("\n", "\r\n")
    assert parse_invoke_message(crlf) == parse_invoke_message(_message())
    assert invoked_skill_needs_tools(crlf, "alice")
    _, pinned = _pin(crlf)
    assert pinned.offered == {_q(t) for t in CONVO} | {"ask_user", "update_plan"}
