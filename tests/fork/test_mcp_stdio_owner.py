"""Stdio MCP connections outlive the request that opened them (src/mcp_stdio_owner.py).

Sandbox finding 2 (docs/fork/sandbox.md): a stdio server added over the API
answered inside that request, then failed every later call with an empty error,
because BaseHTTPMiddleware cancelled the transport with the request.
"""

import asyncio
import sys
import textwrap

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

ECHO_SERVER = textwrap.dedent(
    """
    from mcp.server.fastmcp import FastMCP
    m = FastMCP("echo")

    @m.tool()
    def echo(text: str) -> str:
        return "echo:" + text

    m.run()
    """
)


@pytest.fixture
def echo_script(tmp_path):
    path = tmp_path / "echo_server.py"
    path.write_text(ECHO_SERVER)
    return str(path)


async def _connect(manager, echo_script, server_id="echo1"):
    return await manager.connect_server(
        server_id, "echo", "stdio", command=sys.executable, args=[echo_script]
    )


@pytest.mark.asyncio
async def test_stdio_server_added_in_a_request_keeps_working(echo_script):
    from src.mcp_manager import McpManager

    manager = McpManager()
    app = FastAPI()

    class PassThrough(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            return await call_next(request)

    app.add_middleware(PassThrough)

    @app.post("/add")
    async def add():
        ok = await _connect(manager, echo_script)
        return {"ok": ok, "r": await manager.call_tool("mcp__echo1__echo", {"text": "now"})}

    @app.post("/call")
    async def call():
        return await manager.call_tool("mcp__echo1__echo", {"text": "later"})

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            added = (await client.post("/add")).json()
            assert added["ok"] is True
            assert added["r"]["stdout"] == "echo:now"
            await asyncio.sleep(0.2)
            for _ in range(2):
                later = (await client.post("/call")).json()
                assert later == {"stdout": "echo:later", "stderr": "", "exit_code": 0}
    finally:
        await manager.disconnect_all()


@pytest.mark.asyncio
async def test_disconnect_from_another_task_closes_the_owner(echo_script):
    from src import mcp_stdio_owner
    from src.mcp_manager import McpManager

    manager = McpManager()
    assert await asyncio.create_task(_connect(manager, echo_script)) is True
    owner = manager._owner_tasks["echo1"]
    assert owner in mcp_stdio_owner._OWNER_TASKS

    await manager.disconnect_server("echo1")

    assert owner.done() and not owner.cancelled() and owner.exception() is None
    assert "echo1" not in manager._stacks and "echo1" not in manager._owner_tasks
    result = await manager.call_tool("mcp__echo1__echo", {"text": "x"})
    assert result["error"] == "MCP server not connected: echo1"


@pytest.mark.asyncio
async def test_builtin_style_hold_waits_on_the_existing_owner(echo_script):
    """register_builtin_servers calls hold_owned_connection after connect_server."""
    from src.mcp_manager import McpManager

    manager = McpManager()

    async def builtin_holder():
        assert await _connect(manager, echo_script) is True
        await manager.hold_owned_connection("echo1")

    holder = asyncio.create_task(builtin_holder())
    for _ in range(200):
        if "echo1" in manager._owner_tasks:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.1)  # let the holder reach hold_owned_connection
    assert not holder.done()
    assert manager._owner_tasks["echo1"] is not holder
    result = await manager.call_tool("mcp__echo1__echo", {"text": "b"})
    assert result["stdout"] == "echo:b"

    await manager.disconnect_server("echo1")
    await asyncio.wait_for(holder, 5)
    assert holder.exception() is None


@pytest.mark.asyncio
async def test_connecting_again_replaces_the_live_connection(echo_script):
    from src.mcp_manager import McpManager

    manager = McpManager()
    try:
        assert await _connect(manager, echo_script) is True
        first = manager._owner_tasks["echo1"]
        assert await _connect(manager, echo_script) is True
        assert first.done() and first.exception() is None
        assert manager._owner_tasks["echo1"] is not first
        result = await manager.call_tool("mcp__echo1__echo", {"text": "again"})
        assert result["stdout"] == "echo:again"
    finally:
        await manager.disconnect_all()


@pytest.mark.asyncio
async def test_cancelled_connect_stops_the_owner(monkeypatch):
    from src import mcp_stdio_owner
    from src.mcp_manager import McpManager

    manager = McpManager()
    started = asyncio.Event()

    async def slow_connect(*_args):
        started.set()
        await asyncio.sleep(30)
        return True

    monkeypatch.setattr(manager, "_connect_stdio", slow_connect)
    waiter = asyncio.create_task(
        mcp_stdio_owner.connect_stdio_owned(manager, "slow", "slow", "cmd", [], {})
    )
    await started.wait()
    (owner,) = mcp_stdio_owner._OWNER_TASKS
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0)
    assert owner.cancelled()
    assert "slow" not in manager._owner_tasks


@pytest.mark.asyncio
async def test_connect_error_reaches_the_caller(monkeypatch):
    from src.mcp_manager import McpManager

    manager = McpManager()

    async def broken_connect(*_args):
        raise FileNotFoundError("no such command: nope")

    monkeypatch.setattr(manager, "_connect_stdio", broken_connect)
    ok = await manager.connect_server("bad", "bad", "stdio", command="nope")
    assert ok is False
    assert manager.get_server_status("bad")["status"] == "error"
    assert "bad" not in manager._owner_tasks
