"""Fork: which Odysseus chat a voice question goes into
(docs/fork/voice-assistant.md, "Conversations").

Every voice conversation is an ordinary chat of the token's owner, in the folder
:data:`FOLDER`, so the owner can read what was asked and answered. A follow-up question
within :data:`FOLLOW_UP_SECONDS` of the last one continues the same chat, so "and what
about Friday?" works. A request names its conversation by, in this order:

1. the ``user`` field of the request (Home Assistant's OpenAI integrations send the
   conversation id there), or the ``X-Conversation-Id`` header;
2. otherwise, when the request carries earlier turns, its first question (a client that
   sends the whole conversation each time sends the same first question).

A request with neither starts a new chat. A first question (no earlier turns) always
starts a new chat, but is remembered under itself, so the next turn of that conversation,
which sends it as its first question, finds the chat. The map is kept in this process's
memory (one uvicorn worker, like the brain's paused turns); after a restart the next
question starts a new chat. Keys are per owner, so one owner's conversation never continues another's.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

FOLLOW_UP_SECONDS = 5 * 60
FOLDER = "Voice"
NAME_PREFIX = "Voice: "
MAX_CONVERSATIONS = 256

_MAP: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()
_LOCK = threading.Lock()


class NoModel(RuntimeError):
    """The owner has no default chat model configured."""


def conversation_key(*, explicit: str | None, first_question: str | None) -> str | None:
    """The key a conversation is found under, or None (a new chat every time)."""
    explicit = (explicit or "").strip()
    if explicit:
        return "id:" + explicit[:200]
    if first_question and first_question.strip():
        digest = hashlib.sha256(first_question.strip().encode("utf-8")).hexdigest()[:32]
        return "first:" + digest
    return None


def remembered(owner: str, key: str | None, *, now: float | None = None) -> str | None:
    """The chat id kept for ``key`` while it is fresh, else None."""
    if not key:
        return None
    now = time.time() if now is None else now
    with _LOCK:
        entry = _MAP.get((owner, key))
        if entry is None:
            return None
        session_id, last = entry
        if now - last > FOLLOW_UP_SECONDS:
            _MAP.pop((owner, key), None)
            return None
        return session_id


def remember(owner: str, key: str | None, session_id: str, *, now: float | None = None) -> None:
    if not key:
        return
    now = time.time() if now is None else now
    with _LOCK:
        _MAP[(owner, key)] = (session_id, now)
        _MAP.move_to_end((owner, key))
        while len(_MAP) > MAX_CONVERSATIONS:
            _MAP.popitem(last=False)


def forget_all() -> None:
    with _LOCK:
        _MAP.clear()


def default_model(owner: str) -> tuple[str, str, dict]:
    """The owner's default chat endpoint, model and headers (Settings, default model)."""
    from src.endpoint_resolver import resolve_endpoint

    url, model, headers = resolve_endpoint("default", owner=owner)
    if not url or not model:
        raise NoModel("No default chat model is set in Odysseus.")
    return str(url), str(model), dict(headers or {})


def _file_in_folder(session_id: str) -> None:
    from core.database import Session as DbSession
    from core.database import SessionLocal

    db = SessionLocal()
    try:
        row = db.query(DbSession).filter(DbSession.id == session_id).first()
        if row is not None:
            row.folder = FOLDER
            db.commit()
    finally:
        db.close()


def new_chat(session_manager: Any, owner: str, question: str) -> Any:
    """A new chat of ``owner`` for a voice conversation, on the default model."""
    url, model, headers = default_model(owner)
    session_id = str(uuid.uuid4())
    name = NAME_PREFIX + " ".join((question or "").split())[:60]
    sess = session_manager.create_session(session_id, name, url, model, owner=owner,
                                          headers=headers)
    try:
        _file_in_folder(session_id)
    except Exception:  # noqa: BLE001 -- the chat works outside the folder too
        logger.warning("voice: could not file the chat in the %s folder", FOLDER, exc_info=True)
    return sess


def chat_for(session_manager: Any, owner: str, key: str | None, question: str, *,
             new: bool = False) -> Any:
    """The chat this question goes into: the conversation's while it is fresh and still
    the owner's, else a new one (always a new one with ``new``: a conversation's first
    question). Remembered under ``key``."""
    session_id = None if new else remembered(owner, key)
    sess = None
    if session_id:
        try:
            sess = session_manager.get_session(session_id)
        except KeyError:
            sess = None
        if sess is not None and getattr(sess, "owner", None) != owner:
            sess = None
    if sess is None:
        sess = new_chat(session_manager, owner, question)
    remember(owner, key, str(sess.id))
    return sess
