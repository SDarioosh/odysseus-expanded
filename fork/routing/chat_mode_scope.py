"""Chat-mode questions about servers get an answer, not a shell command.

Upstream promotes a chat-mode turn to agent mode when ``src/action_intents.py``
matches a tool action. For the ``shell`` and ``workspace`` categories it also
turns bash on. Several of those patterns are broad: "named computer task
request" matches any "on <word>" ("my home assistant on port 8123"), and
"terminal workspace request" matches a bare "docker" or "git" anywhere. So a
question such as "why can I not access my home assistant on port 8123? how can I
check if my server has port 8123 active?" asked in chat mode came back as an
approval card for ``ssh ... "ss -tlnp"`` instead of an explanation.

:func:`keep_chat_for_question` says when such a turn should stay in chat mode:
the message asks how or why (the user is the one who will act) and doesn't ask
the assistant to do anything ("can you check ...", "run ...", "please ...",
"for me"). Agent mode, the web-search toggle and the other categories
(calendar, notes, email, web) are left alone.
"""

from __future__ import annotations

import re
from typing import Any

# Categories whose escalation turns bash/workspace tools on.
_SHELL_CATEGORIES = frozenset({"shell", "workspace"})

# The user asks how to do something, or why something happens.
_QUESTION = re.compile(
    r"\bhow\s+(?:can|could|do|does|did|should|would|might|to)\b"
    r"|\bwhy\b"
    r"|\bwhat\s+(?:command|commands|tool|tools|should|would|could|can|does|do|is\s+the\s+(?:command|way|best\s+way))\b"
    r"|\bis\s+there\s+(?:a|any)\s+(?:way|command|tool)\b"
    r"|\bwhich\s+(?:command|tool|port)\b",
    re.I,
)

# The assistant is asked to act. "can you explain/tell me ..." is still a question.
_ASKS_ASSISTANT = re.compile(
    r"\b(?:can|could|would|will)\s+you\s+(?!(?:please\s+)?(?:explain|tell|describe|show\s+me\s+how|help\s+me\s+(?:understand|figure))\b)"
    r"|\bplease\b"
    r"|\bfor\s+me\b"
    r"|\b(?:you\s+(?:should|need\s+to|have\s+to|must)|go\s+ahead|do\s+it|run\s+it|check\s+it)\b"
    r"|\bssh\s+(?:in)?to\b",
    re.I,
)

# A sentence that starts with a command verb is an instruction to the assistant.
_IMPERATIVE_START = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|now|then|and|so|just)[\s,]+)*"
    r"(?:run|execute|exec|ssh|check|restart|reboot|start|stop|kill|fix|debug|inspect|look\s+at|"
    r"open|read|find|tail|grep|deploy|install|build|test|show|list|connect|log\s+into)\b",
    re.I,
)

_SENTENCE_SPLIT = re.compile(r"[.!?;\n]+")


def is_explanatory_question(text: str) -> bool:
    """True when the message asks how/why and asks the assistant to do nothing."""
    if not text or not _QUESTION.search(text):
        return False
    if _ASKS_ASSISTANT.search(text):
        return False
    return not any(_IMPERATIVE_START.match(part) for part in _SENTENCE_SPLIT.split(text))


def keep_chat_for_question(message: Any, tool_intent: Any) -> bool:
    """True when a chat-mode shell/workspace escalation should not happen."""
    if not isinstance(message, str) or tool_intent is None:
        return False
    if not getattr(tool_intent, "needs_tools", False):
        return False
    if getattr(tool_intent, "category", "") not in _SHELL_CATEGORIES:
        return False
    return is_explanatory_question(message)
