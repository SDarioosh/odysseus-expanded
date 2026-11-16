"""Fork: what the owner marked on PDF datasheets in the Write view
(``fork/ink/pdf_marks.py``; the routes of the PDF itself are in ``fork/routes/ink_routes.py``).

* ``GET /api/ink/notes/{id}/marks``: ``{"note_id", "title", "marks": [mark, ...]}``, the
  marks of the note's PDF pages in page order (``[]`` for a note without PDF pages). 404
  when it isn't this user's handwritten note.
* ``GET /api/ink/marks?q=<text>&limit=<1..100, default 30>``: ``{"q", "results":
  [{"note_id", "title", "pdf_name", "mark"}]}``, the marks of every PDF note of this user
  whose text, near line, note title or PDF name holds all words of ``q``
  (case-insensitive), newest mark first. 400 when ``q`` is shorter than 2 characters;
  ``limit`` outside 1..100 is brought into it.

Same users and owner rules as the other ink routes (``require_user``; API tokens are
refused; another user's note id is a 404).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from fork.ink import pdf_marks
from fork.routes.ink_routes import _check_id, _owner

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
        return out

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
