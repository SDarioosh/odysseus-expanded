"""Fork: the News page and its data (``fork/news/brief.py``, docs/fork/news-brief.md).

* ``GET  /news``: ``static/news.html`` (sidebar: News).
* ``GET  /api/news/today``: today's brief (the owner's local day), plus ``refreshing`` and
  ``refresh_error`` for the page's Refresh; 404 ``{"error": "no brief yet", ...}`` when
  there is none.
* ``POST /api/news/refresh``: write a new brief now. Starts the work in the background
  and answers 202 ``{"status": "started"|"running", "generated": <current or null>}`` at
  once (a run outlasts the request timeout); the page polls ``GET /api/news/today``.
* ``GET``/``PUT /api/news/config``: the categories (name, query, enabled, stories, feeds) and the
  delivery time (``H:MM``, the owner's local time); also ``default_stories``,
  ``enabled`` (``NEWS_BRIEF_ENABLED``) and ``timezone`` (read-only). A PUT answers 400
  with the reason when the config is invalid.

The data routes use the same auth as the Today page (admin session).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from core.middleware import require_admin
from fork.news import brief as nb
from fork.news import store
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)


def _owner(request: Request) -> str:
    return effective_user(request) or ""


# Refresh runs per owner; asyncio keeps only weak references to tasks, so hold them.
_REFRESHES: dict[str, asyncio.Task] = {}
_REFRESH_ERRORS: dict[str, str] = {}

REFRESH_PROBLEMS = {
    "disabled": "The news brief is turned off (NEWS_BRIEF_ENABLED=off).",
    "no_categories": "Add a category first.",
    "empty": "No category found any story; the previous brief, if any, stays.",
}


def _refreshing(owner: str) -> bool:
    task = _REFRESHES.get(owner)
    return task is not None and not task.done()


def _refresh_state(owner: str) -> dict[str, Any]:
    return {"refreshing": _refreshing(owner), "refresh_error": _REFRESH_ERRORS.get(owner)}


async def _refresh(owner: str) -> None:
    try:
        status, _ = await nb.generate_now(owner)
        if status in REFRESH_PROBLEMS:
            _REFRESH_ERRORS[owner] = REFRESH_PROBLEMS[status]
    except Exception as e:  # noqa: BLE001 -- shown on the page
        logger.warning("news brief refresh failed: %s", e)
        _REFRESH_ERRORS[owner] = f"Couldn't write the brief: {e}"


def start_refresh(owner: str) -> str:
    """Start a refresh unless one is running; ``started`` or ``running``."""
    if _refreshing(owner):
        return "running"
    _REFRESH_ERRORS.pop(owner, None)
    # A fresh context: the request's own state (authority, activity) stays behind.
    _REFRESHES[owner] = asyncio.create_task(_refresh(owner), context=contextvars.Context())
    return "started"


def _timezone_name(owner: str) -> str:
    try:
        tz = nb.owner_timezone(owner)
        return str(getattr(tz, "key", None) or tz)
    except Exception:  # noqa: BLE001
        return ""


def _config_view(owner: str, config: dict) -> dict:
    return {**config, "default_stories": nb.default_stories(), "enabled": nb.enabled(),
            "timezone": _timezone_name(owner)}


def setup_news_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/news", include_in_schema=False)
    async def page(request: Request):
        from src.app_helpers import serve_html_with_nonce
        from src.constants import BASE_DIR

        return serve_html_with_nonce(request, os.path.join(BASE_DIR, "static/news.html"))

    @router.get("/api/news/today", dependencies=[Depends(require_admin)])
    async def today(request: Request):
        owner = _owner(request)
        found = nb.load_today(owner)
        if not found:
            return JSONResponse(status_code=404, content={"error": "no brief yet",
                                                          **_refresh_state(owner)})
        return {**found, **_refresh_state(owner)}

    @router.post("/api/news/refresh", dependencies=[Depends(require_admin)])
    async def refresh(request: Request):
        owner = _owner(request)
        current = nb.load_today(owner)
        status = start_refresh(owner)
        return JSONResponse(status_code=202, content={
            "status": status, "generated": (current or {}).get("generated")})

    @router.get("/api/news/config", dependencies=[Depends(require_admin)])
    async def get_config(request: Request):
        owner = _owner(request)
        return _config_view(owner, store.load_config(owner))

    @router.put("/api/news/config", dependencies=[Depends(require_admin)])
    async def put_config(request: Request):
        owner = _owner(request)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "body must be JSON") from None
        try:
            saved = store.save_config(owner, body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return _config_view(owner, saved)

    return router
