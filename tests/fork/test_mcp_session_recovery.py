"""Lost HTTP MCP sessions reconnect on their own (src/mcp_session_recovery.py).

After an outage, odysseus-sound's MCP server came back with no memory of Odysseus'
session: every call failed with "Session terminated" while the server still showed
"connected", until Reconnect was pressed. While it was down, a call never returned, so
the "Module Action Items" task stayed in flight and voice-note to-dos stopped being filed.
"""

import asyncio
import socket
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

import src.mcp_session_recovery as rec

ECHO_HTTP_SERVER = textwrap.dedent(
    """
    import sys
    from mcp.server.fastmcp import FastMCP
    m = FastMCP("echo", host="127.0.0.1", port=int(sys.argv[1]))

    @m.tool()
    def echo(text: str) -> str:
        return "echo:" + text

    m.run("streamable-http")
    """
)


# ------------------------------------------------------------------ fakes
class FakeManager:
    def __init__(self, *, builtin=False, name="convo"):
        self._sessions = {"convo": object()}
        self._connections = {"convo": {"status": "connected", "name": name}}
        self._stacks, self._connect_tasks, self._resource_connections = {}, {}, {}
        self._tools = {"convo": []}
        self._generation = 0
        self.builtin = builtin
        self.calls, self.connects = [], 0
        self.connect_ok = True
        self.call_delay = 0.0

    def is_builtin(self, server_id):
        return self.builtin

    def get_server_status(self, server_id):
        return self._connections.get(server_id, {"status": "disconnected"})

    async def connect_server(self, server_id, name, transport, **kw):
        self.connects += 1
        if not self.connect_ok:
            self._connections[server_id] = {"status": "error", "name": name}
            return False
        self._sessions[server_id] = object()
        self._connections[server_id] = {"status": "connected", "name": name}
        return True

    async def _do_call(self, session, tool, args):
        self.calls.append((session, tool))
        if self.call_delay:
            await asyncio.sleep(self.call_delay)
        return {"stdout": "ok", "exit_code": 0}


class SessionTerminated(Exception):
    def __init__(self):
        super().__init__("Session terminated")
        self.error = SimpleNamespace(message="Session terminated")


SRV = SimpleNamespace(name="convo", transport="http", command=None, args=None, env=None,
                      url="http://127.0.0.1:1/mcp", is_enabled=True)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    rec._LOCKS.clear()
    rec._LAST_ATTEMPT.clear()
    rec._HEALERS.clear()
    monkeypatch.setattr(rec, "_server_config", lambda server_id: SRV)
    monkeypatch.setenv("ODYSSEUS_TRUSTED_READONLY_MCP", "convo")
    yield
    for task in rec._HEALERS.values():
        task.cancel()


def _reachable(monkeypatch, value=True):
    async def probe(url):
        return value
    monkeypatch.setattr(rec, "_reachable", probe)


# ------------------------------------------------------------------ unit
async def test_lost_session_reconnects_and_retries_once(monkeypatch):
    _reachable(monkeypatch)
    m = FakeManager()
    dead = m._sessions["convo"]
    res = await rec.recover_call(m, "convo", "list_action_items", {}, SessionTerminated(), dead)
    assert res == {"stdout": "ok", "exit_code": 0}
    assert m.connects == 1
    assert m._sessions["convo"] is not dead and m.calls[-1][0] is m._sessions["convo"]


async def test_other_errors_are_left_alone(monkeypatch):
    _reachable(monkeypatch)
    m = FakeManager()
    res = await rec.recover_call(m, "convo", "t", {}, ValueError("bad argument"),
                                 m._sessions["convo"])
    assert res is None and m.connects == 0


async def test_builtin_servers_keep_upstreams_path(monkeypatch):
    _reachable(monkeypatch)
    m = FakeManager(builtin=True)
    res = await rec.recover_call(m, "convo", "t", {}, SessionTerminated(), m._sessions["convo"])
    assert res is None and m.connects == 0


async def test_sealed_turn_reconnects_but_does_not_retry(monkeypatch):
    _reachable(monkeypatch)
    m = FakeManager()
    res = await rec.recover_call(m, "convo", "t", {}, SessionTerminated(),
                                 m._sessions["convo"], bound_backend=object())
    assert res is None and m.connects == 1 and m.calls == []


async def test_second_caller_uses_the_new_session(monkeypatch):
    _reachable(monkeypatch)
    m = FakeManager()
    dead = m._sessions["convo"]
    await asyncio.gather(*(rec.recover_call(m, "convo", "t", {}, SessionTerminated(), dead)
                           for _ in range(3)))
    assert m.connects == 1 and len(m.calls) == 3


