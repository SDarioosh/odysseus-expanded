"""Fork: Today insight outcome routes (Phase 3), included by ``routes/today_insight_routes.py``.

* ``POST /api/insight/today/outcome`` {ref, outcome: done|open|dismissed|promoted|clear,
  not_task?} (admin session): the owner's action on an item of today's insight
  (``src/insight_outcomes.py``); ``not_task`` with ``dismissed`` marks an open loop "Not a
  task" (the page dismisses the module item itself). ``{"ok": true, "item": {...}}``; 404
  when there is no insight today or the ref isn't in it, 400 for ``done``/``open`` on an
  event or a plan, or ``not_task`` on anything but a dismissed open loop.

Docs: docs/fork/today-insight.md#outcome-log.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import insight_outcomes as io
from src.auth_helpers import effective_user


class Outcome(BaseModel):
    ref: str = Field(min_length=3, max_length=400)
    outcome: str = Field(pattern="^(done|open|dismissed|promoted|clear)$")
    not_task: bool = False


def setup_insight_outcome_routes() -> APIRouter:
    router = APIRouter()

    @router.post("/api/insight/today/outcome", dependencies=[Depends(require_admin)])
    async def outcome(request: Request, body: Outcome) -> dict[str, Any]:
        owner = effective_user(request) or ""
        try:
            item = io.record(owner, body.ref, body.outcome, not_task=body.not_task)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "item": item}

    return router
