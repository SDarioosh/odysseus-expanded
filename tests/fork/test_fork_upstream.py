"""``fork/upstream.py``: every upstream private name the fork reads still exists, and no
other fork file reaches one directly.

When an upstream sync renames or drops one of these names, this file fails and names it:
fix the function in ``fork/upstream.py`` (one place), not the callers.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fork import upstream

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_PACKAGES = {"src", "routes", "core", "services", "mcp_servers", "app"}
# upstream private attributes the fork reads on objects (not by import)
UPSTREAM_PRIVATE_ATTRS = {"_connections", "_tools", "_memory_vector", "_lanes",
                          "_has_active_chat_stream"}
# hook bodies inside McpManager methods: they use its state directly (upstream.py says why)
MCP_HOOK_BODIES = {"fork/mcp/session_recovery.py", "fork/mcp/stdio_owner.py"}


@pytest.mark.parametrize("key", sorted(upstream.PRIVATE_NAMES))
def test_each_private_name_still_exists_upstream(key):
    module, name = upstream.PRIVATE_NAMES[key]
    assert hasattr(importlib.import_module(module), name), (
        f"upstream {module}.{name} is gone: update fork/upstream.py ({key.split(':')[0]})")


def test_every_wrapper_is_listed():
    listed = {key.split(":")[0] for key in upstream.PRIVATE_NAMES}
    source = inspect.getsource(upstream)
    for name, fn in vars(upstream).items():
        if inspect.isfunction(fn) and fn.__module__ == upstream.__name__:
            body = inspect.getsource(fn)
            if "import _" in body or "import (\n        _" in body:
                assert name in listed, name
    assert "sys.modules" in source  # memory_vector never imports src.ai_interaction


def test_mcp_manager_still_has_the_state_the_fork_uses():
    from src.mcp_manager import McpManager

    manager = McpManager()
    for name in upstream.MCP_MANAGER_INTERNALS:
        assert hasattr(manager, name), f"McpManager.{name} is gone (fork/mcp/, fork/upstream.py)"
    assert isinstance(manager._connections, dict) and isinstance(manager._tools, dict)


def test_memory_vector_store_still_has_lanes():
    from src.memory_vector import MemoryVectorStore

    assert "self._lanes" in inspect.getsource(MemoryVectorStore)


def _private_reads(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and (
                node.module.split(".")[0] in UPSTREAM_PACKAGES):
            found += [f"{node.module}.{a.name}" for a in node.names
                      if a.name.startswith("_") and not a.name.startswith("__")]
        elif isinstance(node, ast.Attribute) and node.attr in UPSTREAM_PRIVATE_ATTRS:
            found.append(node.attr)
        elif (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr"
              and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
              and node.args[1].value in UPSTREAM_PRIVATE_ATTRS):
            found.append(node.args[1].value)
    return found


def test_no_other_fork_file_reads_upstream_private_names():
    offenders = {}
    for path in sorted((ROOT / "fork").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "fork/upstream.py" or rel in MCP_HOOK_BODIES:
            continue
        if found := _private_reads(path):
            offenders[rel] = found
    assert offenders == {}


def test_mcp_state_reads_tolerate_a_bare_manager():
    assert upstream.mcp_connections(object()) == {}
    assert upstream.mcp_tools(SimpleNamespace(_tools=None)) == {}
    m = SimpleNamespace(_connections={"s": {"status": "connected"}}, _tools={"s": []})
    assert upstream.mcp_connections(m) is m._connections
    assert upstream.mcp_tools(m) is m._tools


def test_memory_vector_only_when_loaded(monkeypatch):
    monkeypatch.delitem(sys.modules, "src.ai_interaction", raising=False)
    assert upstream.memory_vector() is None
    assert "src.ai_interaction" not in sys.modules
    store = SimpleNamespace(healthy=True, _lanes=["lane"])
    monkeypatch.setitem(sys.modules, "src.ai_interaction", SimpleNamespace(_memory_vector=store))
    assert upstream.memory_vector() is store
    assert upstream.vector_lanes(store) == ["lane"]
    assert upstream.vector_lanes(None) == []


def test_chat_stream_active_follows_upstream(monkeypatch):
    from src import interactive_gate

    monkeypatch.setattr(interactive_gate, "_has_active_chat_stream", lambda: 1)
    assert upstream.chat_stream_active() is True
    monkeypatch.delattr(interactive_gate, "_has_active_chat_stream")
    assert upstream.chat_stream_active() is False


def test_is_local_or_private_host():
    assert upstream.is_local_or_private_host("localhost")
    assert upstream.is_local_or_private_host("192.168.1.20")
    assert upstream.is_local_or_private_host("100.64.0.7")
    assert not upstream.is_local_or_private_host("example.com")
