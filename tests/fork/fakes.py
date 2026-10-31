"""Fakes shared by the fork's tests. Import as ``from tests.fork.fakes import ...``.

``FakeMcpManager`` stands in for ``src.mcp_manager.McpManager`` where fork code only
reads which servers are connected and which tools they offer: ``_connections`` (server
id -> ``{"status", "name"}``), ``_tools`` (server id -> tool dicts) and
``is_builtin()``. It has no ``call_tool``, ``get_server_status`` or sessions on
purpose: a test that needs calls subclasses it and adds them, so a missing method
still fails loudly instead of answering something plausible.
"""
from __future__ import annotations


class FakeMcpManager:
    def __init__(self, tools: dict | None = None, connections: dict | None = None):
        self._tools = tools if tools is not None else {}
        self._connections = connections if connections is not None else {}

    def is_builtin(self, server_id):
        return False
