"""Fork: "Record a call" uploads forwarded to odysseus-sound's MCP HTTP server (src/call_recorder.py)."""
import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import require_admin
from routes.sound_module_routes import setup_sound_module_routes
from src import call_recorder as rec

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
TOKEN = "t" * 32
CFG = rec.Link(base="http://module:8001", token=TOKEN)
ROWS = [("srv1", "convo", f"http://module:8001/mcp?access_token={TOKEN}", True)]


def run(coro):
    return asyncio.run(coro)


class Module:
    """A fake odysseus-sound API that records what it was sent."""

    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else {"ok": True, "stop": False, "reason": None}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    @property
    def transport(self):
        return httpx.MockTransport(self)


def chunk(**kw):
    args = {"data": b"\x1aE\xdf\xa3webm", "session_id": "call-20261022T145930-ab12cd",
            "sequence": 0, "timestamp": "2026-10-22T14:59:30.000Z", "codec": "webm"}
    args.update(kw)
    return args


@pytest.mark.parametrize("url, link", [
    (f"http://module:8001/mcp?access_token={TOKEN}", ("http://module:8001", TOKEN)),
    (f"http://host.docker.internal:8001/mcp/?access_token={TOKEN}&x=1",
     ("http://host.docker.internal:8001", TOKEN)),
    (f"https://h.ts.net/sound/mcp?access_token={TOKEN}", ("https://h.ts.net/sound", TOKEN)),
    ("http://module:8001/mcp", None),  # no token
    ("http://module:8001/mcp?access_token=", None),
    (f"module:8001/mcp?access_token={TOKEN}", None),  # no scheme
    ("", None),
])
def test_link_from_url(url, link):
    got = rec.link_from_url(url)
    assert (None if got is None else (got.base, got.token)) == link


def test_link_repr_hides_the_token():
    assert TOKEN not in repr(CFG) and TOKEN not in str(CFG)


def test_resolve_finds_the_convo_server(monkeypatch):
    monkeypatch.delenv(rec.SERVER_ENV, raising=False)
    monkeypatch.delenv(rec.LEGACY_SERVER_ENV, raising=False)
    monkeypatch.setattr(rec, "_server_rows", lambda: [
        ("other", "github", "http://x/mcp?access_token=zzz", True), *ROWS])
    assert rec.resolve() == CFG
    assert rec.status() == {"configured": True, "problem": None}
    monkeypatch.setenv(rec.SERVER_ENV, "SRV1")  # by id, any case
    assert rec.resolve() == CFG


@pytest.mark.parametrize("rows, words", [
    ([], "No MCP server named 'convo'"),
    ([("srv1", "convo", f"http://m/mcp?access_token={TOKEN}", False)], "turned off"),
    ([("srv1", "convo", "http://m/mcp", True)], "access_token"),
    ([("srv1", "convo", None, True)], "access_token"),
])
def test_resolve_says_what_to_fix(monkeypatch, rows, words):
    monkeypatch.delenv(rec.SERVER_ENV, raising=False)
    monkeypatch.setattr(rec, "_server_rows", lambda: rows)
    with pytest.raises(rec.RecorderError) as e:
        rec.resolve()
    assert e.value.status == 503 and words in e.value.message
    assert rec.status()["configured"] is False and words in rec.status()["problem"]
    assert TOKEN not in e.value.message


def test_device_setting(monkeypatch):
    monkeypatch.delenv(rec.DEVICE_ENV, raising=False)
    assert rec.device() == "computer"
    monkeypatch.setenv(rec.DEVICE_ENV, "bad device!")
    assert rec.device() == "computer"
    monkeypatch.setenv(rec.DEVICE_ENV, "mac")
    assert rec.device() == "mac"


def test_check_chunk_accepts_a_piece_and_normalizes_the_time():
    sid, ts, codec = rec.check_chunk(**chunk(codec="WEBM"), now=NOW)
    assert (sid, ts, codec) == ("call-20261022T145930-ab12cd", "2026-10-22T14:59:30+00:00", "webm")
    _, ts, _ = rec.check_chunk(**chunk(timestamp="2026-10-22T16:59:30+02:00"), now=NOW)
    assert ts == "2026-10-22T14:59:30+00:00"


@pytest.mark.parametrize("bad, status", [
    ({"session_id": "has space"}, 400),
    ({"session_id": "../etc"}, 400),
    ({"session_id": ""}, 400),
    ({"sequence": -1}, 400),
    ({"sequence": rec.MAX_SEQUENCE + 1}, 400),
    ({"codec": "mp3"}, 400),
    ({"data": b""}, 400),
    ({"data": b"x" * (rec.MAX_CHUNK_BYTES + 1)}, 413),
    ({"timestamp": "yesterday"}, 400),
    ({"timestamp": "2026-10-22T14:59:30"}, 400),  # no time zone
    ({"timestamp": "2026-10-22T15:30:00Z"}, 400),  # in the future
    ({"timestamp": "2026-10-01T15:00:00Z"}, 400),  # weeks old
])
def test_check_chunk_rejects(bad, status):
    with pytest.raises(rec.RecorderError) as e:
        rec.check_chunk(**chunk(**bad), now=NOW)
    assert e.value.status == status


