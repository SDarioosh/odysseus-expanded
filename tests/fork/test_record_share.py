"""Fork: share a record as a file (src/record_share.py, routes/sound_module_routes.py,
static/js/forkShareRecord.js)."""
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src import call_recorder as rec
from src import record_share as share

ROOT = Path(__file__).resolve().parents[2]
MCP_TOKEN = "m" * 32
ROWS = [("srv1", "convo", f"http://module:8001/mcp?access_token={MCP_TOKEN}", True)]
MD = b"# Planning the trip\n\n## Transcript\n\n**10:00:00 Me:** hi\n"


class Module:
    """A fake Athena MCP HTTP server with the relay route GET /conversations/{id}/export."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.missing = False
        self.content_type = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = request.url.path.split("/")
        if self.missing or len(parts) != 4 or parts[3] != "export":
            return httpx.Response(404, json={"detail": "Not Found"})
        if parts[2] != "c1":
            return httpx.Response(404, json={"detail": "conversation not found"})
        fmt = request.url.params.get("format", "md")
        ctype = self.content_type or ("text/html; charset=utf-8" if fmt == "html"
                                      else "text/markdown; charset=utf-8")
        name = quote(f"2025-10-09 Café plans.{fmt}", safe="")
        return httpx.Response(200, content=MD, headers={
            "content-type": ctype,
            "content-disposition": f"attachment; filename=\"2025-10-09 Caf_ plans.{fmt}\"; "
                                   f"filename*=UTF-8''{name}"})


def make_app(monkeypatch, module: Module, admin: bool = True) -> TestClient:
    from routes.sound_module_routes import require_admin, setup_sound_module_routes

    monkeypatch.delenv(rec.SERVER_ENV, raising=False)
    monkeypatch.setattr("fork.athena.client._server_rows", lambda: ROWS)
    real_client = rec._client
    monkeypatch.setattr("fork.athena.client._client", lambda transport, timeout=None: real_client(httpx.MockTransport(module)))
    monkeypatch.setattr("core.middleware.auth_disabled", lambda: False)
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        request.state.current_user = "owner"
        return await call_next(request)

    app.include_router(setup_sound_module_routes())
    if admin:
        app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


URL = "/api/modules/sound/conversations/c1/export"


def test_the_file_comes_from_the_module(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module)
    r = c.get(URL, params={"format": "md", "include": "transcript,summary"})
    assert r.status_code == 200 and r.content == MD
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;") and "Caf%C3%A9%20plans.md" in cd
    assert r.headers["cache-control"] == "no-store"
    assert "sandbox" in r.headers["content-security-policy"]
    (req,) = module.requests
    assert req.url.path == "/conversations/c1/export"
    assert "access_token" not in req.url.params
    assert req.headers["authorization"] == f"Bearer {MCP_TOKEN}"
    assert req.url.params["format"] == "md"
    assert req.url.params["include"] == "summary,transcript"  # page order


def test_default_parts_and_html(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module)
    r = c.get(URL, params={"format": "HTML"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["content-disposition"].endswith(".html")
    assert module.requests[-1].url.params["include"] == "summary,action_items,transcript"


def test_bad_input_never_reaches_the_module(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module)
    assert c.get(URL, params={"format": "pdf"}).status_code == 400
    assert c.get(URL, params={"include": "summary,audio"}).status_code == 400
    assert c.get(URL, params={"include": ",,"}).status_code == 400
    assert c.get("/api/modules/sound/conversations/a%20b/export").status_code == 400
    assert module.requests == []


def test_module_answers(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module)
    r = c.get("/api/modules/sound/conversations/c9/export")
    assert r.status_code == 404 and r.json()["detail"] == "conversation not found"
    module.content_type = "application/json"
    r = c.get(URL)
    assert r.status_code == 502 and "not the file" in r.json()["detail"]
    module.missing = True
    r = c.get(URL)
    assert r.status_code == 502 and r.json()["detail"] == share.OLD_MODULE


def test_admin_only(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module, admin=False)
    assert c.get(URL).status_code in (401, 403)
    assert module.requests == []


def test_file_names_are_made_safe():
    assert share.filename_from("attachment; filename*=UTF-8''a%2F..%2Fb%3Cx%3E.md", "md") == "a .. b x.md"
    assert share.filename_from('attachment; filename="x.md"', "html") == "x.md.html"
    assert share.filename_from("", "md") == "Athena record.md"
    cd = share.content_disposition("2025 Café.md")
    assert 'filename="2025 Caf_.md"' in cd and "Caf%C3%A9.md" in cd


def test_reader_has_the_share_button():
    js = (ROOT / "static/js/soundModule.js").read_text()
    assert "import { shareControls } from './forkShareRecord.js';" in js
    assert "share ? share.button : null" in js and "actions, share.panel" in js
    css = (ROOT / "static/css/fork-modules.css").read_text()
    assert ".fm-root .bs-share[hidden] { display: none; }" in css


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_js_file_name_and_url():
    script = f"""
    const m = await import('{(ROOT / "static/js/forkShareRecord.js").as_posix()}');
    console.log(JSON.stringify([
      m.fileName("attachment; filename=\\"a.md\\"; filename*=UTF-8''Caf%C3%A9%20plans.md", 'md'),
      m.fileName('attachment; filename="x/y.html"', 'html'),
      m.fileName('', 'md'),
      m.fileName("attachment; filename*=UTF-8''%E0%A4%A", 'md'),
      m.exportUrl('c 1', {{ format: 'html', include: ['summary', 'transcript'] }}),
    ]));
    """
    out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True,
                         text=True, check=True).stdout
    assert json.loads(out) == [
        "Café plans.md", "x y.html", "Athena record.md", "Athena record.md",
        "/api/modules/athena/conversations/c%201/export?format=html&include=summary%2Ctranscript",
    ]
