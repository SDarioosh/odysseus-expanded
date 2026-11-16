"""Fork: what the owner marked on PDF datasheets in the Write view
(``fork/ink/pdf_marks.py``; the routes of the PDF itself are in ``fork/routes/ink_routes.py``).

* ``GET /api/ink/notes/{id}/marks``: ``{"note_id", "title", "marks": [mark, ...],
  "reading"}``, the marks of the note's PDF pages in page order (``[]`` for a note without
  PDF pages), each with ``written`` and ``reading`` (``fork/ink/mark_reading.py``).
  When an ink mark is unread this schedules a reading run; ``reading`` is true while a
  run of the note is scheduled or reading (the client polls). 404 when it isn't this
  user's handwritten note.
* ``PUT /api/ink/notes/{id}/marks/{mark_id}`` ``{"written": "<text>"}`` (at most 2000
  characters): the owner's correction of what an ink mark says, stored ``reviewed``;
  answers the mark. 404 unknown mark or not this user's note, 400 a highlight or
  underline (no writing) or a bad body.
* ``GET /api/ink/marks?q=<text>&limit=<1..100, default 30>``: ``{"q", "results":
  [{"note_id", "title", "pdf_name", "mark"}]}``, the marks of every PDF note of this user
  whose text, near line, written text, note title or PDF name holds all words of ``q``
  (case-insensitive), newest mark first. 400 when ``q`` is shorter than 2 characters;
  ``limit`` outside 1..100 is brought into it.

Same users and owner rules as the other ink routes (``require_user``; API tokens are
refused; another user's note id is a 404).
"""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException, Request

from fork.ink import mark_reading, pdf_marks
from fork.ink import notes as ink
from fork.routes.ink_routes import _check_id, _json_body, _owner

_MARK_ID = re.compile(r"^m-[A-Za-z0-9_-]{1,64}$")

MAX_QUERY = 200
DEFAULT_LIMIT = 30
MAX_LIMIT = 100


def setup_ink_marks_routes() -> APIRouter:
    router = APIRouter(prefix="/api/ink", tags=["fork-ink"])

    @router.get("/notes/{note_id}/marks")
    async def note_marks(request: Request, note_id: str):
        owner = _owner(request)
        _check_id(note_id)
        out = await asyncio.to_thread(pdf_marks.note_marks, owner, note_id)
        if out is None:
            raise HTTPException(404, "Note not found")
        if out.pop("unread") and not mark_reading.is_active(note_id):
            mark_reading.schedule(owner, note_id)
        out["reading"] = mark_reading.is_active(note_id)
        return out

    @router.put("/notes/{note_id}/marks/{mark_id}")
    async def correct_mark(request: Request, note_id: str, mark_id: str):
        owner = _owner(request)
        _check_id(note_id)
        if not _MARK_ID.match(mark_id):
            raise HTTPException(404, "Mark not found")
        body = await _json_body(request, 16 * 1024)
        try:
            return await asyncio.to_thread(mark_reading.correct, owner, note_id, mark_id,
                                           body.get("written"))
        except ink.InkError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @router.get("/marks")
    async def search_marks(request: Request, q: str = "", limit: int = DEFAULT_LIMIT):
        owner = _owner(request)
        q = q.strip()[:MAX_QUERY]
        if len(q) < 2:
            raise HTTPException(400, "Search for at least 2 characters")
        limit = min(MAX_LIMIT, max(1, limit))
        results = await asyncio.to_thread(pdf_marks.search, owner, q, limit)
        return {"q": q, "results": results}

    return router