def fwd(**kw):
    return {"device_name": "computer", **chunk(**kw)}


def test_forward_chunk_posts_to_the_mcp_upload_route(monkeypatch):
    monkeypatch.setattr(rec, "datetime", _FixedDatetime)
    module = Module(body={"ok": True, "stop": True, "reason": "silence", "session_id": "x"})
    out = run(rec.forward_chunk(CFG, **fwd(sequence=7), transport=module.transport))
    assert (out["ok"], out["stop"], out["reason"]) == (True, True, "silence")
    (req,) = module.requests
    assert (req.url.scheme, req.url.host, req.url.port, req.url.path) == (
        "http", "module", 8001, "/ingest/chunk")
    assert dict(req.url.params) == {"access_token": TOKEN}
    body = req.read()
    for field, value in [("device", "computer"), ("session_id", "call-20261022T145930-ab12cd"),
                         ("sequence", "7"), ("timestamp", "2026-10-22T14:59:30+00:00"),
                         ("codec", "webm"), ("kind", "conversation")]:
        assert f'name="{field}"\r\n\r\n{value}\r\n'.encode() in body
    assert b'name="audio"; filename="000007.webm"' in body and b"\x1aE\xdf\xa3webm" in body


@pytest.mark.parametrize("status, detail, expect", [
    (401, "unauthorized", 502),  # Odysseus's MCP token is wrong: not the user's fault
    (422, "could not decode audio: x", 422),
    (400, "sequence must be >= 0", 400),
    (413, "chunk too large", 413),
    (500, "", 502),
    (502, "Athena's ingest API is not reachable at http://127.0.0.1:8000", 502),
])
def test_forward_chunk_module_errors(monkeypatch, status, detail, expect):
    monkeypatch.setattr(rec, "datetime", _FixedDatetime)
    module = Module(status=status, body={"detail": detail})
    with pytest.raises(rec.RecorderError) as e:
        run(rec.forward_chunk(CFG, **fwd(), transport=module.transport))
    assert e.value.status == expect
    if expect != 502 or detail:
        assert e.value.message == detail or status == 401
    assert TOKEN not in e.value.message  # the token never reaches the page


def test_old_module_says_update(monkeypatch):
    monkeypatch.setattr(rec, "datetime", _FixedDatetime)

    def old(request):  # a module before MCP 0.8.0: Starlette's plain 404
        return httpx.Response(404, text="Not Found")

    with pytest.raises(rec.RecorderError) as e:
        run(rec.forward_chunk(CFG, **fwd(), transport=httpx.MockTransport(old)))
    assert e.value.status == 502 and "update Athena" in e.value.message
    with pytest.raises(rec.RecorderError) as e:
        run(rec.end_session(CFG, "call-1", transport=httpx.MockTransport(old)))
    assert "update Athena" in e.value.message


def test_forward_chunk_unreachable(monkeypatch, caplog):
    monkeypatch.setattr(rec, "datetime", _FixedDatetime)

    def down(request):
        raise httpx.ConnectError(f"refused {request.url}", request=request)

    with pytest.raises(rec.RecorderError) as e:
        run(rec.forward_chunk(CFG, **fwd(), transport=httpx.MockTransport(down)))
    assert (e.value.status, e.value.message) == (502, "Athena is not reachable")
    assert TOKEN not in caplog.text


def test_bad_piece_is_never_sent():
    module = Module()
    for bad in (fwd(codec="exe"), fwd(device_name="bad device"), {**fwd(), "kind": "x"}):
        with pytest.raises(rec.RecorderError):
            run(rec.forward_chunk(CFG, **bad, transport=module.transport))
    assert module.requests == []


def test_relay_source_accepts_the_watch_chunks():
    sid, ts, codec = rec.check_chunk(**chunk(codec="ogg", timestamp="2026-10-10T08:00:00Z"),
                                     source=rec.RELAY, now=NOW)  # 12 days queued
    assert codec == "ogg" and ts == "2026-10-10T08:00:00+00:00"
    assert rec.check_chunk(**chunk(codec="opus", timestamp=str(int(NOW.timestamp()))),
                           source=rec.RELAY, now=NOW)[2] == "opus"
    for bad in ({"codec": "webm"}, {"timestamp": "2026-06-01T00:00:00Z"}):
        with pytest.raises(rec.RecorderError):
            rec.check_chunk(**chunk(**bad), source=rec.RELAY, now=NOW)


