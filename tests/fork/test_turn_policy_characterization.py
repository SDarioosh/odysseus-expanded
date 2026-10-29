"""What the fork's turn hooks in ``routes/chat_routes.py`` decide, pinned per turn.

Drives the real ``/api/chat_stream`` route (only session, context and inference are
stubbed, as in ``tests/test_foreground_model_routing.py``) over a table of fictional
turns and records, for each one, what the fork's hooks decide:

* chat or agent mode (the ``turn_mode`` event, ``auto_escalated``),
* the request authority handed to the agent loop (grants with their actions and
  inputs, denials, sealed backends and owned scopes),
* the turn contract (capabilities, offered tools, required and unavailable tools),
* the tool policy's disabled tools for the offered and personal-store tools,
* the chat's remembered Auto/Ask choice.

The table covers plain chat, how/why questions, the web toggle, Auto vs Ask, slash
skills (``/athena``, ``/context``, a skill without tools, another owner's skill),
personal-store questions, module questions, follow-ups after a used module tool,
exact-approval continuations, owner vs non-owner, trusted vs untrusted MCP servers
and odd messages. The expected decisions are in ``fixtures/turn_policy_decisions.json``,
recorded on the code before ``fork/routing/turn_policy.py`` existed, so a refactor of
the hooks must keep every decision. ``UPDATE_SNAPSHOTS=1`` rewrites the file; only do
that for an intended behaviour change, and explain each changed line.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from tests.test_foreground_model_routing import _chat_stream_endpoint, _RouteRequest

FIXTURE = Path(__file__).parent / "fixtures" / "turn_policy_decisions.json"
SERVER = "athena01"
OTHER = "notes01"
ATHENA_TOOLS = {
    "search_transcripts": True, "search_conversations": True, "get_conversation": True,
    "list_people": True, "list_action_items": True,
    "update_action_item": False, "name_speaker": False,
}
PERSONAL = {"manage_contact", "manage_notes", "manage_calendar", "manage_tasks",
            "manage_memory", "manage_documents", "manage_research", "manage_skills",
            "calculate", "web_search", "web_fetch"}


def _q(tool, server=SERVER):
    return f"mcp__{server}__{tool}"


def _slash(name, request, toolsets='["mcp:convo"]'):
    from fork.routing.skill_invocation import INVOKE_PREFIX

    return (INVOKE_PREFIX + f"---\nname: {name}\nrequires_toolsets: {toolsets}\n---\n\n"
            "## Procedure\n\n1. Search the recordings.\n\n--- END SKILL ---\n\n"
            f"Request: {request}")


def _used(*tools):
    """Session history whose last answer ran ``tools`` successfully."""
    return [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer",
         "metadata": {"tool_events": [{"tool": t, "status": "done"} for t in tools]}},
    ]


# name -> turn. Keys: message, mode (chat/agent), web (toggle on), tool_mode
# (auto/ask/None), trusted (env set), owner (admin/single user), history,
# approval (tool name of an exact approval to continue), form (extra fields).
TURNS: dict[str, dict] = {}


def _turn(name, message, **kw):
    assert name not in TURNS
    TURNS[name] = {"message": message, **kw}


for _mode in ("chat", "agent"):
    m = _mode
    _turn(f"{m}-plain", "What is the capital of France?", mode=m)
    _turn(f"{m}-hello", "hello", mode=m)
    _turn(f"{m}-empty", "", mode=m)
    _turn(f"{m}-spaces", "   ", mode=m)
    _turn(f"{m}-emoji", "\U0001f44d", mode=m)
    _turn(f"{m}-howto-port", "how can I check if my server has port 8123 active?", mode=m)
    _turn(f"{m}-why-docker", "why does docker say permission denied?", mode=m)
    _turn(f"{m}-why-home-assistant", "why can I not access my home assistant on port 8123? "
          "how can I check if my server has port 8123 active?", mode=m)
    _turn(f"{m}-why-git", "why does git say detached head?", mode=m)
    _turn(f"{m}-git-please", "why does git say detached head? please fix it", mode=m)
    _turn(f"{m}-run-port", "check if port 8123 is open on my server", mode=m)
    _turn(f"{m}-can-you-run", "can you run git status for me?", mode=m)
    _turn(f"{m}-web-off-compare", "mistral 7b vs qwen3.5-9b", mode=m, web=False)
    _turn(f"{m}-web-on-compare", "mistral 7b vs qwen3.5-9b", mode=m, web=True)
    _turn(f"{m}-web-on-plain", "What is the capital of France?", mode=m, web=True)
    _turn(f"{m}-web-on-search", "search the web for the latest llama.cpp release", mode=m, web=True)
    _turn(f"{m}-web-off-search", "search the web for the latest llama.cpp release", mode=m, web=False)
    _turn(f"{m}-weather", "what's the weather in Lisbon today?", mode=m)
    _turn(f"{m}-contacts", "list the contact for Alex Example", mode=m)
    _turn(f"{m}-tasks", "what are my tasks for today?", mode=m)
    _turn(f"{m}-calendar", "what's on my calendar tomorrow?", mode=m)
    _turn(f"{m}-notes", "what is in my notes about the trip", mode=m)
    _turn(f"{m}-memory", "what do you remember about my allergies?", mode=m)
    _turn(f"{m}-documents", "find my document about the garden plan", mode=m)
    _turn(f"{m}-tool-named", "use the manage_contacts tool for Alex Example", mode=m)
    _turn(f"{m}-no-tools", "list the contact for Alex Example, no tools please", mode=m)
    _turn(f"{m}-add-task", "add a task to buy milk tomorrow", mode=m)
    _turn(f"{m}-remind", "remind me to call the dentist at 3pm", mode=m)
    _turn(f"{m}-module-said", "What did Alice say about the budget?", mode=m)
    _turn(f"{m}-module-person", "What is Jonas Example allergic to?", mode=m)
    _turn(f"{m}-module-meeting", "what were the action items from the standup?", mode=m)
    _turn(f"{m}-module-untrusted", "What did Alice say about the budget?", mode=m, trusted=False)
    _turn(f"{m}-module-conversation", "summarize my conversation with Bob yesterday", mode=m)
    _turn(f"{m}-our-conversation", "what did we discuss in this conversation?", mode=m)
    _turn(f"{m}-my-dentist", "when is my dentist appointment?", mode=m)
    _turn(f"{m}-slash-athena", _slash("athena", "What did Alice say about the budget?"), mode=m)
    _turn(f"{m}-slash-athena-crlf",
          _slash("athena", "What did Alice say?").replace("\n", "\r\n"), mode=m)
    _turn(f"{m}-slash-context", _slash("context", "when is the dentist?",
                                       '["mcp:convo", "manage_calendar"]'), mode=m)
    _turn(f"{m}-slash-plain", _slash("plain", "tell me a joke", "[]"), mode=m)
    _turn(f"{m}-slash-not-mine", _slash("bobs", "What did Alice say?"), mode=m)
    _turn(f"{m}-slash-unknown", _slash("nope", "What did Alice say?"), mode=m)
    _turn(f"{m}-slash-untrusted", _slash("athena", "What did Alice say?"), mode=m, trusted=False)
    _turn(f"{m}-slash-broken", "Apply the skill below to my request, following its "
          "Procedure / Pitfalls / Verification.\n\n--- BEGIN SKILL ---\nno front matter",
          mode=m)
    _turn(f"{m}-math-auto", "what is 17 * 23?", mode=m, tool_mode="auto")
    _turn(f"{m}-math-ask", "what is 17 * 23?", mode=m, tool_mode="ask")
    _turn(f"{m}-tasks-ask", "what are my tasks for today?", mode=m, tool_mode="ask")
    _turn(f"{m}-module-ask", "What did Alice say about the budget?", mode=m, tool_mode="ask")
    _turn(f"{m}-web-on-ask", "mistral 7b vs qwen3.5-9b", mode=m, web=True, tool_mode="ask")
    _turn(f"{m}-followup-module", "and what did she promise?", mode=m,
          history=_used(_q("search_transcripts")))
    _turn(f"{m}-followup-speaker", "Speaker 2 was Kath", mode=m,
          history=_used(_q("search_transcripts")))
    _turn(f"{m}-followup-untrusted", "and what did she promise?", mode=m,
          history=_used(_q("search_transcripts")), trusted=False)
    _turn(f"{m}-followup-other-server", "and the next one?", mode=m,
          history=_used(_q("search_notes", OTHER)))
    _turn(f"{m}-followup-calendar", "and the day after?", mode=m,
          history=_used("manage_calendar"))
    _turn(f"{m}-nonowner-tasks", "what are my tasks for today?", mode=m, owner=False)
    _turn(f"{m}-nonowner-module", "What did Alice say about the budget?", mode=m, owner=False)
    _turn(f"{m}-nonowner-slash", _slash("athena", "What did Alice say?"), mode=m, owner=False)
    _turn(f"{m}-nonowner-web", "mistral 7b vs qwen3.5-9b", mode=m, web=True, owner=False)
    _turn(f"{m}-approve-module", "Speaker 1 was Marisol", mode=m,
          approval=_q("name_speaker"))
    _turn(f"{m}-approve-module-untrusted", "Speaker 1 was Marisol", mode=m,
          approval=_q("name_speaker"), trusted=False)
    _turn(f"{m}-approve-other-server", "search my notes", mode=m,
          approval=_q("search_notes", OTHER))
    _turn(f"{m}-approve-ask", "Speaker 1 was Marisol", mode=m,
          approval=_q("name_speaker"), tool_mode="ask")
    _turn(f"{m}-mode-field-odd", "what is 2 + 2?", mode=m, tool_mode="sometimes")
    _turn(f"{m}-plan", "plan how to reorganize my notes", mode=m, form={"plan_mode": "true"})


@pytest.fixture
def route_world(monkeypatch, data_dir):
    """A trusted Athena-like server ``convo``, an untrusted ``Notes`` server, skills."""
    import fork.routing.module_turn_scope as mts
    import services.memory.skills as skills_mod
    from fork.routing import auto_mode
    from services.memory.skills import SkillsManager
    from src.mcp_manager import McpManager
    from src.tool_utils import get_mcp_manager, set_mcp_manager

    previous = get_mcp_manager()
    manager = McpManager()
    manager._tools[SERVER] = [
        {"name": n, "description": n, "input_schema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": ro}}
        for n, ro in ATHENA_TOOLS.items()
    ]
    manager._tools[OTHER] = [
        {"name": "search_notes", "description": "search notes",
         "input_schema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": True}},
    ]
    manager._connections[SERVER] = {"status": "connected", "name": "convo"}
    manager._connections[OTHER] = {"status": "connected", "name": "Notes"}

    def identity(qualified):
        """What a connected server's ``resource_identity`` returns (no real session)."""
        from src.agent_runtime.resources import ExternalResource

        parts = qualified.split("__", 2)
        if len(parts) != 3 or not any(t["name"] == parts[2] for t in manager._tools.get(parts[1], [])):
            return None
        return ExternalResource("mcp", f"endpoint:{parts[1]}", parts[1], qualified, "incarnation")

    manager.resource_identity = identity
    set_mcp_manager(manager)
    monkeypatch.setattr("fork.routing.skill_mcp_toolsets._disabled_map", dict)
    # The people cache is fresh, so no background lookup runs during a turn.
    monkeypatch.setattr(mts, "_people", frozenset({"Jonas Example"}))
    monkeypatch.setattr(mts, "_people_at", time.monotonic())
    monkeypatch.setattr(auto_mode, "_CHAT_MODES", {})
    monkeypatch.setattr(skills_mod, "_approval_policy", lambda owner: (True, 0.85))
    sm = SkillsManager(str(data_dir))
    for name, owner, toolsets in (
        ("athena", "alice", ["mcp:convo"]),
        ("context", "alice", ["mcp:convo", "manage_calendar"]),
        ("plain", "alice", []),
        ("bobs", "bob", ["mcp:convo"]),
    ):
        sm.add_skill(name=name, description=f"{name} skill", owner=owner,
                     requires_toolsets=toolsets, procedure=["search the recordings"],
                     status="published", source="managed", confidence=1.0)
    try:
        yield manager
    finally:
        set_mcp_manager(previous)


