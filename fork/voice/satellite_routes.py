"""Fork: the voice satellite's routes (docs/fork/voice-assistant.md#satellites-owner-only-answers-and-announcements).

* ``POST /api/chat/voice/v1/satellite/turn`` (API token with the ``voice`` scope),
  multipart (or a urlencoded form without audio): ``device_id`` (required), ``device_name``, ``area``, ``wake_word``,
  ``started_at`` (epoch ms of the wake word), optional ``audio`` (WAV, the spoken
  command, at most :data:`MAX_AUDIO_BYTES`). Keeps a satellite context for 30 s
  (``fork/voice/satellite.py``) and starts the speaker check in the background; answers
  ``{"ok": true, "context_id", "speaker"}`` (``speaker`` null while the check still runs,
  without audio, or when Athena couldn't check). The audio is never stored or logged.
* ``POST /api/chat/voice/v1/satellite/interrupt`` (the same token), a form with
  ``device_id`` and optional ``context_id`` (what ``…/satellite/turn`` answered): the
  device's wake word interrupted its turn (a barge-in). The hub cuts that context's
  running voice turn short (without an id, or one it doesn't know, the device's), so
  Home Assistant ends its run at once (``fork/voice/interrupts.py``); answers ``{"ok":
  true, "cut": <running turns cut>, "marked": <contexts marked before HA asked>}``.
* ``POST /api/chat/voice/v1/satellite/hello`` (the same token), a form with ``device_id``
  (required), ``device_name``, ``area``, ``app_version_code``: the device says who and
  where it is, at start and hourly (``fork/voice/devices.py``; a turn records the same).
  Answers ``{"ok": true}``.
* ``GET /api/chat/voice/v1/announcements?device_id=...&since=<epoch ms>`` (the same token):
  the announcements and reminders meant for that device (every device, its area or its
  id), not yet expired (``fork/voice/announcements.py``):
  ``{"announcements": [{"id", "at", "kind", "title", "text", "target", "ref",
  "expires_at"}]}``.
* ``POST /api/fork/voice/announce`` (signed-in admin), ``{"message"?: str}``: speak a test
  announcement through Home Assistant (``fork/voice/announce.py``); ``{"ok", "problem"}``.
  With ``"device_id"`` it is an announcement to that one device instead: kept for its feed
  and, only with ``"speak": true``, spoken (``announcements.deliver``); answers ``{"ok",
  "problem", "id", "spoken"}``. ``scripts/echo_verify.py`` sends one to a made-up device
  id, so it reaches no screen and no speaker.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from fork.voice import announce as voice_announce
from fork.voice import announcements, devices, interrupts, satellite
from fork.voice.routes import PREFIX, voice_owner

logger = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 1024 * 1024  # 15 s of 16 kHz mono 16-bit WAV is ~480 KB
MAX_BODY_BYTES = MAX_AUDIO_BYTES + 64 * 1024  # the form fields and multipart framing
MAX_INTERRUPT_BYTES = 4 * 1024
RESPONSE_WAIT_SECONDS = 0.5  # a quick check is answered in the response itself
FIELD_LIMITS = {"device_id": 128, "device_name": 100, "area": 100, "wake_word": 64,
                "app_version_code": 12, "context_id": 64}
MAX_HELLO_BYTES = 4 * 1024
TEST_MESSAGE = "This is a test announcement from Odysseus."


class AnnounceTest(BaseModel):
    message: str = Field(default="", max_length=voice_announce.MAX_MESSAGE)
    # A device's feed only (scripts/echo_verify.py); spoken only with speak.
    device_id: str = Field(default="", max_length=FIELD_LIMITS["device_id"])
    speak: bool = False


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


def device_id_param(raw: str) -> str:
    """A ``device_id`` query or path parameter, whitespace collapsed (422 when empty or
    longer than :data:`FIELD_LIMITS`)."""
    device_id = " ".join(str(raw or "").split())
    if not device_id:
        raise HTTPException(422, "device_id is required")
    if len(device_id) > FIELD_LIMITS["device_id"]:
        raise HTTPException(422, "device_id is too long")
    return device_id


FORM_KINDS = ("multipart/form-data", "application/x-www-form-urlencoded")


def _check_form(request: Request, max_bytes: int, *, need_length: bool = False,
                wrong_kind: str = "Send a form", kinds: tuple[str, ...] = FORM_KINDS) -> None:
    """A form post's headers: Content-Length (411 without it when ``need_length``, 400
    when not a number, 413 over ``max_bytes``) and a Content-Type in ``kinds`` (415)."""
    length = request.headers.get("content-length")
    if length is None and need_length:
        raise HTTPException(411, "Content-Length is required")
    try:
        too_big = length is not None and int(length) > max_bytes
    except ValueError:
        raise HTTPException(400, "Bad Content-Length") from None
    if too_big:
        raise HTTPException(413, "The request is too large")
    kind = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if kind not in kinds:
        raise HTTPException(415, wrong_kind)


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


def _seen(owner: str, device_id: str, name: str | None, area: str | None,
          version: int | None = None) -> None:
    """Record the device in the registry; a failure never fails the request."""
    try:
        devices.seen(owner, device_id, name=name, area=area, app_version_code=version)
    except Exception:  # noqa: BLE001
        logger.warning("voice: recording a satellite device failed", exc_info=True)


def _adopted(turn_id: str, device_id: str, context_id: str) -> None:
    """Store an adopted context's id on its turn and timing row; never fails the post."""
    from fork.voice import ledger, timings

    try:
        ledger.set_context(turn_id, context_id)
        timings.set_context(turn_id, device_id, context_id)
    except Exception:  # noqa: BLE001
        logger.warning("voice: storing an adopted satellite context failed", exc_info=True)


