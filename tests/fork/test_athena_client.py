"""Fork: the one Athena client (fork/athena/client.py).

Athena's own contract snapshots (``tests/fixtures/athena_contract/``, fictional seed data)
are the answers: a tool call through the hub's MCP manager hands back the module's text
unchanged, and the hub's parsers read it; an HTTP call to the relay routes sends the
token only as ``Authorization: Bearer`` and returns the module's JSON as it came. What
every caller does with the client is pinned in ``test_athena_callers.py``.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import httpx
import pytest

from fork.athena import call_recorder as rec
from fork.athena import client as athena
from fork.athena import facts as mf
from fork.athena import meeting_bot as mb
from fork.athena import view
from fork.athena.action_items import parse_items_json
from tests.fork.fakes import FakeMcpManager

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "athena_contract"
NOW = datetime.datetime(2026, 10, 20, 9, 0, tzinfo=datetime.UTC)
TOKEN = "tok-FICTIONAL"
ENV = ("MODULE_ACTION_ITEMS_SERVER", "BARVIS_ACTION_ITEMS_SERVER", "SOUND_MODULE_SERVER",
       "BARVIS_SOUND_SERVER")


def _compact(name: str) -> str:
    return json.dumps(json.loads((FIXTURES / name).read_text(encoding="utf-8")),
                      separators=(",", ":"), ensure_ascii=False)


class Athena(FakeMcpManager):
    """The MCP manager with Athena connected as ``srv-1`` ("Convo"), answering its
    contract snapshots."""

    def __init__(self, answer=None, status="connected"):
        super().__init__(connections={"srv-1": {"status": status, "name": "Convo"},
                                      "srv-2": {"status": "connected", "name": "other"}})
        self.answer = answer
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.answer is not None:
            return self.answer
        tool = name.rsplit("__", 1)[1]
        if tool == "list_people":
            return {"stdout": _compact("list_people.json")}
        if tool == "list_facts":
            return {"stdout": _compact("list_facts_confirmed.json" if arguments.get("confirmed")
                                       else "list_facts.json")}
        if tool == "list_action_items":
            return {"stdout": _compact("list_action_items_status_all.json"
                                       if arguments.get("status") == "all"
                                       else "list_action_items.json")}
        return {"error": f"Unknown tool: {tool}"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    manager = Athena()
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: manager)
    return manager


# ------------------------------------------------------------------------- MCP
async def test_tool_hands_back_the_module_text_and_the_parsers_read_it(env):
    client = athena.AthenaClient()
    facts = await client.tool("list_facts", {"format": "json"})
    assert facts == _compact("list_facts.json")
    assert len(mf.parse_facts_json(facts)) == len(json.loads(facts)["facts"])
    people = mf.parse_people_json(await client.tool("list_people", {"format": "json"}))
    assert sum(bool(p["is_owner"]) for p in people) == 1
    items = await client.tool("list_action_items", {"status": "all", "format": "json"})
    assert parse_items_json(items) == json.loads(items)
    assert view.parse_action_items_result(items)
    assert [name for name, _ in env.calls] == ["mcp__srv-1__list_facts",
                                               "mcp__srv-1__list_people",
                                               "mcp__srv-1__list_action_items"]


async def test_facts_sync_runs_on_the_client_caller(data_dir, monkeypatch):
    monkeypatch.setattr(mf, "_LOCKS", {})
    call = athena.AthenaClient().caller()
    stats = await mf.sync("alice", call, now=NOW)
    assert stats["facts"] == len(json.loads(_compact("list_facts.json"))["facts"])
    assert stats["people"] == len(json.loads(_compact("list_people.json"))["people"])


async def test_a_failed_tool_is_an_athena_error_and_a_runtime_error(env):
    env.answer = {"exit_code": 1, "stderr": "bad args"}
    with pytest.raises(RuntimeError) as e:
        await athena.AthenaClient().tool("list_facts", {})
    err = e.value
    assert isinstance(err, athena.AthenaError)
    assert (str(err), err.status, err.kind, err.tool, err.reason) == (
        "list_facts: bad args", 502, "tool", "list_facts", "bad args")
    env.answer = {"exit_code": 2}
    with pytest.raises(athena.AthenaError, match="^get_transcript: failed$") as e:
        await athena.AthenaClient().tool("get_transcript", {})
    assert e.value.reason is None


async def test_other_failures_of_the_manager_go_up_as_they_are(env):
    env.answer = None

    async def slow(name, arguments):
        raise TimeoutError

    env.call_tool = slow
    with pytest.raises(TimeoutError):
        await athena.AthenaClient().tool("list_facts", {})


async def test_not_connected(env, monkeypatch):
    env._connections["srv-1"]["status"] = "error"
    assert athena.AthenaClient().caller() is None
    with pytest.raises(athena.AthenaError) as e:
        await athena.AthenaClient().tool("list_facts", {})
    assert (e.value.status, e.value.kind, e.value.message) == (
        503, "not_connected", "The Athena MCP server ('convo') is not connected.")
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: None)
    assert athena.AthenaClient(recorder=True).caller() is None


@pytest.mark.parametrize(("env_vars", "recorder", "server"), [
    ({}, False, "srv-1"),
    ({}, True, "srv-1"),
    ({"MODULE_ACTION_ITEMS_SERVER": " OTHER "}, False, "srv-2"),
    ({"MODULE_ACTION_ITEMS_SERVER": " OTHER "}, True, "srv-1"),
    ({"SOUND_MODULE_SERVER": "SRV-2"}, True, "srv-2"),
    ({"SOUND_MODULE_SERVER": "SRV-2"}, False, "srv-1"),
    ({"BARVIS_ACTION_ITEMS_SERVER": "other"}, False, "srv-2"),
    ({"BARVIS_SOUND_SERVER": "other"}, True, "srv-2"),
])
def test_each_caller_keeps_its_own_server_setting(monkeypatch, env_vars, recorder, server):
    for name, value in env_vars.items():
        monkeypatch.setenv(name, value)
    assert athena.AthenaClient(recorder=recorder).server_id() == server
    assert athena.AthenaClient(server="Other").server_id() == "srv-2"


# ------------------------------------------------------------------------ HTTP
ROW = ("srv-1", "Convo", f"http://module:8001/base/mcp/?access_token={TOKEN}", True)


@pytest.fixture
def module(monkeypatch):
    sent = []
    answer = {"fn": lambda request: httpx.Response(
        200, content=(FIXTURES / "list_action_items.json").read_bytes(),
        headers={"content-type": "application/json"})}

    def handler(request):
        sent.append(request)
        return answer["fn"](request)

    real = athena._client
    monkeypatch.setattr(athena, "_server_rows", lambda: [ROW])
    monkeypatch.setattr(athena, "_client", lambda transport, timeout=athena.TIMEOUT: real(
        httpx.MockTransport(handler), timeout))
    return sent, answer


async def test_http_sends_the_token_as_a_header_and_returns_the_answer(module):
    sent, _ = module
    res = await athena.AthenaClient(recorder=True).http("GET", "/action-items",
                                                         params={"status": "open"})
    assert res.json() == json.loads((FIXTURES / "list_action_items.json").read_text())
    (req,) = sent
    assert str(req.url) == "http://module:8001/base/action-items?status=open"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(req.url)


@pytest.mark.parametrize(("exc", "timed_out", "status", "message", "kind"), [
    (httpx.ConnectError, None, 502, "Athena is not reachable", "unreachable"),
    (httpx.ReadTimeout, None, 502, "Athena is not reachable", "unreachable"),
    (httpx.ReadTimeout, (504, "later"), 504, "later", "timeout"),
    (httpx.ConnectError, (504, "later"), 502, "Athena is not reachable", "unreachable"),
])
async def test_no_answer_is_an_athena_error(module, caplog, exc, timed_out, status, message,
                                            kind):
    _, answer = module

    def fail(request):
        raise exc("fake", request=request)

    answer["fn"] = fail
    with pytest.raises(athena.AthenaError) as e:
        await athena.AthenaClient(recorder=True).http("GET", "/health", timed_out=timed_out)
    assert (e.value.status, e.value.message, e.value.kind) == (status, message, kind)
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(("rows", "words"), [
    ([], "No MCP server named 'convo'"),
    ([ROW[:3] + (False,)], "turned off"),
    ([("srv-1", "convo", "http://module:8001/mcp", True)], "access_token"),
])
def test_link_says_what_to_fix(monkeypatch, rows, words):
    monkeypatch.setattr(athena, "_server_rows", lambda: rows)
    with pytest.raises(athena.AthenaError, match=words) as e:
        athena.AthenaClient(recorder=True).link()
    assert (e.value.status, e.value.kind) == (503, "setup")


def test_the_old_error_names_are_the_one_error():
    assert rec.RecorderError is athena.AthenaError
    assert issubclass(mb.MeetingError, athena.AthenaError)
    err = mb.MeetingError(409, "busy", "busy", active="x")
    assert (err.status, err.message, err.code, err.kind, err.active) == (
        409, "busy", "busy", "busy", "x")
    assert rec.Link is athena.Link and repr(athena.Link("http://h", TOKEN)) == \
        "Link(base='http://h')"
