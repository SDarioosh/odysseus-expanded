"""Fork: Athena's phone relay through Odysseus, and pairing it by QR code.

Token routes for the relay (API tokens; an admin session works too, for testing). They
forward to the upload and read routes of the module's MCP HTTP server, with the URL and
token of the connected ``convo`` MCP server (``src/call_recorder.py``):

* ``POST /api/codex/sound/chunk`` (scope ``sound:ingest``): the module's multipart fields
  (``audio``, ``device``, ``session_id``, ``sequence``, ``timestamp``, ``codec``, ``kind``)
  and its JSON answer. Checked first like the call recorder's pieces.
* ``POST /api/codex/sound/sessions/{id}/end`` (``sound:ingest``).
* ``GET  /api/codex/sound/health`` (``sound:ingest`` or ``sound:read``): the module through
  Odysseus is reachable and the token is good (the relay's "Test connection").
* The T5 sync's module reads, the module's own paths and JSON (``sound:read``):
  ``GET /api/codex/sound/conversations``, ``…/conversations/{id}``,
  ``…/conversations/{id}/transcript``, ``…/action-items``, ``…/digests/{date}``; and its
  writes (``sound:write``): ``PATCH …/action-items/{id}``, ``POST …/digests/{date}``.
* ``GET  /api/codex/calendars`` (``calendar:read``): the token owner's calendars, so the
  relay offers a list instead of a typed calendar id.

Pairing (``src/phone_pairing.py``):

* ``GET  /modules/sound/pair``: ``static/phone-pairing.html`` (Conversations → Pair a
  phone); ``POST /api/modules/sound/pair`` {url} (admin) makes a code and its QR;
  ``GET /api/modules/sound/pair/{id}`` (admin) says whether the phone used it.
* ``POST /api/pair/redeem`` {code}: no session or token (auth-exempt hook in ``app.py``),
  rate limited; consumes the code and answers the new token once.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import call_recorder as rec
from src import phone_pairing as pairing
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

INGEST = {"sound:ingest"}
READ = {"sound:read"}
WRITE = {"sound:write"}
CALENDAR_READ = {"calendar:read", "calendar:write"}
REDEEM_PATH = "/api/pair/redeem"


class NewPairing(BaseModel):
    url: str = Field(min_length=1, max_length=300)


class Redeem(BaseModel):
    code: str = Field(min_length=1, max_length=100)


def _allow(request: Request, scopes: set[str]) -> str:
    """The caller's owner: an API token with one of ``scopes``, or an admin session."""
    if getattr(request.state, "api_token", False):
        from routes.codex_routes import _scope_owner

        return _scope_owner(request, scopes)
    require_admin(request)
    return effective_user(request) or ""


async def _module(method: str, path: str, **kwargs: Any) -> JSONResponse:
    try:
        status, body = await rec.forward(rec.resolve(), method, path, **kwargs)
    except rec.RecorderError as exc:
        raise HTTPException(exc.status, exc.message) from None
    return JSONResponse(body, status_code=status)


