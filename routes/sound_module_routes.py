"""Fork: the Athena page (conversations and "who is this?" cards).

``GET /modules/sound`` serves ``static/sound-module.html`` (the former ``/barvis/sound``
redirects there); its script calls:

* ``GET  /api/modules/sound/conversations``  (query, person, since, until, kind, limit)
* ``GET  /api/modules/sound/conversations/{id}``  one conversation (``get_conversation``)
* ``GET  /api/modules/sound/conversations/{id}/transcript``  (start_seconds; 200 lines a page)
* ``POST /api/modules/sound/lines/{line_id}``  {name}: ``reassign_line``
* ``POST /api/modules/sound/rename-speaker``   {old_name, new_name}:
  ``rename_speaker_everywhere`` (a merge when new_name exists; the page confirms first)
* ``GET  /api/modules/sound/unnamed``        (limit)
* ``POST /api/modules/sound/name-speaker``   {conversation_id, speaker_label, name}
* ``GET  /api/modules/sound/action-items``   (status), with where each item was filed
* ``POST /api/modules/sound/action-items/{id}`` {status}
* ``POST /api/modules/sound/action-items/{id}/file`` {target: calendar|todo|note}: Add
  to calendar / to-dos / notes (a suggestion, or a note whose filing failed)
* ``POST /api/modules/sound/action-items/{id}/move`` {kind, due_date?, due_time?,
  list_name?}: Move to calendar / to-dos / a to-do list, Change time
* ``GET  /api/modules/sound/todo-lists``: the owner's to-do list titles (the list picker)
* ``POST /api/modules/sound/action-items/{id}/undo``: remove the Odysseus entry, dismiss
* ``POST /api/modules/sound/unfile-todos`` {apply?, ids?, all?}: the one-off clean-up of to-dos
  filed one per voice-note item (``unfile_auto_todos``; a dry run unless ``apply``;
  ``scripts/unfile_voice_note_todos.py`` calls it)

``GET /modules/sound/record`` serves ``static/call-recorder.html`` ("Record a call"); its
script calls ``GET /api/modules/sound/record/config``, ``POST /api/modules/sound/record/chunk``
(multipart: audio, session_id, sequence, timestamp, codec) and
``POST /api/modules/sound/record/{session_id}/end``. Those forward to the upload routes
of the module's MCP HTTP server (the connected ``convo`` server's URL and token), not its
MCP tools (``src/call_recorder.py``).

Action items are read live from the module, the source of truth; Done/Dismiss call its
``update_action_item``. Filing into the Odysseus calendar and to-dos (and the record of
what went where) is ``src/module_action_items.py``; the last three routes call its
``file_item`` / ``move_item`` / ``unfile_item`` and answer with the item as it is now.

All the others go through the module's MCP tools (``src/sound_module_view.py``); the module's
database is never touched. Admin only: the data is the owner's recordings, and the MCP
server is admin-configured. Naming a speaker from a card is the user's explicit action,
so it doesn't go through the chat approval card.
"""

from __future__ import annotations

import inspect
import logging
import os
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import call_recorder as recorder
from src import sound_module_view as view
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

SERVER_ENV = "SOUND_MODULE_SERVER"
LEGACY_SERVER_ENV = "BARVIS_SOUND_SERVER"  # former name, still read


class ItemStatus(BaseModel):
    status: str = Field(pattern="^(open|done|dismissed)$")


class FileItem(BaseModel):
    target: str = Field(pattern="^(calendar|todo|note)$")


class MoveItem(BaseModel):
    kind: str  # task | event | reminder, checked by the view (400)
    due_date: str | None = Field(default=None, max_length=10)  # YYYY-MM-DD or "none"
    due_time: str | None = Field(default=None, max_length=5)  # HH:MM or "none"
    list_name: str | None = Field(default=None, max_length=60)  # a to-do list or "none"


class UnfileTodos(BaseModel):
    apply: bool = False
    ids: list[int] | None = Field(default=None, max_length=500)
    all: bool = False  # also entries filed in explicit mode or from the page


