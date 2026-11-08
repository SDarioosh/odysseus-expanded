"""Fork: the hub's settings per Echo over HTTP (docs/fork/voice-assistant.md#device-settings).

* ``GET /api/chat/voice/v1/devices/{device_id}/settings`` (API token with the ``voice``
  scope): ``{"version": n, "settings": {...}}`` for that device of the token's owner
  (``fork/voice/device_settings.py``); version 0 and ``{}`` when none are set.
* ``GET /api/fork/voice/devices`` (signed-in admin): the admin's devices from the registry
  (``fork/voice/devices.py``) with their settings, and the keys the card offers.
* ``PUT /api/fork/voice/devices/{device_id}/settings`` (signed-in admin),
  ``{"settings": {key: value or null}}``: the device's whole set (a key left out or null:
  the device decides). 400 for an unknown key or a bad value, 404 for a device the
  registry doesn't have. Answers ``{"version", "settings"}``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from fork.voice import device_settings, devices
from fork.voice.admin import signed_in_admin
from fork.voice.routes import PREFIX, voice_owner
from fork.voice.satellite_routes import device_id_param as _device_id

ADMIN_BASE = "/api/fork/voice/devices"
MAX_BODY_BYTES = 4 * 1024
NO_STORE = {"Cache-Control": "no-store"}


def _admin_owner(request: Request) -> str:
    """The signed-in admin (a browser session; API tokens and tool loopback refused)."""
    return signed_in_admin(request, "Only a signed-in admin can change device settings")


def device_list(owner: str) -> dict[str, Any]:
    stored = device_settings.all_for(owner)
    rows = []
    for d in devices.devices(owner):
        s = stored.get(d.device_id) or {"version": 0, "settings": {}}
        rows.append({**d.as_json(), "version": s["version"], "settings": s["settings"]})
    return {"devices": rows, "keys": [k.as_json() for k in device_settings.KEYS]}


def setup_device_settings_routes() -> APIRouter:
    router = APIRouter(tags=["fork-voice-device-settings"])

    @router.get(PREFIX + "/devices/{device_id}/settings")
    def device_get(request: Request, device_id: str) -> Any:
        owner = voice_owner(request)
        return JSONResponse(device_settings.get(owner, _device_id(device_id)),
                            headers=NO_STORE)

    @router.get(ADMIN_BASE)
    def admin_list(request: Request) -> Any:
        owner = _admin_owner(request)
        return JSONResponse(device_list(owner), headers=NO_STORE)

    @router.put(ADMIN_BASE + "/{device_id}/settings")
    async def admin_put(request: Request, device_id: str) -> Any:
        owner = _admin_owner(request)
        device_id = _device_id(device_id)
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "The request is too large")
        try:
            data = json.loads(body or b"null")
        except ValueError:
            raise HTTPException(400, "The body must be JSON") from None
        if not isinstance(data, dict) or set(data) != {"settings"}:
            raise HTTPException(400, 'Send {"settings": {...}}')
        try:
            wanted = device_settings.validate(data["settings"])
        except device_settings.BadSettings as exc:
            raise HTTPException(400, str(exc)) from None
        if await asyncio.to_thread(devices.get, owner, device_id) is None:
            raise HTTPException(404, "No such device")
        return await asyncio.to_thread(device_settings.replace, owner, device_id, wanted)

    return router
