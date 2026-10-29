"""The order and short-circuits of the chat route's turn policy (fork/routing/turn_policy.py).

What each step decides for real turns is pinned through the route in
``test_turn_policy_characterization.py``; these tests pin which helper runs when, so
the chaining that used to hide inside ``invoked_skill_needs_tools`` and
``pin_skill_contract`` stays explicit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fork.routing import turn_policy
from fork.routing.skill_invocation import INVOKE_PREFIX

SLASH = (INVOKE_PREFIX + "---\nname: athena\n---\n\n## Procedure\n\n1. x\n\n"
         "--- END SKILL ---\n\nRequest: What did Alice say?")


@pytest.fixture
def calls(monkeypatch):
    """Each helper the policy may call records its name and answers from ``answers``."""
    from fork.mcp import trust
    from fork.routing import (
        auto_mode,
        chat_mode_scope,
        module_turn_scope,
        offered_reads,
        personal_read_grants,
        personal_store_escalation,
        skill_invocation,
        web_turn_scope,
    )

    seen: list[str] = []
    answers: dict[str, object] = {}

    def spy(module, name, kind):
        def fake(first=None, *args, **kwargs):
            seen.append(name)
            if kind == "bool":
                return answers.get(name, False)
            return f"{first}+{name}" if isinstance(first, str) else first
        monkeypatch.setattr(module, name, fake)

    spy(chat_mode_scope, "keep_chat_for_question", "bool")
    spy(skill_invocation, "slash_skill_needs_tools", "bool")
    spy(module_turn_scope, "module_turn_requested", "bool")
    spy(personal_store_escalation, "personal_store_turn", "bool")
    spy(trust, "with_approved_tool_grant", "chain")
    spy(web_turn_scope, "with_unclassified_web_search", "chain")
    spy(personal_read_grants, "with_personal_store_reads", "chain")
    spy(auto_mode, "with_calculator_grant", "chain")
    spy(module_turn_scope, "with_module_tools", "chain")
    spy(skill_invocation, "pin_invoked_skill", "chain")
    spy(auto_mode, "with_calculator_offer", "chain")
    spy(offered_reads, "with_offered_read_grants", "chain")
    return SimpleNamespace(seen=seen, answers=answers)


def test_how_why_question_clears_the_intent_in_chat_mode_only(calls):
    intent = SimpleNamespace(reason="terminal workspace request")
    calls.answers["keep_chat_for_question"] = True
    assert turn_policy.scope_tool_intent(intent, chat_mode="chat", message="why?") is None
    assert turn_policy.scope_tool_intent(intent, chat_mode="agent", message="why?") is intent
    assert calls.seen == ["keep_chat_for_question"]  # agent mode doesn't ask


def test_slash_message_asks_only_the_skill(calls):
    calls.answers["slash_skill_needs_tools"] = True
    assert turn_policy.escalate_chat_mode("chat", False, message=SLASH, owner="alice") == ("agent", True)
    assert calls.seen == ["slash_skill_needs_tools"]


def test_module_question_comes_before_store_lookup(calls):
    calls.answers["module_turn_requested"] = True
    assert turn_policy.escalate_chat_mode("chat", False, message="What did Alice say?",
                                          owner="alice") == ("agent", True)
    assert calls.seen == ["module_turn_requested"]
    calls.seen.clear()
    calls.answers.update(module_turn_requested=False, personal_store_turn=True)
    assert turn_policy.escalate_chat_mode("chat", False, message="my tasks?",
                                          owner="alice") == ("agent", True)
    assert calls.seen == ["module_turn_requested", "personal_store_turn"]


def test_escalation_leaves_agent_mode_and_unmatched_turns_alone(calls):
    assert turn_policy.escalate_chat_mode("agent", False, message="hi", owner="alice") == ("agent", False)
    assert calls.seen == []
    assert turn_policy.escalate_chat_mode("chat", False, message="hi", owner="alice") == ("chat", False)
    assert turn_policy.escalate_chat_mode("chat", True, message="hi", owner="alice") == ("chat", True)


def test_authority_grants_in_order(calls):
    out = turn_policy.extend_authority("A", exact_approval=None, session_id="s",
                                       web_search_enabled=True)
    assert calls.seen == ["with_unclassified_web_search", "with_personal_store_reads",
                          "with_calculator_grant"]
    assert out == "A+with_unclassified_web_search+with_personal_store_reads+with_calculator_grant"


def test_approval_continuation_gets_only_the_approved_tool(calls):
    out = turn_policy.extend_authority("A", exact_approval=object(), session_id="s",
                                       web_search_enabled=True)
    assert calls.seen == ["with_approved_tool_grant"]
    assert out == "A+with_approved_tool_grant"


def test_contract_scope_then_calculator(calls):
    out = turn_policy.extend_contract("C", message="What did Alice say?", owner="alice",
                                      session_id="s", schemas=[], policy=None)
    assert calls.seen == ["with_module_tools", "with_calculator_offer"]
    assert out == "C+with_module_tools+with_calculator_offer"
    calls.seen.clear()
    turn_policy.extend_contract("C", message=SLASH, owner="alice", session_id="s",
                                schemas=[], policy=None)
    assert calls.seen == ["pin_invoked_skill", "with_calculator_offer"]


def test_old_entry_points_are_the_policy_steps(calls):
    from fork.routing.skill_invocation import (
        invoked_skill_needs_tools,
        pin_skill_contract,
    )

    calls.answers["personal_store_turn"] = True
    assert invoked_skill_needs_tools("my tasks?", "alice")
    assert calls.seen == ["module_turn_requested", "personal_store_turn"]
    calls.seen.clear()
    pin_skill_contract("C", message=SLASH, owner="alice", schemas=[], policy=None)
    assert calls.seen == ["pin_invoked_skill"]  # no calculator: that is the route's step


def test_offered_reads_only_on_agent_turns(calls):
    assert turn_policy.admit_offered_reads("A", chat_mode="chat", contract=None, policy=None,
                                           disabled_tools=(), owner="alice") == "A"
    assert calls.seen == []
    assert turn_policy.admit_offered_reads("A", chat_mode="agent", contract=None, policy=None,
                                           disabled_tools=(), owner="alice") == "A+with_offered_read_grants"


def test_read_actions_have_one_source():
    from fork.routing.offered_reads import OFFERED_READ_TOOLS, read_actions
    from fork.routing.personal_read_grants import (
        PERSONAL_STORE_TOOLS,
        store_read_actions,
    )
    from fork.upstream import private_action_reads

    for tool in set(PERSONAL_STORE_TOOLS) | OFFERED_READ_TOOLS | {"manage_settings", "nope"}:
        assert store_read_actions(tool) == frozenset(private_action_reads().get(tool, ()))
        assert read_actions(tool) == (store_read_actions(tool) if tool in OFFERED_READ_TOOLS
                                      else frozenset())
