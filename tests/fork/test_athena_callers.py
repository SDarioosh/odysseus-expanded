"""Fork: what every Athena caller of the hub sends and makes of each answer, pinned.

Each caller that reaches Athena (MCP tool calls through the hub's MCP manager, HTTP calls
to the relay routes of Athena's MCP HTTP server) is run against a table of answers: for
MCP, no manager, no connected server, a server picked by id or name, and tool results
that are text, empty, an error, a non-zero exit, a timeout or another exception; for
HTTP, each server row Settings → MCP can hold, and status codes with and without a
``detail``, non-JSON bodies, a timeout and a refused connection. What each caller sent
(tool and arguments; method, URL, headers, body, timeout) and what it returned, raised or
logged is compared with ``fixtures/athena_callers.json``, recorded from the callers as
they were before they shared one Athena client (``fork/athena/client.py``). The callers
must keep doing exactly that.

The fakes sit at the edges every version of the callers goes through:
``src.tool_utils.get_mcp_manager``, ``core.database.SessionLocal`` (the MCP server rows)
and ``httpx.AsyncClient``. Log lines are pinned without the logger's name. Fictional
data only; the token is a placeholder.

Run this file as a script to record the fixture (only on the callers as they were).
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

FIXTURE = Path(__file__).with_name("fixtures") / "athena_callers.json"
TOKEN = "tok-FICTIONAL"
MID = "0123456789abcdef0123456789abcdef"
NOW = datetime.fromisoformat("2026-10-20T09:00:00+00:00")
ENV = ("MODULE_ACTION_ITEMS_SERVER", "BARVIS_ACTION_ITEMS_SERVER", "SOUND_MODULE_SERVER",
       "BARVIS_SOUND_SERVER", "SOUND_MODULE_CALL_DEVICE")


# ------------------------------------------------------------------------ MCP fakes
class CallManager:
    """``McpManager`` as the callers use it: ``_connections`` and ``call_tool``."""

    def __init__(self, connections: dict, answer: Any):
        self._connections = connections
        self._tools: dict = {}
        self.answer = answer
        self.calls: list = []

    def is_builtin(self, server_id):
        return False

    async def call_tool(self, name, arguments):
        self.calls.append([name, arguments])
        if isinstance(self.answer, BaseException):
            raise self.answer
        return dict(self.answer)


CONNECTED = {"srv-1": {"status": "connected", "name": "Convo"},
             "srv-2": {"status": "connected", "name": "other"},
             "srv-3": {"status": "error", "name": "third"}}
MCP_CASES: dict[str, tuple[Any, Any, dict]] = {
    # name: (connections or "none"/"raises", call_tool answer, environment)
    "no_manager": ("none", {"stdout": "x"}, {}),
    "manager_raises": ("raises", {"stdout": "x"}, {}),
    "not_connected": ({"srv-1": {"status": "error", "name": "convo"}}, {"stdout": "x"}, {}),
    "text": (CONNECTED, {"stdout": "Hello from Athena"}, {}),
    "empty_stdout": (CONNECTED, {"stdout": ""}, {}),
    "no_stdout": (CONNECTED, {}, {}),
    "error": (CONNECTED, {"error": "boom"}, {}),
    "unknown_tool": (CONNECTED, {"error": "Unknown tool: retire_fact"}, {}),
    "exit_stderr": (CONNECTED, {"exit_code": 1, "stderr": "bad args"}, {}),
    "exit_bare": (CONNECTED, {"exit_code": 2}, {}),
    "error_and_stdout": (CONNECTED, {"error": "half", "stdout": "partial"}, {}),
    "timeout": (CONNECTED, TimeoutError(), {}),
    "exception": (CONNECTED, ConnectionError("pipe closed"), {}),
    "env_other": (CONNECTED, {"stdout": "x"}, {"MODULE_ACTION_ITEMS_SERVER": " OTHER ",
                                                "SOUND_MODULE_SERVER": "OTHER"}),
    "env_id": (CONNECTED, {"stdout": "x"}, {"MODULE_ACTION_ITEMS_SERVER": "SRV-2",
                                             "SOUND_MODULE_SERVER": "srv-2"}),
    "env_old_names": (CONNECTED, {"stdout": "x"}, {"BARVIS_ACTION_ITEMS_SERVER": "other",
                                                    "BARVIS_SOUND_SERVER": "other"}),
    "env_down_server": (CONNECTED, {"stdout": "x"}, {"MODULE_ACTION_ITEMS_SERVER": "third",
                                                      "SOUND_MODULE_SERVER": "third"}),
    "env_missing": (CONNECTED, {"stdout": "x"}, {"MODULE_ACTION_ITEMS_SERVER": "nope",
                                                  "SOUND_MODULE_SERVER": "nope"}),
}


# ----------------------------------------------------------------------- HTTP fakes
class Rows:
    """``core.database.SessionLocal`` answering ``query(McpServer).all()`` with rows."""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        return self

    def query(self, model):
        return self

    def all(self):
        return [type("Row", (), {"id": i, "name": n, "url": u, "is_enabled": e})()
                for i, n, u, e in self.rows]

    def close(self):
        pass


GOOD_ROWS = [("srv-1", "Convo", f"http://module:8001/mcp?access_token={TOKEN}", True)]
ROW_CASES: dict[str, tuple[list, dict]] = {
    "good": (GOOD_ROWS, {}),
    "no_rows": ([], {}),
    "disabled": ([("srv-1", "convo", f"http://module:8001/mcp?access_token={TOKEN}", False)],
                 {}),
    "enabled_first": ([("a", "convo", "http://old:1/mcp?access_token=old", False),
                       ("b", " CONVO ", f"https://new.example:8443/x/mcp/?access_token={TOKEN}",
                        True)], {}),
    "no_token": ([("srv-1", "convo", "http://module:8001/mcp", True)], {}),
    "blank_token": ([("srv-1", "convo", "http://module:8001/mcp?access_token=%20", True)], {}),
    "ftp": ([("srv-1", "convo", f"ftp://module/mcp?access_token={TOKEN}", True)], {}),
    "no_url": ([("srv-1", "convo", None, True)], {}),
    "by_id": ([("Convo-ID", "something", f"http://m:2/mcp?access_token={TOKEN}", True)],
              {"SOUND_MODULE_SERVER": "convo-id"}),
    "env_name": ([("srv-9", "Athena", f"http://m:3/mcp?access_token={TOKEN}", True)],
                 {"SOUND_MODULE_SERVER": " athena "}),
    "old_env": ([("srv-9", "Athena", f"http://m:3/mcp?access_token={TOKEN}", True)],
                {"BARVIS_SOUND_SERVER": "Athena"}),
    "facts_env_ignored": (GOOD_ROWS, {"MODULE_ACTION_ITEMS_SERVER": "other"}),
}

OK_BODY = {"ok": True, "stop": False, "reason": None, "id": MID, "state": "recording",
           "session_id": "zoom-1", "meetings": [{"id": "x", "state": "ended"},
                                                {"id": MID, "state": "recording"}],
           "configured": True, "signed_in": True, "join_token": "obf", "missing": ["ZOOM_X"],
           "url": "https://zoom.us/oauth/authorize?state=s", "expires_in": 300,
           "result": "deleted", "model": "m", "words": "a, b", "conversations": []}


def _json(status, body):
    return lambda request: httpx.Response(status, json=body)


def _text(status, text, ctype="text/plain"):
    return lambda request: httpx.Response(status, text=text, headers={"content-type": ctype})


def _raise(exc):
    def handler(request):
        raise exc("fake", request=request)
    return handler


HTTP_CASES: dict[str, Any] = {
    "200": _json(200, OK_BODY),
    "200_list": _json(200, [1, 2]),
    "200_stop": _json(200, {"stop": True, "reason": "silence", "duplicate": True}),
    "200_empty": _json(200, {}),
    "201": _json(201, OK_BODY),
    "202": _json(202, OK_BODY),
    "200_text": _text(200, "hello"),
    "200_file": lambda request: httpx.Response(200, content=b"# Title\n", headers={
        "content-type": "text/markdown; charset=utf-8",
        "content-disposition": "attachment; filename*=UTF-8''Team%20sync.md"}),
    "400": _json(400, {"detail": "bad thing"}),
    "400_bare": _json(400, {}),
    "401": _json(401, {"detail": "Unauthorized"}),
    "403": _text(403, "Forbidden"),
    "404_text": _text(404, "Not Found"),
    "404_not_found": _json(404, {"detail": "Not Found"}),
    "404_session": _json(404, {"detail": "session not found"}),
    "404_conversation": _json(404, {"detail": "conversation not found"}),
    "405": _json(405, {"detail": "Method Not Allowed"}),
    "409": _json(409, {"detail": "busy", "active": MID}),
    "409_bad_active": _json(409, {"detail": "busy", "active": "../x"}),
    "413": _json(413, {"detail": "too big"}),
    "415": _json(415, {"detail": "unsupported"}),
    "422": _json(422, {"detail": "unknown model"}),
    "422_list": _json(422, {"detail": [{"msg": "field required"}, "plain"]}),
    "500": _json(500, {"detail": "crash"}),
    "500_text": _text(500, "Internal Server Error"),
    "501": _json(501, {"detail": "ZOOM_SDK_KEY"}),
    "502_detail": _json(502, {"detail": "zoom_unavailable"}),
    "502_ingest": _json(502, {"detail": "ingest API down"}),
    "502_text": _text(502, "Bad Gateway"),
    "503_signin": _json(503, {"detail": "zoom_signin_needed"}),
    "503": _json(503, {"detail": "starting"}),
    "read_timeout": _raise(httpx.ReadTimeout),
    "connect_timeout": _raise(httpx.ConnectTimeout),
    "refused": _raise(httpx.ConnectError),
    "protocol": _raise(httpx.RemoteProtocolError),
}


class Wire:
    """Everything the callers send over HTTP, and the answer each request gets."""

    def __init__(self):
        self.handler = HTTP_CASES["200"]
        self.sent: list[dict] = []
        self.real = httpx.AsyncClient

    def client(self, *args, **kwargs):
        seen = {k: (str(v) if k == "timeout" else v) for k, v in sorted(kwargs.items())
                if k != "transport"}
        self.sent.append({"client": seen, "args": len(args)})
        kwargs["transport"] = httpx.MockTransport(self.record)
        return self.real(*args, **kwargs)

    def record(self, request: httpx.Request) -> httpx.Response:
        body: Any = None
        ctype = request.headers.get("content-type", "")
        if request.content:
            if ctype.startswith("application/json"):
                body = json.loads(request.content)
            elif ctype.startswith("multipart/form-data"):
                body = _multipart(request.content, ctype)
            else:
                body = request.content.decode("utf-8", "replace")
        self.sent.append({
            "method": request.method,
            "url": str(request.url.copy_with(query=None)),
            "query": parse_qsl(request.url.query.decode() if isinstance(
                request.url.query, bytes) else request.url.query, keep_blank_values=True),
            "headers": {k: v for k, v in sorted(request.headers.items())
                        if k in ("authorization", "accept") or (k == "content-type"
                                                               and "boundary" not in v)},
            "content_type": ctype.split(";")[0],
            "body": body,
        })
        return self.handler(request)


def _multipart(content: bytes, ctype: str) -> list:
    boundary = ctype.split("boundary=")[1].encode()
    parts = []
    for raw in content.split(b"--" + boundary):
        raw = raw.strip(b"\r\n")
        if not raw or raw == b"--":
            continue
        head, _, value = raw.partition(b"\r\n\r\n")
        parts.append([head.decode().replace("\r\n", " | "), value.decode("utf-8", "replace")])
    return parts


# ------------------------------------------------------------------------ helpers
class Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        if record.name.startswith(("fork", "src.module", "src.sound", "src.call",
                                   "src.meeting", "src.record", "src.ink", "routes.")):
            self.lines.append(f"{record.levelname} {record.getMessage()}")


def _error(exc: BaseException, catches: tuple) -> dict:
    """What a caller's caller sees of an exception: the first class of ``catches`` it is
    (the ones that caller catches), its text and the attributes it reads."""
    label = next((name for name, cls in catches if isinstance(exc, cls)),
                 type(exc).__name__)
    out: dict[str, Any] = {"raises": label, "text": str(exc)}
    for attr in ("status", "message", "code", "active", "status_code", "detail"):
        if label != "RuntimeError" and hasattr(exc, attr):
            out[attr] = getattr(exc, attr)
    return out


def _jsonable(value):
    if isinstance(value, (bytes, bytearray)):
        return {"bytes": value.decode("utf-8", "replace")}
    if hasattr(value, "__dataclass_fields__"):
        import dataclasses

        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _run(make, catches: tuple) -> dict:
    try:
        result = make()
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        return {"result": _jsonable(result)}
    except BaseException as exc:  # every outcome is pinned
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return _error(exc, catches)


class Env:
    def __init__(self, monkeypatch):
        self.mp = monkeypatch

    def set(self, values: dict):
        for name in ENV:
            self.mp.delenv(name, raising=False)
        for name, value in values.items():
            self.mp.setenv(name, value)


# --------------------------------------------------------------------- MCP callers
def _mcp_callers(mp, tmp: Path) -> dict:
    """label -> (callable, exceptions its callers catch)."""
    from fastapi import HTTPException

    from fork.athena import action_items as mai
    from fork.athena import facts as mf
    from fork.athena import routes as athena_routes
    from fork.insight import today
    from fork.routes import beliefs_routes as br
    from src.builtin_actions import TaskNoop

    catches = (("HTTPException", HTTPException), ("TaskNoop", TaskNoop),
               ("RuntimeError", RuntimeError))

    async def fake_items_sync(owner, call, **kw):
        out = await call("list_action_items", {"status": "all", "format": "json"})
        return {"new": len(out), "pushed": 0}

    async def fake_facts_sync(owner, call, **kw):
        out = await call("list_facts", {"confirmed": True, "format": "json"})
        return {"answer": out} if out else None

    async def no_sweep():
        return None

    mp.setattr(mai, "sync", fake_items_sync)
    mp.setattr(mf, "sync", fake_facts_sync)
    mp.setattr(mf, "sweep_handwritten", no_sweep)
    mp.setattr(mf, "enabled", lambda: True)
    mp.setattr(mf, "changed", lambda stats: bool(stats))
    mp.setattr(mf, "summary", lambda stats: f"summary {sorted(stats)}")
    mp.setattr(br.mf, "enabled", lambda: True)

    async def via(get_call, tool, args):
        call = get_call()
        if call is None:
            return {"caller": None}
        return {"answer": await call(tool, args)}

    async def run_facts():
        res = await br.run_facts("ana")
        return {"status": res.status_code, "body": json.loads(res.body)}

    belief = {"evidence": [{"source": mf.SOURCE, "source_id": "7"},
                           {"source": "other", "source_id": "8"},
                           {"source": mf.SOURCE, "source_id": "9"}]}

    async def forget():
        stats = await mf.propagate_forget("ana", belief)
        state = mf.load_state("ana")
        return {"stats": stats, "no_retire_fact": state.get("no_retire_fact")}

    return {
        "action_items.action_sync_action_items": (
            lambda: mai.action_sync_action_items("ana"), catches),
        "facts.action_sync_facts": (lambda: mf.action_sync_facts("ana"), catches),
        "facts.propagate_forget": (forget, catches),
        "today._module_caller": (
            lambda: via(today._module_caller, "get_daily_digest", {"date": "2026-10-20"}),
            catches),
        "beliefs_routes.module_caller": (
            lambda: via(br.module_caller, "retire_fact", {"fact_id": 7, "reason": "r"}),
            catches),
        "beliefs_routes.run_facts": (run_facts, catches),
        "athena_routes._caller": (
            lambda: via(athena_routes._caller, "get_conversation",
                        {"conversation_id": "c-1"}), catches),
        "action_items.module_server_id": (
            lambda: {"default": mai.module_server_id(_manager()),
                     "OTHER": mai.module_server_id(_manager(), "OTHER"),
                     "srv-3": mai.module_server_id(_manager(), "srv-3"),
                     "empty": mai.module_server_id(_manager(), "")}, catches),
    }


_CURRENT: dict[str, Any] = {}


def _manager():
    return _CURRENT.get("manager")


def observe_mcp(monkeypatch) -> dict:
    import src.constants
    import src.tool_utils

    env = Env(monkeypatch)
    out: dict[str, dict] = {}
    logs = Logs()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(logs)
    root.setLevel(logging.DEBUG)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            callers = _mcp_callers(monkeypatch, Path(tmp))
            for case, (connections, answer, values) in MCP_CASES.items():
                env.set(values)
                for label, (make, catches) in callers.items():
                    data = Path(tmp) / f"{case}-{label}"
                    data.mkdir()
                    monkeypatch.setattr(src.constants, "DATA_DIR", str(data))
                    if connections == "none":
                        manager = None
                        monkeypatch.setattr(src.tool_utils, "get_mcp_manager", lambda: None)
                    elif connections == "raises":
                        manager = None

                        def broken():
                            raise RuntimeError("manager not ready")
                        monkeypatch.setattr(src.tool_utils, "get_mcp_manager", broken)
                    else:
                        manager = CallManager(json.loads(json.dumps(connections)), answer)
                        monkeypatch.setattr(src.tool_utils, "get_mcp_manager",
                                            lambda m=manager: m)
                    _CURRENT["manager"] = manager
                    logs.lines.clear()
                    row = _run(make, catches)
                    row["calls"] = manager.calls if manager is not None else []
                    if logs.lines:
                        row["logs"] = list(logs.lines)
                    out[f"{case} {label}"] = row
        env.set({})
        return out
    finally:
        _CURRENT.clear()
        root.removeHandler(logs)
        root.setLevel(old_level)


# --------------------------------------------------------------------- HTTP callers
def _app():
    from fastapi import FastAPI

    from core.middleware import require_admin
    from fork.athena import relay_routes, summary_model_routes, voice_enroll_routes
    from fork.athena import whisper_vocabulary_routes as vocab

    app = FastAPI()
    for router in (summary_model_routes.setup_summary_model_routes(),
                   vocab.setup_whisper_vocabulary_routes(),
                   voice_enroll_routes.setup_voice_enroll_routes(),
                   relay_routes.setup_sound_relay_routes()):
        app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: None
    return app


def _http_callers(mp) -> dict:
    from fastapi.testclient import TestClient

    from fork.athena import call_recorder as rec
    from fork.athena import meeting_bot as mb
    from fork.athena import record_share as share
    from fork.athena import relay_routes
    from fork.ink import athena as ink_athena
    from fork.ink import notes as ink

    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    mp.setattr(rec, "datetime", _Fixed)
    mp.setattr(relay_routes, "require_admin", lambda request: None)
    mp.setattr(relay_routes, "effective_user", lambda request: "ana")
    client = TestClient(_app())
    at = "2026-10-20T08:59:30+00:00"

    def page(method, path, **kw):
        res = client.request(method, path, **kw)
        try:
            body = res.json()
        except ValueError:
            body = res.text
        return {"status": res.status_code, "body": body}

    rec_c = (("RecorderError", rec.RecorderError),)
    meet_c = (("MeetingError", mb.MeetingError),)
    share_c = (("LookupError", LookupError), ("RecorderError", rec.RecorderError))
    ink_c = (("InkError", ink.InkError),)
    join = mb.parse_join("https://us02web.zoom.us/j/123456789?pwd=abc")

    def link():
        return rec.resolve()

    async def forwarded(*a, **kw):
        return list(await rec.forward(link(), *a, **kw))

    async def fetched():
        f = await share.fetch(link(), "c 1/2", "md", ("summary", "transcript"))
        return {"filename": f.filename, "content_type": f.content_type, "data": f.data}

    return {
        "call_recorder.resolve": (lambda: repr(rec.resolve()) + " " + rec.resolve().token,
                                  rec_c),
        "call_recorder.status": (rec.status, rec_c),
        "call_recorder.forward_chunk": (lambda: rec.forward_chunk(
            link(), data=b"OggS-fake", session_id="s-1", sequence=3, timestamp=at,
            codec="ogg", device_name="computer", kind="note"), rec_c),
        "call_recorder.forward_chunk_relay": (lambda: rec.forward_chunk(
            link(), data=b"OggS-fake", session_id="s-1", sequence=0, timestamp=at,
            codec="opus", device_name="watch", source=rec.RELAY), rec_c),
        "call_recorder.end_session": (lambda: rec.end_session(link(), "s-1"), rec_c),
        "call_recorder.forward_get": (lambda: forwarded(
            "GET", "/conversations", params={"since": None, "limit": 5, "q": ""}), rec_c),
        "call_recorder.forward_patch": (lambda: forwarded(
            "PATCH", "/action-items/4", json={"status": "done"}), rec_c),
        "meeting_bot.resolve": (lambda: repr(mb.resolve()), meet_c),
        "meeting_bot.join": (lambda: mb.join(mb.resolve(), join, "Standup"), meet_c),
        "meeting_bot.get": (lambda: mb.get(mb.resolve(), MID), meet_c),
        "meeting_bot.current": (lambda: mb.current(mb.resolve()), meet_c),
        "meeting_bot.leave": (lambda: mb.leave(mb.resolve(), MID), meet_c),
        "meeting_bot.signin_status": (lambda: mb.signin_status(mb.resolve()), meet_c),
        "meeting_bot.signin_start": (lambda: mb.signin_start(mb.resolve()), meet_c),
        "meeting_bot.signin_finish": (
            lambda: mb.signin_finish(mb.resolve(), " https://hub/cb?code=abc "), meet_c),
        "summary_model.get": (lambda: page("GET", "/api/fork/summary-model"), ()),
        "summary_model.put": (lambda: page("PUT", "/api/fork/summary-model",
                                           json={"model": "  m  "}), ()),
        "summary_model.put_null": (lambda: page("PUT", "/api/fork/summary-model",
                                                json={"model": " "}), ()),
        "vocabulary.get": (lambda: page("GET", "/api/fork/whisper-vocabulary"), ()),
        "vocabulary.put": (lambda: page("PUT", "/api/fork/whisper-vocabulary",
                                        json={"text": " a, b "}), ()),
        "voice_enroll.get": (lambda: page("GET", "/api/fork/voice-enroll"), ()),
        "voice_enroll.post": (lambda: page("POST", "/api/fork/voice-enroll",
                                           json={"name": "  Ana   Lee "}), ()),
        "voice_enroll.delete": (lambda: page("DELETE", "/api/fork/voice-enroll"), ()),
        "relay.chunk": (lambda: page("POST", "/api/codex/sound/chunk", data={
            "device": "watch", "session_id": "s-2", "sequence": "1", "timestamp": at,
            "codec": "ogg"}, files={"audio": ("1.ogg", b"OggS-fake", "audio/ogg")}), ()),
        "relay.end": (lambda: page("POST", "/api/codex/sound/sessions/s-2/end"), ()),
        "relay.health": (lambda: page("GET", "/api/codex/sound/health"), ()),
        "relay.conversations": (lambda: page(
            "GET", "/api/codex/sound/conversations?limit=5&since=2026-10-01"), ()),
        "relay.transcript": (lambda: page(
            "GET", "/api/codex/sound/conversations/c%201/transcript?start=1.5"), ()),
        "relay.action_items": (lambda: page("GET", "/api/codex/sound/action-items"), ()),
        "relay.patch_item": (lambda: page("PATCH", "/api/codex/sound/action-items/4",
                                          json={"status": "done"}), ()),
        "relay.make_digest": (lambda: page("POST", "/api/codex/sound/digests/2026-10-20"),
                              ()),
        "record_share.fetch": (fetched, share_c),
        "ink_athena._call": (lambda: ink_athena._call(
            "POST", "/ingest/text", json={"source_id": "hub-note:n1", "text": "hi"}), ink_c),
        "ink_athena._delete": (lambda: ink_athena._delete("n1", None), ink_c),
    }


def observe_http(monkeypatch) -> dict:
    import core.database

    env = Env(monkeypatch)
    wire = Wire()
    monkeypatch.setattr(httpx, "AsyncClient", wire.client)
    callers = _http_callers(monkeypatch)
    out: dict[str, dict] = {}
    logs = Logs()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(logs)
    root.setLevel(logging.DEBUG)
    try:
        cases = [(f"rows={r} http=200", r, "200") for r in ROW_CASES]
        cases += [(f"rows=good http={h}", "good", h) for h in HTTP_CASES if h != "200"]
        for case, rows, answer in cases:
            values = ROW_CASES[rows]
            env.set(values[1])
            monkeypatch.setattr(core.database, "SessionLocal", Rows(values[0]))
            wire.handler = HTTP_CASES[answer]
            for label, (make, catches) in callers.items():
                if rows != "good" and label not in (
                        "call_recorder.resolve", "call_recorder.status",
                        "call_recorder.forward_get", "meeting_bot.resolve",
                        "summary_model.get", "voice_enroll.post", "relay.health",
                        "record_share.fetch", "ink_athena._call"):
                    continue
                wire.sent.clear()
                logs.lines.clear()
                row = _run(make, catches)
                sent = json.loads(json.dumps(wire.sent))
                first = out.get(f"rows=good http=200 {label}", {}).get("sent")
                # The same requests as with a 200 answer: say so instead of repeating them.
                row["sent"] = "as with 200" if answer != "200" and sent == first else sent
                if logs.lines:
                    row["logs"] = list(logs.lines)
                out[f"{case} {label}"] = row
        env.set({})
        return out
    finally:
        root.removeHandler(logs)
        root.setLevel(old_level)


def observe(monkeypatch) -> dict:
    return {"mcp": observe_mcp(monkeypatch), "http": observe_http(monkeypatch)}


def _compare(seen: dict, expected: dict) -> None:
    for section in ("mcp", "http"):
        assert sorted(seen[section]) == sorted(expected[section]), section
        for key, row in expected[section].items():
            assert seen[section][key] == row, key


def dump(seen: dict) -> str:
    """The fixture's layout: one row per line, so a diff names the caller and case."""
    lines = ["{"]
    for n, section in enumerate(("http", "mcp")):
        lines.append(f" {json.dumps(section)}: {{")
        rows = sorted(seen[section].items())
        for i, (key, row) in enumerate(rows):
            comma = "," if i < len(rows) - 1 else ""
            lines.append(f"  {json.dumps(key)}: "
                         f"{json.dumps(row, sort_keys=True, ensure_ascii=True)}{comma}")
        lines.append(" }," if n == 0 else " }")
    return "\n".join(lines) + "\n}\n"


def test_every_athena_caller_behaves_as_recorded(monkeypatch):
    seen = json.loads(json.dumps(observe(monkeypatch)))
    _compare(seen, json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_the_fixture_covers_every_caller_and_answer():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(expected["mcp"]) == len(MCP_CASES) * 8
    labels = {k.split(" ", 1)[1] for k in expected["mcp"]}
    assert len(labels) == 8
    http = {k.rsplit(" ", 1)[1] for k in expected["http"]}
    assert len(http) >= 34
    # No row sends the token anywhere but the Authorization header.
    for key, row in expected["http"].items():
        for sent in row["sent"] if isinstance(row.get("sent"), list) else []:
            if "url" in sent:
                assert TOKEN not in sent["url"] + json.dumps(sent["query"]), key
                if "authorization" in sent["headers"]:
                    assert sent["headers"]["authorization"].startswith("Bearer "), key


if __name__ == "__main__":  # pragma: no cover -- records the fixture (old callers only)
    mp = pytest.MonkeyPatch()
    try:
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(dump(observe(mp)), encoding="utf-8")
    finally:
        mp.undo()
