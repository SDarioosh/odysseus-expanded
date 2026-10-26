"""Fork: odysseus-sound's summary model in Settings → AI Defaults
(routes/summary_model_routes.py, static/js/forkBackgroundModels.js)."""
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.middleware import require_admin
from routes.summary_model_routes import OLD_MODULE, setup_summary_model_routes
from src import call_recorder as rec

ROOT = Path(__file__).resolve().parents[1]
MCP_TOKEN = "m" * 32
ROWS = [("srv1", "convo", f"http://module:8001/mcp?access_token={MCP_TOKEN}", True)]
INFO = {"base_url": "http://127.0.0.1:8082/v1", "model": "qwen3.5-9b",
        "default_model": "qwen3.5-9b", "override": None,
        "available": ["qwen3.5-9b", "qwen3.6-35b"]}


class Module:
    """A fake odysseus-sound MCP HTTP server for /settings/llm."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.info = dict(INFO)
        self.missing = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.missing or request.url.path != "/settings/llm":
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.method == "PUT":
            model = json.loads(request.content).get("model")
            if model and model not in self.info["available"]:
                return httpx.Response(422, json={"detail": f"the LLM server has no model {model!r}"})
            self.info.update(model=model or self.info["default_model"], override=model)
        return httpx.Response(200, json=self.info)


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

    app.include_router(setup_summary_model_routes())
    if admin:
        app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_get_passes_the_module_answer_through(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module)
    r = c.get("/api/fork/summary-model")
    assert r.status_code == 200 and r.json() == {"connected": True, **INFO}
    (req,) = module.requests
    assert req.method == "GET" and req.url.path == "/settings/llm"
    assert req.url.params["access_token"] == MCP_TOKEN


def test_put_sends_only_the_model_name(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module)
    r = c.put("/api/fork/summary-model", json={"model": " qwen3.6-35b ",
                                                "base_url": "https://cloud.example/v1"})
    assert r.status_code == 200 and r.json()["model"] == "qwen3.6-35b"
    assert json.loads(module.requests[-1].content) == {"model": "qwen3.6-35b"}
    r = c.put("/api/fork/summary-model", json={"model": ""})
    assert r.json()["override"] is None
    assert json.loads(module.requests[-1].content) == {"model": None}


def test_put_shows_the_module_refusal(monkeypatch):
    c = make_app(monkeypatch, Module())
    r = c.put("/api/fork/summary-model", json={"model": "gpt-4o"})
    assert r.status_code == 422 and "no model 'gpt-4o'" in r.json()["detail"]


def test_problems_are_shown_not_raised(monkeypatch):
    module = Module()
    module.missing = True  # an odysseus-sound without the route
    r = make_app(monkeypatch, module).get("/api/fork/summary-model")
    assert r.status_code == 200 and r.json() == {"connected": False, "problem": OLD_MODULE}
    r = make_app(monkeypatch, Module(), rows=[]).get("/api/fork/summary-model")
    assert r.json()["connected"] is False and "No MCP server named 'convo'" in r.json()["problem"]
    r = make_app(monkeypatch, module).put("/api/fork/summary-model", json={"model": "x"})
    assert r.status_code == 502 and r.json()["detail"] == OLD_MODULE


def test_admin_only(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module, admin=False)
    assert c.get("/api/fork/summary-model").status_code in (401, 403)
    assert c.put("/api/fork/summary-model", json={"model": "qwen3.6-35b"}).status_code in (401, 403)
    assert module.requests == []


def test_hooks_are_in_place():
    app_py = (ROOT / "app.py").read_text()
    assert "setup_summary_model_routes" in app_py
    html = (ROOT / "static/index.html").read_text()
    assert '<script type="module" src="/static/js/forkBackgroundModels.js"></script>' in html
    js = (ROOT / "static/js/forkBackgroundModels.js").read_text()
    # the card writes upstream's background-task setting and the module route
    assert "task_endpoint_id" in js and "task_model" in js and "/api/fork/summary-model" in js
    # it sits in the AI Defaults panel, next to the Utility Model card
    assert "set-utilityEpSelect" in js and "set-utilityEpSelect" in html


def test_background_tasks_setting_is_saved_and_used(monkeypatch):
    """The card's hub half is upstream's task_endpoint_id/task_model: an accepted setting
    that src/task_endpoint.py puts first in the background chain."""
    from src.settings import DEFAULT_SETTINGS

    assert "task_endpoint_id" in DEFAULT_SETTINGS and "task_model" in DEFAULT_SETTINGS
    from src import task_endpoint

    calls = []
    monkeypatch.setattr(task_endpoint, "resolve_endpoint",
                        lambda prefix, *a, **k: calls.append(prefix) or (f"http://{prefix}", f"m-{prefix}", {}))
    monkeypatch.setattr(task_endpoint, "resolve_utility_fallback_candidates", lambda *a, **k: [], raising=False)
    got = task_endpoint.resolve_task_candidates()
    assert calls[0] == "task" and got[0][1] == "m-task"
