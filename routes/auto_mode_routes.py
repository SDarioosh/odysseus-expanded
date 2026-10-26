"""Fork: the default for the composer's Auto/Ask button.

* ``GET /api/fork/auto-mode``: ``{"enabled": bool}``, the default for chats that
  haven't chosen (any signed-in user; the button shows it on a new chat).
* ``PUT /api/fork/auto-mode`` ``{"enabled": bool}``: change the default (admin,
  Settings → Tools).

Each chat's own choice is sent with its messages (``fork_tool_mode``); what Auto
allows is in ``src/auto_mode.py``. The default is stored as the
``fork_auto_mode_default`` key in the normal settings file (absent: Auto).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.middleware import require_admin
from src import auto_mode


class AutoModePut(BaseModel):
    enabled: bool


def setup_auto_mode_routes() -> APIRouter:
    router = APIRouter(tags=["fork-auto-mode"])

    @router.get("/api/fork/auto-mode")
    async def get_auto_mode():
        return {"enabled": auto_mode.enabled()}

    @router.put("/api/fork/auto-mode", dependencies=[Depends(require_admin)])
    async def put_auto_mode(body: AutoModePut):
        return {"enabled": auto_mode.set_enabled(body.enabled)}

    return router
