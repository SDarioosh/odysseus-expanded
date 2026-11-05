"""Fork: voice turn timings over HTTP (docs/fork/voice-assistant.md#timings).

* ``POST /api/chat/voice/v1/satellite/timing`` (API token with the ``voice`` scope), JSON
  ``{"device_id", "started_at" (epoch ms of the wake word), "marks": {stage: ms after the
  wake word}, "flags"?: {"thinking_sound", "barge_in", "voice_check": bool},
  "start_mode"?}``: the satellite's half of a turn's timing, sent once its turn ended.
  Joined to the hub's half (``fork/voice/timings.py``); answers ``{"ok": true,
  "turn_id": <the joined turn or null>}``.
* ``GET /api/fork/voice/timings?days=7`` (signed-in admin): every step's median, 90th
  percentile and slowest, and each tool's, over the last ``days`` days.
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from fork.voice import timings
from fork.voice.routes import PREFIX, voice_owner

MAX_BODY_BYTES = 8 * 1024


def setup_timing_routes() -> APIRouter:
    from core.middleware import require_admin

    router = APIRouter(tags=["fork-voice-timings"])

    @router.post(PREFIX + "/satellite/timing")
    async def satellite_timing(request: Request) -> Any:
        owner = voice_owner(request)
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "The report is too large")
        try:
            report = timings.clean_device(json.loads(body or b"null"))
        except ValueError as exc:
            message = str(exc) if isinstance(exc, timings.BadReport) else "The body must be JSON."
            raise HTTPException(422, message) from None
        return {"ok": True, "turn_id": timings.save_device(owner, report)}

    @router.get("/api/fork/voice/timings", dependencies=[Depends(require_admin)])
    def timing_summary(days: float = 7) -> Any:
        days = max(0.01, min(float(timings.KEEP_DAYS), days))
        found = timings.rows(since=time.time() - days * 86400)
        return {"days": days, "turns": len(found), "steps": timings.summary(found),
                "tools": timings.tool_summary(found)}

    return router
