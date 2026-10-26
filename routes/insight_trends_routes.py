"""Fork: Today insight trends routes (Phase 3), included by ``routes/today_insight_routes.py``.

* ``GET /api/insight/trends?weeks=8`` (admin session; ``weeks`` 1–26): per ISO week,
  newest first, the counts of ``src/insight_trends.py`` (insights, votes, items shown
  and their outcomes). Counts only.

Docs: docs/fork/today-insight.md#trends-and-the-insight-eval.
"""

from __future__ import annotations

from datetime import date as Date
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from core.middleware import require_admin
from src import insight_trends
from src import today_insight as ti
from src.auth_helpers import effective_user


def setup_insight_trends_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/insight/trends", dependencies=[Depends(require_admin)])
    async def trends(request: Request,
                     weeks: int = Query(insight_trends.DEFAULT_WEEKS, ge=1,
                                        le=insight_trends.MAX_WEEKS)) -> dict[str, Any]:
        owner = effective_user(request) or ""
        today = Date.fromisoformat(ti.today_str(owner))
        return insight_trends.trends(owner, today, weeks)

    return router
