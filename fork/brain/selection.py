"""Fork: what the owner has selected or open for a brain turn: selected notes and an open
email (docs/fork/brain.md, "Selected notes and open email").

Both go into the turn's volatile context message (``prompt.context_message``, position 4
of the prompt layout), never the stable system message, so llama-server's cached system
prompt, tools and history stay byte-identical. Each is wrapped the way upstream wraps
them for its agent (``src/prompt_security.py``'s ``untrusted_context_message``, with
upstream's labels), and the context message carries upstream's untrusted marking, so the
run is armed (``ToolRunSecurityContext.observe_messages``) and the turn's exchange is
replayed untrusted: a note or an email can carry injected instructions.

- **Selected notes:** the text ``chat_stream`` already loaded and formatted
  (``_format_selected_notes_context``: the owner's own notes only, ``Note.owner ==
  owner``); nothing is read again here. Cut to :data:`NOTES_CHARS`.
- **An open email:** upstream passes only its uid, folder and account (its cache
  enrichment in ``chat_stream`` imports names ``routes/email_routes.py`` doesn't export,
  so it never adds anything); its agent then calls ``read_email``. The brain makes that
  same call once while it sets the turn up (:func:`email_text`), through upstream's
  runner with the turn's authority, tool policy and disabled tools, so upstream's rules
  decide: ``read_email`` is admin-only (``NON_ADMIN_BLOCKED_TOOLS``) and never for an API
  token (``delegated_credential_blocked_tools``). When the turn doesn't offer it (those
  users, the global list, the ``email`` group off) no email content is shown, only
  :data:`EMAIL_UNAVAILABLE`. The call is the owner's own (they opened the email), not
  the model's, so it is made before the run is armed by this context; every call the
  model makes after it meets the armed gate. Cut to :data:`EMAIL_CHARS`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from typing import Any

logger = logging.getLogger(__name__)

NOTES_CHARS = 8000
EMAIL_CHARS = 6000
EMAIL_LOAD_SECONDS = 20.0
NOTES_LABEL = "Selected notes and todos"  # upstream's labels (routes/chat_helpers.py,
EMAIL_LABEL = "active email reader"       # src/agent_loop.py)
READ_EMAIL = "read_email"
NOTES_INTRO = ("The owner selected these notes and todos for this message. Use them "
               "directly; don't call manage_notes just to read them again.")
EMAIL_INTRO = ("The owner has this email open in the reader right now; \"this\", \"it\", "
               "\"summarise\" and \"reply\" refer to it. A reply is drafted with "
               "draft_email_reply (uid, folder, account below), which asks the owner first.")
EMAIL_UNAVAILABLE = ("[Context] The owner has an email open in the reader, but email "
                     "isn't available in this chat, so its content isn't shown.")
EMAIL_NOT_LOADED = ("The email could not be loaded; read_email with the uid, folder and "
                    "account above may still read it.")


def notes_item(text: Any) -> tuple[str, str] | None:
    """``(label, text)`` for the selected notes the route loaded, or None."""
    from fork.brain.tools import trim

    text = str(text or "").strip()
    if not text:
        return None
    return NOTES_LABEL, NOTES_INTRO + "\n\n" + trim(text, NOTES_CHARS)


def _pointer(email: dict) -> str:
    lines = [f"UID: {email.get('uid')}", f"Folder: {email.get('folder') or 'INBOX'}"]
    if email.get("account"):
        lines.append(f"Account: {email['account']}")
    return "\n".join(lines)


def _failed(result: dict, text: str) -> bool:
    return bool(result.get("error") or result.get("blocked")
                or (result.get("exit_code") not in (None, 0))
                or text.lstrip().startswith("Error:") or text == "(no output)")


async def email_text(run: Any, email: dict | None) -> tuple[str, str] | str | None:
    """The open email for this turn: ``(label, text)`` to show as outside content,
    :data:`EMAIL_UNAVAILABLE` (a plain line) when the turn may not read email, or None
    when no email is open. Never raises."""
    from fork.brain import tools as brain_tools

    uid = str((email or {}).get("uid") or "").strip()
    if not uid:
        return None
    if READ_EMAIL not in run.offered or READ_EMAIL in run.disabled:
        return EMAIL_UNAVAILABLE
    args = {"uid": uid, "folder": str(email.get("folder") or "INBOX")}
    if email.get("account"):
        args["account"] = str(email["account"])
    content = json.dumps(args, ensure_ascii=False)
    from src.tool_capabilities import ToolRunSecurityContext

    # The owner's call, made before this context arms the run: a security context of
    # its own (same credential), so the turn's own one stays as built.
    loader = replace(run, security=ToolRunSecurityContext(
        delegated_credential=run.security.delegated_credential), tainted=False)
    body = ""
    try:
        _, _, result = await asyncio.wait_for(
            brain_tools.execute(loader, READ_EMAIL, content), EMAIL_LOAD_SECONDS)
        text = brain_tools.result_text(result)
        if not _failed(result, text):
            body = brain_tools.trim(text, EMAIL_CHARS)
    except Exception:  # noqa: BLE001 -- the turn runs without the email's text
        logger.warning("brain: the open email could not be loaded", exc_info=True)
    return EMAIL_LABEL, EMAIL_INTRO + "\n\n" + _pointer(args) + "\n\n" + (
        body or EMAIL_NOT_LOADED)
