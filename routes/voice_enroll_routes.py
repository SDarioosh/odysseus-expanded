"""Fork: "Enroll my voice" in Settings → AI Defaults (admin).

The owner presses Start, records on the watch while reading the statements the card
shows, and stops; Athena enrolls that recording as the owner's voice instead of filing it
as a conversation. Recording on the watch matters: the voiceprint then sounds like the
owner's daily recordings.

* ``GET /api/fork/voice-enroll``: the module's state (``state``: none, waiting,
  processing, done, failed, cancelled, expired; ``recording`` while one comes in;
  ``samples``, ``speech_seconds``, ``relabeled_lines`` when done; ``error``), or
  ``{"connected": false, "problem": ...}`` when the module can't be reached.
* ``POST /api/fork/voice-enroll`` ``{"name": "Shahyah"}``: wait for the next recording
  (an empty name: the module's default, the owner's name). Always as the owner.
* ``DELETE /api/fork/voice-enroll``: stop waiting.

Odysseus reaches the module only through the MCP server's HTTP routes
(``/speakers/enroll/next``, behind its MCP token; ``src/call_recorder.py``), never its
storage. Module side: ``server/convo_server/voice_enroll.py`` in Athena (MCP 0.20.0).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import call_recorder as rec

PATH = "/speakers/enroll/next"
OLD_MODULE = ("This version of Athena can't enroll a voice from here yet: update Athena "
              "(scripts/fork_deploy.sh deploy).")


class EnrollStart(BaseModel):
    name: str = Field(default="", max_length=100)


async def _module(method: str, json: Any = None) -> tuple[int, Any]:
    try:
        return await rec.forward(rec.resolve(), method, PATH, json=json)
    except rec.RecorderError as exc:
        message = OLD_MODULE if exc.message == rec.UPDATE_MODULE else exc.message
        raise rec.RecorderError(exc.status, message) from None


def _answer(status: int, body: Any) -> dict[str, Any]:
    if status != 200 or not isinstance(body, dict):
        detail = body.get("detail") if isinstance(body, dict) else None
        raise HTTPException(status if 400 <= status < 500 else 502,
                            detail or f"Athena HTTP {status}")
    return {"connected": True, **body}


def setup_voice_enroll_routes() -> APIRouter:
    router = APIRouter(tags=["fork-voice-enroll"])

    @router.get("/api/fork/voice-enroll", dependencies=[Depends(require_admin)])
    async def get_enrollment():
        try:
            status, body = await _module("GET")
        except rec.RecorderError as exc:
            return {"connected": False, "problem": exc.message}
        if status != 200 or not isinstance(body, dict):
            detail = body.get("detail") if isinstance(body, dict) else None
            return {"connected": False, "problem": detail or f"Athena HTTP {status}"}
        return {"connected": True, **body}

    @router.post("/api/fork/voice-enroll", dependencies=[Depends(require_admin)])
    async def start_enrollment(body: EnrollStart):
        name = " ".join(body.name.split())
        try:
            status, answer = await _module("POST", {"name": name or None, "is_owner": True})
        except rec.RecorderError as exc:
            raise HTTPException(exc.status, exc.message) from None
        return _answer(status, answer)

    @router.delete("/api/fork/voice-enroll", dependencies=[Depends(require_admin)])
    async def cancel_enrollment():
        try:
            status, answer = await _module("DELETE")
        except rec.RecorderError as exc:
            raise HTTPException(exc.status, exc.message) from None
        return _answer(status, answer)

    return router
