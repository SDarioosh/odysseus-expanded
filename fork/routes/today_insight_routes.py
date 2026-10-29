"""Fork: the Today insight page and its data (``src/today_insight.py``).

* ``GET  /today``: ``static/today.html`` (sidebar: Today).
* ``GET  /api/insight/today``: today's insight with its ``votes`` (404 when none yet),
  plus ``refreshing`` and ``refresh_error`` for the page's Refresh. In the owner's order
  (``src/insight_outcomes.py``: "↑ first" rows first in ``focus``, "not today" rows left
  out and listed under ``dismissed``), with ``outcomes`` ({ref: outcome}).
* ``POST /api/insight/today/vote`` {vote: up|down}: 👍/👎 on today's insight.
* ``POST /api/insight/refresh``: write it now, even when the inputs didn't change. Starts
  the work in the background and answers 202 ``{"status": "started"|"running",
  "generated": <the current insight's generated or null>}`` at once (a model call takes
  longer than the request timeout); the page polls ``GET /api/insight/today`` until
  ``generated`` changes or ``refreshing`` is false.
* ``GET  /api/codex/insight/today``: for API-token clients (Athena's phone and PC
  relays, the T5 PDA) with scope ``insight:read``; same JSON (owner order applied)
  without ``input_hash``, ``votes`` and outcomes, 404 ``{"error": "no insight yet"}`` when there is none.

The page routes use the same auth as the Athena page (admin session). To-dos are
ticked through the notes API's own ``toggle`` route, not a new one.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import insight_outcomes as io
from src import today_insight as ti
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

INSIGHT_READ_SCOPES = {"insight:read"}


class Vote(BaseModel):
    vote: str = Field(pattern="^(up|down)$")


def _owner(request: Request) -> str:
    return effective_user(request) or ""


def _no_insight(**extra: Any) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "no insight yet", **extra})


def _outcome(owner: str, insight: dict) -> dict | None:
    try:
        return io.load(owner, str(insight.get("date") or ""))
    except Exception as e:  # noqa: BLE001 -- the page works without it
        logger.warning("today insight: outcome log unreadable: %s", e)
        return None


def _owner_order(owner: str, insight: dict) -> dict:
    try:
        return io.ordered(owner, insight)
    except Exception as e:  # noqa: BLE001
        logger.warning("today insight: owner order failed: %s", e)
        return insight


# Refresh runs per owner; asyncio keeps only weak references to tasks, so hold them.
_REFRESHES: dict[str, asyncio.Task] = {}
_REFRESH_ERRORS: dict[str, str] = {}


def _refreshing(owner: str) -> bool:
    task = _REFRESHES.get(owner)
    return task is not None and not task.done()


def _refresh_state(owner: str) -> dict[str, Any]:
    return {"refreshing": _refreshing(owner), "refresh_error": _REFRESH_ERRORS.get(owner)}


async def _refresh(owner: str) -> None:
    try:
        if await ti.generate_now(owner, wait_quiet=False) is None:
            _REFRESH_ERRORS[owner] = ("The model's answer wasn't usable; the previous "
                                      "insight stays.")
    except Exception as e:  # noqa: BLE001 -- shown on the page
        logger.warning("today insight refresh failed: %s", e)
        _REFRESH_ERRORS[owner] = f"Couldn't write the insight: {e}"


def start_refresh(owner: str) -> str:
    """Start a refresh unless one is running; ``started`` or ``running``."""
    if _refreshing(owner):
        return "running"
    _REFRESH_ERRORS.pop(owner, None)
    # A fresh context: the request's own state (authority, activity) stays behind.
    _REFRESHES[owner] = asyncio.create_task(_refresh(owner), context=contextvars.Context())
    return "started"


def setup_today_insight_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/today", include_in_schema=False)
    async def page(request: Request):
        from src.app_helpers import serve_html_with_nonce
        from src.constants import BASE_DIR

        return serve_html_with_nonce(request, os.path.join(BASE_DIR, "static/today.html"))

    @router.get("/api/codex/insight/today")
    async def token_insight(request: Request):
        from routes.codex_routes import _scope_owner

        owner = _scope_owner(request, INSIGHT_READ_SCOPES)
        insight = ti.load_today(owner)
        return ti.public(_owner_order(owner, insight)) if insight else _no_insight()

    @router.get("/api/insight/today", dependencies=[Depends(require_admin)])
    async def insight(request: Request):
        owner = _owner(request)
        found = ti.load_today(owner)
        if not found:
            return _no_insight(**_refresh_state(owner))
        outcome = _outcome(owner, found)
        return {**ti.public(io.ordered(owner, found, outcome or {})),
                "votes": found.get("votes") or [], "outcomes": io.outcome_map(outcome),
                "dismissed": io.dismissed_rows(found, outcome, owner),
                **_refresh_state(owner)}

    @router.post("/api/insight/today/vote", dependencies=[Depends(require_admin)])
    async def vote(request: Request, body: Vote) -> dict[str, Any]:
        found = await ti.add_vote(_owner(request), body.vote)
        if not found:
            raise HTTPException(404, "no insight yet")
        return {"ok": True, "votes": found.get("votes") or []}

    @router.post("/api/insight/refresh", dependencies=[Depends(require_admin)])
    async def refresh(request: Request):
        owner = _owner(request)
        current = ti.load_today(owner)
        status = start_refresh(owner)
        return JSONResponse(status_code=202, content={
            "status": status, "generated": (current or {}).get("generated")})

    # Phase 3: outcome log, weights and trends (docs/fork/today-insight.md)
    from routes.insight_outcome_routes import setup_insight_outcome_routes
    from routes.insight_trends_routes import setup_insight_trends_routes
    from routes.insight_weights_routes import setup_insight_weights_routes

    for sub in (setup_insight_outcome_routes(), setup_insight_weights_routes(),
                setup_insight_trends_routes()):
        router.include_router(sub)
    return router
