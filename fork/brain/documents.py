"""Fork: the brain's documents group (docs/fork/brain.md, "Documents").

Three tools, in this order:

- ``manage_documents``: upstream's tool over the owner's stored editor documents. Its
  ``list``/``read`` actions only read (upstream's ``_PRIVATE_ACTION_READS``) and run
  without asking; ``delete``/``tidy`` ask. A ``delete`` without a ``document_id`` is
  refused before it asks (:func:`refusal`): upstream's tool would then delete the most
  recent document, which the approval card can't name.
- ``create_document``: upstream's tool; asks first like every write. The model's native
  JSON arguments are written in upstream's line format first (:func:`call_content`, as
  upstream's agent does with ``function_call_to_tool_block``), so the card shows what is
  created and the approval matches the call that runs. Once it ran, the chat window gets
  upstream's editor events (:func:`editor_events`) and the tool card upstream's document
  fields (:func:`output_fields`), so the new document opens in the editor; the model gets
  a line naming it (:func:`describe`).
- ``search_documents``: the fork's own tool over the owner's personal document library
  (:func:`search`): upstream's ``rag_manager.search(query, k, owner=owner)``, kept to the
  hits of this owner and at or above upstream's ``RAG_SIMILARITY_THRESHOLD``, each passage
  cut to :data:`PASSAGE_CHARS`. Library files are outside content: a result with hits is
  marked ``untrusted_content`` and arms the run (``tools.py``). Offered only when a
  ``rag_manager`` exists, the chat's documents toggle isn't off, and upstream would let
  this user and credential use ``manage_documents`` (``tools.turn_disabled_tools``).

The two upstream tools are offered with upstream's parameters and shorter descriptions
(:data:`DESCRIPTIONS`), to keep the brain's schemas small.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

MANAGE = "manage_documents"
CREATE = "create_document"
SEARCH_DOCUMENTS = "search_documents"
TOOLS = (MANAGE, CREATE, SEARCH_DOCUMENTS)
DEFAULT_RESULTS = 5   # upstream's chat RAG uses k=5
MAX_RESULTS = 8
PASSAGE_CHARS = 1200
DEFAULT_THRESHOLD = 0.35  # upstream's ChatProcessor.RAG_SIMILARITY_THRESHOLD, if unreadable
DELETE_NEEDS_ID = ("Give the document_id of the document to delete (manage_documents "
                   "action=list finds it).")

DESCRIPTIONS = {
    MANAGE: ("The owner's stored documents in the editor. action=list with search text "
             "finds documents by title or content and returns their document_id; "
             "action=read with document_id reads one (read it before answering about "
             "it). delete and tidy change documents."),
    CREATE: ("Create a new document and open it in the owner's editor. Use it for "
             "long-form writing the owner asks for (a letter, plan, report, story, long "
             "list or code), with the whole text in content; markdown for prose."),
}

SEARCH_DOCUMENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": SEARCH_DOCUMENTS,
        "description": (
            "Search the owner's personal document library (their uploaded and indexed "
            "files: PDFs, manuals, contracts, records, saved texts) by meaning. Returns "
            "the best passages with their filename; cite the filename next to each fact."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, in a few words."},
                "limit": {"type": "integer", "description": "Most passages (1-8, default 5)."},
            },
            "required": ["query"],
        },
    },
}


# ------------------------------------------------------------------------ schemas
def schema(name: str, builtin: dict[str, dict]) -> dict | None:
    """The schema the brain offers for one tool of this group (None: not available)."""
    if name == SEARCH_DOCUMENTS:
        return SEARCH_DOCUMENTS_SCHEMA
    upstream = builtin.get(name)
    if upstream is None or name not in DESCRIPTIONS:
        return upstream
    out = copy.deepcopy(upstream)
    out["function"]["description"] = DESCRIPTIONS[name]
    return out


def schemas() -> list[dict]:
    """The group's schemas, in order (for the budget check)."""
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    builtin = {s["function"]["name"]: s for s in FUNCTION_TOOL_SCHEMAS}
    return [s for s in (schema(n, builtin) for n in TOOLS) if s is not None]


# ---------------------------------------------------------------------- the calls
def _object(content: Any) -> dict | None:
    try:
        args = json.loads(content or "{}")
    except (TypeError, ValueError):
        return None
    return args if isinstance(args, dict) else None


def call_content(name: str, arguments: Any) -> Any:
    """``create_document``'s JSON arguments in upstream's line format (title, language,
    content), the way upstream's agent passes native calls on; everything else as it is."""
    if name != CREATE or _object(arguments) is None:
        return arguments
    from src.tool_schemas import function_call_to_tool_block

    block = function_call_to_tool_block(name, arguments)
    return block.content if block is not None else arguments


def refusal(name: str, content: Any) -> str | None:
    """Why this call is refused before it can ask, or None."""
    if name != MANAGE:
        return None
    args = _object(content) or {}
    if str(args.get("action") or "").strip().lower() == "delete" and not any(
            str(args.get(k) or "").strip() for k in ("document_id", "id", "uid")):
        return DELETE_NEEDS_ID
    return None