def setup_sound_relay_routes() -> APIRouter:
    router = APIRouter(tags=["sound-relay"])

    # ------------------------------------------------------------ relay: uploads
    @router.post("/api/codex/sound/chunk")
    async def relay_chunk(
        request: Request,
        audio: Annotated[UploadFile, File()],
        device: Annotated[str, Form()],
        session_id: Annotated[str, Form()],
        sequence: Annotated[int, Form()],
        timestamp: Annotated[str, Form()],
        codec: Annotated[str, Form()] = "ogg",
        kind: Annotated[str, Form()] = "conversation",
    ) -> dict[str, Any]:
        _allow(request, INGEST)
        data = await audio.read(rec.MAX_CHUNK_BYTES + 1)
        try:
            return await rec.forward_chunk(rec.resolve(), data=data, session_id=session_id,
                                           sequence=sequence, timestamp=timestamp,
                                           codec=codec, device_name=device, kind=kind,
                                           source=rec.RELAY)
        except rec.RecorderError as exc:
            raise HTTPException(exc.status, exc.message) from None

    @router.post("/api/codex/sound/sessions/{session_id}/end")
    async def relay_end(request: Request, session_id: str) -> dict[str, Any]:
        _allow(request, INGEST)
        try:
            out = await rec.end_session(rec.resolve(), session_id)
        except rec.RecorderError as exc:
            raise HTTPException(exc.status, exc.message) from None
        if not out["found"]:  # the relay treats this like the module's own 404
            raise HTTPException(404, "session not found")
        return {"ok": True, "stop": True}

    @router.get("/api/codex/sound/health")
    async def relay_health(request: Request):
        _allow(request, INGEST | READ)
        return await _module("GET", "/health")

    # ------------------------------------------------------- relay: T5 module reads
    @router.get("/api/codex/sound/conversations")
    async def relay_conversations(request: Request, since: str | None = None,
                                  until: str | None = None, limit: int = 50):
        _allow(request, READ)
        return await _module("GET", "/conversations",
                             params={"since": since, "until": until, "limit": limit})

    @router.get("/api/codex/sound/conversations/{conv_id}")
    async def relay_conversation(request: Request, conv_id: str):
        _allow(request, READ)
        return await _module("GET", f"/conversations/{rec.path_part(conv_id)}")

    @router.get("/api/codex/sound/conversations/{conv_id}/transcript")
    async def relay_transcript(request: Request, conv_id: str, start: float | None = None,
                               end: float | None = None):
        _allow(request, READ)
        return await _module("GET", f"/conversations/{rec.path_part(conv_id)}/transcript",
                             params={"start": start, "end": end})

    @router.get("/api/codex/sound/action-items")
    async def relay_action_items(request: Request, status: str | None = "open"):
        _allow(request, READ)
        return await _module("GET", "/action-items", params={"status": status})

    @router.patch("/api/codex/sound/action-items/{item_id}")
    async def relay_action_item(request: Request, item_id: int,
                                body: dict[str, Any] = Body(default_factory=dict)):
        _allow(request, WRITE)
        return await _module("PATCH", f"/action-items/{item_id}", json=body)

    @router.get("/api/codex/sound/digests/{date}")
    async def relay_digest(request: Request, date: str):
        _allow(request, READ)
        return await _module("GET", f"/digests/{rec.path_part(date)}")

    @router.post("/api/codex/sound/digests/{date}")
    async def relay_make_digest(request: Request, date: str):
        _allow(request, WRITE)
        return await _module("POST", f"/digests/{rec.path_part(date)}")

    # ------------------------------------------------------------ calendar list
    @router.get("/api/codex/calendars")
    async def token_calendars(request: Request) -> dict[str, Any]:
        """The token owner's calendars (read only: nothing is created here)."""
        owner = _allow(request, CALENDAR_READ)
        from core.database import CalendarCal, SessionLocal

        db = SessionLocal()
        try:
            cals = db.query(CalendarCal).filter(CalendarCal.owner == owner).all()
            return {"calendars": [{"name": c.name, "href": c.id, "source": c.source}
                                  for c in cals]}
        finally:
            db.close()

    # ------------------------------------------------------------------ pairing
    @router.get("/modules/sound/pair")
    async def pair_page(request: Request):
        from src.app_helpers import serve_html_with_nonce
        from src.constants import BASE_DIR

        return serve_html_with_nonce(request, os.path.join(BASE_DIR, "static/phone-pairing.html"))

    @router.post("/api/modules/sound/pair", dependencies=[Depends(require_admin)])
    async def pair_start(request: Request, body: NewPairing) -> dict[str, Any]:
        try:
            pending, code = pairing.PAIRINGS.create(effective_user(request) or "", body.url)
        except pairing.PairingError as exc:
            raise HTTPException(exc.status, exc.message) from None
        link = pairing.pairing_link(pending.url, code)
        return {"id": pending.id, "url": pending.url, "link": link,
                "qr": pairing.qr_png(link), "expires_in": pairing.CODE_TTL,
                "scopes": pairing.RELAY_SCOPES, "module": rec.status()}

    @router.get("/api/modules/sound/pair/{pairing_id}", dependencies=[Depends(require_admin)])
    async def pair_state(request: Request, pairing_id: str) -> dict[str, Any]:
        return pairing.PAIRINGS.state(pairing_id, effective_user(request) or "")

    @router.post(REDEEM_PATH)
    async def pair_redeem(request: Request, body: Redeem) -> JSONResponse:
        client = request.client.host if request.client else "?"

        def make_token(owner: str, name: str, scopes: list[str]) -> dict[str, str]:
            token = pairing.create_api_token(owner, name, scopes)
            invalidate = getattr(request.app.state, "invalidate_token_cache", None)
            if invalidate:
                invalidate()
            return token

        try:
            out = pairing.PAIRINGS.redeem(body.code, client, make_token)
        except pairing.PairingError as exc:
            if exc.status == 429:
                logger.warning("phone pairing: rate limited %s", client)
            return JSONResponse({"detail": exc.message}, status_code=exc.status,
                                headers={"Cache-Control": "no-store"})
        logger.info("phone pairing: %s paired", out["name"])
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    return router
