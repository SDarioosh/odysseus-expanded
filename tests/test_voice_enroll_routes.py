"""Fork: "Enroll My Voice" in Settings → AI Defaults
(routes/voice_enroll_routes.py, static/js/forkVoiceEnroll.js)."""
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.middleware import require_admin
from routes.voice_enroll_routes import OLD_MODULE, setup_voice_enroll_routes
from src import call_recorder as rec

ROOT = Path(__file__).resolve().parents[1]
MCP_TOKEN = "m" * 32
ROWS = [("srv1", "convo", f"http://module:8001/mcp?access_token={MCP_TOKEN}", True)]


class Module:
    """A fake Athena MCP HTTP server for /speakers/enroll/next."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.state = {"state": "none", "name": None, "default_name": "Sam",
                      "owner_voice": None, "available": True, "recording": None}
        self.missing = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.missing or request.url.path != "/speakers/enroll/next":
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.method == "POST":
            body = json.loads(request.content)
            if self.state["state"] == "processing":
                return httpx.Response(409, json={"detail": "a recording is being enrolled"})
            self.state = {**self.state, "state": "waiting",
                          "name": body.get("name") or self.state["default_name"],
                          "is_owner": body.get("is_owner")}
        elif request.method == "DELETE":
            self.state = {**self.state, "state": "cancelled"}
        return httpx.Response(200, json=self.state)


def make_app(monkeypatch, module: Module, admin: bool = True, rows=ROWS) -> TestClient:
    monkeypatch.delenv(rec.SERVER_ENV, raising=False)
    monkeypatch.setattr(rec, "_server_rows", lambda: rows)
    real_client = rec._client
    monkeypatch.setattr(rec, "_client", lambda transport: real_client(httpx.MockTransport(module)))
    monkeypatch.setattr("core.middleware.auth_disabled", lambda: False)
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        request.state.current_user = "owner"
        return await call_next(request)

    app.include_router(setup_voice_enroll_routes())
    if admin:
        app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_get_passes_the_module_state_through(monkeypatch):
    module = Module()
    r = make_app(monkeypatch, module).get("/api/fork/voice-enroll")
    assert r.status_code == 200 and r.json() == {"connected": True, **module.state}
    (req,) = module.requests
    assert req.method == "GET" and req.url.path == "/speakers/enroll/next"
    assert "access_token" not in req.url.params
    assert req.headers["authorization"] == f"Bearer {MCP_TOKEN}"


def test_start_always_enrolls_the_owner(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module)
    r = c.post("/api/fork/voice-enroll", json={"name": "  Shahyah   Example "})
    assert r.status_code == 200 and r.json()["state"] == "waiting"
    assert json.loads(module.requests[-1].content) == {"name": "Shahyah Example",
                                                       "is_owner": True}
    r = c.post("/api/fork/voice-enroll", json={"name": " "})
    assert json.loads(module.requests[-1].content) == {"name": None, "is_owner": True}
    assert r.json()["name"] == "Sam"
    assert c.post("/api/fork/voice-enroll", json={"name": "x" * 101}).status_code == 422


def test_cancel_and_module_refusals(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module)
    assert c.delete("/api/fork/voice-enroll").json()["state"] == "cancelled"
    module.state["state"] = "processing"
    r = c.post("/api/fork/voice-enroll", json={})
    assert r.status_code == 409 and r.json()["detail"] == "a recording is being enrolled"


def test_old_module_and_missing_server(monkeypatch):
    module = Module()
    module.missing = True
    c = make_app(monkeypatch, module)
    assert c.get("/api/fork/voice-enroll").json() == {"connected": False, "problem": OLD_MODULE}
    r = c.post("/api/fork/voice-enroll", json={})
    assert r.status_code == 502 and r.json()["detail"] == OLD_MODULE
    body = make_app(monkeypatch, Module(), rows=[]).get("/api/fork/voice-enroll").json()
    assert body["connected"] is False and "No MCP server" in body["problem"]


def test_admin_only(monkeypatch):
    c = make_app(monkeypatch, Module(), admin=False)
    assert c.get("/api/fork/voice-enroll").status_code in (401, 403)
    assert c.post("/api/fork/voice-enroll", json={}).status_code in (401, 403)
    assert c.delete("/api/fork/voice-enroll").status_code in (401, 403)


def test_hooks_are_in_place():
    assert "setup_voice_enroll_routes()" in (ROOT / "app.py").read_text()
    html = (ROOT / "static/index.html").read_text()
    assert '<script type="module" src="/static/js/forkVoiceEnroll.js"></script>' in html


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_card_texts():
    script = """
const m = await import(process.argv[1]);
const out = {
  n: m.STATEMENTS.length,
  last: m.STATEMENTS[m.STATEMENTS.length - 1].after,
  none: m.describe({connected: true, state: 'none', owner_voice: null}),
  enrolled: m.describe({connected: true, state: 'none', owner_voice: 'Sam'}),
  waiting: m.describe({connected: true, state: 'waiting', recording: null}),
  rec: m.describe({connected: true, state: 'waiting',
                   recording: {device: 'twatch-ultra', seconds: 44.6, status: 'recording'}}),
  ended: m.describe({connected: true, state: 'waiting',
                     recording: {device: 'twatch-ultra', seconds: 90, status: 'ended'}}),
  done: m.describe({connected: true, state: 'done', name: 'Sam', speech_seconds: 74.6,
                    samples: 8, relabeled_lines: 1}),
  failed: m.describe({connected: true, state: 'failed', error: 'only 12 s of speech'}),
  off: m.describe({connected: true, available: false, state: 'none'}),
  down: m.describe({connected: false, problem: 'Athena is not reachable'}),
};
console.log(JSON.stringify(out));
"""
    path = (ROOT / "static/js/forkVoiceEnroll.js").as_uri()
    res = subprocess.run(["node", "--input-type=module", "-e", script, path],
                         capture_output=True, text=True, check=True)
    out = json.loads(res.stdout)
    assert out["n"] >= 8 and out["last"] == "Now stop the recording on the watch."
    assert "No voice is enrolled as yours yet" in out["none"]
    assert "enrolled as Sam" in out["enrolled"]
    assert out["waiting"].startswith("Waiting for the watch recording")
    assert out["rec"] == "Recording on twatch-ultra: 45 s received so far."
    assert "enrolls it in a moment" in out["ended"]
    assert out["done"].startswith("Done: Sam is enrolled as the owner from 75 s of speech (8 samples)")
    assert "1 earlier line of unnamed speakers" in out["done"]
    assert out["failed"] == "It didn't work: only 12 s of speech."
    assert "speaker recognition is off" in out["off"]
    assert out["down"] == "Athena is not reachable"
