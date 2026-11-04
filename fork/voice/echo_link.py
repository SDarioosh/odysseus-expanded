"""Fork: link an Echo to Odysseus from the owner's phone (docs/fork/voice-assistant.md).

Step 3 of the one-QR Echo setup. The Echo's own setup page sends the owner's phone to
``GET /echo/link?return=<Echo URL>&name=<name>``. The signed-in admin sees which Echo
asks and where the token will go, and taps **Approve**: a same-origin ``POST /echo/link``
makes an API token with only the ``voice`` scope, named ``Echo <name> <YYYY-MM-DD>``
(listed and revoked in Settings → API tokens like any other), and answers a page that
sends the phone on to the Echo with the fields in the URL *fragment*,
``<return>#token=…&base_url=…&ha_url=…`` (a script does ``location.replace``; a
**Continue** link to the same URL is the fallback):

* ``token``: the voice token, shown nowhere else and never logged;
* ``base_url``: ``<origin the browser used>/api/chat/voice/v1``;
* ``ha_url``: the Home Assistant address saved by "Set up Home Assistant"
  (``fork.voice.ha_settings.home_assistant_url``), or empty.

The return URL is checked here (:func:`check_return`): ``http``, port 10801, path
``/setup/<code>/odysseus``, nothing else, on a private or Tailscale address or a home
network name. The Echo's setup server only listens while its setup screen is open, and
its one-time path code is what guards it. The Echo's page reads the fragment, posts it to
itself (same origin) and clears it. A fragment never reaches a server or a ``Referer``,
``replace`` keeps the URL out of the tab's back history, and a plain navigation from
https to the Echo's http raises no "form isn't secure" warning, which a form post would.

Approve is a POST from this page only: the session cookie is SameSite=Lax (no cookie on a
cross-site POST, as ``companion/routes.py`` and ``POST /api/tokens`` rely on), and
``Sec-Fetch-Site``/``Origin`` must say same-origin (a ``*.ts.net`` tailnet is one *site*,
so Lax alone would let another https service on it post here). Neither page is cached,
sends a referrer or can be framed.
"""
from __future__ import annotations

import html
import ipaddress
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from fork.athena.phone_pairing import RateLimit

logger = logging.getLogger(__name__)

PATH = "/echo/link"
ECHO_PORT = 10801
NAME_MAX = 40
TOKEN_PREFIX = "Echo"
SCOPE = "voice"
_RETURN_RE = re.compile(r"^http://([A-Za-z0-9.-]{1,253}):10801(/setup/[A-Za-z0-9_-]{16,64}/odysseus)$")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# A last label a browser reads as a number makes the whole host an IPv4 address (WHATWG
# URL): "10.1" is 10.0.0.1, "0x0a.1" too. Such a host must be a plain dotted quad.
_NUMERIC_LABEL_RE = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")
_HOME_SUFFIXES = (".ts.net", ".local", ".lan", ".home.arpa")
_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
# Names that are never the Echo: they mean the phone itself.
_LOOPBACK_NAMES = frozenset({"localhost"})


