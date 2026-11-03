"""Fork: "Set up Home Assistant" (fork/voice/ha_setup.py, ha_routes.py, ha_settings.py,
scripts/ha_voice_setup.py), against a fake Home Assistant: its REST API through an httpx
MockTransport, its websocket API through a fake connection. Fictional data only."""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fork.voice import ha_routes, ha_setup

HA = "http://ha.example.test:8123"
ODY = "https://odysseus.tail0000.ts.net"
ADMIN_TOKEN = "ha-admin-token-fake"
VOICE = "ody_voice_fake"
DOMAIN = ha_setup.DOMAIN
PIPELINE_REQUIRED = ("conversation_engine", "conversation_language", "language", "name",
                     "stt_engine", "stt_language", "tts_engine", "tts_language", "tts_voice",
                     "wake_word_entity", "wake_word_id")


class FakeHA:
    """Just enough of Home Assistant (2026.x, Extended OpenAI Conversation 3.0.0)."""

    def __init__(self, *, admin=True, eoc=True, speech=True, reach="ok"):
        self.admin, self.eoc, self.reach = admin, eoc, reach
        self.entries: dict[str, dict] = {}
        self.registry: list[dict] = [
            {"entity_id": "light.kitchen", "platform": "hue", "config_entry_id": "hue1"},
            {"entity_id": "tts.google_translate_en_com", "platform": "google_translate"},
        ]
        if speech:
            self.registry += [
                {"entity_id": "stt.faster_whisper", "platform": "wyoming",
                 "config_entry_id": "w1", "original_name": None},
                {"entity_id": "tts.piper", "platform": "wyoming", "config_entry_id": "w2"},
            ]
        self.pipelines: list[dict] = [{
            "id": "p-default", "name": "Home Assistant", "language": "en",
            "conversation_engine": "conversation.home_assistant", "conversation_language": "en",
            "stt_engine": None, "stt_language": None, "tts_engine": None, "tts_language": None,
            "tts_voice": None, "wake_word_entity": None, "wake_word_id": None,
            "prefer_local_intents": False}]
        self.preferred = "p-default"
        self.flows: dict[str, dict] = {}
        self.calls: list[str] = []
        self.revoked: list[str] = []
        self.counter = 0

    def _id(self, prefix):
        self.counter += 1
        return f"{prefix}{self.counter}"

    # ------------------------------------------------------------------ REST
    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append(f"{method} {path}")
        if path == "/auth/token":
            form = parse_qs(request.content.decode())
            if form.get("code") == ["good-code"] and form.get("client_id") == [ODY + "/"]:
                return httpx.Response(200, json={"access_token": ADMIN_TOKEN,
                                                 "refresh_token": "ha-refresh-fake",
                                                 "token_type": "Bearer", "expires_in": 1800})
            return httpx.Response(400, json={"error": "invalid_request",
                                             "error_description": "Invalid code"})
        if path == "/auth/revoke":
            self.revoked += parse_qs(request.content.decode()).get("token", [])
            return httpx.Response(200)
        if request.headers.get("authorization") != f"Bearer {ADMIN_TOKEN}":
            return httpx.Response(401, text="401: Unauthorized")
        body = json.loads(request.content) if request.content else None
        if path == "/api/config":
            return httpx.Response(200, json={"version": "2026.11.0"})
        if path == "/api/config/config_entries/entry" and method == "GET":
            domain = request.url.params.get("domain")
            return httpx.Response(200, json=[
                {k: v for k, v in e.items() if k not in ("data", "subentries")}
                for e in self.entries.values() if domain in (None, e["domain"])])
        m = re.fullmatch(r"/api/config/config_entries/entry/([^/]+)", path)
        if m and method == "DELETE":
            if not self.admin:
                return httpx.Response(403)
            self.entries.pop(m.group(1))
            self.registry = [e for e in self.registry if e.get("config_entry_id") != m.group(1)]
            return httpx.Response(200, json={"require_restart": False})
        if path == "/api/config/config_entries/flow" and method == "POST":
            if not self.admin:
                return httpx.Response(401, text="Unauthorized")
            if body["handler"] != DOMAIN or not self.eoc:
                return httpx.Response(404, json={"message": "Invalid handler specified"})
            flow_id = self._id("flow")
            self.flows[flow_id] = {"kind": "entry"}
            return httpx.Response(200, json={
                "type": "form", "flow_id": flow_id, "handler": DOMAIN, "step_id": "user",
                "errors": None, "data_schema": [{"name": n} for n in (
                    "name", "api_key", "base_url", "api_version", "organization",
                    "skip_authentication", "follow_redirects", "api_provider")]})
        m = re.fullmatch(r"/api/config/config_entries/flow/([^/]+)", path)
        if m and method == "DELETE":
            self.flows.pop(m.group(1), None)
            return httpx.Response(200, json={})
        if m and method == "POST":
            self.flows.pop(m.group(1))
            if self.reach != "ok" and not body.get("skip_authentication"):
                return httpx.Response(200, json={"type": "form", "flow_id": m.group(1),
                                                 "errors": {"base": self.reach}})
            return httpx.Response(200, json={"type": "create_entry", "result": self._add_entry(body)})
        if path == "/api/config/config_entries/subentries/flow" and method == "POST":
            entry_id, kind = body["handler"]
            flow_id = self._id("subflow")
            self.flows[flow_id] = {"kind": kind, "entry_id": entry_id}
            # no skills are installed, so EOC leaves "skills" out of its form
            return httpx.Response(200, json={
                "type": "form", "flow_id": flow_id, "step_id": "init",
                "data_schema": [{"name": n} for n in (
                    "name", "prompt", "chat_model", "max_tokens",
                    "max_function_calls_per_conversation", "functions", "context_threshold",
                    "context_truncate_strategy", "advanced_options")]})
        m = re.fullmatch(r"/api/config/config_entries/subentries/flow/([^/]+)", path)
        if m and method == "POST":
            flow = self.flows.pop(m.group(1))
            allowed = {"name", "prompt", "chat_model", "max_tokens", "functions",
                       "max_function_calls_per_conversation", "context_threshold",
                       "context_truncate_strategy", "advanced_options"}
            if set(body) - allowed:
                return httpx.Response(400, json={"message": "extra keys not allowed"})
            data = dict(body)
            title = data.pop("name")
            self._add_subentry(flow["entry_id"], "conversation", title, data)
            return httpx.Response(200, json={"type": "create_entry", "title": title})
        return httpx.Response(404, text=f"no fake for {method} {path}")

    def _add_entry(self, data):
        entry_id = self._id("entry")
        self.entries[entry_id] = {"entry_id": entry_id, "domain": DOMAIN,
                                  "title": data.get("name", "ChatGPT"), "state": "loaded",
                                  "data": dict(data), "subentries": {}}
        self._add_subentry(entry_id, "conversation", "Extended OpenAI Conversation",
                           {"chat_model": "gpt-5-mini", "functions": "- spec: ..."})
        self._add_subentry(entry_id, "ai_task_data", "Extended OpenAI AI Task", {})
        return {"entry_id": entry_id, "domain": DOMAIN, "title": data.get("name"),
                "state": "loaded"}

    def _add_subentry(self, entry_id, kind, title, data):
        sub_id = self._id("sub")
        self.entries[entry_id]["subentries"][sub_id] = {
            "subentry_id": sub_id, "subentry_type": kind, "title": title, "data": data,
            "unique_id": None}
        domain = "conversation" if kind == "conversation" else "ai_task"
        base = f"{domain}.{re.sub(r'[^a-z0-9]+', '_', title.lower()).strip('_')}"
        entity_id, n = base, 1
        while any(e["entity_id"] == entity_id for e in self.registry):
            n += 1
            entity_id = f"{base}_{n}"
        self.registry.append({"entity_id": entity_id, "platform": DOMAIN,
                              "config_entry_id": entry_id, "config_subentry_id": sub_id})

    # ----------------------------------------------------------------- websocket
    def command(self, msg: dict):
        kind = msg["type"]
        self.calls.append(f"WS {kind}")
        if kind == "auth/current_user":
            return {"id": "u1", "name": "Owner", "is_admin": self.admin}
        if kind == "config/entity_registry/list":
            return [dict(e) for e in self.registry]
        if kind == "config/entity_registry/update":
            for e in self.registry:
                if e["entity_id"] == msg["entity_id"]:
                    e["entity_id"] = msg["new_entity_id"]
            return {}
        if kind == "stt/engine/list":
            return {"providers": [{"engine_id": e["entity_id"], "supported_languages": ["en"]}
                                  for e in self.registry if e["entity_id"].startswith("stt.")]}
        if kind == "tts/engine/list":
            return {"providers": [{"engine_id": e["entity_id"],
                                   "supported_languages": ["en_US", "en_GB"]}
                                  for e in self.registry if e["entity_id"].startswith("tts.")]}
        if kind == "tts/engine/voices":
            assert msg["language"] == "en_US"
            return {"voices": [{"voice_id": "en_US-lessac-medium", "name": "lessac"},
                               {"voice_id": "en_US-amy-low", "name": "amy"}]}
        if kind == "config_entries/subentries/list":
            return [{k: v for k, v in s.items() if k != "data"}
                    for s in self.entries[msg["entry_id"]]["subentries"].values()]
        if kind == "config_entries/subentries/delete":
            self.entries[msg["entry_id"]]["subentries"].pop(msg["subentry_id"])
            self.registry = [e for e in self.registry
                             if e.get("config_subentry_id") != msg["subentry_id"]]
            return None
        if kind == "assist_pipeline/pipeline/list":
            return {"pipelines": [dict(p) for p in self.pipelines],
                    "preferred_pipeline": self.preferred}
        if kind in ("assist_pipeline/pipeline/create", "assist_pipeline/pipeline/update"):
            fields = {k: v for k, v in msg.items() if k not in ("id", "type", "pipeline_id")}
            missing = [k for k in PIPELINE_REQUIRED if k not in fields]
            if missing:
                raise ValueError(f"required key not provided @ data['{missing[0]}']")
            if kind.endswith("create"):
                pipeline = {"id": self._id("p-"), **fields}
                self.pipelines.append(pipeline)
                return pipeline
            pipeline = next(p for p in self.pipelines if p["id"] == msg["pipeline_id"])
            pipeline.update(fields)
            return pipeline
        if kind == "assist_pipeline/pipeline/set_preferred":
            self.preferred = msg["pipeline_id"]
            return None
        raise ValueError(f"unknown command {kind}")

    def connect(self):
        fake = self

        class Conn:
            def __init__(self):
                self.out = [json.dumps({"type": "auth_required", "ha_version": "2026.11.0"})]
                self.authed = False

            async def send(self, raw):
                msg = json.loads(raw)
                if not self.authed:
                    self.authed = msg.get("access_token") == ADMIN_TOKEN
                    self.out.append(json.dumps({"type": "auth_ok" if self.authed
                                                else "auth_invalid"}))
                    return
                try:
                    result = fake.command(msg)
                    self.out.append(json.dumps({"id": msg["id"], "type": "result",
                                                "success": True, "result": result}))
                except ValueError as exc:
                    self.out.append(json.dumps({"id": msg["id"], "type": "result",
                                                "success": False, "error": {
                                                    "code": "invalid_format",
                                                    "message": str(exc)}}))

            async def recv(self):
                return self.out.pop(0)

            async def close(self):
                pass

        async def ws_connect(url):
            fake.calls.append(f"WS-CONNECT {url}")
            return Conn()

        return ws_connect

    def http(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def odysseus_entries(self):
        return [e for e in self.entries.values() if e["title"] == "Odysseus"]


def _setup(fake, voice=VOICE, **kw):
    return asyncio.run(ha_setup.setup_voice(HA, ADMIN_TOKEN, ODY, voice, http=fake.http(),
                                            ws_connect=fake.connect(), pause=0, **kw))


def _status(report):
    return {s["step"]: s["status"] for s in report["steps"]}


# ----------------------------------------------------------------------- setup_voice
def test_first_run_creates_the_agent_pipeline_and_makes_it_preferred():
    fake = FakeHA()
    report = _setup(fake)
    assert report["ok"], report
    assert _status(report) == {"Home Assistant": "ok", "Speech": "ok",
                               "Conversation agent": "changed", "Assist pipeline": "changed",
                               "Preferred assistant": "changed"}
    [entry] = fake.odysseus_entries()
    assert entry["data"]["api_key"] == VOICE
    assert entry["data"]["base_url"] == ODY + "/api/chat/voice/v1"
    assert entry["data"]["skip_authentication"] is False
    # only our agent is left: no GPT default agent, no AI task
    [sub] = entry["subentries"].values()
    assert sub["title"] == "Odysseus" and sub["subentry_type"] == "conversation"
    assert sub["data"]["chat_model"] == "odysseus"
    assert sub["data"]["functions"] == "[]"  # "" would bring back EOC's default tools
    assert "skills" not in sub["data"]  # not in the form, so not sent
    assert report["conversation_entity"] == "conversation.odysseus"
    pipeline = next(p for p in fake.pipelines if p["name"] == "Odysseus")
    assert pipeline == {
        "id": pipeline["id"], "name": "Odysseus", "language": "en",
        "conversation_engine": "conversation.odysseus", "conversation_language": "*",
        "stt_engine": "stt.faster_whisper", "stt_language": "en",
        "tts_engine": "tts.piper", "tts_language": "en_US", "tts_voice": "en_US-lessac-medium",
        "wake_word_entity": None, "wake_word_id": None, "prefer_local_intents": True}
    assert fake.preferred == pipeline["id"] == report["pipeline_id"]
    assert "WS-CONNECT ws://ha.example.test:8123/api/websocket" in fake.calls


def test_a_rerun_replaces_the_agent_and_updates_the_same_pipeline():
    fake = FakeHA()
    _setup(fake, voice="ody_voice_old")
    first = next(p["id"] for p in fake.pipelines if p["name"] == "Odysseus")
    fake.pipelines[-1]["wake_word_id"] = "hey_athena"  # set by hand in HA: kept
    report = _setup(fake)
    assert report["ok"], report
    [entry] = fake.odysseus_entries()  # one agent, with the new key
    assert entry["data"]["api_key"] == VOICE
    assert [p["name"] for p in fake.pipelines].count("Odysseus") == 1
    pipeline = next(p for p in fake.pipelines if p["name"] == "Odysseus")
    assert pipeline["id"] == first and pipeline["wake_word_id"] == "hey_athena"
    # the new agent got conversation.odysseus back once the old one was gone
    assert pipeline["conversation_engine"] == "conversation.odysseus"
    assert _status(report)["Assist pipeline"] == "ok"
    assert _status(report)["Preferred assistant"] == "ok"
    assert "replaced 1 older" in report["steps"][2]["detail"]


def test_missing_whisper_or_piper_stops_before_anything_changes():
    fake = FakeHA(speech=False)
    report = _setup(fake)
    assert not report["ok"]
    assert _status(report) == {"Home Assistant": "ok", "Speech": "error",
                               "Conversation agent": "skipped", "Assist pipeline": "skipped",
                               "Preferred assistant": "skipped"}
    assert "install the whisper and piper add-ons" in report["steps"][1]["detail"].lower()
    assert not fake.entries and len(fake.pipelines) == 1
    assert report["agent_created"] is False


def test_missing_integration_and_unreachable_odysseus_are_explained():
    report = _setup(FakeHA(eoc=False))
    assert "Install Extended OpenAI Conversation" in report["steps"][2]["detail"]
    fake = FakeHA(reach="cannot_connect")
    report = _setup(fake)
    assert "couldn't reach Odysseus at " + ODY in report["steps"][2]["detail"]
    assert not fake.entries and not fake.flows  # the flow was aborted
    assert report["agent_created"] is False


def test_validate_false_lets_ha_skip_its_key_check():
    fake = FakeHA(reach="invalid_auth")
    assert _setup(fake, validate=False)["ok"]
    assert fake.odysseus_entries()[0]["data"]["skip_authentication"] is True


def test_a_non_admin_or_a_bad_token_is_refused():
    report = _setup(FakeHA(admin=False))
    assert report["steps"][0] == {"step": "Home Assistant", "status": "error",
                                  "detail": "Sign in to Home Assistant with an administrator account."}
    fake = FakeHA()
    report = asyncio.run(ha_setup.setup_voice(HA, "wrong", ODY, VOICE, http=fake.http(),
                                              ws_connect=fake.connect()))
    assert report["steps"][0]["status"] == "error"
    assert "didn't accept the sign-in" in report["steps"][0]["detail"]


@pytest.mark.parametrize("url", ["ftp://ha.example.test", "http://user:pw@ha.example.test",
                                 "http://ha.example.test/?x=1", "http://ha.example.test/lovelace",
                                 "http://ha.example.test#x", "ha.example.test", ""])
def test_bad_ha_addresses_are_refused(url):
    with pytest.raises(ValueError):
        ha_setup.check_ha_url(url)


def test_ha_address_is_normalised():
    assert ha_setup.check_ha_url(" HTTP://HA.Example.test:8123/ ") == "http://ha.example.test:8123"
    assert ha_setup.voice_base_url(ODY + "/") == ODY + "/api/chat/voice/v1"
    with pytest.raises(ValueError):
        ha_setup.voice_base_url("http://odysseus.example.test/?q=1")


# ---------------------------------------------------------------------------- tokens
@pytest.fixture
def token_db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from core.database import ApiToken

    # one in-memory database for every thread (run_setup uses worker threads)
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    ApiToken.__table__.create(engine)
    Session = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def session():
        db = Session()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    monkeypatch.setattr("core.database.get_db_session", session)

    def rows():
        with session() as db:
            return sorted((t.owner, t.name, t.scopes) for t in db.query(ApiToken).all())

    def add(id_, owner, name, scopes):
        with session() as db:
            db.add(ApiToken(id=id_, owner=owner, name=name, token_hash="x", token_prefix="ody_x",
                            scopes=scopes, is_active=True))

    return SimpleNamespace(rows=rows, add=add)


def test_old_home_assistant_tokens_go_once_the_new_one_is_in_use(token_db):
    token_db.add("old1", "alice", "Home Assistant 2026-10-01", "voice")
    token_db.add("man1", "alice", "Voice assistant 2026-10-01", "voice")  # made by hand
    token_db.add("odd1", "alice", "Home Assistant 2026-10-02", "voice,chat")  # not ours
    token_db.add("bob1", "bob", "Home Assistant 2026-10-01", "voice")  # someone else's
    fake = FakeHA()
    invalidated = []
    report = asyncio.run(ha_setup.run_setup(
        "alice", HA, ADMIN_TOKEN, ODY, invalidate=lambda: invalidated.append(1),
        http=fake.http(), ws_connect=fake.connect(), pause=0))
    assert report["ok"] and report["old_tokens_deleted"] == 1
    names = [r[1] for r in token_db.rows() if r[0] == "alice"]
    assert ha_setup.token_name() in names and "Home Assistant 2026-10-01" not in names
    assert "Voice assistant 2026-10-01" in names and "Home Assistant 2026-10-02" in names
    assert ("bob", "Home Assistant 2026-10-01", "voice") in token_db.rows()
    new_key = fake.odysseus_entries()[0]["data"]["api_key"]
    assert new_key.startswith("ody_") and new_key != VOICE
    assert ("alice", ha_setup.token_name(), "voice") in token_db.rows()
    assert invalidated  # the app's token cache learns about the change


def test_a_token_home_assistant_never_got_is_deleted_again(token_db):
    token_db.add("old1", "alice", "Home Assistant 2026-10-01", "voice")
    fake = FakeHA(speech=False)
    report = asyncio.run(ha_setup.run_setup("alice", HA, ADMIN_TOKEN, ODY, http=fake.http(),
                                            ws_connect=fake.connect(), pause=0))
    assert not report["ok"] and report["token_name"] == ""
    assert token_db.rows() == [("alice", "Home Assistant 2026-10-01", "voice")]


# --------------------------------------------------------------------------- settings
def test_ha_address_is_saved_in_the_settings_file(tmp_path, monkeypatch):
    from fork.voice import ha_settings
    from src import settings

    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings._invalidate_caches()
    try:
        assert ha_settings.get_ha_url() == ""
        assert ha_settings.set_ha_url("http://HA.example.test:8123/") == "http://ha.example.test:8123"
        assert ha_settings.get_ha_url() == "http://ha.example.test:8123"
        saved = json.loads((tmp_path / "settings.json").read_text())
        assert saved[ha_settings.SETTING_KEY] == "http://ha.example.test:8123"
        with pytest.raises(ValueError):
            ha_settings.set_ha_url("http://ha.example.test/?x")
        ha_settings.set_ha_url("")
        assert ha_settings.get_ha_url() == ""
    finally:
        settings._invalidate_caches()


# ---------------------------------------------------------------------- the routes
@pytest.fixture
def web(monkeypatch):
    """The routes on a bare app; a header the fake auth reads says who is signed in."""
    from fastapi import HTTPException

    def require_admin(request):
        if request.headers.get("x-test-user") != "alice":
            raise HTTPException(403, "Admin only")

    monkeypatch.setattr("core.middleware.require_admin", require_admin)
    monkeypatch.setattr("src.auth_helpers.get_current_user",
                        lambda request: request.headers.get("x-test-user"))
    monkeypatch.setattr(ha_routes, "STATES", ha_routes.States())
    monkeypatch.setattr(ha_routes, "RUNS", {})
    saved = []
    monkeypatch.setattr("fork.voice.ha_settings.set_ha_url", saved.append)
    monkeypatch.setattr("fork.voice.ha_settings.get_ha_url", lambda: saved[-1] if saved else "")
    fake = FakeHA()
    runs = []

    async def fake_run_setup(owner, ha_url, access_token, odysseus_url, **kw):
        runs.append((owner, ha_url, access_token, odysseus_url))
        if fake.reach == "boom":
            raise RuntimeError("boom")
        return {"ok": True, "steps": [{"step": "Speech", "status": "ok", "detail": "<b>x</b>"}],
                "token_name": "Home Assistant 2026-11-03"}

    monkeypatch.setattr(ha_setup, "run_setup", fake_run_setup)
    real_exchange, real_revoke = ha_setup.exchange_code, ha_setup.revoke
    monkeypatch.setattr(ha_setup, "exchange_code", lambda *a, **k: real_exchange(
        *a, http=fake.http(), **k))
    monkeypatch.setattr(ha_setup, "revoke", lambda *a, **k: real_revoke(*a, http=fake.http(), **k))

    app = FastAPI()
    app.state.invalidate_token_cache = lambda: None

    @app.middleware("http")
    async def fake_token_auth(request, call_next):
        request.state.api_token = request.headers.get("authorization", "").startswith("Bearer")
        return await call_next(request)

    app.include_router(ha_routes.setup_ha_routes())
    client = TestClient(app, base_url=ODY)
    same = {"x-test-user": "alice", "origin": ODY, "sec-fetch-site": "same-origin"}
    return SimpleNamespace(client=client, fake=fake, runs=runs, saved=saved, same=same)


def _connect(web, **body):
    return web.client.post("/api/fork/voice/ha/connect", headers=web.same,
                           json={"ha_url": HA, **body})


def test_connect_sends_the_browser_to_ha_login_with_our_origin(web):
    res = _connect(web)
    assert res.status_code == 200, res.text
    url = urlsplit(res.json()["authorize_url"])
    query = parse_qs(url.query)
    assert f"{url.scheme}://{url.netloc}{url.path}" == HA + "/auth/authorize"
    assert query["client_id"] == [ODY + "/"]
    assert query["redirect_uri"] == [ODY + "/api/fork/voice/ha/callback"]
    assert query["response_type"] == ["code"] and len(query["state"][0]) >= 30


def test_callback_sets_up_revokes_and_saves(web):
    state = parse_qs(urlsplit(_connect(web, odysseus_url="http://192.168.1.5:7000").json()
                              ["authorize_url"]).query)["state"][0]
    res = web.client.get("/api/fork/voice/ha/callback",
                         params={"code": "good-code", "state": state}, headers=web.same)
    assert res.status_code == 200, res.text
    assert web.runs == [("alice", HA, ADMIN_TOKEN, "http://192.168.1.5:7000")]
    assert web.fake.revoked == ["ha-refresh-fake"]
    assert web.saved == [HA]
    assert "&lt;b&gt;x&lt;/b&gt;" in res.text and "<b>x</b>" not in res.text  # escaped
    assert res.headers["cache-control"] == "no-store"
    # the state is used up
    again = web.client.get("/api/fork/voice/ha/callback",
                           params={"code": "good-code", "state": state}, headers=web.same)
    assert again.status_code == 400 and len(web.runs) == 1
    status = web.client.get("/api/fork/voice/ha", headers=web.same).json()
    assert status["ha_url"] == HA and status["report"]["ok"] and not status["running"]


def test_revoke_happens_even_when_the_setup_fails(web):
    web.fake.reach = "boom"
    state = parse_qs(urlsplit(_connect(web).json()["authorize_url"]).query)["state"][0]
    res = web.client.get("/api/fork/voice/ha/callback",
                         params={"code": "good-code", "state": state}, headers=web.same)
    assert res.status_code == 502 and "Unexpected error" in res.text
    assert web.fake.revoked == ["ha-refresh-fake"]


def test_callback_refuses_unknown_foreign_or_expired_states(web, monkeypatch):
    res = web.client.get("/api/fork/voice/ha/callback",
                         params={"code": "good-code", "state": "made-up"}, headers=web.same)
    assert res.status_code == 400 and not web.runs
    # another admin's state
    state = parse_qs(urlsplit(_connect(web).json()["authorize_url"]).query)["state"][0]
    monkeypatch.setattr("src.auth_helpers.get_current_user", lambda request: "mallory")
    res = web.client.get("/api/fork/voice/ha/callback",
                         params={"code": "good-code", "state": state},
                         headers={**web.same})
    assert res.status_code == 400 and not web.runs
    monkeypatch.setattr("src.auth_helpers.get_current_user",
                        lambda request: request.headers.get("x-test-user"))
    # expired
    state = parse_qs(urlsplit(_connect(web).json()["authorize_url"]).query)["state"][0]
    for pending in ha_routes.STATES._by_hash.values():
        pending.expires = 0
    res = web.client.get("/api/fork/voice/ha/callback",
                         params={"code": "good-code", "state": state}, headers=web.same)
    assert res.status_code == 400 and not web.runs
    # HA said no: nothing runs, nothing to revoke
    state = parse_qs(urlsplit(_connect(web).json()["authorize_url"]).query)["state"][0]
    res = web.client.get("/api/fork/voice/ha/callback",
                         params={"error": "access_denied", "state": state}, headers=web.same)
    assert res.status_code == 400 and "access_denied" in res.text and not web.runs


def test_a_bad_code_is_reported_and_nothing_runs(web):
    state = parse_qs(urlsplit(_connect(web).json()["authorize_url"]).query)["state"][0]
    res = web.client.get("/api/fork/voice/ha/callback",
                         params={"code": "bad-code", "state": state}, headers=web.same)
    assert res.status_code == 502 and "Invalid code" in res.text
    assert not web.runs and not web.saved


def test_only_a_same_origin_admin_session_may_use_the_routes(web):
    for headers in ({**web.same, "x-test-user": "bob"},  # not an admin
                    {**web.same, "authorization": "Bearer ody_token"},  # an API token
                    {**web.same, "sec-fetch-site": "cross-site"},  # another site
                    {**web.same, "sec-fetch-site": "same-site"},  # another tailnet service
                    {"x-test-user": "alice"}):  # no Origin at all
        res = web.client.post("/api/fork/voice/ha/connect", headers=headers, json={"ha_url": HA})
        assert res.status_code == 403, headers
    assert web.client.get("/api/fork/voice/ha", headers={"x-test-user": "bob"}).status_code == 403
    assert web.client.get("/api/fork/voice/ha/callback", params={"state": "x", "code": "y"},
                          headers={"authorization": "Bearer ody_token"}).status_code == 403
    assert web.client.get("/api/fork/voice/ha/connect", headers=web.same).status_code == 405


def test_connect_checks_the_addresses(web):
    assert _connect(web, ha_url="http://ha.example.test/?x=1").status_code == 400
    assert _connect(web, odysseus_url="javascript:alert(1)").status_code == 400
    # HA refuses a sign-in for a Tailscale IP: say so before sending the owner there
    res = web.client.post("/api/fork/voice/ha/connect", json={"ha_url": HA},
                          headers={**web.same, "origin": "http://100.101.102.103:7000"})
    assert res.status_code == 400 and "by its name" in res.json()["detail"]


# ---------------------------------------------------------------------- the script
def test_script_never_revokes_the_token_it_was_given(monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "ha_voice_setup.py"
    spec = importlib.util.spec_from_file_location("ha_voice_setup_script", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    fake = FakeHA(reach="invalid_auth")  # the running Odysseus doesn't know the token yet
    calls = {}

    async def fake_run_setup(owner, ha_url, token, odysseus_url, **kw):
        calls.update(owner=owner, token=token, **kw)
        return await ha_setup.setup_voice(ha_url, token, odysseus_url, VOICE, http=fake.http(),
                                          ws_connect=fake.connect(), pause=0, **kw) | {
            "token_name": "Home Assistant 2026-11-03"}

    monkeypatch.setattr(ha_setup, "run_setup", fake_run_setup)
    monkeypatch.setattr(script, "known_owner", lambda owner: owner == "alice")
    saved = []
    monkeypatch.setattr("fork.voice.ha_settings.set_ha_url", saved.append)
    monkeypatch.setenv("HA_TOKEN", ADMIN_TOKEN)
    monkeypatch.delenv("VOICE_TOKEN", raising=False)
    code = script.main(["--ha-url", HA, "--ha-token", "argv-token-ignored",
                        "--odysseus-url", ODY, "--owner", "alice"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert calls == {"owner": "alice", "token": ADMIN_TOKEN, "validate": False}
    assert fake.revoked == [] and "/auth/revoke" not in " ".join(fake.calls)
    assert "Restart Odysseus" in out and saved == [HA]
    assert script.main(["--ha-url", HA, "--odysseus-url", ODY, "--owner", "nobody"]) == 2
