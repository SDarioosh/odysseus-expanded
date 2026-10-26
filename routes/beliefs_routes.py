"""Fork: the "What do you know about me" review page over the belief ledger
(``src/evidence_intake.py``; plan stages 1.3 and 1.4, ``docs/fork/beliefs.md``).

* ``GET  /beliefs``: ``static/beliefs.html`` (sidebar: Beliefs).
* ``GET  /api/beliefs?status=<tentative|confirmed|stale|rejected|all>&subject=<str>``:
  ``{"beliefs": [...], "stats": {...}}``; without ``status``, all but rejected.
* ``GET  /api/beliefs/{id}``: one belief with its evidence and events.
* ``POST /api/beliefs/{id}/confirm``, ``POST /api/beliefs/{id}/restore``.
* ``POST /api/beliefs/{id}/correct`` {text}: 400 on empty text.
* ``POST /api/beliefs/{id}/forget`` {reason?}: forgotten completely
  (``src/belief_forget.py``, stage 4.2): rejected and erased here, the Athena
  facts behind it erased in the module (``erase_fact``; ``retire_fact`` on an older
  module). Answers ``{"belief", "propagated": {"erased", "retired", "failed",
  "unsupported"}, "erased": {"memory", "insights", "remaining", "pending"}}``. Never fails
  because the module is down: the module side is retried by the facts job. Also erases a
  belief rejected before 4.2. Confirm, correct and restore answer 409 on an erased belief.
* ``POST /api/beliefs/{id}/dismiss`` {reason?}: not worth keeping, nothing erased
  (``belief_forget.dismiss``): rejected here, ``retire_fact`` in the module (the
  transcript keeps its words). Answers ``{"belief", "propagated": {"retired", "failed",
  "unsupported"}}``; Restore brings it back. 409 on an erased belief.
* ``POST /api/beliefs/{id}/move`` {subject_id? | name?, fix_line=true}: the belief is
  about someone else (``src/belief_move.py``): it moves to that subject (``name``: one
  with that name, else a new person) and is confirmed by the owner; with ``fix_line``
  Athena gives the line that said it to that person (``reassign_line``) when the
  old subject said it. Answers the belief plus ``lines`` (``{"reassigned", "kept",
  "not_found", "failed", "unsupported"}`` or null). 400 with no target, 404 for an
  unknown belief or subject, 409 on an erased belief.
* ``GET  /api/beliefs/export``: the owner's ledger and memories as one JSON download
  (``src/belief_export.py``).
* ``POST /api/beliefs/run``: the facts job for this owner now (one module call, at most
  ``RUN_TIMEOUT`` seconds); answers its counts line.

Subjects and contacts (plan stage 2.1, ``docs/fork/beliefs.md#review-page-13``):

* ``GET  /api/beliefs?attribute=<one of ATTRIBUTES>``: structured beliefs only.
* ``GET  /api/beliefs/subjects?kind=<person|project|thing|owner>``:
  ``{"subjects": [...], "stats": {...}}``.
* ``GET  /api/beliefs/subjects/{id}``: ``{"subject", "beliefs", "summary", "stats"}``;
  beliefs of every status but rejected.
* ``POST /api/beliefs/subjects/{id}/kind`` {kind}, ``.../rename`` {display},
  ``.../link`` {contact_id}, ``.../unlink``; ``POST /api/beliefs/subjects/merge``
  {keep_id, drop_id}. 400 with the ledger's message, 404 for an unknown subject.
* ``GET  /api/beliefs/contacts``: the owner's contacts for the link picker,
  ``{"contacts": [{"id", "name", "subject_id"}]}``.
* ``GET  /api/beliefs/contact/{contact_id}?status=``: ``beliefs_for_contact``.

Belief ids are ``int`` path parameters (``{belief_id:int}``), so another router's literal
path under ``/api/beliefs/`` (e.g. ``/search``) isn't swallowed by them.

Same auth as the Today page: admin session (``require_admin``); API tokens are refused.
Another owner's id is a 404.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from core.middleware import require_admin
from src import evidence_intake as ledger
from src import module_facts as mf
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

RUN_TIMEOUT = 60.0
FORGET_TIMEOUT = 30.0
DEFAULT_STATUSES = ("tentative", "confirmed", "stale")


class Correction(BaseModel):
    text: str = ""


class Move(BaseModel):
    subject_id: int | None = None
    name: str = ""
    fix_line: bool = True


class Forget(BaseModel):
    reason: str = ""


class Kind(BaseModel):
    kind: str = ""


class Rename(BaseModel):
    display: str = ""


class Link(BaseModel):
    contact_id: str = ""


class Merge(BaseModel):
    keep_id: int
    drop_id: int


def _owner(request: Request) -> str:
    return effective_user(request) or ""


def _found(fn, *args, **kwargs):
    """A ledger call; another owner's or an unknown id is a 404."""
    try:
        return fn(*args, **kwargs)
    except ledger.BeliefNotFound as e:
        raise HTTPException(404, "belief not found") from e
    except ledger.BeliefErased as e:  # Fork 4.2: an erased belief stays erased
        raise HTTPException(409, str(e)) from e


