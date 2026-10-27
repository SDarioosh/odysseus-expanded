"""Fork: handwritten notes, stage H1: save, load and delete the Write view's strokes
(``src/ink_notes.py``; plan ``docs/fork/handwritten-notes.md``).

* ``GET    /api/ink/notes``: ``{"notes": [{id, title, image_url, updated_at, pages,
  edited_at, link}]}``, newest first. Runs the sidecar sweep first (a note deleted in the
  Notes window loses its strokes).
* ``GET    /api/ink/notes/{id}``: the note's sidecar plus ``title``, ``image_url`` and
  ``link``. 404 when it isn't this user's handwritten note.
* ``PUT    /api/ink/notes/{id}`` ``{title?, paper, edited_at, pages: [...], final?}``:
  save (``paper`` is ``lined``, ``dotted`` or ``blank``; an older view sends ``ruled``). The first save creates the note (the id comes from the client, so a note can be
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

Stage H4, into Athena (``src/ink_athena.py``):

* The transcripts answers above carry ``athena``: ``{state: not_sent|sent|changed,
  sent_at, conversation_id, transcript_link, waiting: [{page, why}], has_text, sending}``.
* ``POST /api/ink/notes/{id}/send``: sends the reviewed text to Athena (or replaces it
  there); answers the transcripts plus ``sent: {result, conversation_id, lines,
  erased_lines, transcript_link}``. 409 ``{"detail"}`` while pages wait for review, 413
  over Athena's limits (the detail says what is over), 400 no reviewed text, 503 Athena
  not set up, 502 Athena unreachable. Nothing is sent by any other route.
* ``DELETE`` above also removes a sent note from Athena (``athena``: ``deleted``,
  ``already`` or ``pending``, retried by the sweep); listing notes runs the sweep.

Stage H6, photos of paper pages (``src/ink_notes.py`` ``add_photos``):

* ``POST /api/ink/notes/{id}/photos`` (multipart): ``photos`` (one or more files, JPEG,
  PNG or WebP, at most ``ink.MAX_PHOTOS_PER_UPLOAD`` of ``ink.MAX_PHOTO_BYTES`` each),
  optional ``edited_at`` (ms), ``after`` (a page id: the photos go after it, else at the
  end), ``title`` and ``paper`` (used when this creates the note). Each photo becomes a
  page; answers the stored note like ``GET``. 400 not an image, 410 deleted, 411 no
  ``Content-Length``, 413 too large or the note full.
* ``GET /api/ink/notes/{id}/photos/{page_id}``: the stored photo (JPEG) of a photo page.

Any signed-in user, for their own notes, like upstream's notes routes
(``require_user``; API tokens are refused). Another user's note id is a 404.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from src import ink_athena as athena
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
# H6: every photo of one upload at its largest, plus the form around them.
MAX_PHOTO_BODY = ink.MAX_PHOTOS_PER_UPLOAD * ink.MAX_PHOTO_BYTES + 64 * 1024


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
    async def list_notes(request: Request, background: BackgroundTasks):
        owner = _owner(request)
        notes = await asyncio.to_thread(ink.list_notes, owner)
        # Deleted notes Athena still holds: asked after the answer, not before it.
        background.add_task(_sweep_athena, owner)
        return {"notes": notes}

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
        store = await asyncio.to_thread(ink._store_owner, owner, note_id)
        if not await asyncio.to_thread(ink.delete, owner, note_id):
            raise HTTPException(404, "Note not found")
        sent = await athena.after_delete(store, note_id)
        return {"ok": True, "athena": sent} if sent else {"ok": True}

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

    @router.post("/notes/{note_id}/send")
    async def send(request: Request, note_id: str):
        owner = _owner(request)
        _check_id(note_id)
        await _json_body(request, 1024)  # no fields; refuses a large or odd body
        try:
            return await athena.send(owner, note_id, request_base=str(request.base_url))
        except ink.InkError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @router.post("/notes/{note_id}/photos")
    async def add_photos(request: Request, note_id: str):
        owner = _owner(request)
        _check_id(note_id)
        length = request.headers.get("content-length") or ""
        if not length.isdigit():
            raise HTTPException(411, "The upload needs a Content-Length")
        if int(length) > MAX_PHOTO_BODY:
            raise HTTPException(413, "The upload is too large")
        if not (request.headers.get("content-type") or "").startswith("multipart/form-data"):
            raise HTTPException(400, "Send the photos as multipart/form-data")
        form = await request.form(max_files=ink.MAX_PHOTOS_PER_UPLOAD, max_fields=16,
                                  max_part_size=64 * 1024)
        try:
            files = [v for k, v in form.multi_items() if k == "photos" and not isinstance(v, str)]
            if not files:
                raise HTTPException(400, "No photo was sent")
            fields = {k: v for k, v in form.multi_items() if isinstance(v, str)}
            edited = fields.get("edited_at") or "0"
            if not (edited.isascii() and edited.isdigit()):
                raise HTTPException(400, "edited_at must be a number")
            prepared = []
            for upload in files:
                data = await upload.read(ink.MAX_PHOTO_BYTES + 1)
                prepared.append(await asyncio.to_thread(ink.prepare_photo, data))
            return await asyncio.to_thread(
                ink.add_photos, owner, note_id, prepared, edited_at=int(edited),
                after=fields.get("after") or None, title=fields.get("title"),
                paper=fields.get("paper"))
        except ink.InkError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        finally:
            await form.close()

    @router.get("/notes/{note_id}/photos/{page_id}")
    async def get_photo(request: Request, note_id: str, page_id: str):
        owner = _owner(request)
        _check_id(note_id)
        if not _PAGE_ID.match(page_id):
            raise HTTPException(404, "Photo not found")
        path = await asyncio.to_thread(ink.photo_file, owner, note_id, page_id)
        if path is None:
            raise HTTPException(404, "Photo not found")
        # A page's photo never changes (a new photo is a new page).
        return FileResponse(path, media_type="image/jpeg", headers={
            "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})

    return router


async def _sweep_athena(owner: str | None) -> None:
    try:
        await athena.sweep(owner)
    except Exception as exc:  # noqa: BLE001 -- the next list or the hourly sync retries
        import logging

        logging.getLogger(__name__).warning("ink: Athena sweep failed: %s", exc)