# --------------------------------------------------------------- the library search
def rag_manager(request: Any) -> Any:
    """The library's ``rag_manager``, where upstream's chat RAG finds it (the app's
    ``personal_docs_manager``), or None."""
    try:
        docs = request.app.state.personal_docs_manager
    except AttributeError:
        return None
    return getattr(docs, "rag_manager", None)


def threshold() -> float:
    try:
        from src.chat_processor import ChatProcessor
        return float(ChatProcessor.RAG_SIMILARITY_THRESHOLD)
    except Exception:  # noqa: BLE001 -- upstream moved it: its last known value
        return DEFAULT_THRESHOLD


def _kept(hit: Any, owner: Any, minimum: float) -> bool:
    if not isinstance(hit, dict):
        return False
    try:
        if float(hit.get("similarity") or 0) < minimum:
            return False
    except (TypeError, ValueError):
        return False
    meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    # The store filters by owner already; checked again so a store that doesn't can't
    # hand another owner's passage over. No owner: single-user mode, as upstream.
    return not owner or meta.get("owner") == owner


def _passage(hit: dict) -> str:
    meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    name = str(meta.get("filename") or meta.get("source") or "unknown")
    text = str(hit.get("document") or "").strip()
    if len(text) > PASSAGE_CHARS:
        text = text[:PASSAGE_CHARS].rstrip() + " […]"
    return f"[{name}]\n{text}"


async def search(owner: Any, rag: Any, content: Any) -> tuple[str, dict]:
    """``search_documents``: ``(description, result)``."""
    args = _object(content)
    if args is None:
        args = {"query": str(content or "")}
    query = " ".join(str(args.get("query") or "").split())
    if not query:
        return f"{SEARCH_DOCUMENTS}: no query", {"error": "Give a query.", "exit_code": 1}
    if rag is None:
        return f"{SEARCH_DOCUMENTS}: unavailable", {
            "error": "The document library isn't available.", "exit_code": 1}
    try:
        limit = max(1, min(MAX_RESULTS, int(args.get("limit") or DEFAULT_RESULTS)))
    except (TypeError, ValueError):
        limit = DEFAULT_RESULTS
    try:
        hits = await asyncio.to_thread(rag.search, query, k=limit, owner=owner or None)
    except Exception:  # noqa: BLE001
        logger.warning("brain: document library search failed", exc_info=True)
        return f"{SEARCH_DOCUMENTS}: failed", {"error": "The document search failed.",
                                               "exit_code": 1}
    minimum = threshold()
    kept = [h for h in hits or () if _kept(h, owner, minimum)][:limit]
    if not kept:
        return f"{SEARCH_DOCUMENTS}: 0 result(s)", {
            "output": "No matching passages in the owner's documents.", "exit_code": 0}
    output = "\n\n---\n\n".join(_passage(h) for h in kept)
    return f"{SEARCH_DOCUMENTS}: {len(kept)} result(s)", {
        "output": output, "exit_code": 0, "untrusted_content": True}


# ------------------------------------------------------------------- after a run
_DRAFTS = frozenset({"draft_email", "draft_email_reply", "ai_draft_email_reply"})


def _created(name: str, result: dict) -> bool:
    return (name == CREATE and bool(result.get("doc_id")) and not result.get("error")
            and not result.get("blocked") and result.get("action") == "create")


def describe(name: str, result: dict) -> None:
    """Give a created document's result a line for the model (upstream's result has no
    text, so the model would read "(no output)")."""
    if _created(name, result) and not result.get("output"):
        title = str(result.get("title") or "Untitled")
        result["output"] = (
            f'Created the document "{title}" ({result.get("language") or "text"}, '
            f'{len(str(result.get("content") or ""))} characters); it is open in the '
            f'editor: [{title}](#document-{result["doc_id"]}). document_id: {result["doc_id"]}')


def editor_events(name: str, result: dict) -> list[dict]:
    """The events upstream's agent sends after a created document, in its order: the
    editor stream (``doc_stream_open``, ``doc_stream_delta``), then the saved document
    (``doc_update``), so the chat window opens it in the editor."""
    if not _created(name, result):
        return []
    title, language = str(result.get("title") or ""), str(result.get("language") or "")
    content = str(result.get("content") or "")
    events = [{"type": "doc_stream_open", "title": title, "language": language}]
    if content:
        events.append({"type": "doc_stream_delta", "content": content})
    events.append({"type": "doc_update", "doc_id": result["doc_id"], "content": content,
                   "version": result.get("version", 1), "title": title,
                   "language": language})
    return events


def output_fields(name: str, result: dict) -> dict:
    """The document fields upstream's agent puts on a document tool's ``tool_output``
    (the window's fallback for opening it)."""
    if not result.get("doc_id"):
        return {}
    if name == CREATE and "action" in result:
        return {"document_action": result.get("action"),
                "document_title": result.get("title", ""),
                "document_language": result.get("language", ""),
                "document_version": result.get("version"),
                "document_content": result.get("content", "")}
    if name in _DRAFTS:
        return {"document_action": "create", "document_title": result.get("title", ""),
                "document_language": result.get("language", "email"),
                "document_version": result.get("version", 1),
                "document_content": result.get("content", "")}
    return {}