def _subject_call(fn, *args, **kwargs):
    """A ledger subject call: an unknown or another owner's subject is a 404, invalid
    input a 400 with the ledger's message."""
    try:
        return fn(*args, **kwargs)
    except ledger.SubjectNotFound as e:
        raise HTTPException(404, "subject not found") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _statuses(status: str | None) -> Any:
    return None if status == "all" else (status or DEFAULT_STATUSES)


async def owner_contacts(owner: str) -> list[dict]:
    """The owner's Odysseus contacts as ``{"id", "name"}``, by name: the contacts routes'
    own listing (local book or CardDAV, owner-scoped the same way)."""
    from routes.contacts.contacts_routes import _fetch_contacts

    rows = await asyncio.to_thread(_fetch_contacts, owner=owner or None)
    out = [{"id": str(c.get("uid") or ""), "name": str(c.get("name") or "").strip()}
           for c in rows or [] if c.get("uid")]
    return sorted(out, key=lambda c: (c["name"].lower(), c["id"]))


def module_caller() -> mf.CallTool | None:
    """A ``list_facts``/``retire_fact`` caller for the connected Athena MCP
    server, or None when it isn't connected."""
    from src.tool_utils import get_mcp_manager

    manager = get_mcp_manager()
    server_id = mf.module_server_id(manager) if manager is not None else None
    return mf.mcp_caller(manager, server_id) if server_id else None


async def forget(owner: str, belief_id: int, reason: str = "") -> dict[str, Any]:
    """Forget completely (``src/belief_forget.py``): rejected and erased here, the facts
    behind it erased in the module; module problems are counted, never raised."""
    from src import belief_forget

    return await _found_async(
        belief_forget.forget(owner, belief_id, reason, call=module_caller(),
                             timeout=FORGET_TIMEOUT))


async def dismiss(owner: str, belief_id: int, reason: str = "") -> dict[str, Any]:
    """Dismiss (``src/belief_forget.py``): rejected here, the facts behind it retired in
    the module; nothing erased."""
    from src import belief_forget

    return await _found_async(
        belief_forget.dismiss(owner, belief_id, reason, call=module_caller(),
                              timeout=FORGET_TIMEOUT))


async def _found_async(coro):
    try:
        return await coro
    except ledger.BeliefNotFound as e:
        raise HTTPException(404, "belief not found") from e
    except ledger.BeliefErased as e:
        raise HTTPException(409, str(e)) from e


