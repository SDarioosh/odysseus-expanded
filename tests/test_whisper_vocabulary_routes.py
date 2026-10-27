"""Fork: Athena's whisper vocabulary in Settings → AI Defaults
(routes/whisper_vocabulary_routes.py, static/js/forkWhisperVocabulary.js)."""
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.middleware import require_admin
from routes.whisper_vocabulary_routes import OLD_MODULE, setup_whisper_vocabulary_routes
from src import call_recorder as rec

ROOT = Path(__file__).resolve().parents[1]
MCP_TOKEN = "m" * 32
ROWS = [("srv1", "convo", f"http://module:8001/mcp?access_token={MCP_TOKEN}", True)]


class Module:
    """A fake Athena MCP HTTP server for /settings/vocabulary."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.words: list[str] = []
        self.missing = False

    def info(self):
        return {"words": self.words, "text": ", ".join(self.words), "max_words": 50,
                "env_prompt": "", "prompt": (", ".join(self.words) + ".") if self.words else ""}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.missing or request.url.path != "/settings/vocabulary":
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.method == "PUT":
            text = json.loads(request.content).get("words") or ""
            words = [w.strip() for w in text.split(",") if w.strip()]
            if len(words) > 50:
                return httpx.Response(422, json={"detail": "at most 50 words (got 51)"})
            self.words = words
        return httpx.Response(200, json=self.info())


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

    app.include_router(setup_whisper_vocabulary_routes())
    if admin:
        app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_get_passes_the_module_answer_through(monkeypatch):
    module = Module()
    module.words = ["RokTalk"]
    r = make_app(monkeypatch, module).get("/api/fork/whisper-vocabulary")
    assert r.status_code == 200 and r.json() == {"connected": True, **module.info()}
    (req,) = module.requests
    assert req.method == "GET" and req.url.path == "/settings/vocabulary"
    assert req.url.params["access_token"] == MCP_TOKEN


def test_put_sends_the_text_as_words(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module)
    r = c.put("/api/fork/whisper-vocabulary", json={"text": " RokTalk, Meshtastic, Rokland "})
    assert r.status_code == 200 and r.json()["words"] == ["RokTalk", "Meshtastic", "Rokland"]
    assert json.loads(module.requests[-1].content) == {"words": "RokTalk, Meshtastic, Rokland"}
    r = c.put("/api/fork/whisper-vocabulary", json={"text": "  "})
    assert r.json()["words"] == []
    assert json.loads(module.requests[-1].content) == {"words": None}


def test_put_shows_the_module_refusal(monkeypatch):
    c = make_app(monkeypatch, Module())
    r = c.put("/api/fork/whisper-vocabulary", json={"text": ",".join(f"w{i}" for i in range(51))})
    assert r.status_code == 422 and "at most 50 words" in r.json()["detail"]
    assert c.put("/api/fork/whisper-vocabulary", json={"text": "x" * 4001}).status_code == 422


def test_problems_are_shown_not_raised(monkeypatch):
    module = Module()
    module.missing = True  # an Athena without the route
    r = make_app(monkeypatch, module).get("/api/fork/whisper-vocabulary")
    assert r.status_code == 200 and r.json() == {"connected": False, "problem": OLD_MODULE}
    r = make_app(monkeypatch, Module(), rows=[]).get("/api/fork/whisper-vocabulary")
    assert r.json()["connected"] is False and "No MCP server named 'convo'" in r.json()["problem"]
    r = make_app(monkeypatch, module).put("/api/fork/whisper-vocabulary", json={"text": "x"})
    assert r.status_code == 502 and r.json()["detail"] == OLD_MODULE


def test_admin_only(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module, admin=False)
    assert c.get("/api/fork/whisper-vocabulary").status_code in (401, 403)
    assert c.put("/api/fork/whisper-vocabulary", json={"text": "a"}).status_code in (401, 403)
    assert module.requests == []


def test_hooks_are_in_place():
    assert "setup_whisper_vocabulary_routes" in (ROOT / "app.py").read_text()
    html = (ROOT / "static/index.html").read_text()
    assert '<script type="module" src="/static/js/forkWhisperVocabulary.js"></script>' in html
    js = (ROOT / "static/js/forkWhisperVocabulary.js").read_text()
    assert "/api/fork/whisper-vocabulary" in js
    # it sits in the AI Defaults panel, after the Background & Summaries card
    assert "fork-background-models-card" in js and "set-utilityEpSelect" in html
