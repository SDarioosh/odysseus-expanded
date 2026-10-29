"""Fork: reconnect an HTTP/SSE MCP server whose session was lost, and retry the call once.

When a Streamable HTTP server restarts (a module redeploy, a server reboot, an outage),
the session id Odysseus holds is unknown to it: every call is answered 404, which the MCP
client reports as "Session terminated". Upstream only reconnects built-in stdio servers,
so a user server stayed "connected" while every call failed until someone pressed
Reconnect. Module pages showed errors and background tasks (e.g. the "Module Action
Items" filing) failed every pass in the meantime.

Here, a call to a non-built-in ``http``/``sse`` server that fails with a lost session or a
closed transport reconnects that server from its stored config (what the Reconnect button
does) and retries the call once. Retrying is safe for these errors only: the server
refused the request (unknown session) or it was never sent (connection refused, stream
closed), so the tool did not run. Any other error is returned unchanged, and nothing is
retried inside a turn's sealed backend operation (the new connection is a new resource
identity; the next call uses it). If the server is still down, a background retry
connects it once it is back (every ``HEAL_SECONDS``, for a day; it stops when the server
is disabled, deleted, or connected some other way).

Module servers (those in ``ODYSSEUS_TRUSTED_READONLY_MCP``, and the action-items server)
also get a deadline per call, ``CALL_TIMEOUT_SECONDS``. While such a server is down, the
MCP client logs the connection error but never answers the waiting call, so it waited
forever, even after a Reconnect, and a scheduled task stuck in it never ran again (the
scheduler skips a task whose last run is still in flight). A call past the deadline
returns an error and reconnects the server, without a retry: the tool may have run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

RETRY_SECONDS = 30.0
CALL_TIMEOUT_SECONDS = 60.0
HEAL_SECONDS = 60.0
HEAL_ATTEMPTS = 24 * 60  # a day
PROBE_SECONDS = 3.0
_HEALERS: dict[str, asyncio.Task] = {}
_BACKGROUND: set[asyncio.Task] = set()
_LOCKS: dict[str, asyncio.Lock] = {}
_LAST_ATTEMPT: dict[str, float] = {}


def _messages(exc: BaseException) -> list[str]:
    out = [str(exc)]
    err = getattr(exc, "error", None)  # mcp.shared.exceptions.McpError
    if err is not None:
        out.append(str(getattr(err, "message", "") or ""))
    return [m.strip().lower() for m in out if m]


def is_lost_session(exc: BaseException) -> bool:
    """The call never ran: the server dropped our session, or the transport is gone."""
    if any(m == "session terminated" for m in _messages(exc)):
        return True
    try:
        import anyio
        import httpx
    except ImportError:  # pragma: no cover
        return False
    # not EndOfStream: the answer can be cut off after the tool ran
    return isinstance(exc, (httpx.ConnectError, anyio.ClosedResourceError,
                            anyio.BrokenResourceError))


class CallTimeout(Exception):
    """A module MCP call got no answer within ``CALL_TIMEOUT_SECONDS``."""


def _is_module_server(manager: Any, server_id: str) -> bool:
    from src.mcp_trust import trusted_readonly_server_keys

    keys = set(trusted_readonly_server_keys())
    try:
        from src.module_action_items import server_key

        keys.add(server_key())
    except Exception:  # noqa: BLE001
        pass
    conn = (getattr(manager, "_connections", {}) or {}).get(server_id) or {}
    name = str(conn.get("name") or "").strip().casefold()
    return str(server_id).casefold() in keys or bool(name and name in keys)


async def guarded_call(manager: Any, server_id: str, session: Any, tool_name: str,
                       arguments: dict) -> dict:
    """``manager._do_call`` with a deadline for module servers; raises ``CallTimeout``."""
    if manager.is_builtin(server_id) or not _is_module_server(manager, server_id):
        return await manager._do_call(session, tool_name, arguments)
    try:
        return await asyncio.wait_for(manager._do_call(session, tool_name, arguments),
                                      CALL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as e:
        raise CallTimeout(f"no answer from MCP server {server_id} within "
                          f"{CALL_TIMEOUT_SECONDS:g}s (is it running?)") from e


def _server_config(server_id: str) -> Any:
    from src.database import McpServer, SessionLocal

    db = SessionLocal()
    try:
        srv = db.query(McpServer).filter(McpServer.id == server_id).first()
        if srv is not None:
            db.expunge(srv)
        return srv
    finally:
        db.close()


async def _connect(manager: Any, server_id: str, srv: Any) -> bool:
    return await manager.connect_server(
        server_id=server_id, name=srv.name, transport=srv.transport,
        command=srv.command, args=json.loads(srv.args) if srv.args else [],
        env=json.loads(srv.env) if srv.env else {}, url=srv.url)


def _usable(srv: Any) -> bool:
    return srv is not None and srv.transport in {"http", "sse"} and bool(srv.is_enabled)


async def _close_quietly(stack: Any) -> None:
    try:
        await stack.aclose()
    except BaseException as e:  # noqa: BLE001 -- a dead connection; nothing to keep
        logger.debug("closing a lost MCP connection: %s", e)


def _drop(manager: Any, server_id: str) -> None:
    """Forget the dead session the way ``disconnect_server`` does, and close its stack
    in a task of its own: the HTTP transport was entered by another task, and closing it
    cancels whichever task does the closing (it must not be the caller's)."""
    stack = manager._stacks.pop(server_id, None)
    task = manager._connect_tasks.pop(server_id, None)
    if task is not None and not task.done():
        task.cancel()
    manager._sessions.pop(server_id, None)
    manager._resource_connections.pop(server_id, None)
    manager._tools.pop(server_id, None)
    manager._connections.pop(server_id, None)
    manager._generation += 1
    if stack is not None:
        _BACKGROUND.add(t := asyncio.create_task(_close_quietly(stack)))
        t.add_done_callback(_BACKGROUND.discard)


async def _reachable(url: str | None) -> bool:
    """Does anything answer HTTP at ``url``? (While the server is down, the MCP client
    neither fails nor finishes a connect, so don't start one.)"""
    if not url:
        return False
    try:
        import httpx

        async with httpx.AsyncClient(timeout=PROBE_SECONDS) as client:
            await client.get(url)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _reconnect(manager: Any, server_id: str) -> bool:
    srv = _server_config(server_id)
    if not _usable(srv):
        return False
    _drop(manager, server_id)
    ok = await _reachable(srv.url) and await _isolated(_connect(manager, server_id, srv),
                                                       server_id)
    if ok:
        logger.info("MCP server %s reconnected after its session was lost", srv.name)
    else:
        logger.warning("MCP server %s is unreachable after its session was lost; trying "
                       "again every %gs", srv.name, HEAL_SECONDS)
        manager._connections[server_id] = {
            "status": "error", "name": srv.name, "transport": srv.transport,
            "error": "Unreachable after the connection was lost; reconnecting automatically"}
        _start_healer(manager, server_id)
    return ok


async def _isolated(coro: Any, server_id: str) -> bool:
    """Run ``coro`` in a task of its own. Closing the old HTTP connection exits AnyIO cancel
    scopes that another task entered, which cancels whichever task does the closing;
    that must not be the caller's (a chat turn, a scheduled task)."""
    task = asyncio.create_task(coro)
    await asyncio.wait({task})
    if task.cancelled():
        logger.info("MCP reconnect step for %s was cancelled", server_id)
        return False
    if task.exception() is not None:
        logger.warning("MCP reconnect of %s failed: %s", server_id, task.exception())
        return False
    return bool(task.result())


def _start_healer(manager: Any, server_id: str) -> None:
    """Keep trying to connect a server that was down when its session was lost, so it
    doesn't stay disconnected until someone presses Reconnect."""
    task = _HEALERS.get(server_id)
    if task is None or task.done():
        _HEALERS[server_id] = asyncio.create_task(_heal(manager, server_id))


async def _heal(manager: Any, server_id: str) -> None:
    for _ in range(HEAL_ATTEMPTS):
        await asyncio.sleep(HEAL_SECONDS)
        srv = _server_config(server_id)
        status = (manager.get_server_status(server_id) or {}).get("status")
        # gone, disabled, or reconnected some other way (Reconnect, OAuth): stop
        if not _usable(srv) or status != "error" or server_id in manager._sessions:
            return
        if await _reachable(srv.url) and await _isolated(_connect(manager, server_id, srv),
                                                         server_id):
            logger.info("MCP server %s is back; reconnected", srv.name)
            return


async def recover_call(manager: Any, server_id: str, tool_name: str, arguments: dict,
                       exc: BaseException, failed_session: Any,
                       bound_backend: Any = None) -> dict | None:
    """Reconnect and retry once; the call's result, or None to keep the original error.
    After a ``CallTimeout`` it reconnects for the next call but never retries."""
    retry = is_lost_session(exc)
    if manager.is_builtin(server_id) or not (retry or isinstance(exc, CallTimeout)):
        return None
    lock = _LOCKS.setdefault(server_id, asyncio.Lock())
    async with lock:
        session = manager._sessions.get(server_id)
        if session is None or session is failed_session:  # nobody reconnected it yet
            now = time.monotonic()
            if now - _LAST_ATTEMPT.get(server_id, -RETRY_SECONDS) < RETRY_SECONDS:
                return None
            _LAST_ATTEMPT[server_id] = now
            if not await _reconnect(manager, server_id):
                return None
            _LAST_ATTEMPT.pop(server_id, None)
            session = manager._sessions.get(server_id)
    if session is None or bound_backend is not None or not retry:
        return None
    try:
        return await guarded_call(manager, server_id, session, tool_name, arguments)
    except Exception as e:  # noqa: BLE001
        logger.error("MCP tool call failed after reconnect: %s/%s: %s", server_id, tool_name, e)
        return {"error": str(e), "exit_code": 1}
