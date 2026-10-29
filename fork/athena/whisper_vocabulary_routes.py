"""Fork: words Athena's whisper is nudged toward, for Settings → AI Defaults (admin).

* ``GET /api/fork/whisper-vocabulary``: the module's answer, ``{"words", "text",
  "max_words", "env_prompt", "prompt"}``, or ``{"connected": false, "problem": ...}``
  when the module can't be reached (the card then says why instead of failing).
* ``PUT /api/fork/whisper-vocabulary`` ``{"text": "RokTalk, Meshtastic, Rokland"}``:
  comma-separated words, used from the next recording on; empty clears them. The module
  cleans the list and refuses (422) one past its limits.

Odysseus reaches the module only through the MCP server's HTTP routes
(``/settings/vocabulary``, behind its MCP token; ``src/call_recorder.py``), never its
storage. Module side: ``server/convo_server/vocabulary.py`` in Athena.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.middleware import require_admin
from fork.athena import call_recorder as rec

PATH = "/settings/vocabulary"
OLD_MODULE = ("This version of Athena can't take a vocabulary from here yet: update "
              "Athena (scripts/fork_deploy.sh deploy).")


class VocabularyPut(BaseModel):
    text: str = Field(default="", max_length=4000)


async def _module(method: str, json: Any = None) -> tuple[int, Any]:
    return await rec.module_json(method, PATH, json=json, old_module=OLD_MODULE)


def setup_whisper_vocabulary_routes() -> APIRouter:
    router = APIRouter(tags=["fork-whisper-vocabulary"])

    @router.get("/api/fork/whisper-vocabulary", dependencies=[Depends(require_admin)])
    async def get_vocabulary():
        try:
            status, body = await _module("GET")
        except rec.RecorderError as exc:
            return {"connected": False, "problem": exc.message}
        if status != 200 or not isinstance(body, dict):
            detail = body.get("detail") if isinstance(body, dict) else None
            return {"connected": False, "problem": detail or f"Athena HTTP {status}"}
        return {"connected": True, **body}

    @router.put("/api/fork/whisper-vocabulary", dependencies=[Depends(require_admin)])
    async def put_vocabulary(body: VocabularyPut):
        text = body.text.strip()
        try:
            status, answer = await _module("PUT", {"words": text or None})
        except rec.RecorderError as exc:
            raise HTTPException(exc.status, exc.message) from None
        if status != 200 or not isinstance(answer, dict):
            detail = answer.get("detail") if isinstance(answer, dict) else None
            raise HTTPException(status if 400 <= status < 500 else 502,
                                detail or f"Athena HTTP {status}")
        return JSONResponse({"connected": True, **answer})

    return router
