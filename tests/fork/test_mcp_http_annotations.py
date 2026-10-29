"""Streamable HTTP MCP servers keep their tool annotations (src/mcp_manager.py).

_connect_http used to store only name, description and input_schema, so
readOnlyHint / destructiveHint were lost for HTTP servers while stdio and SSE
servers kept them. These tests drive the real _connect_http with a fake
transport that returns real ``mcp.types.Tool`` objects.
"""
import asyncio
from contextlib import asynccontextmanager

import mcp
import mcp.client.streamable_http
import pytest
from mcp.types import ListToolsResult, Tool, ToolAnnotations

import src.mcp_oauth
from src import mcp_trust
from src.mcp_manager import McpManager, mcp_tool_is_readonly

SCHEMA = {"type": "object", "properties": {}}
TOOLS = [
    # Read-only by annotation, but its name has no read verb.
    Tool(name="transcript_for_day", inputSchema=SCHEMA,
         annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False)),
    # Writes, although its name starts with a read verb.
    Tool(name="list_and_archive", inputSchema=SCHEMA,
         annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)),
]


class _FakeSession:
    def __init__(self, read_stream, write_stream):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        return ListToolsResult(tools=TOOLS)


@asynccontextmanager
async def _fake_streamablehttp_client(url, auth=None):
    yield (object(), object(), lambda: None)


@pytest.fixture
def http_manager(monkeypatch):
    monkeypatch.setattr(mcp.client.streamable_http, "streamablehttp_client",
                        _fake_streamablehttp_client)
    monkeypatch.setattr(mcp, "ClientSession", _FakeSession)
    monkeypatch.setattr(src.mcp_oauth, "build_provider", lambda *a, **k: None)
    mgr = McpManager()
    assert asyncio.run(mgr._connect_http("srv-http", "kb", "http://127.0.0.1:1/mcp"))
    return mgr


def _tool(mgr, name):
    return next(t for t in mgr._tools["srv-http"] if t["name"] == name)


def test_http_connect_keeps_annotations(http_manager):
    ro = _tool(http_manager, "transcript_for_day")
    assert ro["annotations"].readOnlyHint is True
    # Plan-mode gating follows the server's hints, not the name heuristic.
    assert mcp_tool_is_readonly(ro)
    assert not mcp_tool_is_readonly(_tool(http_manager, "list_and_archive"))


def test_http_server_read_only_tool_is_trusted(http_manager, monkeypatch):
    monkeypatch.setenv(mcp_trust.TRUSTED_READONLY_MCP_ENV, "kb")
    trusted = mcp_trust.trusted_readonly_mcp_tools(http_manager)
    assert trusted == frozenset({"mcp__srv-http__transcript_for_day"})
    assert mcp_trust.is_trusted_readonly_mcp_tool("mcp__srv-http__transcript_for_day",
                                                  http_manager)
