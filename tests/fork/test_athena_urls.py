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
# The same callers with tokens holding the Athena scope names.
CALLERS_NEW = {**CALLERS, "ingest": (None, "athena:ingest"), "read": (None, "athena:read"),
               "write": (None, "athena:write")}
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


def call(c: TestClient, method: str, path: str, caller: tuple[str | None, str | None]):
    user, scopes = caller
    headers = {}
    if user is not None:
        headers["X-Test-User"] = user
    if scopes is not None:
        headers["X-Test-Scopes"] = scopes
    kw = {}
    if path.endswith("/chunk") and method == "POST":
        kw = {"data": CHUNK_FORM, "files": {"audio": ("w-1-3.opus", b"OggS....", "audio/ogg")}}
    return c.request(method, concrete(path), headers=headers, follow_redirects=False, **kw)


def table(c: TestClient, word: str, callers: dict = CALLERS) -> dict:
    """``"METHOD path"`` -> (handler, statuses in :data:`CALLERS` order, redirect target)."""
    out = {}
    for method, path, handler in routes_under(c.app, word):
        answers = [call(c, method, path, who) for who in callers.values()]
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


# The old page addresses now redirect to the Athena ones (on purpose); every other old
# route answers exactly as recorded.
PAGES = {
    "GET /barvis/sound": ("former_page", "/modules/athena"),
    "GET /modules/sound": ("old_page", "/modules/athena"),
    "GET /modules/sound/record": ("old_record_page", "/modules/athena/record"),
    "GET /modules/sound/pair": ("old_pair_page", "/modules/athena/pair"),
}


def expected_old() -> dict:
    out = dict(OLD_TABLE)
    for key, (handler, target) in PAGES.items():
        out[key] = (handler, (302,) * len(CALLERS), target)
    return out


def expected_new() -> dict:
    """The Athena paths answer as the old ones did, by the same handlers."""
    return {key.replace("/sound", "/athena"): answer for key, answer in OLD_TABLE.items()
            if key != "GET /barvis/sound"}


def test_old_routes_answer_as_recorded(client):
    assert table(client, "sound") == expected_old()


def test_athena_routes_answer_like_the_old_ones(client):
    assert table(client, "athena") == expected_new()


def test_athena_scopes_pass_the_same_checks_on_both_paths(client):
    """A token with an Athena scope gets what one with the old scope got, on the old
    paths and the new; a token with neither is refused (the ``other`` column)."""
    assert table(client, "sound", CALLERS_NEW) == expected_old()
    assert table(client, "athena", CALLERS_NEW) == expected_new()


def test_old_scope_token_on_new_relay_route_and_new_on_old(client):
    for path, scopes in (("/api/codex/athena/health", "sound:ingest"),
                         ("/api/codex/sound/health", "athena:read"),
                         ("/api/codex/athena/action-items", "sound:read"),
                         ("/api/codex/sound/action-items", "athena:read")):
        r = client.get(path, headers={"X-Test-Scopes": scopes})
        assert r.status_code == 503, (path, scopes)  # past the scope check; Athena not set up
    for path in ("/api/codex/athena/action-items", "/api/codex/sound/action-items"):
        r = client.get(path, headers={"X-Test-Scopes": "athena:ingest,sound:write,todos:read"})
        assert r.status_code == 403
        assert "athena:read" in r.json()["detail"] and "sound:read" in r.json()["detail"]


@pytest.mark.parametrize(("old", "new"), [
    ("/modules/sound", "/modules/athena"),
    ("/modules/sound?embedded=1&x=a%20b", "/modules/athena?embedded=1&x=a%20b"),
    ("/modules/sound/record?auto=1", "/modules/athena/record?auto=1"),
    ("/modules/sound/pair?url=http%3A%2F%2Fhub", "/modules/athena/pair?url=http%3A%2F%2Fhub"),
    ("/barvis/sound?q=1", "/modules/athena?q=1"),
])
def test_page_redirects_keep_the_sub_page_and_query(client, old, new):
    r = client.get(old, headers={"X-Test-User": "admin"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (302, new)
    page = client.get(old, headers={"X-Test-User": "admin"})  # followed
    assert page.status_code == 200 and "<html" in page.text.lower()


def test_api_posts_are_served_not_redirected(client):
    """No API route answers with a redirect: a POST body or a phone must never see one."""
    for key, (_, statuses, location) in {**table(client, "athena"),
                                         **table(client, "sound")}.items():
        if "/api/" in key:
            assert location is None and not any(300 <= s < 400 for s in statuses), key


def test_aliases_share_the_endpoint_and_dependencies():
    from fork import routes

    app = FastAPI()
    routes.mount(app)
    by_path = {(m, r.path): r for r in all_routes(app.routes) for m in r.methods}
    pairs = 0
    for (method, path), route in by_path.items():
        for old, new in (("/api/modules/sound", "/api/modules/athena"),
                         ("/api/codex/sound", "/api/codex/athena")):
            if path.startswith(old + "/") or path == old:
                twin = by_path[(method, new + path[len(old):])]
                assert twin.endpoint is route.endpoint
                assert [d.dependency for d in twin.dependencies] == \
                    [d.dependency for d in route.dependencies]
                assert twin.tags == route.tags and twin.status_code == route.status_code
                pairs += 1
    assert pairs == sum(1 for k in OLD_TABLE if "/api/" in k)
    ids = [r.unique_id for r in all_routes(app.routes)]
    assert len(ids) == len(set(ids))  # no duplicate OpenAPI operation ids


def test_new_pairings_get_both_scope_names_and_tokens_allow_them():
    from fork.athena import phone_pairing as pp
    from fork.athena import urls
    from routes.api_token_routes import ALLOWED_SCOPES

    assert set(urls.SCOPES) | set(urls.OLD_SCOPES) <= set(pp.RELAY_SCOPES) <= ALLOWED_SCOPES


def test_admin_lists_the_athena_scopes_and_marks_the_old_names():
    from pathlib import Path

    admin_js = (Path(__file__).resolve().parents[2] / "static/js/admin.js").read_text()
    for action in ("ingest", "read", "write"):
        assert f"key: 'athena:{action}'" in admin_js
        old = next(line for line in admin_js.splitlines() if f"key: 'sound:{action}'" in line)
        assert "(old name)" in old and f"athena:{action}" in old
