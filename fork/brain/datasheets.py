"""Fork: the brain's ``search_datasheet_marks`` (docs/fork/brain.md, "Datasheet marks").

What the owner marked on the datasheets (PDFs) in Write: highlights, underlines and
handwritten notes, circled words and arrows, each with the PDF text it covers and what
was written (``fork/ink/pdf_marks.py``, ``fork/ink/mark_reading.py``). :func:`search` runs
``pdf_marks.rank`` for the run's owner in a thread and gives the model one compact block
per mark: the datasheet (its title, part-number tags in brackets, the file), the page,
the kind, the PDF text, what the owner wrote (said to be corrected by them when
reviewed), the line it is beside or that its arrow points to, and a link that opens Write
at the mark (``/#open=write&note=…&page=…&mark=…``, ``static/js/forkInkPdf.js``).

Read-only and owner-scoped. A result with marks is outside content (the PDF's text) and
the owner's handwriting: it is marked ``untrusted_content`` and arms the run
(``fork/brain/tools.py`` ``distrust``). Offered like ``search_beliefs``: not to non-admin
users or API tokens, nor when ``manage_notes`` is disabled (``tools.turn_disabled_tools``).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

SEARCH_DATASHEET_MARKS = "search_datasheet_marks"
DEFAULT_RESULTS = 10
MAX_RESULTS = 20
TEXT_CHARS = 300  # each text of a mark in the result
NOTHING = "No marks on the owner's datasheets match that."
# First line of a result with marks: small local models sometimes answered "see the links
# above" without writing them out (sandbox run with a 4B model, 1 answer in 3); said here,
# next to the links, it is followed far more reliably than in the system prompt alone.
LINK_REMINDER = ("Write each mark's [open](...) link, exactly as given, in your answer "
                 "next to what you say about that mark.")

SCHEMA = {
    "type": "function",
    "function": {
        "name": SEARCH_DATASHEET_MARKS,
        "description": (
            "Search what the owner marked on their datasheets and other PDFs in Write: "
            "highlights, underlines, circles, arrows and handwritten notes, with the PDF "
            "text each covers "
            "and what they wrote. Returns the best marks with a link to each; quote the "
            "mark and put its link next to it."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Part, pin, value or topic, in a few words."},
                "limit": {"type": "integer", "description": "Most marks (1-20, default 10)."},
            },
            "required": ["query"],
        },
    },
}

KIND_NAMES = {"highlight": "highlight", "underline": "underline", "ink": "handwritten note",
              "circle": "circled", "arrow": "arrow"}
MAX_PARTS_SHOWN = 5


def _cut(text: Any, limit: int = TEXT_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def link(note_id: str, page_id: str, mark_id: str) -> str:
    """The address that opens Write at a mark."""
    def q(value: Any) -> str:
        return quote(str(value or ""), safe="-_")

    return f"/#open=write&note={q(note_id)}&page={q(page_id)}&mark={q(mark_id)}"


def block(hit: dict[str, Any]) -> str:
    """One mark for the model (``hit``: a row of ``pdf_marks.rank``)."""
    mark = hit.get("mark") or {}
    title = _cut(hit.get("title") or "", 120)
    name = _cut(hit.get("pdf_name") or "", 120)
    sheet = title or name or "Datasheet"
    parts = [_cut(p, 40) for p in hit.get("parts") or [] if str(p).strip()]
    if parts:
        sheet += " [" + ", ".join(parts[:MAX_PARTS_SHOWN]) + "]"
    if name and title and name.lower().removesuffix(".pdf") != title.lower():
        sheet += f" ({name})"
    kind = str(mark.get("kind") or "")
    lines = [f"{sheet}, page {mark.get('page_no') or '?'}, {KIND_NAMES.get(kind, kind)}"]
    if mark.get("text"):
        # "printed" on every PDF line and "handwritten by you" on the writing: a 4B model
        # once quoted the line beside a note as what the owner wrote (sandbox, 1 in 4).
        label = "printed line it points to" if kind == "arrow" else "printed text it covers"
        lines.append(f"  {label}: {_cut(mark['text'])}")
    written, reading = str(mark.get("written") or "").strip(), mark.get("reading")
    near = str(mark.get("near") or "").strip()
    if written:
        # One sentence with the handwriting first and the printed line as its place: a 4B
        # model quoted a separate "printed line" row as what the owner wrote.
        where = ""
        if near:
            how = "with an arrow to" if mark.get("points_to") else "next to"
            where = f", {how} the printed line \"{_cut(near)}\""
            near = ""
        lines.append(f"  the owner wrote by hand: \"{_cut(written)}\"{where}"
                     + (" (corrected by the owner)" if reading == "reviewed" else ""))
    elif kind == "ink" and reading == "read":
        lines.append("  a drawing (no words)")
    elif kind == "ink" and reading in ("waiting", "pending"):
        lines.append("  what you wrote isn't read yet")
    elif kind == "ink" and reading == "failed":
        lines.append("  what you wrote couldn't be read")
    if near:
        label = "printed line it points to" if mark.get("points_to") else "printed line beside it"
        lines.append(f"  {label}: {_cut(near)}")
    lines.append(f"  [open]({link(hit.get('note_id'), mark.get('page_id'), mark.get('id'))})")
    return "\n".join(lines)


def _args(content: Any) -> dict:
    try:
        args = json.loads(content or "{}")
    except (TypeError, ValueError):
        return {"query": str(content or "")}
    return args if isinstance(args, dict) else {"query": str(args)}


async def search(owner: Any, content: Any) -> tuple[str, dict]:
    """``search_datasheet_marks``: ``(description, result)``."""
    from fork.ink import pdf_marks

    args = _args(content)
    query = " ".join(str(args.get("query") or "").split())
    if not query:
        return f"{SEARCH_DATASHEET_MARKS}: no query", {"error": "Give a query.",
                                                        "exit_code": 1}
    try:
        limit = max(1, min(MAX_RESULTS, int(args.get("limit") or DEFAULT_RESULTS)))
    except (TypeError, ValueError):
        limit = DEFAULT_RESULTS
    try:
        hits = await asyncio.to_thread(pdf_marks.rank, owner, query, limit)
    except Exception:  # noqa: BLE001
        logger.warning("brain: datasheet marks search failed", exc_info=True)
        return f"{SEARCH_DATASHEET_MARKS}: failed", {
            "error": "The datasheet marks search failed.", "exit_code": 1}
    if not hits:
        return f"{SEARCH_DATASHEET_MARKS}: 0 result(s)", {"output": NOTHING, "exit_code": 0}
    output = LINK_REMINDER + "\n\n" + "\n\n".join(block(h) for h in hits)
    return f"{SEARCH_DATASHEET_MARKS}: {len(hits)} result(s)", {
        "output": output, "exit_code": 0, "untrusted_content": True}