def test_end_session():
    module = Module(body={"ok": True, "stop": True})
    assert run(rec.end_session(CFG, "call-1", transport=module.transport)) == {
        "ok": True, "found": True}
    assert module.requests[0].url.path == "/sessions/call-1/end"
    missing = Module(status=404, body={"detail": "session not found"})
    assert run(rec.end_session(CFG, "call-2", transport=missing.transport)) == {
        "ok": True, "found": False}
    with pytest.raises(rec.RecorderError):
        run(rec.end_session(CFG, "a/b", transport=module.transport))


def test_forward_reads():
    module = Module(body={"conversations": []})
    status, body = run(rec.forward(CFG, "GET", "/conversations",
                                   params={"since": 5, "until": None},
                                   transport=module.transport))
    assert (status, body) == (200, {"conversations": []})
    assert dict(module.requests[0].url.params) == {"since": "5", "access_token": TOKEN}
    gone = Module(status=404, body={"detail": "conversation not found"})
    assert run(rec.forward(CFG, "GET", "/conversations/x", transport=gone.transport)) == (
        404, {"detail": "conversation not found"})
    with pytest.raises(rec.RecorderError) as e:
        run(rec.forward(CFG, "GET", "/x", transport=httpx.MockTransport(
            lambda r: httpx.Response(404, text="Not Found"))))
    assert "update Athena" in e.value.message
    with pytest.raises(rec.RecorderError) as e:
        run(rec.forward(CFG, "GET", "/x", transport=Module(status=401, body={}).transport))
    assert e.value.status == 502


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


# ------------------------------------------------------------------ routes
@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv(rec.SERVER_ENV, raising=False)
    monkeypatch.delenv(rec.DEVICE_ENV, raising=False)
    monkeypatch.setattr(rec, "_server_rows", lambda: ROWS)
    monkeypatch.setattr(rec, "datetime", _FixedDatetime)
    module = Module()
    real_client = rec._client
    monkeypatch.setattr(rec, "_client", lambda transport: real_client(module.transport))
    app = FastAPI()
    app.include_router(setup_sound_module_routes())
    app.dependency_overrides[require_admin] = lambda: None
    with TestClient(app) as c:
        c.module = module
        yield c


def post_chunk(client, headers=None, **form):
    data = {"session_id": "call-1", "sequence": "0", "timestamp": "2026-10-22T14:59:30Z",
            "codec": "webm", **form}
    return client.post("/api/modules/sound/record/chunk", data=data,
                       files={"audio": ("000000.webm", b"\x1aE\xdf\xa3", "audio/webm")},
                       headers={"X-Odysseus-Recorder": "1"} if headers is None else headers)


def test_routes_forward_and_end(client):
    assert client.get("/api/modules/sound/record/config").json() == {
        "configured": True, "problem": None, "device": "computer",
        "max_bytes": rec.MAX_CHUNK_BYTES}
    r = post_chunk(client)
    assert r.status_code == 200 and r.json() == {"ok": True, "stop": False, "reason": None}
    assert client.module.requests[-1].url.path == "/ingest/chunk"
    r = client.post("/api/modules/sound/record/call-1/end", headers={"X-Odysseus-Recorder": "1"})
    assert r.json() == {"ok": True, "found": True}


def test_routes_need_the_recorder_header(client):
    assert post_chunk(client, headers={}).status_code == 400
    assert client.post("/api/modules/sound/record/call-1/end").status_code == 400
    assert client.module.requests == []


def test_routes_pass_errors_through(client):
    r = post_chunk(client, codec="mp3")
    assert r.status_code == 400 and "codec" in r.json()["detail"]
    assert client.module.requests == []


def test_routes_not_configured(client, monkeypatch):
    monkeypatch.setattr(rec, "_server_rows", lambda: [])
    cfg = client.get("/api/modules/sound/record/config").json()
    assert cfg["configured"] is False and "No MCP server named 'convo'" in cfg["problem"]
    r = post_chunk(client)
    assert r.status_code == 503 and "Settings → MCP" in r.json()["detail"]
    assert client.module.requests == []


def test_record_page_is_served(client):
    r = client.get("/modules/sound/record")
    assert r.status_code == 200 and "Record a call" in r.text and "callRecorder.js" in r.text


def test_routes_are_admin_only(monkeypatch):
    app = FastAPI()
    app.include_router(setup_sound_module_routes())
    monkeypatch.setattr("core.middleware.auth_disabled", lambda: False)
    with TestClient(app) as c:
        assert c.get("/api/modules/sound/record/config").status_code == 403
        assert post_chunk(c).status_code == 403