class LinkError(ValueError):
    """A request this page can't serve; ``message`` is shown to the owner."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class EchoReturn:
    url: str  # the return URL exactly as checked
    origin: str  # http://<host>:10801, for the form-action of the token page
    host: str
    tailscale: bool  # reached over Tailscale (encrypted), not over plain Wi-Fi


def check_return(raw: str) -> EchoReturn:
    """The Echo's return URL, or :class:`LinkError` (400).

    Exactly: ``http://<host>:10801/setup/<16-64 of A-Za-z0-9_->/odysseus`` with no user,
    query or fragment, where host is a private IPv4 address (10/8, 172.16/12,
    192.168/16), a Tailscale one (100.64/10), or a name that is one label or ends in
    ``.ts.net``, ``.local``, ``.lan`` or ``.home.arpa``. ``localhost`` is refused: it
    would send the token to an app on the phone, never to an Echo.
    """
    text = raw if isinstance(raw, str) else ""
    match = _RETURN_RE.fullmatch(text)  # fullmatch: "$" would let a final "\n" in
    bad = LinkError(400, "This link doesn't come from an Echo's setup page (its return "
                         "address isn't one an Echo uses). Start again on the Echo.")
    if not match:
        raise bad
    host = match.group(1).lower()
    labels = host.split(".")
    if _NUMERIC_LABEL_RE.match(labels[-1]):
        try:
            ip = ipaddress.IPv4Address(host)  # strict dotted quad, no leading zeros
        except ValueError:
            raise bad from None
        if any(ip in net for net in _PRIVATE_NETS):
            tailscale = False
        elif ip in _TAILSCALE_NET:
            tailscale = True
        else:
            raise bad
    else:
        if not all(_LABEL_RE.match(label) for label in labels) or host in _LOOPBACK_NAMES:
            raise bad
        if len(labels) > 1 and not any(
                host.endswith(s) and len(host) > len(s) for s in _HOME_SUFFIXES):
            raise bad
        tailscale = host.endswith(".ts.net")
    origin = f"http://{host}:{ECHO_PORT}"
    return EchoReturn(url=origin + match.group(2), origin=origin, host=host,
                      tailscale=tailscale)


def clean_name(raw: str) -> str:
    """The Echo's name as shown and put in the token name: printable, one line, ≤ 40."""
    text = "".join(ch if ch.isprintable() else " " for ch in (raw or ""))
    return " ".join(text.split())[:NAME_MAX].strip()


def token_name(name: str, day: date | None = None) -> str:
    stamp = (day or date.today()).isoformat()
    return f"{TOKEN_PREFIX} {name} {stamp}" if name else f"{TOKEN_PREFIX} {stamp}"


# Of an Echo's earlier tokens (same name), the newest besides the new one is kept: if the
# phone never reaches the Echo, the Echo still works with it. Older ones are deleted.
KEEP_EARLIER = 1


def prune_echo_tokens(owner: str, name: str, keep: str) -> int:
    """Delete this Echo's old voice tokens: ``owner``'s "Echo <name> <date>" tokens with only
    the ``voice`` scope, except ``keep`` (the new one) and the newest earlier one. Unnamed
    Echos can't be told apart, so nothing is deleted for them. Returns how many went."""
    if not name:
        return 0
    from core.database import ApiToken, get_db_session

    pattern = re.compile(r"^" + re.escape(f"{TOKEN_PREFIX} {name} ") + r"\d{4}-\d{2}-\d{2}$")
    removed = 0
    with get_db_session() as db:
        rows = [r for r in db.query(ApiToken).filter(ApiToken.owner == owner).all()
                if r.id != keep and pattern.match(r.name or "")
                and (r.scopes or "").strip() == SCOPE]
        rows.sort(key=lambda r: (r.last_used_at or r.created_at or datetime.min,
                                 r.created_at or datetime.min), reverse=True)
        for row in rows[KEEP_EARLIER:]:
            db.delete(row)
            removed += 1
    return removed


_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(?::[0-9]{1,5})?$|^https?://\[[0-9A-Fa-f:.]+\](?::[0-9]{1,5})?$")


