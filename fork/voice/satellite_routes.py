"""Fork: the voice satellite's routes (docs/fork/voice-assistant.md#satellites-owner-only-answers-and-announcements).

* ``POST /api/chat/voice/v1/satellite/turn`` (API token with the ``voice`` scope),
  multipart (or a urlencoded form without audio): ``device_id`` (required), ``device_name``, ``area``, ``wake_word``,
  ``started_at`` (epoch ms of the wake word), optional ``audio`` (WAV, the spoken
  command, at most :data:`MAX_AUDIO_BYTES`). Keeps a satellite context for 30 s
  (``fork/voice/satellite.py``) and starts the speaker check in the background; answers
  ``{"ok": true, "context_id", "speaker"}`` (``speaker`` null while the check still runs,
  without audio, or when Athena couldn't check). The audio is never stored or logged.
* ``POST /api/fork/voice/announce`` (signed-in admin), ``{"message"?: str}``: speak a test
  announcement through Home Assistant (``fork/voice/announce.py``); ``{"ok", "problem"}``.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from fork.voice import announce as voice_announce
from fork.voice import satellite
from fork.voice.routes import PREFIX, voice_owner

MAX_AUDIO_BYTES = 1024 * 1024  # 15 s of 16 kHz mono 16-bit WAV is ~480 KB
MAX_BODY_BYTES = MAX_AUDIO_BYTES + 64 * 1024  # the form fields and multipart framing
RESPONSE_WAIT_SECONDS = 0.5  # a quick check is answered in the response itself
FIELD_LIMITS = {"device_id": 128, "device_name": 100, "area": 100, "wake_word": 64}
TEST_MESSAGE = "This is a test announcement from Odysseus."


class AnnounceTest(BaseModel):
    message: str = Field(default="", max_length=voice_announce.MAX_MESSAGE)


def _text(form: Any, name: str, *, required: bool = False) -> str:
    value = form.get(name)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise HTTPException(422, f"{name} must be text")
    value = " ".join(value.split())
    if required and not value:
        raise HTTPException(422, f"{name} is required")
    if len(value) > FIELD_LIMITS[name]:
        raise HTTPException(422, f"{name} is too long (at most {FIELD_LIMITS[name]} characters)")
    return value


def _started_at(form: Any) -> int:
    raw = form.get("started_at")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return int(time.time() * 1000)
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise HTTPException(422, "started_at must be epoch milliseconds") from None
    if not 0 < value < 10 ** 14:
        raise HTTPException(422, "started_at must be epoch milliseconds")
    return value


def is_wav(data: bytes) -> bool:
    return len(data) >= 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


async def _audio(form: Any) -> bytes | None:
    upload = form.get("audio")
    if upload is None or (isinstance(upload, str) and not upload):
        return None
    if not isinstance(upload, UploadFile):
        raise HTTPException(422, "audio must be a file")
    data = await upload.read(MAX_AUDIO_BYTES + 1)
    if not data:
        return None
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio is too large (15 s of 16 kHz WAV at most)")
    if not is_wav(data):
        raise HTTPException(415, "audio must be WAV (16 kHz mono 16-bit)")
    return data


def setup_satellite_routes() -> APIRouter:
    from core.middleware import require_admin

    router = APIRouter(tags=["fork-voice-satellite"])

    @router.post(PREFIX + "/satellite/turn")
    async def satellite_turn(request: Request) -> Any:
        owner = voice_owner(request)
        length = request.headers.get("content-length")
        if length is None:
            raise HTTPException(411, "Content-Length is required")
        try:
            too_big = int(length) > MAX_BODY_BYTES
        except ValueError:
            raise HTTPException(400, "Bad Content-Length") from None
        if too_big:
            raise HTTPException(413, "The request is too large")
        kind = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if kind not in ("multipart/form-data", "application/x-www-form-urlencoded"):
            raise HTTPException(415, "Send multipart/form-data")
        async with request.form(max_files=1, max_fields=16) as form:
            device_id = _text(form, "device_id", required=True)
            fields = {name: _text(form, name)
                      for name in ("device_name", "area", "wake_word")}
            started = _started_at(form)
            audio = await _audio(form)
        entry = satellite.register(owner, device_id=device_id, started_at_ms=started,
                                   audio=audio, **fields)
        del audio  # only the speaker check's task holds it now
        ctx = await satellite.wait_for_check(entry, RESPONSE_WAIT_SECONDS)
        return {"ok": True, "context_id": ctx.context_id,
                "speaker": ctx.speaker.as_json() if ctx.speaker else None}

    @router.post("/api/fork/voice/announce", dependencies=[Depends(require_admin)])
    async def announce_test(request: Request, body: AnnounceTest) -> Any:
        if getattr(request.state, "api_token", False):
            raise HTTPException(403, "Only a signed-in admin can send a test announcement")
        try:
            await voice_announce.send(body.message.strip() or TEST_MESSAGE)
        except voice_announce.Refused as exc:
            return {"ok": False, "problem": str(exc)}
        return {"ok": True, "problem": None}

    return router
