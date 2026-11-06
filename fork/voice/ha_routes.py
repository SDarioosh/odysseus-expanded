"""Fork: "Set up Home Assistant" for the voice assistant (docs/fork/voice-assistant.md).

Settings → Integrations → Voice assistant → **Set up Home Assistant**
(``static/js/forkVoiceAssistant.js``):

1. ``POST /api/fork/voice/ha/connect`` (signed-in admin, same-origin fetch) with
   ``ha_url`` and ``odysseus_url`` (Odysseus as Home Assistant reaches it, default the
   browser's origin) answers ``authorize_url``: HA's login page, with ``client_id`` the
   origin the browser uses for Odysseus and ``redirect_uri`` the callback below on it
   (HA accepts a redirect on the client id's own host). A random ``state`` is kept in
   memory, hashed, single use, for :data:`STATE_TTL`, bound to the admin.
2. The owner signs in to HA (an administrator). HA sends the browser to
   ``GET /api/fork/voice/ha/callback?code=…&state=…``. The code is exchanged at HA's
   ``/auth/token``; :func:`fork.voice.ha_setup.run_setup` makes the voice token and sets
   up HA; HA's refresh token is then **always** revoked (``/auth/revoke``), so Odysseus
   keeps no HA login; the HA address is saved (``fork/voice/ha_settings.py``). The page
   shows each step's result.

``GET /api/fork/voice/ha`` gives the card the saved addresses (Home Assistant's, and
Odysseus's as HA reaches it) and the last run's report.
Only a signed-in admin's browser session may use these routes, never an API token.
Connect is a POST (the session cookie is SameSite=Lax, and ``Sec-Fetch-Site`` must say
same-origin): a link on another site can't start a run toward a Home Assistant of its
choice, which would hand that site a voice token.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from fork.voice import ha_setup

logger = logging.getLogger(__name__)

BASE = "/api/fork/voice/ha"
CALLBACK = BASE + "/callback"
STATE_TTL = 10 * 60  # seconds
MAX_PENDING = 10
# app.py stops a request after 45 s; the run itself goes on (shielded) and the card shows
# its report when it ends.
PAGE_WAIT = 35.0


class Connect(BaseModel):
    ha_url: str
    odysseus_url: str = ""


@dataclass
class Pending:
    owner: str
    ha_url: str
    odysseus_url: str
    client_id: str
    redirect_uri: str
    expires: float


class States:
    """Pending sign-ins by SHA-256 of their ``state``; single use."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._by_hash: dict[str, Pending] = {}

    @staticmethod
    def _hash(state: str) -> str:
        return hashlib.sha256(state.encode()).hexdigest()

    def create(self, pending: Pending) -> str:
        state = secrets.token_urlsafe(24)
        now = self._clock()
        with self._lock:
            for key in [k for k, p in self._by_hash.items() if p.expires <= now]:
                del self._by_hash[key]
            while len(self._by_hash) >= MAX_PENDING:
                del self._by_hash[min(self._by_hash, key=lambda k: self._by_hash[k].expires)]
            self._by_hash[self._hash(state)] = pending
        return state

    def take(self, state: str, owner: str) -> Pending | None:
        """The pending sign-in of ``state`` if it is ``owner``'s and not expired; used up."""
        with self._lock:
            pending = self._by_hash.pop(self._hash(state or ""), None)
        if pending is None or pending.owner != owner or pending.expires <= self._clock():
            return None
        return pending


STATES = States()
# The last run per owner, for the card: {"running": bool, "report": {...}, "at": epoch}.
RUNS: dict[str, dict[str, Any]] = {}
_TASKS: set[asyncio.Task] = set()


# ----------------------------------------------------------------------------- checks
def _admin_owner(request: Request) -> str:
    from core.middleware import require_admin
    from src.agent_runtime.authority import is_internal_tool_request
    from src.auth_helpers import get_current_user

    if getattr(request.state, "api_token", False) or is_internal_tool_request(request):
        raise HTTPException(403, "Only a signed-in admin can set up Home Assistant.")
    require_admin(request)
    owner = get_current_user(request)
    if not owner:
        raise HTTPException(403, "Not signed in")
    return str(owner)


