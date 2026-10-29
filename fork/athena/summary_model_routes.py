"""Fork: Athena's summary model, for Settings → AI Defaults (admin session).

* ``GET /api/fork/summary-model``: the module's answer, ``{"base_url", "model",
  "default_model", "override", "available"}``, or ``{"connected": false, "problem": ...}``
  when the module can't be reached (the card then says why instead of failing).
* ``PUT /api/fork/summary-model`` ``{"model": name | null}``: use that model for summaries
  from the next one on; ``null`` goes back to the module's ``CONVO_LLM_MODEL``. The
  module refuses (422) a name its LLM server doesn't list.

Odysseus reaches the module only through the MCP server's HTTP routes
(``/settings/llm``, behind its MCP token; ``src/call_recorder.py``), never its storage.
Only the model name is sent: the module's LLM server stays what its ``.env`` says.
The hub's own background model is a normal setting (``task_endpoint_id`` /
``task_model``, ``POST /api/auth/settings``); the card in
``static/js/forkBackgroundModels.js`` sets both.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import call_recorder as rec

PATH = "/settings/llm"
OLD_MODULE = ("This version of Athena can't change its summary model from here yet: update "
              "Athena (scripts/fork_deploy.sh deploy).")


class SummaryModelPut(BaseModel):
    model: str | None = Field(default=None, max_length=200)


async def _module(method: str, json: Any = None) -> tuple[int, Any]:
    try:
        return await rec.forward(rec.resolve(), method, PATH, json=json)
    except rec.RecorderError as exc:
        message = OLD_MODULE if exc.message == rec.UPDATE_MODULE else exc.message
        raise rec.RecorderError(exc.status, message) from None


def setup_summary_model_routes() -> APIRouter:
    router = APIRouter(tags=["fork-summary-model"])

    @router.get("/api/fork/summary-model", dependencies=[Depends(require_admin)])
    async def get_summary_model():
        try:
            status, body = await _module("GET")
        except rec.RecorderError as exc:
            return {"connected": False, "problem": exc.message}
        if status != 200 or not isinstance(body, dict):
            detail = body.get("detail") if isinstance(body, dict) else None
            return {"connected": False, "problem": detail or f"Athena HTTP {status}"}
        return {"connected": True, **body}

    @router.put("/api/fork/summary-model", dependencies=[Depends(require_admin)])
    async def put_summary_model(body: SummaryModelPut):
        model = (body.model or "").strip() or None
        try:
            status, answer = await _module("PUT", {"model": model})
        except rec.RecorderError as exc:
            raise HTTPException(exc.status, exc.message) from None
        if status != 200 or not isinstance(answer, dict):
            detail = answer.get("detail") if isinstance(answer, dict) else None
            raise HTTPException(status if 400 <= status < 500 else 502,
                                detail or f"Athena HTTP {status}")
        return JSONResponse({"connected": True, **answer})

    return router
