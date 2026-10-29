"""Fork: Athena's hub URLs and token scopes, old names and new.

A characterization table pins, for every route under the old ``sound`` names, which
handler answers and the status an admin session, a non-admin session and API tokens get.
The route list comes from the mounted fork routers (``fork.routes.mount``), so a route
added later is in the table without editing it.
"""
from __future__ import annotations

import re

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

# Who calls, in table column order: name -> (X-Test-User, X-Test-Scopes). The tokens
# hold the old scope names; a token with neither name gets ``other``.
CALLERS = {
    "admin": ("admin", None),
    "user": ("someone", None),
    "ingest": (None, "sound:ingest"),
    "read": (None, "sound:read"),
    "write": (None, "sound:write"),
    "other": (None, "todos:read,calendar:read"),
}
CHUNK_FORM = {"device": "twatch", "session_id": "w-1", "sequence": "3",
              "timestamp": "2026-10-20T10:00:00Z", "codec": "ogg"}


class _Auth:
    is_configured = True

    @staticmethod
    def is_admin(user):
        return user == "admin"


def build_app(monkeypatch) -> TestClient:
    from fork import routes

    monkeypatch.setattr("core.middleware.auth_disabled", lambda: False)
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: None)  # Athena not connected
    monkeypatch.setattr("fork.athena.client._server_rows", list)
    monkeypatch.setattr("fork.athena.action_items.todo_lists", lambda owner: ["Home"])  # no DB
    app = FastAPI()
    app.state.auth_manager = _Auth()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        # Like app.py's AuthMiddleware: "X-Test-Scopes" stands in for an ody_ token.
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        if scopes is not None:
            request.state.current_user = "api"
            request.state.api_token_owner = "owner"
            request.state.api_token_scopes = [s for s in scopes.split(",") if s]
        else:
            request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    routes.mount(app)
    return TestClient(app)


def all_routes(routes) -> list[APIRoute]:
    """Every APIRoute, also those of included routers (FastAPI keeps them nested)."""
    out = []
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            out.extend(all_routes(nested.routes))
        elif isinstance(route, APIRoute):
            out.append(route)
    return out


def routes_under(app: FastAPI, word: str) -> list[tuple[str, str, str]]:
    """(method, path, handler name) of every route with ``/<word>`` as a path segment."""
    found = []
    for route in all_routes(app.routes):
        if not re.search(rf"/{word}(/|$)", route.path):
            continue
        for method in sorted(route.methods):
            found.append((method, route.path, route.endpoint.__name__))
    return found


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def call(c: TestClient, method: str, path: str, caller: str):
    user, scopes = CALLERS[caller]
    headers = {}
    if user is not None:
        headers["X-Test-User"] = user
    if scopes is not None:
        headers["X-Test-Scopes"] = scopes
    kw = {}
    if path.endswith("/chunk") and method == "POST":
        kw = {"data": CHUNK_FORM, "files": {"audio": ("w-1-3.opus", b"OggS....", "audio/ogg")}}
    return c.request(method, concrete(path), headers=headers, follow_redirects=False, **kw)


def table(c: TestClient, word: str) -> dict:
    """``"METHOD path"`` -> (handler, statuses in :data:`CALLERS` order, redirect target)."""
    out = {}
    for method, path, handler in routes_under(c.app, word):
        answers = [call(c, method, path, who) for who in CALLERS]
        out[f"{method} {path}"] = (handler, tuple(r.status_code for r in answers),
                                   answers[0].headers.get("location"))
    return out