def browser_origin(request: Request) -> str:
    """The origin of a same-origin browser request (403 otherwise)."""
    headers = request.headers
    site = (headers.get("sec-fetch-site") or "").lower()
    origin = (headers.get("origin") or "").strip()
    cross = HTTPException(403, "Start this from Odysseus's Settings in your browser.")
    if site and site != "same-origin":
        raise cross
    hosts = {h.strip().lower() for h in (headers.get("host"), headers.get("x-forwarded-host")) if h}
    if not origin or origin == "null":
        raise cross
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise cross
    if not site and parts.netloc.lower() not in hosts:
        raise cross
    return f"{parts.scheme}://{parts.netloc}"


def check_client_origin(origin: str) -> None:
    """HA accepts a client id on a name or a private/loopback IPv4, not on another IP."""
    host = urlsplit(origin).hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if address.is_loopback or (address.version == 4 and any(
            address in ipaddress.ip_network(n)
            for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))):
        return
    raise HTTPException(400, "Home Assistant only accepts a sign-in for Odysseus opened by "
                             "its name (for example https://odysseus.<tailnet>.ts.net) or a "
                             "home-network address. Open Odysseus by its name and try again.")


# ------------------------------------------------------------------------------- page
def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    page = f"""<!DOCTYPE html>
<html lang="en" class="fm-page">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>{html.escape(title)} — Odysseus</title>
  <link rel="icon" href="/static/icon.ico">
  <script src="/static/js/forkPageTheme.js"></script>
  <link rel="stylesheet" href="/static/css/00-tokens.css?v=20260929csssplit">
  <!-- Fork: Set up Home Assistant (fork/voice/ha_routes.py) -->
  <link rel="stylesheet" href="/static/css/fork-modules.css">
</head>
<body class="fm-card-page">
  <div class="bs-wrap">
    <div class="bs-top"><h1>{html.escape(title)}</h1></div>
{body}
    <div class="bs-row"><a class="bs-btn primary" href="/">Back to Odysseus</a></div>
  </div>
</body>
</html>"""
    return HTMLResponse(page, status_code=status, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY"})


MARKS = {"ok": "✓", "changed": "✓", "error": "✗", "skipped": "–"}


def report_html(report: dict[str, Any]) -> str:
    esc = html.escape
    items = "".join(
        f'<li>{MARKS.get(s["status"], "?")} <strong>{esc(s["step"])}</strong>: '
        f'{esc(s["status"])}{(" — " + esc(s["detail"])) if s.get("detail") else ""}</li>'
        for s in report.get("steps", []))
    head = ("Home Assistant is set up: the Odysseus assistant is ready."
            if report.get("ok") else "Home Assistant isn't fully set up yet.")
    extra = ""
    if report.get("token_name"):
        extra = (f'<p class="bs-text bs-meta">Home Assistant uses the voice token '
                 f'"{esc(report["token_name"])}" (Settings → API tokens). Odysseus signed '
                 f'out of Home Assistant again.</p>')
    return (f'    <div class="bs-card"><p class="bs-text">{esc(head)}</p>'
            f'<ul class="bs-list">{items}</ul>{extra}</div>')


def _error(message: str, status: int = 400) -> HTMLResponse:
    return _page("Set up Home Assistant",
                 f'    <div class="bs-notice" role="alert">{html.escape(message)}</div>', status)


# ----------------------------------------------------------------------------- the run
async def _run(owner: str, pending: Pending, code: str, invalidate: Any) -> dict[str, Any]:
    """Exchange the code, set up, always revoke HA's tokens, save the address."""
    tokens = await ha_setup.exchange_code(pending.ha_url, code, pending.client_id)
    try:
        report = await ha_setup.run_setup(owner, pending.ha_url, tokens["access_token"],
                                          pending.odysseus_url, invalidate=invalidate)
    finally:
        revoked = await ha_setup.revoke(pending.ha_url, tokens.get("refresh_token", ""))
        if not revoked:
            logger.warning("voice: Home Assistant's sign-in couldn't be revoked; remove "
                           "the Odysseus login under your HA profile → Security")
    if report.get("ok"):  # a failed setup's address isn't handed to Echos
        try:
            from fork.voice.ha_settings import set_ha_url, set_odysseus_url

            await asyncio.to_thread(set_ha_url, pending.ha_url)
            await asyncio.to_thread(set_odysseus_url, pending.odysseus_url)
        except Exception:
            logger.warning("voice: couldn't save the Home Assistant address", exc_info=True)
    report["revoked"] = revoked
    return report


async def _tracked(owner: str, pending: Pending, code: str, invalidate: Any) -> dict[str, Any]:
    RUNS[owner] = {"running": True, "report": None, "at": time.time()}
    try:
        report = await _run(owner, pending, code, invalidate)
    except ha_setup.HAError as exc:
        report = {"ok": False, "steps": [{"step": "Sign in", "status": "error",
                                          "detail": str(exc)}]}
    except Exception:
        logger.exception("voice: Home Assistant setup failed")
        report = {"ok": False, "steps": [{"step": "Setup", "status": "error",
                                          "detail": "Unexpected error; see Odysseus's log."}]}
    RUNS[owner] = {"running": False, "report": report, "at": time.time()}
    return report


def setup_ha_routes() -> APIRouter:
    router = APIRouter(tags=["fork-voice"])

    @router.get(BASE)
    def status(request: Request) -> Any:
        owner = _admin_owner(request)
        from fork.voice.ha_settings import get_ha_url, get_odysseus_url

        run = RUNS.get(owner) or {}
        return JSONResponse({"ha_url": get_ha_url(), "odysseus_url": get_odysseus_url(),
                             "running": bool(run.get("running")),
                             "report": run.get("report")},
                            headers={"Cache-Control": "no-store"})

    @router.post(BASE + "/connect")
    async def connect(request: Request, body: Connect) -> Any:
        owner = _admin_owner(request)
        origin = browser_origin(request)
        check_client_origin(origin)
        try:
            ha_url = ha_setup.check_ha_url(body.ha_url)
            odysseus_url = (body.odysseus_url or "").strip() or origin
            ha_setup.voice_base_url(odysseus_url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        from fork.voice.announce import Refused, check_url

        try:  # the same rule as announcements: only Home Assistant on the owner's network
            await check_url(ha_url)
        except Refused as exc:
            raise HTTPException(400, f"{exc}.") from None
        if (RUNS.get(owner) or {}).get("running"):
            raise HTTPException(409, "A setup is still running; wait for it to finish.")
        from core.middleware import with_asgi_root_path

        client_id = origin + "/"
        redirect_uri = origin + with_asgi_root_path(request.scope, CALLBACK)
        state = STATES.create(Pending(owner=owner, ha_url=ha_url,
                                      odysseus_url=odysseus_url.rstrip("/"),
                                      client_id=client_id, redirect_uri=redirect_uri,
                                      expires=time.time() + STATE_TTL))
        return JSONResponse({"authorize_url": ha_setup.authorize_url(
            ha_url, client_id, redirect_uri, state)}, headers={"Cache-Control": "no-store"})

    @router.get(CALLBACK)
    async def callback(request: Request) -> Any:
        try:
            owner = _admin_owner(request)
        except HTTPException as exc:
            return _error(str(exc.detail), exc.status_code)
        query = request.query_params
        pending = STATES.take(query.get("state", ""), owner)
        if pending is None:
            return _error("This sign-in link isn't valid any more. Start again from "
                          "Settings → Integrations → Voice assistant.")
        if query.get("error") or not query.get("code"):
            return _error("Home Assistant didn't sign you in "
                          f"({query.get('error') or 'no code'}). Nothing was changed.")
        task = asyncio.create_task(_tracked(owner, pending, query["code"],
                                            getattr(request.app.state, "invalidate_token_cache",
                                                    None)))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        try:
            report = await asyncio.wait_for(asyncio.shield(task), PAGE_WAIT)
        except TimeoutError:
            return _page("Set up Home Assistant",
                         '    <div class="bs-card"><p class="bs-text">Still working with Home '
                         'Assistant. The result appears in Settings → Integrations → Voice '
                         'assistant when it is done.</p></div>')
        return _page("Set up Home Assistant", report_html(report),
                     200 if report.get("ok") else 502)

    return router
