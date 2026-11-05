"""Fork: the document open in the editor, for a brain turn (docs/fork/brain.md, "Open
document").

The chat window sends ``active_doc_id`` with every message while a document is open in
the editor. Those turns all went to upstream's agent. Now the brain takes the ones that
ask about the document ("what does this say?", "summarise it", "is the date right?"),
and upstream's agent keeps the ones that change it (:func:`wants_edit`): it has the
in-place editing tools (``edit_document``, ``update_document``, ``suggest_document``)
and the editor events that apply them, which the brain doesn't offer.

The document is loaded here, owner-checked (:func:`item`), the way ``chat_stream``
loads it for upstream's agent (after the brain's hook, so the brain can't reuse it):
by id, only a document of the turn's owner. It goes into the turn's volatile context
message beside selected notes and an open email (``fork/brain/selection.py``), in
upstream's untrusted wrapper with upstream's label, so the run is armed and the
exchange replays untrusted: a document can hold pasted outside text. Cut to
:data:`DOCUMENT_CHARS`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

DOCUMENT_CHARS = 12000
DOCUMENT_LABEL = "active editor document"  # upstream's label (src/agent_loop.py)
DOCUMENT_INTRO = ("The owner has this document open in the editor right now; \"this\", "
                  "\"it\" and \"the document\" refer to it. Answer from its text below.")

# Upstream's own words for a change to the open document (``chat_stream``'s
# "active document edit request" escalation), plus the other verbs of an edit. A turn
# with one of them goes to upstream's agent, which edits the document in place.
_EDIT = re.compile(
    r"\b(?:make|sound|rewrite|revise|rework|edit|update|change|polish|professional|fun|"
    r"formal|casual|shorter|longer|friendlier|warmer|clearer|"
    r"fix|correct|proofread|shorten|expand|translate|replace|insert|append|add|remove|"
    r"delete|cut|reword|rephrase|capitali[sz]e|uppercase|lowercase|format|reformat|"
    r"restructure|reorder|rename|tweak|improve|tighten|simplify|continue|finish|"
    r"complete|draft|write|fill|sign)\b",
    re.IGNORECASE)


def wants_edit(message: Any) -> bool:
    """True when the message asks for a change to the open document."""
    return bool(_EDIT.search(str(message or "")))


def _load(doc_id: str, owner: Any) -> tuple[str, str, str] | None:
    from core.database import Document, SessionLocal
    from fork.upstream import owner_documents_filter

    db = SessionLocal()
    try:
        doc = owner_documents_filter(
            db.query(Document).filter(Document.id == doc_id), owner).first()
        if doc is None:
            return None
        doc_owner = getattr(doc, "owner", None)
        if doc_owner and owner and doc_owner != owner:
            return None
        return (str(doc.title or "Untitled"), str(doc.language or "text"),
                str(doc.current_content or ""))
    finally:
        db.close()


async def item(doc_id: Any, owner: Any) -> tuple[str, str] | None:
    """``(label, text)`` for the owner's open document, or None (no id, not found,
    another owner's). Never raises."""
    from fork.brain.tools import trim

    doc_id = str(doc_id or "").strip()
    if not doc_id:
        return None
    try:
        found = await asyncio.to_thread(_load, doc_id, owner)
    except Exception:  # noqa: BLE001 -- the turn runs without the document
        logger.warning("brain: the open document could not be loaded", exc_info=True)
        return None
    if found is None:
        return None
    title, language, content = found
    return DOCUMENT_LABEL, (f"{DOCUMENT_INTRO}\n\nTitle: {title}\nLanguage: {language}\n"
                            f"Content:\n{trim(content, DOCUMENT_CHARS) or '(empty)'}")
