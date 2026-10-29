"""Fork: the one way the hub reaches Athena.

Athena (docs/fork/modules/athena.md) is reached two ways, both through the MCP server
Odysseus has for it (Settings → MCP), and this file holds both:

* **MCP tools** (:meth:`AthenaClient.tool`, :meth:`AthenaClient.caller`): a call through
  Odysseus's MCP manager to ``mcp__<server id>__<tool>``, on the connected server whose id
  or name is the setting's value (case-insensitive). A tool that answers with an error or
  a non-zero exit raises :class:`AthenaError` (``"<tool>: <error>"``); anything else the
  manager raises (a timeout, a broken pipe) goes up as it is.
* **HTTP** (:meth:`AthenaClient.http`, :func:`request`): the plain routes of Athena's MCP
  HTTP server (``/ingest/chunk``, ``/meetings``, ``/settings/llm`` …), next to ``/mcp``
  and behind the same token. The base URL and token come from that server's row in
  Settings → MCP (``http://h:8001/mcp?access_token=T``); the token is sent as
  ``Authorization: Bearer``, never in a URL, because httpx logs request URLs at INFO.
  A refused connection or timeout raises :class:`AthenaError` (502, "Athena is not
  reachable"), or the caller's own answer for a timeout (``timed_out``).

Which setting names the server stays per caller, as it always was (fork/common/settings.py):
``AthenaClient()`` reads :func:`settings.athena_server` (to-dos, facts, today insight,
beliefs), ``AthenaClient(recorder=True)`` reads :func:`settings.athena_recorder_server`
(uploads, the Athena page, meeting bot, settings cards, voice enrollment, relay, record
share, handwritten notes). Both default to ``convo``. Athena's server under its old name
``convo`` and its new name ``athena`` is the same server: a setting naming either finds it
under either (exact name first; ``fork/common/athena_names.py``).

Each caller keeps its own words for errors: it catches :class:`AthenaError` (``status``,
``message``, ``kind``) and answers as before. ``call_recorder.RecorderError`` is this
class under its old name; ``meeting_bot.MeetingError`` is a subclass.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

from fork.common import athena_names, settings

logger = logging.getLogger(__name__)

CallTool = Callable[[str, dict], Awaitable[str]]

TIMEOUT = httpx.Timeout(60.0, connect=5.0)  # the module decodes and runs VAD before it answers
UNREACHABLE = "Athena is not reachable"


class AthenaError(RuntimeError):
    """Athena couldn't be asked, or said no. ``status`` is the HTTP status a page answers
    with, ``message`` the words it shows; ``kind`` says what went wrong (``setup``,
    ``not_connected``, ``tool``, ``timeout``, ``unreachable``, ``token``, ``error``).
    For a failed tool, ``tool`` and ``reason`` (the tool's error or stderr, or None).

    A RuntimeError, because the MCP callers have always caught a failed tool as one."""

    def __init__(self, status: int, message: str, kind: str = "error", *,
                 tool: str | None = None, reason: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.kind = kind
        self.tool = tool
        self.reason = reason


# ------------------------------------------------------------------- MCP: which server
def server_id(manager: Any, key: str) -> str | None:
    """Id of the connected MCP server whose id or name is ``key`` (case-insensitive);
    for Athena's ``athena`` / ``convo``, the other name when none has the one given."""
    from fork.upstream import mcp_connections

    connections = mcp_connections(manager)
    for want in athena_names.server_aliases(key):
        for sid, conn in connections.items():
            conn = conn or {}
            if conn.get("status") != "connected":
                continue
            if (str(sid).casefold() == want
                    or str(conn.get("name") or "").strip().casefold() == want):
                return sid
    return None


def bound_caller(manager: Any, server: str) -> CallTool:
    """``call(tool, args) -> stdout`` on the MCP server ``server`` (an id)."""
    async def call(tool: str, arguments: dict) -> str:
        res = await manager.call_tool(f"mcp__{server}__{tool}", arguments)
        if res.get("exit_code") or res.get("error"):
            reason = res.get("error") or res.get("stderr")
            raise AthenaError(502, f"{tool}: {reason or 'failed'}", "tool", tool=tool,
                              reason=reason)
        return res.get("stdout") or ""
    return call


def _mcp_manager() -> Any:
    from src.tool_utils import get_mcp_manager

    return get_mcp_manager()


# ------------------------------------------------------------------ HTTP: where it is
@dataclass(frozen=True)
class Link:
    """Where the module's HTTP routes are: the MCP server's URL without ``/mcp``."""

    base: str
    token: str

    def __repr__(self) -> str:  # never print the token
        return f"Link(base={self.base!r})"


def link_from_url(url: str) -> Link | None:
    """``http://h:8001/mcp?access_token=T`` -> ``Link("http://h:8001", "T")``; None when the
    URL is not http(s) or carries no ``access_token``."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    token = (parse_qs(parts.query).get("access_token") or [""])[0].strip()
    if not token:
        return None
    path = parts.path.rstrip("/").removesuffix("/mcp")
    return Link(base=urlunsplit((parts.scheme, parts.netloc, path, "", "")), token=token)


def _server_rows() -> list[tuple[str, str, str | None, bool]]:
    """(id, name, url, enabled) of Odysseus's configured MCP servers."""
    from core.database import McpServer, SessionLocal

    db = SessionLocal()
    try:
        return [(r.id, r.name or "", r.url, bool(r.is_enabled))
                for r in db.query(McpServer).all()]
    finally:
        db.close()


def auth_headers(link: Link) -> dict[str, str]:
    """The token as ``Authorization: Bearer``, which the module's BearerAuth accepts like
    ``?access_token=``; httpx logs request URLs at INFO, so the token never goes in one."""
    return {"Authorization": f"Bearer {link.token}"}


def _client(transport: httpx.AsyncBaseTransport | None,
            timeout: httpx.Timeout = TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)


async def request(link: Link, method: str, path: str, *, params: dict[str, Any] | None = None,
                  json: Any = None, data: Any = None, files: Any = None,
                  timeout: httpx.Timeout = TIMEOUT,
                  transport: httpx.AsyncBaseTransport | None = None,
                  timed_out: tuple[int, str] | None = None,
                  unreachable: str = UNREACHABLE) -> httpx.Response:
    """One request to the module's HTTP routes; the response as it came, whatever its
    status. Raises :class:`AthenaError` when no answer came: ``timed_out`` (status, words)
    for a timeout when the caller has its own, else 502 ``unreachable``. Only the base and
    the error's type are logged, never the request."""
    try:
        async with _client(transport, timeout) as client:
            return await client.request(method, f"{link.base}{path}", params=params or None,
                                        json=json, data=data, files=files,
                                        headers=auth_headers(link))
    except httpx.HTTPError as exc:
        if timed_out is not None and isinstance(exc, httpx.TimeoutException):
            logger.warning("Athena timed out at %s %s", method, path)
            raise AthenaError(timed_out[0], timed_out[1], "timeout") from None
        logger.warning("Athena unreachable at %s: %s", link.base, type(exc).__name__)
        raise AthenaError(502, unreachable, "unreachable") from None


# ---------------------------------------------------------------------- the client
class AthenaClient:
    """Athena through the MCP server named by one setting (or ``server``)."""

    def __init__(self, *, recorder: bool = False, server: str | None = None):
        self._recorder = recorder
        self._server = server

    def server_key(self) -> str:
        """The MCP server's name or id, read now (a changed setting counts at once)."""
        if self._server is not None:
            return self._server
        return settings.athena_recorder_server() if self._recorder else settings.athena_server()

    # ------------------------------------------------------------------------- MCP
    def server_id(self, manager: Any = None) -> str | None:
        manager = manager if manager is not None else _mcp_manager()
        return server_id(manager, self.server_key()) if manager is not None else None

    def caller(self, manager: Any = None) -> CallTool | None:
        """A ``call(tool, args)`` bound to the server connected now, or None when it
        isn't (no MCP manager, or no connected server of that name)."""
        manager = manager if manager is not None else _mcp_manager()
        sid = server_id(manager, self.server_key()) if manager is not None else None
        return bound_caller(manager, sid) if sid else None

    async def tool(self, name: str, args: dict) -> str:
        """One tool call; its text. :class:`AthenaError` 503 when not connected."""
        call = self.caller()
        if call is None:
            raise AthenaError(503, f"The Athena MCP server ({self.server_key()!r}) is not "
                                   "connected.", "not_connected")
        return await call(name, args)

    # ------------------------------------------------------------------------ HTTP
    def link(self) -> Link:
        """The module's HTTP link from the MCP server row named by the setting (enabled
        rows first). Raises a 503 :class:`AthenaError` that says what to fix."""
        key = self.server_key()
        wants = athena_names.server_aliases(key)
        rank = {want: n for n, want in enumerate(wants)}
        rows = [(r, min(rank.get(str(r[0]).casefold(), 99),
                        rank.get(r[1].strip().casefold(), 99))) for r in _server_rows()]
        rows = [r for r in rows if r[1] < 99]
        if not rows:
            raise AthenaError(503, f"No MCP server named {key!r}: add Athena's MCP "
                                   "server in Settings → MCP (docs/fork/modules/odysseus-sound.md).",
                              "setup")
        rows.sort(key=lambda r: (not r[0][3], r[1]))  # enabled first, then the name given
        rows = [r for r, _ in rows]
        if not rows[0][3]:
            raise AthenaError(503, f"The MCP server {key!r} is turned off in Settings → MCP.",
                              "setup")
        found = link_from_url(rows[0][2] or "")
        if found is None:
            raise AthenaError(503, f"The MCP server {key!r} needs an http(s) URL with "
                                   "?access_token=<CONVO_MCP_TOKEN> (Settings → MCP).", "setup")
        return found

    async def http(self, method: str, path: str, *, link: Link | None = None,
                   **kwargs: Any) -> httpx.Response:
        """:func:`request` to this server's link (resolved now unless ``link`` is given)."""
        return await request(link or self.link(), method, path, **kwargs)

    def refused_token(self, kind: str = "token") -> AthenaError:
        """The module refused Odysseus's MCP token (401/403): not the page's fault."""
        return AthenaError(502, f"Athena refused the token of the {self.server_key()!r} "
                                "MCP server (Settings → MCP).", kind)


ATHENA = AthenaClient()
RECORDER = AthenaClient(recorder=True)
