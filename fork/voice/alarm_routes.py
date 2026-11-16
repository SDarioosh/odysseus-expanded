"""Fork: an Echo's alarms over HTTP (docs/fork/voice-assistant.md#alarms).

* ``GET /api/chat/voice/v1/alarms?device_id=<id>`` (API token with the ``voice`` scope):
  ``{"alarms": [{"id", "time", "days", "date", "label"}], "updated_at": iso}`` for that
  device of the token's owner, the next to ring first (``fork/voice/alarms.py``); an
  unknown device has none.
* ``DELETE /api/chat/voice/v1/alarms/{alarm_id}?device_id=<id>`` (the same token): delete
  one (the Echo's menu); ``{"ok": true}``, or ``{"ok": true, "already": true}`` when it
  isn't there. The device's feed gets a ``kind: "alarms"`` item.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from fork.voice import alarms
from fork.voice.routes import PREFIX, voice_owner
from fork.voice.satellite_routes import device_id_param

NO_STORE = {"Cache-Control": "no-store"}
MAX_ALARM_ID = 64


def setup_alarm_routes() -> APIRouter:
    router = APIRouter(tags=["fork-voice-alarms"])

    @router.get(PREFIX + "/alarms")
    async def alarm_list(request: Request, device_id: str = "") -> Any:
        owner = voice_owner(request)
        device_id = device_id_param(device_id)
        body = await asyncio.to_thread(alarms.body, owner, device_id)
        return JSONResponse(body, headers=NO_STORE)

    @router.delete(PREFIX + "/alarms/{alarm_id}")
    async def alarm_delete(request: Request, alarm_id: str, device_id: str = "") -> Any:
        owner = voice_owner(request)
        device_id = device_id_param(device_id)
        alarm_id = alarm_id.strip()
        if not alarm_id or len(alarm_id) > MAX_ALARM_ID:
            raise HTTPException(422, "alarm id is required")
        gone = await asyncio.to_thread(alarms.remove, owner, device_id, [alarm_id])
        return {"ok": True} if gone else {"ok": True, "already": True}

    return router