def ha_origin(url: str) -> str:
    """``scheme://host[:port]`` of a saved Home Assistant address, or "" if it isn't a
    plain http(s) address (then it isn't passed on)."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        return ""
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        return ""
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    if port is None and ":" in parts.netloc.rsplit("]", 1)[-1]:
        return ""
    return origin if _ORIGIN_RE.match(origin) else ""


def saved_ha_url() -> str:
    """The HA address "Set up Home Assistant" saved, if it is a plain http(s) address."""
    try:
        from fork.voice.ha_settings import home_assistant_url
    except ImportError:
        return ""
    try:
        url = (home_assistant_url() or "").strip()
    except Exception:  # noqa: BLE001 -- an unreadable setting is "not set"
        logger.warning("echo link: couldn't read the Home Assistant address", exc_info=True)
        return ""
    return url.rstrip("/") if ha_origin(url) else ""


# --------------------------------------------------------------------------- requests
def browser_origin(request: Request) -> str:
    """The origin of a same-origin browser POST, or :class:`LinkError` (403).

    ``Sec-Fetch-Site`` (every current browser sends it) must be ``same-origin`` when
    present; without it the ``Origin`` header must name this host (``Host`` or the
    proxy's ``X-Forwarded-Host``). The origin returned is the one the browser used, which
    is the public address (as the phone-pairing page uses ``location.origin``).
    """
    headers = request.headers
    site = (headers.get("sec-fetch-site") or "").lower()
    origin = (headers.get("origin") or "").strip()
    if origin == "null":
        origin = ""
    cross = LinkError(403, "Approve only from this page in your browser.")
    if site and site != "same-origin":
        raise cross
    hosts = {h.strip().lower() for h in (headers.get("host"), headers.get("x-forwarded-host"))
             if h}
    if origin:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise cross
        if not site and parts.netloc.lower() not in hosts:
            raise cross
        return f"{parts.scheme}://{parts.netloc}"
    if not site:
        raise cross
    scheme = (headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0]
    host = headers.get("x-forwarded-host") or headers.get("host") or request.url.netloc
    return f"{scheme.strip()}://{host.strip()}"


def _admin_owner(request: Request) -> str:
    """The signed-in admin (browser session), or :class:`LinkError` (403)."""
    from core.middleware import require_admin
    from src.agent_runtime.authority import is_internal_tool_request
    from src.auth_helpers import get_current_user

    if getattr(request.state, "api_token", False) or is_internal_tool_request(request):
        raise LinkError(403, "Only a signed-in admin can link an Echo.")
    try:
        require_admin(request)
    except HTTPException:
        raise LinkError(403, "Only an Odysseus admin can link an Echo.") from None
    owner = get_current_user(request)
    if not owner:
        raise LinkError(403, "Sign in to Odysseus first.")
    return str(owner)


class _Approvals:
    """Rate limit of approvals: per owner and overall (each makes a token)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.rate = RateLimit(per_client=10, overall=30, window=600.0)

    def allow(self, owner: str) -> bool:
        with self._lock:
            return self.rate.allow(owner, time.time())


APPROVALS = _Approvals()


# ------------------------------------------------------------------------------ pages
def _nonce(request: Request) -> str:
    # SecurityHeadersMiddleware (core/middleware.py) sets its own CSP with this nonce;
    # using the same one keeps the inline script working under either header.
    nonce = getattr(request.state, "csp_nonce", None)
    if not nonce:
        nonce = secrets.token_hex(16)
        request.state.csp_nonce = nonce
    return nonce


def _csp(nonce: str) -> str:
    return ("default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; "
            "form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'")


def _page(request: Request, title: str, body: str, *, status: int = 200,
          script: str = "") -> HTMLResponse:
    """A small card page in the fork's page style (phone-pairing.html's look).

    In the app, ``SecurityHeadersMiddleware`` replaces the CSP header with its own (same
    nonce, also ``frame-ancestors 'none'``); this one applies where it doesn't run.
    """
    nonce = _nonce(request)
    esc = html.escape
    script_tag = f'<script nonce="{esc(nonce)}">{script}</script>' if script else ""
    page = f"""<!DOCTYPE html>
<html lang="en" class="fm-page">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>{esc(title)} — Odysseus</title>
  <meta name="theme-color" content="#282c34">
  <link rel="icon" href="/static/icon.ico">
  <script src="/static/js/forkPageTheme.js"></script>
  <link rel="stylesheet" href="/static/css/00-tokens.css?v=20260929csssplit">
  <!-- Fork: "Link this Echo" (fork/voice/echo_link.py), the phone-pairing page's look -->
  <link rel="stylesheet" href="/static/css/fork-modules.css">
</head>
<body class="fm-card-page">
  <div class="bs-wrap">
    <div class="bs-top"><h1>{esc(title)}</h1></div>
{body}
  </div>
{script_tag}
</body>
</html>"""
    return HTMLResponse(page, status_code=status, headers={
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": _csp(nonce),
    })


def _error_page(request: Request, exc: LinkError) -> HTMLResponse:
    body = f'    <div class="bs-notice" role="alert">{html.escape(exc.message)}</div>'
    return _page(request, "Link this Echo", body, status=exc.status)


def _action(request: Request) -> str:
    from core.middleware import with_asgi_root_path

    return with_asgi_root_path(request.scope, PATH)


def approve_page(request: Request, target: EchoReturn, name: str) -> HTMLResponse:
    esc = html.escape
    shown = name or "(no name)"
    where = f"{target.host}:{ECHO_PORT}"
    if target.tailscale:
        transport = ("It's reached over Tailscale, so the token travels encrypted.")
    else:
        transport = ("<b>Over plain Wi-Fi:</b> the token crosses your home network "
                     "unencrypted on its way to the Echo. Link it over Tailscale if you "
                     "can (the Echo then shows its Tailscale name).")
    body = f"""    <div class="bs-card">
      <p class="bs-text">Link Echo <b>“{esc(shown)}”</b> at <b>{esc(where)}</b>?</p>
      <ul class="bs-list">
        <li>Odysseus makes it a token that can only ask the voice assistant, named
          <b>{esc(token_name(name))}</b>; revoke it in Settings → API tokens.</li>
        <li>{transport}</li>
        <li>Approve only if you just started this on that Echo.</li>
      </ul>
      <form method="post" action="{esc(_action(request))}" class="bs-row">
        <input type="hidden" name="return" value="{esc(target.url)}">
        <input type="hidden" name="name" value="{esc(name)}">
        <button type="submit" class="bs-btn primary">Approve</button>
        <a class="bs-btn" href="/">Cancel</a>
      </form>
    </div>"""
    return _page(request, "Link this Echo", body)


def handoff_url(target: EchoReturn, fields: dict[str, str]) -> str:
    """``<return>#token=…&base_url=…&ha_url=…`` (form-encoded, so the Echo parses it
    with ``URLSearchParams``)."""
    return target.url + "#" + urlencode(fields, quote_via=quote)


def handoff_page(request: Request, target: EchoReturn, fields: dict[str, str]) -> HTMLResponse:
    """The page that sends the phone to the Echo with the token in the fragment (by
    itself, or on **Continue**). The script reads the URL from the link, so no value is
    ever written into script text."""
    url = handoff_url(target, fields)
    body = f"""    <div class="bs-card">
      <p class="bs-text">Linked. Taking you back to the Echo…</p>
      <div class="bs-row">
        <a id="echo-handoff" class="bs-btn primary" href="{html.escape(url)}"
           rel="noreferrer">Continue</a>
      </div>
      <p class="bs-meta">If nothing happens, tap Continue.</p>
    </div>"""
    script = "location.replace(document.getElementById('echo-handoff').href);"
    return _page(request, "Link this Echo", body, script=script)


# ----------------------------------------------------------------------------- routes
def setup_echo_link_routes() -> APIRouter:
    from fork.voice.routes import PREFIX

    router = APIRouter(tags=["fork-voice"])

    @router.get(PATH, include_in_schema=False)
    def link_page(request: Request) -> Any:
        try:
            _admin_owner(request)
            target = check_return(request.query_params.get("return", ""))
        except LinkError as exc:
            return _error_page(request, exc)
        return approve_page(request, target, clean_name(request.query_params.get("name", "")))

    @router.post(PATH, include_in_schema=False)
    async def approve(request: Request) -> Any:
        try:
            owner = _admin_owner(request)
            origin = browser_origin(request)
            form = await request.form()
            target = check_return(str(form.get("return") or ""))
            name = clean_name(str(form.get("name") or ""))
            if not APPROVALS.allow(owner):
                logger.warning("echo link: rate limited %s", owner)
                raise LinkError(429, "Too many Echo links in a short time; wait a few "
                                     "minutes.")
        except LinkError as exc:
            return _error_page(request, exc)
        from core.middleware import with_asgi_root_path
        from fork.athena.phone_pairing import create_api_token

        label = token_name(name)
        token = create_api_token(owner, label, [SCOPE])
        try:  # re-linking an Echo doesn't pile up its old tokens
            removed = prune_echo_tokens(owner, name, token["id"])
        except Exception:  # noqa: BLE001 -- the link itself still works
            logger.warning("echo link: couldn't delete old Echo tokens", exc_info=True)
            removed = 0
        invalidate = getattr(request.app.state, "invalidate_token_cache", None)
        if callable(invalidate):
            invalidate()
        logger.info("echo link: %s made voice token %s (%r) for %s; %d old removed", owner,
                    token["id"], label, target.host, removed)
        fields = {"token": token["token"],
                  "base_url": origin + with_asgi_root_path(request.scope, PREFIX),
                  "ha_url": saved_ha_url()}
        return handoff_page(request, target, fields)

    return router