# Recorded on the code before the Athena names were added (statuses in CALLERS order).
OLD_TABLE = {
    "GET /barvis/sound": ("former_page", (301, 301, 301, 301, 301, 301), '/modules/sound'),
    "GET /modules/sound": ("page", (200, 200, 200, 200, 200, 200), None),
    "GET /api/modules/sound/conversations": ("conversations", (503, 403, 403, 403, 403, 403), None),
    "GET /api/modules/sound/conversations/{conversation_id}": ("conversation", (503, 403, 403, 403, 403, 403), None),
    "GET /api/modules/sound/conversations/{conversation_id}/transcript": ("transcript", (503, 403, 403, 403, 403, 403), None),
    "GET /api/modules/sound/conversations/{conversation_id}/export": ("export_conversation", (503, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/lines/{line_id}": ("reassign_line", (422, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/rename-speaker": ("rename_speaker", (422, 403, 403, 403, 403, 403), None),
    "GET /api/modules/sound/unnamed": ("unnamed", (503, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/name-speaker": ("name_speaker", (422, 403, 403, 403, 403, 403), None),
    "GET /api/modules/sound/action-items": ("action_items", (503, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/action-items/{item_id}/file": ("file_action_item", (422, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/action-items/{item_id}/move": ("move_action_item", (422, 403, 403, 403, 403, 403), None),
    "GET /api/modules/sound/todo-lists": ("todo_lists", (200, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/action-items/{item_id}/undo": ("undo_action_item", (503, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/unfile-todos": ("unfile_todos", (422, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/action-items/{item_id}": ("set_action_item", (422, 403, 403, 403, 403, 403), None),
    "GET /modules/sound/record": ("record_page", (200, 200, 200, 200, 200, 200), None),
    "GET /api/modules/sound/record/config": ("record_config", (200, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/record/chunk": ("record_chunk", (400, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/record/{session_id}/end": ("record_end", (400, 403, 403, 403, 403, 403), None),
    "GET /api/modules/sound/meetings/current": ("meeting_current", (503, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/meetings": ("meeting_join", (422, 403, 403, 403, 403, 403), None),
    "GET /api/modules/sound/meetings/{meeting_id}": ("meeting_state", (503, 403, 403, 403, 403, 403), None),
    "DELETE /api/modules/sound/meetings/{meeting_id}": ("meeting_leave", (400, 403, 403, 403, 403, 403), None),
    "GET /api/modules/sound/zoom-signin": ("zoom_signin_status", (503, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/zoom-signin/start": ("zoom_signin_start", (400, 403, 403, 403, 403, 403), None),
    "POST /api/modules/sound/zoom-signin/finish": ("zoom_signin_finish", (400, 403, 403, 403, 403, 403), None),
    "GET /api/modules/sound/auto-join": ("auto_join_state", (422, 403, 403, 403, 403, 403), None),
    "PUT /api/modules/sound/auto-join": ("auto_join_set", (422, 403, 403, 403, 403, 403), None),
    "POST /api/codex/sound/chunk": ("relay_chunk", (503, 403, 503, 403, 403, 403), None),
    "POST /api/codex/sound/sessions/{session_id}/end": ("relay_end", (503, 403, 503, 403, 403, 403), None),
    "GET /api/codex/sound/health": ("relay_health", (503, 403, 503, 503, 403, 403), None),
    "GET /api/codex/sound/conversations": ("relay_conversations", (503, 403, 403, 503, 403, 403), None),
    "GET /api/codex/sound/conversations/{conv_id}": ("relay_conversation", (503, 403, 403, 503, 403, 403), None),
    "GET /api/codex/sound/conversations/{conv_id}/transcript": ("relay_transcript", (503, 403, 403, 503, 403, 403), None),
    "GET /api/codex/sound/action-items": ("relay_action_items", (503, 403, 403, 503, 403, 403), None),
    "PATCH /api/codex/sound/action-items/{item_id}": ("relay_action_item", (503, 403, 403, 403, 503, 403), None),
    "GET /api/codex/sound/digests/{date}": ("relay_digest", (503, 403, 403, 503, 403, 403), None),
    "POST /api/codex/sound/digests/{date}": ("relay_make_digest", (503, 403, 403, 403, 503, 403), None),
    "GET /modules/sound/pair": ("pair_page", (200, 200, 200, 200, 200, 200), None),
    "POST /api/modules/sound/pair": ("pair_start", (422, 403, 403, 403, 403, 403), None),
    "GET /api/modules/sound/pair/{pairing_id}": ("pair_state", (200, 403, 403, 403, 403, 403), None),
}


@pytest.fixture
def client(monkeypatch):
    return build_app(monkeypatch)


def test_old_routes_answer_as_recorded(client):
    assert table(client, "sound") == OLD_TABLE