_NATIVE = re.compile(
    r"NativeBackendResource\(tool_id='([^']*)', namespace='native', external=False, contained=False\)")


_FAKE_MCP = re.compile(
    r"ExternalResource\(namespace='mcp', endpoint_id='endpoint:(\w+)', server_id='\1', "
    r"tool_id='([^']*)', incarnation='incarnation', external=True, contained=False, owner=''\)")


def _backend(resource) -> str:
    """A sealed backend: ``native:<tool>`` for a plain native one, ``mcp:<tool>`` for one of
    the fake servers' tools as ``route_world`` seals it, else its full repr."""
    text = repr(resource)
    if match := _NATIVE.fullmatch(text):
        return f"native:{match[1]}"
    if match := _FAKE_MCP.fullmatch(text):
        return f"mcp:{match[2]}"
    return text


def _authority(authority):
    if authority is None:
        return None
    return {
        "grants": [[g.tool, None if g.actions is None else sorted(g.actions),
                    None if g.inputs is None else sorted(g.inputs)] for g in authority.grants],
        "denied": sorted(authority.denied),
        "block_all": authority.block_all,
        "disable_mcp": authority.disable_mcp,
        "inherited": authority.inherited,
        "backends": None if authority.backend_resources is None
        else [_backend(b) for b in authority.backend_resources],
        "owned_scopes": None if authority.owned_scopes is None
        else [repr(s) for s in authority.owned_scopes],
    }