async def test_module_call_gets_a_deadline_and_no_retry(monkeypatch):
    _reachable(monkeypatch)
    monkeypatch.setattr(rec, "CALL_TIMEOUT_SECONDS", 0.05)
    m = FakeManager()
    m.call_delay = 1
    with pytest.raises(rec.CallTimeout) as exc:
        await rec.guarded_call(m, "convo", m._sessions["convo"], "t", {})
    m.call_delay = 0
    res = await rec.recover_call(m, "convo", "t", {}, exc.value, m.calls[0][0])
    assert res is None  # the tool may have run: reconnect, never repeat it
    assert m.connects == 1 and len(m.calls) == 1


async def test_other_servers_get_no_deadline(monkeypatch):
    monkeypatch.setattr(rec, "CALL_TIMEOUT_SECONDS", 0.01)
    m = FakeManager(name="someone-elses")
    m._connections["other"] = {"status": "connected", "name": "other"}
    m.call_delay = 0.05
    assert (await rec.guarded_call(m, "other", object(), "t", {}))["exit_code"] == 0


async def test_unreachable_server_heals_when_back(monkeypatch):
    up = {"value": False}

    async def probe(url):
        return up["value"]

    monkeypatch.setattr(rec, "_reachable", probe)
    monkeypatch.setattr(rec, "HEAL_SECONDS", 0.01)
    m = FakeManager()
    res = await rec.recover_call(m, "convo", "t", {}, SessionTerminated(), m._sessions["convo"])
    assert res is None
    assert m.get_server_status("convo")["status"] == "error" and "convo" not in m._sessions
    assert m.connects == 0  # no connect started while nothing answers
    up["value"] = True
    await asyncio.wait_for(rec._HEALERS["convo"], 2)
    assert m.get_server_status("convo")["status"] == "connected"


async def test_healer_stops_when_server_is_disabled(monkeypatch):
    _reachable(monkeypatch, False)
    monkeypatch.setattr(rec, "HEAL_SECONDS", 0.01)
    m = FakeManager()
    await rec.recover_call(m, "convo", "t", {}, SessionTerminated(), m._sessions["convo"])
    monkeypatch.setattr(rec, "_server_config",
                        lambda server_id: SimpleNamespace(**{**vars(SRV), "is_enabled": False}))
    await asyncio.wait_for(rec._HEALERS["convo"], 2)
    assert m.connects == 0


# ------------------------------------------------------------ real transport
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(script, port):
    p = subprocess.Popen([sys.executable, script, str(port)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    pytest.skip("echo MCP server did not start")


def _stop(p):
    p.terminate()
    p.wait(10)


async def test_real_http_server_restart_and_outage(tmp_path, monkeypatch):
    """Restart: the next call works. Down: calls end at the deadline. Back: reconnected."""
    from src.mcp_manager import McpManager

    script = tmp_path / "echo_http.py"
    script.write_text(ECHO_HTTP_SERVER)
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    monkeypatch.setattr(rec, "_server_config", lambda server_id: SimpleNamespace(
        name="convo", transport="http", command=None, args=None, env=None, url=url,
        is_enabled=True))
    monkeypatch.setattr(rec, "CALL_TIMEOUT_SECONDS", 3)
    monkeypatch.setattr(rec, "HEAL_SECONDS", 0.5)
    m = McpManager()
    p = _start(str(script), port)
    try:
        assert await m.connect_server("convo", "convo", "http", url=url)
        assert (await m.call_tool("mcp__convo__echo", {"text": "a"}))["stdout"] == "echo:a"

        _stop(p)
        p = _start(str(script), port)
        res = await m.call_tool("mcp__convo__echo", {"text": "b"})
        assert res.get("stdout") == "echo:b", res

        _stop(p)
        res = await asyncio.wait_for(m.call_tool("mcp__convo__echo", {"text": "c"}), 20)
        assert res["exit_code"] == 1 and "no answer" in res["error"]
        assert m.get_server_status("convo")["status"] == "error"

        p = _start(str(script), port)
        for _ in range(100):
            if m.get_server_status("convo")["status"] == "connected":
                break
            await asyncio.sleep(0.1)
        res = await m.call_tool("mcp__convo__echo", {"text": "d"})
        assert res.get("stdout") == "echo:d", res
    finally:
        _stop(p)
        for task in list(rec._HEALERS.values()):
            task.cancel()