class NameSpeaker(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=200)
    speaker_label: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)


class ReassignLine(BaseModel):
    name: str  # 1-100 after trimming, checked by the view (400)


class RenameSpeaker(BaseModel):
    old_name: str
    new_name: str


def _caller() -> view.CallTool:
    from src.tool_utils import get_mcp_manager

    key = (os.environ.get(SERVER_ENV) or os.environ.get(LEGACY_SERVER_ENV) or "convo").strip()
    manager = get_mcp_manager()
    server_id = view.server_id(manager, key) if manager is not None else None
    if server_id is None:
        raise HTTPException(503, f"The Athena MCP server ({key!r}) is not connected.")

    async def call(tool: str, args: dict) -> str:
        res = await manager.call_tool(f"mcp__{server_id}__{tool}", args)
        if res.get("exit_code") or res.get("error"):
            logger.warning("Athena %s failed: %s", tool, res.get("error") or res.get("stderr"))
            raise HTTPException(502, f"Athena {tool} failed")
        return res.get("stdout") or ""

    return call


def _owner(request: Request) -> str:
    return effective_user(request) or ""


def _filing():
    """src/module_action_items.py's page helpers; 503 when this Odysseus lacks them."""
    from src import module_action_items as mai

    if not all(hasattr(mai, n) for n in ("file_item", "unfile_item", "move_item", "filed_state")):
        raise HTTPException(503, "Filing action items isn't available in this Odysseus.")
    return mai


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _filed_state(owner: str) -> dict:
    """What went where; empty (nothing shown as filed) if the helper is missing or fails."""
    try:
        from src import module_action_items as mai

        fn = getattr(mai, "filed_state", None)
        if fn is None:
            return {}
        return (await _maybe_await(fn(owner))) or {}
    except Exception as e:  # noqa: BLE001 -- the list still shows without it
        logger.warning("Athena filed state unavailable: %s", e)
        return {}


async def _item_now(call: view.CallTool, owner: str, item_id: int) -> dict | None:
    """The item after a change, merged with where it is filed (None when no longer listed)."""
    for item in await view.action_items(call, "all", await _filed_state(owner)):
        if item["id"] == item_id:
            return item
    return None


async def _filing_call(what: str, coro) -> Any:
    try:
        return await coro
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:  # logged; the page shows a short error
        logger.warning("Athena %s failed: %s", what, e)
        raise HTTPException(502, f"Could not {what} the action item.") from e


