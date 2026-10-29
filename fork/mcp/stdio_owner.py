"""Open stdio MCP connections in a task that outlives the caller.

``stdio_client`` and ``ClientSession`` enter AnyIO task groups and cancel scopes.
Their reader/writer tasks live inside the cancel scope of the task that entered
them, and AnyIO requires that same task to exit them.

A server added, enabled or reconnected over the API was entered inside the HTTP
request. Starlette's ``BaseHTTPMiddleware`` (Odysseus has several) cancels its task
group once the response is sent, which also cancels the transport tasks: the call
made inside that request works, and every later call fails with an empty
``ClosedResourceError`` (``MCP tool call failed: <name>:``, ``exit_code=1``).

Built-in servers already avoid this by holding the connection in their own
long-lived task (``McpManager.hold_owned_connection``). This module does the same
for every stdio connection: a dedicated owner task, started with a fresh context
(no request-scoped context variables), connects and then holds the connection
until ``disconnect_server`` signals it. The caller only waits for the result.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any, Dict, List, Set

# asyncio keeps only weak references to tasks; hold the owners strongly.
_OWNER_TASKS: Set[asyncio.Task] = set()


async def connect_stdio_owned(
    manager: Any,
    server_id: str,
    name: str,
    command: str,
    args: List[str],
    env: Dict[str, str],
) -> bool:
    """Connect a stdio server in its own owner task and return whether it connected."""
    current = asyncio.current_task()
    old = manager._owner_tasks.get(server_id)
    if old is not None and not old.done() and old is not current:
        # Connecting again over a live connection would orphan the old owner and
        # its subprocess; close it first, from its own task.
        await manager.disconnect_server(server_id)

    ready: asyncio.Future = asyncio.get_running_loop().create_future()

    async def _own() -> None:
        try:
            ok = await manager._connect_stdio(server_id, name, command, args, env)
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            return
        if not ready.done():
            ready.set_result(ok)
        if ok:
            # Registers this task as the owner before its first await, so a
            # disconnect right after the caller resumes finds it.
            await manager.hold_owned_connection(server_id)

    task = asyncio.create_task(_own(), context=contextvars.Context())
    _OWNER_TASKS.add(task)
    task.add_done_callback(_OWNER_TASKS.discard)
    try:
        return await asyncio.shield(ready)
    except asyncio.CancelledError:
        # The caller gave up (e.g. the 20 s startup timeout) before the server
        # connected: stop connecting. The owner task closes whatever it opened
        # in its own finally blocks. A connection that already succeeded stays.
        if not ready.done():
            task.cancel()
        raise


async def await_existing_owner(manager: Any, server_id: str) -> bool:
    """Wait on the server's owner task if another task already holds it.

    Built-in servers call ``hold_owned_connection`` after ``connect_server``. Their
    connection now already has an owner, so the built-in task just waits until that
    owner closes. Returns False when there is no other owner and the caller should
    hold the connection itself.
    """
    owner = manager._owner_tasks.get(server_id)
    if owner is None or owner.done() or owner is asyncio.current_task():
        return False
    await owner
    return True
