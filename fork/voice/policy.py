"""Fork: what a voice turn's tool calls may do (docs/fork/voice-assistant.md, "How a
question is answered").

A voice turn is offered the same tools as a chat turn of its owner, in the same order
(the shared prompt cache, ``fork/brain/turn.py`` ``read_only_turn``). What it may run is
decided here, at execution, by :class:`VoiceGate` (``ToolRun.gate``, called by
``fork/brain/tools.py`` before its read-only refusal):

1. email tools are never run (the answer is read aloud, to anyone in the room);
2. with ``FORK_VOICE_PRIVATE=owner`` and a voice that isn't the owner's
   (``fork/voice/satellite.py``, ``personal_allowed``), the personal tools are refused
   with its ``PRIVATE_REPLY``; web and weather stay;
3. reads fall through to the brain's read-only checks;
4. a write of the allow-list (``fork/voice/confirm.py``) is kept as a proposal, not run
   (a to-do's due date the owner never said is dropped first: ``without_unsaid_due``);
5. every other write falls through to the brain's read-only refusal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fork.voice import confirm

EMAIL_TOOLS = frozenset({
    "list_email_accounts", "list_emails", "search_emails", "read_email", "draft_email",
    "draft_email_reply", "send_email", "reply_to_email",
})
EMAIL_REFUSED = ("Not run: email isn't available by voice (the answer is read aloud to "
                 "anyone in the room). The owner can read email in the Odysseus app.")
PRIVATE_REPLY = "I can only answer that for the owner of this assistant."
PERSONAL_TOOLS = frozenset({
    "manage_calendar", "manage_notes", "manage_tasks", "resolve_contact", "manage_contact",
    "manage_memory", "search_beliefs", "search_documents", "manage_documents",
    "create_document", "edit_document", "update_document", "suggest_document",
    "search_chats",
}) | EMAIL_TOOLS


def is_personal(name: str) -> bool:
    """Athena (every MCP tool, to be safe), the personal stores, documents, chats."""
    return name.startswith("mcp__") or name in PERSONAL_TOOLS


@dataclass
class VoiceGate:
    owner: Any
    session_id: str
    turn_id: str
    personal: bool = True  # this speaker may get personal answers
    private_reply: str = PRIVATE_REPLY
    earlier: str = ""  # the conversation's previous question (a date said there counts)

    async def __call__(self, run: Any, name: str, content: str) -> str | dict | None:
        """Instead of running the call: a refusal (str), the result of a kept proposal
        (dict), or None to go on."""
        from fork.brain import toolset

        if name in EMAIL_TOOLS:
            return EMAIL_REFUSED
        if not self.personal and is_personal(name):
            return self.private_reply
        try:
            if toolset.is_read(name, content):
                return None
        except Exception:  # noqa: BLE001 -- unknown: treated as a write
            pass
        said = f"{self.earlier} {getattr(run, 'request_text', '') or ''}"
        content = confirm.without_unsaid_due(name, content, said)
        change = await confirm.describe(self.owner, name, content)
        if change is None:
            return None  # the brain's read-only refusal
        confirm.keep(self.owner, self.session_id, self.turn_id, run, name, content, change)
        return {"output": confirm.PROPOSED, "exit_code": 0, "proposed": True}