async def run_facts(owner: str) -> JSONResponse:
    if not mf.enabled():
        return JSONResponse(status_code=409, content={"error": "MODULE_FACTS_SYNC is off"})
    call = module_caller()
    if call is None:
        return JSONResponse(status_code=503, content={
            "error": f"The Athena MCP server ({mf.server_key()!r}) is not connected."})
    try:
        stats = await asyncio.wait_for(mf.sync(owner, call), RUN_TIMEOUT)
    except asyncio.TimeoutError:
        return JSONResponse(status_code=504, content={
            "error": f"Athena didn't answer within {int(RUN_TIMEOUT)} s"})
    except RuntimeError as e:  # a failed tool call, or MemoryStoreUnreadable
        logger.warning("facts sync from the review page failed: %s", e)
        return JSONResponse(status_code=502, content={"error": f"Facts sync failed: {e}"})
    if stats is None:
        return JSONResponse(content={"summary": "Athena answered with text; "
                                                "nothing synced (update the module)",
                                     "stats": None})
    return JSONResponse(content={"summary": mf.summary(stats), "stats": stats})


def setup_beliefs_routes() -> APIRouter:
    router = APIRouter()
    admin = [Depends(require_admin)]

    @router.get("/beliefs", include_in_schema=False)
    async def page(request: Request):
        from src.app_helpers import serve_html_with_nonce
        from src.constants import BASE_DIR

        return serve_html_with_nonce(request, os.path.join(BASE_DIR, "static/beliefs.html"))

    @router.get("/api/beliefs", dependencies=admin)
    async def list_beliefs(request: Request, status: str | None = None,
                           subject: str | None = None,
                           attribute: str | None = None) -> dict[str, Any]:
        owner = _owner(request)
        try:
            beliefs = ledger.list_beliefs(owner, _statuses(status),
                                          (subject or "").strip() or None,
                                          attribute=(attribute or "").strip() or None)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"beliefs": beliefs, "stats": ledger.stats(owner)}

    # Export (4.2): one JSON download, built per request, never stored or sent elsewhere.
    @router.get("/api/beliefs/export", dependencies=admin)
    async def export(request: Request) -> Response:
        from src import belief_export

        try:
            data = belief_export.build(_owner(request))
        except RuntimeError as e:  # MemoryStoreUnreadable: not an export without memories
            raise HTTPException(503, f"memory could not be read: {e}") from e
        return Response(
            content=json.dumps(data, ensure_ascii=False, indent=1),
            media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="{belief_export.filename()}"',
                     "Cache-Control": "no-store"})

    # Subjects and contacts (2.1). Literal paths, declared before /{belief_id}.
    @router.get("/api/beliefs/subjects", dependencies=admin)
    async def subjects(request: Request, kind: str | None = None) -> dict[str, Any]:
        owner = _owner(request)
        found = _subject_call(ledger.list_subjects, owner, (kind or "").strip() or None)
        return {"subjects": found, "stats": ledger.stats(owner)}

    @router.post("/api/beliefs/subjects/merge", dependencies=admin)
    async def merge(request: Request, body: Merge) -> dict[str, Any]:
        return _subject_call(ledger.merge_subjects, _owner(request), body.keep_id,
                             body.drop_id)

    @router.get("/api/beliefs/subjects/{subject_id:int}", dependencies=admin)
    async def subject(request: Request, subject_id: int) -> dict[str, Any]:
        owner = _owner(request)
        found = _subject_call(ledger.get_subject, owner, subject_id)
        return {"subject": found,
                "beliefs": ledger.list_beliefs(owner, DEFAULT_STATUSES, subject_id=found["id"]),
                "summary": ledger.subject_summary(owner, found["id"]),
                "stats": ledger.stats(owner)}

    @router.post("/api/beliefs/subjects/{subject_id:int}/kind", dependencies=admin)
    async def subject_kind(request: Request, subject_id: int, body: Kind) -> dict[str, Any]:
        return _subject_call(ledger.set_subject_kind, _owner(request), subject_id,
                             body.kind.strip())

    @router.post("/api/beliefs/subjects/{subject_id:int}/rename", dependencies=admin)
    async def subject_rename(request: Request, subject_id: int,
                             body: Rename) -> dict[str, Any]:
        return _subject_call(ledger.rename_subject, _owner(request), subject_id, body.display)

    @router.post("/api/beliefs/subjects/{subject_id:int}/link", dependencies=admin)
    async def subject_link(request: Request, subject_id: int, body: Link) -> dict[str, Any]:
        owner = _owner(request)
        _subject_call(ledger.get_subject, owner, subject_id)  # 404 before 400
        contact_id = body.contact_id.strip()
        if not contact_id:
            raise HTTPException(400, "a contact id is required")
        if contact_id not in {c["id"] for c in await owner_contacts(owner)}:
            raise HTTPException(400, f"contact {contact_id} not found")
        return _subject_call(ledger.link_contact, owner, subject_id, contact_id)

    @router.post("/api/beliefs/subjects/{subject_id:int}/unlink", dependencies=admin)
    async def subject_unlink(request: Request, subject_id: int) -> dict[str, Any]:
        return _subject_call(ledger.unlink_contact, _owner(request), subject_id)

    @router.get("/api/beliefs/contacts", dependencies=admin)
    async def contacts(request: Request) -> dict[str, Any]:
        owner = _owner(request)
        linked = {s["contact_id"]: s["id"] for s in ledger.list_subjects(owner)
                  if s["contact_id"]}
        return {"contacts": [{**c, "subject_id": linked.get(c["id"])}
                             for c in await owner_contacts(owner)]}

    @router.get("/api/beliefs/contact/{contact_id}", dependencies=admin)
    async def contact_beliefs(request: Request, contact_id: str,
                              status: str | None = None) -> dict[str, Any]:
        try:
            beliefs = ledger.beliefs_for_contact(_owner(request), contact_id,
                                                 _statuses(status))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"contact_id": contact_id, "beliefs": beliefs}

    # /run before /{belief_id}: a literal path, not an id.
    @router.post("/api/beliefs/run", dependencies=admin)
    async def run(request: Request):
        return await run_facts(_owner(request))

    @router.get("/api/beliefs/{belief_id:int}", dependencies=admin)
    async def get_one(request: Request, belief_id: int) -> dict[str, Any]:
        return _found(ledger.get_belief, _owner(request), belief_id)

    @router.post("/api/beliefs/{belief_id:int}/confirm", dependencies=admin)
    async def confirm(request: Request, belief_id: int) -> dict[str, Any]:
        return _found(ledger.confirm, _owner(request), belief_id)

    @router.post("/api/beliefs/{belief_id:int}/restore", dependencies=admin)
    async def restore(request: Request, belief_id: int) -> dict[str, Any]:
        return _found(ledger.restore, _owner(request), belief_id)

    @router.post("/api/beliefs/{belief_id:int}/correct", dependencies=admin)
    async def correct(request: Request, belief_id: int, body: Correction) -> dict[str, Any]:
        owner = _owner(request)
        _found(ledger.get_belief, owner, belief_id)  # 404 before 400
        try:
            return _found(ledger.correct, owner, belief_id, body.text)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/api/beliefs/{belief_id:int}/move", dependencies=admin)
    async def move_one(request: Request, belief_id: int, body: Move) -> dict[str, Any]:
        from src import belief_move

        try:
            return await _found_async(belief_move.move(
                _owner(request), belief_id, subject_id=body.subject_id, name=body.name,
                fix_line=body.fix_line, call=module_caller() if body.fix_line else None))
        except ledger.SubjectNotFound as e:
            raise HTTPException(404, "subject not found") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/api/beliefs/{belief_id:int}/forget", dependencies=admin)
    async def forget_one(request: Request, belief_id: int,
                         body: Forget | None = None) -> dict[str, Any]:
        return await forget(_owner(request), belief_id, (body.reason if body else ""))

    @router.post("/api/beliefs/{belief_id:int}/dismiss", dependencies=admin)
    async def dismiss_one(request: Request, belief_id: int,
                          body: Forget | None = None) -> dict[str, Any]:
        return await dismiss(_owner(request), belief_id, (body.reason if body else ""))

    return router
