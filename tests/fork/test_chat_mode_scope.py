"""Chat-mode how/why questions stay in chat mode (src/chat_mode_scope.py)."""

import pytest

from src.action_intents import classify_tool_intent
from src.chat_mode_scope import is_explanatory_question, keep_chat_for_question


STAY_CHAT = [
    "why can I not access my home assistant on port 8123? how can I check if my server has port 8123 active?",
    "why is my docker container not starting?",
    "why does git say my branch has diverged?",
    "what command shows which process is using port 443 on my server?",
    "is there a way to see the docker logs from the terminal?",
    "can you explain why my tmux session dies on logout?",
    "how would I check whether the server is running?",
]

ESCALATE = [
    "can you check if my server has port 8123 active?",
    "check if port 8123 is running on homelab",
    "ssh into homelab and check port 8123",
    "why is port 8123 closed? run ss -tlnp on homelab to see",
    "please restart the docker container",
    "why is it down? ssh into homelab and look",
    "run the tests",
]


@pytest.mark.parametrize("message", STAY_CHAT)
def test_how_why_question_stays_chat(message):
    intent = classify_tool_intent(message)
    if intent.needs_tools:
        assert intent.category in {"shell", "workspace"}
    assert is_explanatory_question(message)
    assert keep_chat_for_question(message, intent) == intent.needs_tools


def test_screenshot_message_was_escalated_upstream():
    message = STAY_CHAT[0]
    intent = classify_tool_intent(message)
    assert intent.needs_tools and intent.category == "workspace"
    assert keep_chat_for_question(message, intent)


@pytest.mark.parametrize("message", ESCALATE)
def test_requests_to_act_still_escalate(message):
    intent = classify_tool_intent(message)
    assert intent.needs_tools
    assert not keep_chat_for_question(message, intent)


def test_other_categories_untouched():
    message = "why not remind me to call the plumber tomorrow"
    intent = classify_tool_intent(message)
    assert intent.needs_tools and intent.category == "notes"
    assert not keep_chat_for_question(message, intent)


def test_no_intent():
    assert not keep_chat_for_question("why is the sky blue?", None)
    assert not keep_chat_for_question(None, classify_tool_intent("run the tests"))