async def _device_test(request: Request, device_id: str, body: AnnounceTest) -> dict:
    """A test announcement to one device: its feed only, or also spoken with ``speak``."""
    from src.auth_helpers import effective_user

    owner = effective_user(request) or ""
    message = body.message.strip() or TEST_MESSAGE
    target = {"device_id": device_id}
    if body.speak:
        out = await announcements.deliver(owner, message, target, title="Test announcement")
        return {"ok": out.item is not None and out.spoken, "problem": out.problem,
                "id": out.item["id"] if out.item else None, "spoken": out.spoken}
    try:
        item = await asyncio.to_thread(announcements.store, owner, message, target,
                                       title="Test announcement")
    except ValueError as exc:
        return {"ok": False, "problem": str(exc), "id": None, "spoken": False}
    return {"ok": True, "problem": None, "id": item["id"], "spoken": False}


def setup_satellite_routes() -> APIRouter:
    from core.middleware import require_admin

    router = APIRouter(tags=["fork-voice-satellite"])

    @router.post(PREFIX + "/satellite/turn")
    async def satellite_turn(request: Request) -> Any:
        owner = voice_owner(request)
        _check_form(request, MAX_BODY_BYTES, need_length=True,
                    wrong_kind="Send multipart/form-data")
        async with request.form(max_files=1, max_fields=16) as form:
            device_id = _text(form, "device_id", required=True)
            fields = {name: _text(form, name)
                      for name in ("device_name", "area", "wake_word")}
            started = _started_at(form)
            audio = await _audio(form)
        entry = satellite.register(owner, device_id=device_id, started_at_ms=started,
                                   audio=audio, **fields)
        await asyncio.to_thread(_seen, owner, device_id, fields.get("device_name"),
                                fields.get("area"))
        adopted = interrupts.adopt(owner, device_id, entry.ctx.context_id)
        if adopted:
            # Home Assistant asked before this post arrived: the context is that turn's,
            # so a barge-in on this device can end it (fork/voice/interrupts.py), and
            # its id finds the turn and its timing.
            satellite.mark_claimed(entry)
            await asyncio.to_thread(_adopted, adopted, device_id, entry.ctx.context_id)
        del audio  # only the speaker check's task holds it now
        ctx = await satellite.wait_for_check(entry, RESPONSE_WAIT_SECONDS)
        return {"ok": True, "context_id": ctx.context_id,
                "speaker": ctx.speaker.as_json() if ctx.speaker else None}

    @router.post(PREFIX + "/satellite/hello")
    async def satellite_hello(request: Request) -> Any:
        owner = voice_owner(request)
        _check_form(request, MAX_HELLO_BYTES)
        async with request.form(max_files=0, max_fields=8) as form:
            device_id = _text(form, "device_id", required=True)
            name, area = _text(form, "device_name"), _text(form, "area")
            version = _text(form, "app_version_code")
        if version and not (version.isascii() and version.isdigit()):
            raise HTTPException(422, "app_version_code must be a whole number")
        await asyncio.to_thread(_seen, owner, device_id, name, area,
                                int(version) if version else None)
        return {"ok": True}

    @router.get(PREFIX + "/announcements")
    def announcement_feed(request: Request, device_id: str = "", since: int = 0) -> Any:
        owner = voice_owner(request)
        device_id = device_id_param(device_id)
        return {"announcements": announcements.feed(owner, device_id, since_ms=since)}

    @router.post(PREFIX + "/satellite/interrupt")
    async def satellite_interrupt(request: Request) -> Any:
        owner = voice_owner(request)
        _check_form(request, MAX_INTERRUPT_BYTES)
        async with request.form(max_files=0, max_fields=4) as form:
            device_id = _text(form, "device_id", required=True)
            context_id = _text(form, "context_id")
        cut, marked = interrupts.interrupt(owner, device_id, context_id or None)
        if cut or marked:
            logger.info("voice: barge-in on a satellite cut %d running turn(s), marked %d "
                        "waiting context(s)", cut, marked)
        return {"ok": True, "cut": cut, "marked": marked}

    @router.post("/api/fork/voice/announce", dependencies=[Depends(require_admin)])
    async def announce_test(request: Request, body: AnnounceTest) -> Any:
        if getattr(request.state, "api_token", False):
            raise HTTPException(403, "Only a signed-in admin can send a test announcement")
        device_id = " ".join(body.device_id.split())
        if device_id:
            return await _device_test(request, device_id, body)
        try:
            await voice_announce.send(body.message.strip() or TEST_MESSAGE)
        except voice_announce.Refused as exc:
            return {"ok": False, "problem": str(exc)}
        return {"ok": True, "problem": None}

    return router