def setup_sound_module_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/barvis/sound", include_in_schema=False)
    async def former_page() -> RedirectResponse:
        return RedirectResponse("/modules/sound", status_code=301)

    @router.get("/modules/sound")
    async def page(request: Request):
        from src.app_helpers import serve_html_with_nonce
        from src.constants import BASE_DIR

        return serve_html_with_nonce(request, os.path.join(BASE_DIR, "static/sound-module.html"))

    @router.get("/api/modules/sound/conversations", dependencies=[Depends(require_admin)])
    async def conversations(query: str = "", person: str = "", since: str = "",
                            until: str = "", kind: str = "", limit: int = 30) -> dict[str, Any]:
        rows = await view.conversations(_caller(), query=query[:200], person=person[:100],
                                        since=since[:32], until=until[:32], kind=kind[:16],
                                        limit=limit)
        return {"conversations": rows}

    @router.get("/api/modules/sound/conversations/{conversation_id}",
                dependencies=[Depends(require_admin)])
    async def conversation(conversation_id: str) -> dict[str, Any]:
        try:
            view.check_conversation_id(conversation_id)
            return {"conversation": await view.conversation(_caller(), conversation_id)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except LookupError as e:
            raise HTTPException(404, str(e)) from e

    @router.get("/api/modules/sound/conversations/{conversation_id}/transcript",
                dependencies=[Depends(require_admin)])
    async def transcript(conversation_id: str, start_seconds: int = 0) -> dict[str, Any]:
        try:
            view.check_conversation_id(conversation_id)
            return await view.transcript(_caller(), conversation_id, start_seconds)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except LookupError as e:
            raise HTTPException(404, str(e)) from e

    @router.post("/api/modules/sound/lines/{line_id}", dependencies=[Depends(require_admin)])
    async def reassign_line(line_id: int, body: ReassignLine) -> dict[str, Any]:
        try:
            if line_id < 1:
                raise ValueError("line id must be positive")
            view.check_name(body.name)
            return await view.reassign_line(_caller(), line_id, body.name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/api/modules/sound/rename-speaker", dependencies=[Depends(require_admin)])
    async def rename_speaker(body: RenameSpeaker) -> dict[str, Any]:
        try:
            view.check_name(body.old_name, "old_name")
            view.check_name(body.new_name, "new_name")
            return await view.rename_speaker(_caller(), body.old_name, body.new_name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.get("/api/modules/sound/unnamed", dependencies=[Depends(require_admin)])
    async def unnamed(limit: int = 10) -> dict[str, Any]:
        return {"speakers": await view.unnamed_speakers(_caller(), limit)}

    @router.post("/api/modules/sound/name-speaker", dependencies=[Depends(require_admin)])
    async def name_speaker(body: NameSpeaker) -> dict[str, Any]:
        try:
            message = await view.name_speaker(_caller(), body.conversation_id,
                                              body.speaker_label, body.name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": not message.startswith("No lines"), "message": message}

    @router.get("/api/modules/sound/action-items", dependencies=[Depends(require_admin)])
    async def action_items(request: Request, status: str = "open") -> dict[str, Any]:
        try:
            if status not in {"open", "done", "dismissed", "all"}:
                raise ValueError("status must be open, done, dismissed or all")
            call = _caller()
            filed = await _filed_state(_owner(request))
            return {"action_items": await view.action_items(call, status, filed)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/api/modules/sound/action-items/{item_id}/file",
                 dependencies=[Depends(require_admin)])
    async def file_action_item(item_id: int, body: FileItem, request: Request
                               ) -> dict[str, Any]:
        """Add to calendar / to-dos: the owner's tap on a suggestion."""
        try:
            view.check_item_id(item_id)
            view.check_target(body.target)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        mai, call, owner = _filing(), _caller(), _owner(request)
        try:
            row = await view.find_item(call, item_id, "open")
        except LookupError as e:
            raise HTTPException(404, f"No open action item #{item_id}.") from e
        if view.filed_for(await _filed_state(owner), item_id):
            raise HTTPException(409, f"Action item #{item_id} is filed already; move it instead.")
        filed = await _filing_call("file", mai.file_item(owner, row, call, target=body.target))
        where = {"calendar": "calendar", "note": "notes"}.get(body.target, "to-dos")
        return {"ok": True, "message": f"Added to {where}.", "filed": filed,
                "item": await _item_now(call, owner, item_id)}

    @router.post("/api/modules/sound/action-items/{item_id}/move",
                 dependencies=[Depends(require_admin)])
    async def move_action_item(item_id: int, body: MoveItem, request: Request
                               ) -> dict[str, Any]:
        """Move to calendar / to-dos, or Change time: the module item, then its entry."""
        try:
            view.check_item_id(item_id)
            kind, due_date, due_time = view.check_move(body.kind, body.due_date, body.due_time)
            list_name = view.check_list_name(body.list_name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        mai, call, owner = _filing(), _caller(), _owner(request)
        extra = {"list_name": list_name} if list_name is not None else {}
        filed = await _filing_call("move", mai.move_item(
            owner, item_id, call, kind=kind, due_date=due_date, due_time=due_time, **extra))
        return {"ok": True, "message": "Saved.", "filed": filed,
                "item": await _item_now(call, owner, item_id)}

    @router.get("/api/modules/sound/todo-lists", dependencies=[Depends(require_admin)])
    async def todo_lists(request: Request) -> dict[str, Any]:
        """The owner's to-do list titles, for "Add to list" on an item."""
        mai = _filing()
        fn = getattr(mai, "todo_lists", None)
        return {"lists": fn(_owner(request)) if fn else []}

    @router.post("/api/modules/sound/action-items/{item_id}/undo",
                 dependencies=[Depends(require_admin)])
    async def undo_action_item(item_id: int, request: Request) -> dict[str, Any]:
        """Remove the Odysseus event / to-do and dismiss the item (it isn't filed again)."""
        try:
            view.check_item_id(item_id)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        mai, call, owner = _filing(), _caller(), _owner(request)
        result = await _filing_call("undo", mai.unfile_item(owner, item_id, call))
        return {"ok": True, "message": "Undone: removed from Odysseus and dismissed.",
                "result": result, "item": await _item_now(call, owner, item_id)}

    @router.post("/api/modules/sound/unfile-todos", dependencies=[Depends(require_admin)])
    async def unfile_todos(body: UnfileTodos, request: Request) -> dict[str, Any]:
        """List (dry run) or remove the single to-dos filed from voice notes."""
        from src import module_action_items as mai

        fn = getattr(mai, "unfile_auto_todos", None)
        if fn is None:
            raise HTTPException(503, "This Odysseus can't clean up filed to-dos.")
        only = set(body.ids) if body.ids is not None else None
        result = await _filing_call("clean up", fn(_owner(request), _caller(),
                                                   apply=body.apply, only=only,
                                                   include_all=body.all))
        return {"ok": True, **result}

    @router.post("/api/modules/sound/action-items/{item_id}",
                 dependencies=[Depends(require_admin)])
    async def set_action_item(item_id: int, body: ItemStatus) -> dict[str, Any]:
        message = await view.set_action_item(_caller(), item_id, body.status)
        return {"ok": message.startswith("Action item"), "message": message}

    # ------------------------------------------------ record a call (src/call_recorder.py)
    @router.get("/modules/sound/record")
    async def record_page(request: Request):
        from src.app_helpers import serve_html_with_nonce
        from src.constants import BASE_DIR

        return serve_html_with_nonce(request, os.path.join(BASE_DIR, "static/call-recorder.html"))

    @router.get("/api/modules/sound/record/config", dependencies=[Depends(require_admin)])
    async def record_config() -> dict[str, Any]:
        return {**recorder.status(), "device": recorder.device(),
                "max_bytes": recorder.MAX_CHUNK_BYTES}

    @router.post("/api/modules/sound/record/chunk", dependencies=[Depends(require_admin)])
    async def record_chunk(
        audio: Annotated[UploadFile, File()],
        session_id: Annotated[str, Form()],
        sequence: Annotated[int, Form()],
        timestamp: Annotated[str, Form()],
        codec: Annotated[str, Form()] = "webm",
        x_odysseus_recorder: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        _recorder_header(x_odysseus_recorder)
        data = await audio.read(recorder.MAX_CHUNK_BYTES + 1)
        try:
            out = await recorder.forward_chunk(recorder.resolve(), data=data,
                                               session_id=session_id, sequence=sequence,
                                               timestamp=timestamp, codec=codec,
                                               device_name=recorder.device())
            return {"ok": out["ok"], "stop": out["stop"], "reason": out["reason"]}
        except recorder.RecorderError as exc:
            raise HTTPException(exc.status, exc.message) from None

    @router.post("/api/modules/sound/record/{session_id}/end",
                 dependencies=[Depends(require_admin)])
    async def record_end(session_id: str,
                         x_odysseus_recorder: Annotated[str | None, Header()] = None
                         ) -> dict[str, Any]:
        _recorder_header(x_odysseus_recorder)
        try:
            return await recorder.end_session(recorder.resolve(), session_id)
        except recorder.RecorderError as exc:
            raise HTTPException(exc.status, exc.message) from None

    return router


def _recorder_header(value: str | None) -> None:
    # A custom header makes these uploads preflighted, so another site can't post audio
    # into the owner's recordings with a plain form (the session cookie is also SameSite).
    if value != "1":
        raise HTTPException(400, "missing X-Odysseus-Recorder header")
