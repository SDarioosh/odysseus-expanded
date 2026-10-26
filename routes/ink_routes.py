"""Fork: handwritten notes, stage H1: save, load and delete the Write view's strokes
(``src/ink_notes.py``; plan ``docs/fork/handwritten-notes.md``).

* ``GET    /api/ink/notes``: ``{"notes": [{id, title, image_url, updated_at, pages,
  edited_at, link}]}``, newest first. Runs the sidecar sweep first (a note deleted in the
  Notes window loses its strokes).
* ``GET    /api/ink/notes/{id}``: the note's sidecar plus ``title``, ``image_url`` and
  ``link``. 404 when it isn't this user's handwritten note.
* ``PUT    /api/ink/notes/{id}`` ``{title?, ruled, edited_at, pages: [...], final?}``:
  save. The first save creates the note (the id comes from the client, so a note can be
  started offline). Page 1's image is redrawn at most once a minute while writing, and
  on a ``final`` save (the view sends it when the page is left or the app hidden).
  Answers the stored note; 409 ``{"detail", "note"}`` when a newer edit is already
  stored (last edit wins, the client takes ``note``); 410 when the note was deleted;
  400 for a malformed body; 413 above ``MAX_BODY`` bytes.
* ``DELETE /api/ink/notes/{id}``: deletes the note and its strokes. 404 when it isn't
  this user's.

Stage H2, transcription and review (``src/ink_transcribe.py``):

* ``GET  /api/ink/notes/{id}/transcripts``: ``{"note_id", "pages": [{id, index, status,
  text, model, at, error, ink, changed, unreadable, previous?}], "running"}``.
* ``POST /api/ink/notes/{id}/transcribe`` ``{pages?: [page id], only_changed?}``:
  marks the pages ``pending`` and reads them with the vision model in the background
  (one band at a time); answers the transcripts at once. Without ``pages``, every page
  that needs it. 503 ``{"detail"}`` when no local model can read images.
* ``PUT  /api/ink/notes/{id}/pages/{page_id}/transcript`` ``{text}``: the owner's
  reviewed text; status ``reviewed``, and the note's ``content`` becomes the reviewed
  pages' text. Nothing is sent anywhere.

Any signed-in user, for their own notes, like upstream's notes routes
(``require_user``; API tokens are refused). Another user's note id is a 404.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from src import ink_notes as ink
from src import ink_transcribe as tr
from src.auth_helpers import require_user

MAX_BODY = 12 * 1024 * 1024


def _owner(request: Request) -> str | None:
    return require_user(request) or None  # same as routes/note/note_routes.py


def _check_id(note_id: str) -> None:
    if not ink.valid_note_id(note_id):
        raise HTTPException(404, "Note not found")


_PAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_REVIEW_BODY = 256 * 1024


async def _json_body(request: Request, limit: int) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > limit:
        raise HTTPException(413, "The request is too large")
    try:
        body: Any = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "Body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be an object")
    return body


def setup_ink_routes() -> APIRouter:
    router = APIRouter(prefix="/api/ink", tags=["fork-ink"])

    @router.get("/notes")
    async def list_notes(request: Request):
        owner = _owner(request)
        return {"notes": await asyncio.to_thread(ink.list_notes, owner)}

    @router.get("/notes/{note_id}")
    async def get_note(request: Request, note_id: str):
        owner = _owner(request)
        _check_id(note_id)
        note = await asyncio.to_thread(ink.load, owner, note_id)
        if note is None:
            raise HTTPException(404, "Note not found")
        return note

    @router.put("/notes/{note_id}")
    async def save_note(request: Request, note_id: str):
        owner = _owner(request)
        _check_id(note_id)
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "The note is too large")
        try:
            body: Any = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(400, "Body must be JSON") from None
        title = body.get("title") if isinstance(body, dict) else None
        final = bool(body.get("final")) if isinstance(body, dict) else False
        try:
            return await asyncio.to_thread(ink.save, owner, note_id, body, title=title,
                                           final=final)
        except ink.Stale as exc:
            return JSONResponse({"detail": str(exc), "note": exc.current}, status_code=409)
        except ink.InkError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @router.delete("/notes/{note_id}")
    async def delete_note(request: Request, note_id: str):
        owner = _owner(request)
        _check_id(note_id)
        if not await asyncio.to_thread(ink.delete, owner, note_id):
            raise HTTPException(404, "Note not found")
        return {"ok": True}

    @router.get("/notes/{note_id}/transcripts")
    async def get_transcripts(request: Request, note_id: str):
        owner = _owner(request)
        _check_id(note_id)
        try:
            return await asyncio.to_thread(tr.transcripts, owner, note_id)
        except ink.InkError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @router.post("/notes/{note_id}/transcribe")
    async def transcribe(request: Request, note_id: str):
        owner = _owner(request)
        _check_id(note_id)
        body = await _json_body(request, 64 * 1024)
        pages = body.get("pages")
        try:
            return await tr.start(owner, note_id, page_ids=pages,
                                  only_changed=bool(body.get("only_changed")))
        except tr.NoModel as exc:
            raise HTTPException(503, str(exc)) from None
        except ink.InkError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @router.put("/notes/{note_id}/pages/{page_id}/transcript")
    async def save_review(request: Request, note_id: str, page_id: str):
        owner = _owner(request)
        _check_id(note_id)
        if not _PAGE_ID.match(page_id):
            raise HTTPException(404, "Page not found")
        body = await _json_body(request, MAX_REVIEW_BODY)
        try:
            return await asyncio.to_thread(tr.save_review, owner, note_id, page_id,
                                           body.get("text"))
        except ink.InkError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    return router
