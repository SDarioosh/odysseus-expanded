"""Fork: wake word clips over HTTP (docs/fork/voice-assistant.md#wake-word-clips).

* ``POST /api/chat/voice/v1/satellite/wakeword/clip`` (API token with the ``voice``
  scope), multipart: ``audio`` (WAV, 16 kHz mono 16-bit PCM, at most 400 KB), ``clip_id``,
  ``device_id``, ``device_name``, ``wake_word``, ``kind`` (guided, missed, heard),
  ``score``, ``threshold``, ``prompt``, ``recorded_at`` (epoch ms). Stores the clip for the
  token's owner (``fork/voice/wakeword_clips.py``); answers ``{"ok": true, "id",
  "already"}``. 411 without Content-Length, 413 too large, 415 not multipart or not such
  a WAV, 422 a bad field, 507 store full (the headers are checked by
  ``fork/voice/satellite_routes.py``'s ``_check_form``).
* ``GET /api/fork/voice/wakeword/clips?wake_word=…`` (signed-in admin,
  ``fork/voice/admin.py``): ``{"wake_words", "counts", "clips"}``, newest first, at
  most 500.
* ``GET /api/fork/voice/wakeword/clips/{clip_id}/audio`` (the same): the WAV.
* ``DELETE /api/fork/voice/wakeword/clips/{clip_id}`` (the same): ``{"ok": true}``, 404
  for an unknown clip.
* ``GET /api/fork/voice/wakeword/export.zip?wake_word=…`` (the same): every clip (of that
  wake word) as a zip for training, with ``clips.jsonl`` and a README.

The audio is never logged.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile

from fork.voice import wakeword_clips as clips
from fork.voice.admin import signed_in_admin
from fork.voice.routes import PREFIX, voice_owner
from fork.voice.satellite_routes import _check_form, _seen

logger = logging.getLogger(__name__)

CLIP_PATH = PREFIX + "/satellite/wakeword/clip"
ADMIN_BASE = "/api/fork/voice/wakeword"
MAX_BODY_BYTES = clips.MAX_AUDIO_BYTES + 64 * 1024  # the form fields and multipart framing
TEXT_FIELDS = ("clip_id", "device_id", "device_name", "wake_word", "kind", "score",
               "threshold", "prompt", "recorded_at")
NO_STORE = {"Cache-Control": "no-store"}


def _admin_owner(request: Request) -> str:
    """The signed-in admin's name (403 for anyone else, and for an API token)."""
    return signed_in_admin(request, "Only a signed-in admin can see wake word clips")


async def _audio(form: Any) -> bytes:
    upload = form.get("audio")
    if not isinstance(upload, UploadFile):
        raise HTTPException(422, "audio is required (a WAV file)")
    data = await upload.read(clips.MAX_AUDIO_BYTES + 1)
    if not data:
        raise HTTPException(422, "audio is empty")
    if len(data) > clips.MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio is too large (10 s of 16 kHz WAV at most)")
    return data


def _clip_id(raw: str) -> str:
    if not clips.valid_clip_id(raw):
        raise HTTPException(404, "No such clip")
    return raw


def _wake_word(raw: str) -> str:
    raw = (raw or "").strip()
    if raw and not clips.valid_wake_word(raw):
        raise HTTPException(422, "wake_word must be lower-case letters, digits or _")
    return raw


def setup_wakeword_routes() -> APIRouter:
    router = APIRouter(tags=["fork-voice-wakeword"])

    @router.post(CLIP_PATH)
    async def upload_clip(request: Request) -> Any:
        owner = voice_owner(request)
        _check_form(request, MAX_BODY_BYTES, need_length=True,
                    wrong_kind="Send multipart/form-data", kinds=("multipart/form-data",))
        async with request.form(max_files=1, max_fields=16) as form:
            raw: dict[str, Any] = {}
            for name in TEXT_FIELDS:
                value = form.get(name)
                if value is not None and not isinstance(value, str):
                    raise HTTPException(422, f"{name} must be text")
                raw[name] = value
            try:
                fields = clips.clean_fields(raw)
            except clips.ClipError as exc:
                raise HTTPException(exc.status, str(exc)) from None
            audio = await _audio(form)
        try:
            saved = await asyncio.to_thread(clips.save, owner, fields, audio)
        except clips.ClipError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        finally:
            del audio
        await asyncio.to_thread(_seen, owner, fields["device_id"],
                                fields["device_name"] or None, None)
        if not saved.already:
            logger.info("voice: stored a wake word clip (%s, %s) from device %s",
                        fields["wake_word"], fields["kind"], fields["device_id"])
        return {"ok": True, "id": saved.id, "already": saved.already}

    @router.get(ADMIN_BASE + "/clips")
    def list_clips(request: Request, wake_word: str = "") -> Any:
        owner = _admin_owner(request)
        word = _wake_word(wake_word)
        return JSONResponse(clips.listing(owner, word), headers=NO_STORE)

    @router.get(ADMIN_BASE + "/clips/{clip_id}/audio")
    def clip_audio(request: Request, clip_id: str) -> Any:
        owner = _admin_owner(request)
        path = clips.audio_path(owner, _clip_id(clip_id))
        if path is None:
            raise HTTPException(404, "No such clip")
        return FileResponse(path, media_type="audio/wav", headers=NO_STORE)

    @router.delete(ADMIN_BASE + "/clips/{clip_id}")
    def delete_clip(request: Request, clip_id: str) -> Any:
        owner = _admin_owner(request)
        if not clips.delete(owner, _clip_id(clip_id)):
            raise HTTPException(404, "No such clip")
        return {"ok": True}

    @router.get(ADMIN_BASE + "/export.zip")
    async def export_zip(request: Request, wake_word: str = "") -> Any:
        owner = _admin_owner(request)
        word = _wake_word(wake_word)
        fd, tmp = tempfile.mkstemp(prefix="odysseus-wakeword-", suffix=".zip")
        os.close(fd)
        try:
            await asyncio.to_thread(clips.write_zip, owner, tmp, word)
        except BaseException:
            os.unlink(tmp)
            raise
        name = f"wakeword-clips-{word or 'all'}.zip"
        return FileResponse(tmp, media_type="application/zip", filename=name,
                            headers=NO_STORE, background=BackgroundTask(os.unlink, tmp))

    return router