def _contract(contract):
    if contract is None:
        return None
    schema_names = sorted(json.loads(s)["function"]["name"] for s in contract.schema_json)
    return {
        "capabilities": sorted(contract.capabilities),
        "offered": sorted(contract.offered),
        "required": sorted(contract.required),
        "unavailable": sorted(contract.unavailable),
        "schemas": schema_names,
        "executable_mcp": sorted(n for n in contract.executable if n.startswith("mcp__")),
        "executable_calculate": "calculate" in contract.executable,
    }


async def _run(monkeypatch, turn):
    from fork.routing import auto_mode
    from routes import chat_routes
    from src import tool_security

    if turn.get("trusted", True):
        monkeypatch.setenv("ODYSSEUS_TRUSTED_READONLY_MCP", "convo")
    else:
        monkeypatch.delenv("ODYSSEUS_TRUSTED_READONLY_MCP", raising=False)
    monkeypatch.delenv("ODYSSEUS_PERSONAL_READ_GRANTS", raising=False)
    monkeypatch.delenv("ODYSSEUS_PERSONAL_STORE_ESCALATION", raising=False)
    mode = turn.get("mode", "chat")
    captured: dict = {}
    endpoint = _chat_stream_endpoint(monkeypatch, mode, captured,
                                     session_history=turn.get("history", ()))
    message = turn["message"]
    monkeypatch.setattr(chat_routes, "coerce_message_and_session",
                        lambda *args, **kwargs: (message, "session-1"))
    owner = turn.get("owner", True)
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda o: owner)
    seen = []

    async def capture_agent(*args, **kwargs):
        seen.append(kwargs)
        yield 'data: {"delta":"agent ran"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_agent_loop", capture_agent)
    request = _RouteRequest(mode)
    form = request._form
    form["message"] = message
    form["compare_mode"] = "false"
    form.pop("allow_web_search", None)
    if turn.get("web"):
        form["allow_web_search"] = "true"
    elif turn.get("web") is False:
        form["allow_web_search"] = "false"
    if turn.get("tool_mode") is not None:
        form[auto_mode.CHAT_MODE_FIELD] = turn["tool_mode"]
    form.update(turn.get("form") or {})
    if turn.get("approval"):
        from src.tool_capabilities import capabilities_for_action

        pending = chat_routes.tool_approval_store.create(
            owner="alice", session_id="session-1", origin_run_id="run-1",
            tool_name=turn["approval"], content='{"speaker": "Speaker 1", "name": "Marisol"}',
            workspace=None, external_untrusted_context_seen=False,
            capabilities=capabilities_for_action(turn["approval"], "{}"),
            request_text=message,
        )
        form["tool_approval_id"] = pending.approval_id
        form["tool_approval_decision"] = "approve"
    response = await endpoint(request)
    chunks = [chunk async for chunk in response.body_iterator]
    turn_mode = None
    for chunk in chunks:
        if chunk.startswith("data: "):
            try:
                event = json.loads(chunk[6:])
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "turn_mode":
                turn_mode = {k: v for k, v in event.items() if k != "type"}
    out = {
        "status": response.status_code,
        "turn_mode": turn_mode,
        "ran": "agent" if seen else ("chat" if "chat" in captured else None),
        "remembered_auto": auto_mode._CHAT_MODES.get(("alice", "session-1")),
    }
    if seen:
        kw = seen[0]
        policy = kw.get("tool_policy")
        contract = kw.get("turn_contract")
        watched = PERSONAL | set(contract.offered if contract else ()) | {
            _q(t) for t in ATHENA_TOOLS} | {_q("search_notes", OTHER)}
        out.update({
            "authority": _authority(kw.get("request_authority")),
            "contract": _contract(contract),
            "policy_blocks": sorted(n for n in watched if policy is not None and policy.blocks(n)),
            "disabled_watched": sorted(set(kw.get("disabled_tools") or ()) & watched),
            "forced_tools": sorted(kw.get("forced_tools") or ()),
            "exact_approval": kw.get("exact_approval") is not None,
        })
    return out


