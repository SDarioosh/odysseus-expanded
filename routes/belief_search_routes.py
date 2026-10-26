"""Fork: search over the belief ledger (``src/belief_search.py``; plan stage 2.3,
``docs/fork/beliefs.md`` "Retrieval (2.3)").

* ``GET /api/beliefs/search?q=<text>&k=<1..50>&status=<status|a,b|all>``:
  ``{"query", "results": [<belief + score + why>], "semantic", "pending"}``. ``status``
  defaults to ``confirmed``; 400 for an unknown status.

Same auth as the review page (``routes/beliefs_routes.py``): admin session; API tokens are
refused; every call is scoped to the logged-in owner. Mounted before the review page's
router so ``/api/beliefs/search`` isn't read as a belief id.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from core.middleware import require_admin
from src import belief_search
from src import evidence_intake as ledger
from src.auth_helpers import effective_user

MAX_K = 50


def setup_belief_search_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/beliefs/search", dependencies=[Depends(require_admin)])
    def search(request: Request, q: str = "", k: int = 10, status: str = "confirmed"):
        wanted = [s.strip() for s in (status or "confirmed").split(",") if s.strip()]
        if wanted == ["all"]:
            statuses = None
        else:
            bad = [s for s in wanted if s not in ledger.STATUSES]
            if bad or not wanted:
                raise HTTPException(400, f"unknown status: {', '.join(bad) or status}")
            statuses = wanted
        found = belief_search.search_details(effective_user(request) or "", q,
                                             k=max(1, min(int(k), MAX_K)), status=statuses)
        return {"query": q, "results": found["results"], "semantic": found["semantic"],
                "pending": found["pending"]}

    return router