def _load():
    try:
        return json.loads(FIXTURE.read_text())
    except FileNotFoundError:
        return {}


def _save(recorded: dict) -> None:
    """One line per turn, so a changed decision shows as one changed line."""
    lines = [f"{json.dumps(k)}: {json.dumps(v, sort_keys=True, separators=(',', ':'))}"
             for k, v in sorted(recorded.items())]
    FIXTURE.write_text("{\n" + ",\n".join(lines) + "\n}\n")


_UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(TURNS))
async def test_turn_decisions_are_unchanged(monkeypatch, route_world, name):
    got = await _run(monkeypatch, TURNS[name])
    # Round-trip so tuples and lists compare the way the file stores them.
    got = json.loads(json.dumps(got))
    if _UPDATE:
        recorded = _load()
        recorded[name] = got
        _save(recorded)
        return
    expected = _load().get(name)
    assert expected is not None, f"no recorded decision for {name}; run with UPDATE_SNAPSHOTS=1"
    assert got == expected


def test_every_recorded_turn_is_still_in_the_table():
    assert set(_load()) == set(TURNS)


def test_the_table_covers_both_outcomes():
    """A sanity check on the recording: the table exercises every hook both ways."""
    recorded = _load()
    agent = [r for r in recorded.values() if r["ran"] == "agent"]
    assert any(r["ran"] == "chat" for r in recorded.values())
    assert any(r["turn_mode"] and r["turn_mode"]["auto_escalated"] for r in agent)
    grants = [{g[0] for g in r["authority"]["grants"]} for r in agent if r["authority"]]
    assert any("web_search" in g for g in grants)
    assert any("calculate" in g for g in grants)
    assert any("manage_tasks" in g for g in grants)
    assert any(_q("name_speaker") in g for g in grants)
    offered = [set(r["contract"]["offered"]) for r in agent if r["contract"]]
    assert any("calculate" in o for o in offered)
    assert any(_q("search_transcripts") in o for o in offered)
    assert any(_q("update_action_item") in o for o in offered)
    assert any(r["exact_approval"] for r in agent)
    assert any(r["remembered_auto"] is False for r in recorded.values())
