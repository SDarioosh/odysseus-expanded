"""Fork: the phone relay through Odysseus and pairing by QR code
(routes/sound_relay_routes.py, src/phone_pairing.py, src/call_recorder.py)."""
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.middleware import require_admin
from routes.sound_relay_routes import setup_sound_relay_routes
from src import call_recorder as rec
from src import phone_pairing as pp

ROOT = Path(__file__).resolve().parents[1]
MCP_TOKEN = "m" * 32
ROWS = [("srv1", "convo", f"http://module:8001/mcp?access_token={MCP_TOKEN}", True)]
NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class Module:
    """A fake odysseus-sound MCP HTTP server: answers by path, records requests."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.get(f"{request.method} {request.url.path}")
        if answer is not None:
            return answer
        if request.url.path == "/ingest/chunk":
            return httpx.Response(200, json={"ok": True, "stop": False, "reason": None,
                                             "duplicate": False, "session_id": "s",
                                             "sequence": 0})
        return httpx.Response(200, json={"ok": True, "path": request.url.path})


def make_app(monkeypatch, module: Module | None = None, admin: bool = True) -> TestClient:
    monkeypatch.delenv(rec.SERVER_ENV, raising=False)
    monkeypatch.setattr(rec, "_server_rows", lambda: ROWS)
    monkeypatch.setattr(rec, "datetime", _FixedDatetime)
    module = module or Module()
    real_client = rec._client
    monkeypatch.setattr(rec, "_client", lambda transport: real_client(
        httpx.MockTransport(module)))
    monkeypatch.setattr("core.middleware.auth_disabled", lambda: False)
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        # Like app.py's AuthMiddleware: "X-Test-Scopes" stands in for an ody_ token.
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        if scopes is not None:
            request.state.current_user = "api"
            request.state.api_token_owner = "owner"
            request.state.api_token_scopes = [s for s in scopes.split(",") if s]
        else:
            request.state.current_user = "owner"
        return await call_next(request)

    app.include_router(setup_sound_relay_routes())
    if admin:
        app.dependency_overrides[require_admin] = lambda: None
        monkeypatch.setattr("routes.sound_relay_routes.require_admin", lambda request: None)
    client = TestClient(app)
    client.module = module
    return client


def relay_chunk(c, scopes="sound:ingest", **form):
    data = {"device": "twatch", "session_id": "w-1", "sequence": "3",
            "timestamp": "2026-10-20T10:00:00Z", "codec": "ogg", **form}
    headers = {} if scopes is None else {"X-Test-Scopes": scopes}
    return c.post("/api/codex/sound/chunk", data=data, headers=headers,
                  files={"audio": ("w-1-3.opus", b"OggS....", "audio/ogg")})


# ----------------------------------------------------------------------- scopes
def test_new_scopes_are_allowed_and_listed():
    from routes.api_token_routes import ALLOWED_SCOPES

    assert {"sound:ingest", "sound:read", "sound:write"} <= ALLOWED_SCOPES
    assert set(pp.RELAY_SCOPES) <= ALLOWED_SCOPES
    admin_js = (ROOT / "static/js/admin.js").read_text()
    for scope in ("sound:ingest", "sound:read", "sound:write"):
        assert f"key: '{scope}'" in admin_js


def test_hooks_are_in_app():
    app_py = (ROOT / "app.py").read_text()
    assert 'AUTH_EXEMPT_EXACT.add("/api/pair/redeem")' in app_py
    assert "app.include_router(setup_sound_relay_routes())" in app_py


# ----------------------------------------------------------------------- relay
def test_relay_chunk_forwards_with_device_and_kind(monkeypatch):
    c = make_app(monkeypatch)
    r = relay_chunk(c, kind="note")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["duplicate"] is False
    (req,) = c.module.requests
    assert req.url.path == "/ingest/chunk" and dict(req.url.params) == {"access_token": MCP_TOKEN}
    body = req.read()
    for field, value in [("device", "twatch"), ("kind", "note"), ("codec", "ogg"),
                         ("timestamp", "2026-10-20T10:00:00+00:00"), ("sequence", "3")]:
        assert f'name="{field}"\r\n\r\n{value}\r\n'.encode() in body


@pytest.mark.parametrize("scopes, status", [
    ("calendar:read", 403), ("sound:read", 403), ("", 403), ("sound:ingest", 200),
])
def test_relay_chunk_needs_sound_ingest(monkeypatch, scopes, status):
    c = make_app(monkeypatch)
    assert relay_chunk(c, scopes=scopes).status_code == status


def test_relay_rejects_bad_pieces_before_the_module(monkeypatch):
    c = make_app(monkeypatch)
    assert relay_chunk(c, codec="webm").status_code == 400
    assert relay_chunk(c, device="bad device").status_code == 400
    assert relay_chunk(c, kind="podcast").status_code == 400
    assert relay_chunk(c, session_id="../x").status_code == 400
    assert relay_chunk(c, timestamp="2025-01-01T00:00:00Z").status_code == 400
    assert c.module.requests == []


def test_relay_passes_module_errors(monkeypatch):
    module = Module()
    module.answers["POST /ingest/chunk"] = httpx.Response(
        400, json={"detail": "session w-1 is a note session, not conversation"})
    c = make_app(monkeypatch, module)
    r = relay_chunk(c)
    assert r.status_code == 400 and "note session" in r.json()["detail"]
    # Odysseus's own MCP token refused: 502 (retry later), never the phone's 401.
    module.answers["POST /ingest/chunk"] = httpx.Response(401, text="unauthorized")
    assert relay_chunk(c).status_code == 502


def test_relay_end_session(monkeypatch):
    module = Module()
    c = make_app(monkeypatch, module)
    h = {"X-Test-Scopes": "sound:ingest"}
    assert c.post("/api/codex/sound/sessions/w-1/end", headers=h).json() == {
        "ok": True, "stop": True}
    module.answers["POST /sessions/w-2/end"] = httpx.Response(
        404, json={"detail": "session not found"})
    assert c.post("/api/codex/sound/sessions/w-2/end", headers=h).status_code == 404
    assert c.post("/api/codex/sound/sessions/w-1/end",
                  headers={"X-Test-Scopes": "sound:read"}).status_code == 403


def test_relay_reads_and_writes(monkeypatch):
    c = make_app(monkeypatch)
    read = {"X-Test-Scopes": "sound:read"}
    write = {"X-Test-Scopes": "sound:write"}
    assert c.get("/api/codex/sound/health", headers={"X-Test-Scopes": "sound:ingest"}
                 ).json()["path"] == "/health"
    r = c.get("/api/codex/sound/conversations?since=100&limit=5", headers=read)
    assert r.json()["path"] == "/conversations"
    assert dict(c.module.requests[-1].url.params) == {
        "since": "100", "limit": "5", "access_token": MCP_TOKEN}
    assert c.get("/api/codex/sound/conversations/c:1 x/transcript", headers=read
                 ).status_code == 200
    assert c.module.requests[-1].url.raw_path.startswith(b"/conversations/c%3A1%20x/transcript")
    assert c.get("/api/codex/sound/action-items", headers=read).status_code == 200
    assert c.get("/api/codex/sound/digests/2026-10-21", headers=read).status_code == 200
    # Writes need sound:write.
    assert c.patch("/api/codex/sound/action-items/4", json={"status": "done"},
                   headers=read).status_code == 403
    r = c.patch("/api/codex/sound/action-items/4", json={"status": "done"}, headers=write)
    assert r.status_code == 200 and c.module.requests[-1].read() == b'{"status":"done"}'
    assert c.post("/api/codex/sound/digests/2026-10-21", headers=read).status_code == 403
    assert c.post("/api/codex/sound/digests/2026-10-21", headers=write).status_code == 200
    assert c.get("/api/codex/sound/conversations", headers={"X-Test-Scopes": "sound:ingest"}
                 ).status_code == 403


def test_relay_routes_need_admin_without_a_token(monkeypatch):
    c = make_app(monkeypatch, admin=False)
    c.app.state.auth_manager = None  # no admin session
    assert relay_chunk(c, scopes=None).status_code == 403
    assert c.get("/api/codex/sound/conversations").status_code == 403


def test_relay_without_module_says_so(monkeypatch):
    c = make_app(monkeypatch)
    monkeypatch.setattr(rec, "_server_rows", lambda: [])
    r = relay_chunk(c)
    assert r.status_code == 503 and "No MCP server" in r.json()["detail"]


def test_token_calendars(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database as dbm

    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    dbm.CalendarCal.__table__.create(engine)
    session = sessionmaker(bind=engine)
    monkeypatch.setattr(dbm, "SessionLocal", session)
    with session() as db:
        db.add(dbm.CalendarCal(id="cal-1", owner="owner", name="Personal", source="caldav"))
        db.add(dbm.CalendarCal(id="cal-2", owner="someone-else", name="Theirs", source="local"))
        db.commit()
    c = make_app(monkeypatch)
    r = c.get("/api/codex/calendars", headers={"X-Test-Scopes": "calendar:read"})
    assert r.json() == {"calendars": [{"name": "Personal", "href": "cal-1", "source": "caldav"}]}
    assert c.get("/api/codex/calendars", headers={"X-Test-Scopes": "sound:read"}
                 ).status_code == 403


# ----------------------------------------------------------------------- pairing
class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def tokens():
    made = []

    def make(owner, name, scopes):
        made.append((owner, name, scopes))
        return {"id": f"t{len(made)}", "token": f"ody_secret{len(made)}"}

    return made, make


def test_pairing_code_is_single_use_and_hashed():
    pairs = pp.Pairings(clock=Clock())
    pending, code = pairs.create("owner", "https://ody.tail1.ts.net/")
    assert pending.url == "https://ody.tail1.ts.net"
    assert code not in repr(pairs._by_hash) and pp._hash(code) in pairs._by_hash
    made, make = tokens()
    out = pairs.redeem(code, "1.2.3.4", make)
    assert out["token"] == "ody_secret1" and out["url"] == "https://ody.tail1.ts.net"
    assert out["scopes"] == pp.RELAY_SCOPES and out["name"].startswith("Phone relay ")
    assert made == [("owner", out["name"], pp.RELAY_SCOPES)]
    with pytest.raises(pp.PairingError) as e:
        pairs.redeem(code, "1.2.3.4", make)
    assert e.value.status == 400 and len(made) == 1
    assert pairs.state(pending.id, "owner") == {"status": "paired", "token_name": out["name"]}
    assert pairs.state(pending.id, "someone-else") == {"status": "unknown"}


def test_pairing_code_expires():
    clock = Clock()
    pairs = pp.Pairings(clock=clock)
    pending, code = pairs.create("owner", "http://100.64.0.5:7000")
    assert pairs.state(pending.id, "owner")["status"] == "waiting"
    clock.now += pp.CODE_TTL + 1
    assert pairs.state(pending.id, "owner") == {"status": "expired"}
    made, make = tokens()
    with pytest.raises(pp.PairingError) as e:
        pairs.redeem(code, "1.2.3.4", make)
    assert e.value.status == 400 and made == []


def test_unknown_code_gets_the_same_answer():
    pairs = pp.Pairings(clock=Clock())
    _, make = tokens()
    with pytest.raises(pp.PairingError) as e:
        pairs.redeem("not-a-code", "1.2.3.4", make)
    assert e.value.status == 400 and "not valid any more" in e.value.message


def test_redeem_is_rate_limited():
    clock = Clock()
    pairs = pp.Pairings(clock=clock, rate=pp.RateLimit(per_client=3, overall=5, window=60))
    _, make = tokens()
    for _ in range(3):
        with pytest.raises(pp.PairingError) as e:
            pairs.redeem("guess", "1.1.1.1", make)
        assert e.value.status == 400
    _, code = pairs.create("owner", "https://o.ts.net")
    with pytest.raises(pp.PairingError) as e:  # even a good code waits once limited
        pairs.redeem(code, "1.1.1.1", make)
    assert e.value.status == 429
    for _ in range(2):
        with pytest.raises(pp.PairingError):
            pairs.redeem("guess", "2.2.2.2", make)
    with pytest.raises(pp.PairingError) as e:  # overall limit
        pairs.redeem(code, "3.3.3.3", make)
    assert e.value.status == 429
    clock.now += 61
    assert pairs.redeem(code, "1.1.1.1", make)["token"]


@pytest.mark.parametrize("url", [
    "ftp://x", "odysseus", "https://user:pw@h.ts.net", "https://h.ts.net/?a=1", "",
])
def test_pairing_url_is_checked(url):
    with pytest.raises(pp.PairingError):
        pp.Pairings(clock=Clock()).create("owner", url)


def test_pairing_link_round_trip():
    link = pp.pairing_link("https://ody.tail1.ts.net:8443/x", "abc-_123")
    assert link.startswith("odysseus-pair:v1?url=https%3A%2F%2F")
    assert pp.parse_link(link) == ("https://ody.tail1.ts.net:8443/x", "abc-_123")


def test_qr_png_is_rendered_locally():
    import base64

    uri = pp.qr_png(pp.pairing_link("https://o.ts.net", "code"))
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"


def test_pair_routes(monkeypatch):
    pairs = pp.Pairings(clock=Clock())
    monkeypatch.setattr(pp, "PAIRINGS", pairs)
    made, make = tokens()
    monkeypatch.setattr(pp, "create_api_token", make)
    c = make_app(monkeypatch)
    invalidated = []
    c.app.state.invalidate_token_cache = lambda: invalidated.append(1)
    r = c.post("/api/modules/sound/pair", json={"url": "https://ody.tail1.ts.net"})
    assert r.status_code == 200
    data = r.json()
    assert data["qr"].startswith("data:image/png;base64,") and data["expires_in"] == 600
    assert data["module"] == {"configured": True, "problem": None}
    _, code = pp.parse_link(data["link"])
    assert c.get(f"/api/modules/sound/pair/{data['id']}").json()["status"] == "waiting"

    r = c.post("/api/pair/redeem", json={"code": code})
    assert r.status_code == 200 and r.json()["token"] == "ody_secret1"
    assert r.headers["cache-control"] == "no-store" and invalidated == [1]
    again = c.post("/api/pair/redeem", json={"code": code})
    assert again.status_code == 400 and "token" not in again.json()
    assert c.get(f"/api/modules/sound/pair/{data['id']}").json()["status"] == "paired"


def test_pair_creation_is_admin_only(monkeypatch):
    c = make_app(monkeypatch, admin=False)
    c.app.state.auth_manager = None
    assert c.post("/api/modules/sound/pair", json={"url": "https://o.ts.net"}).status_code == 403
    # An API token (even one with every relay scope) can't make codes.
    assert c.post("/api/modules/sound/pair", json={"url": "https://o.ts.net"},
                  headers={"X-Test-Scopes": ",".join(pp.RELAY_SCOPES)}).status_code == 403


def test_redeem_never_logs_the_code_or_token(monkeypatch, caplog):
    pairs = pp.Pairings(clock=Clock())
    monkeypatch.setattr(pp, "PAIRINGS", pairs)
    _, make = tokens()
    monkeypatch.setattr(pp, "create_api_token", make)
    c = make_app(monkeypatch)
    _, code = pairs.create("owner", "https://o.ts.net")
    with caplog.at_level("DEBUG"):
        assert c.post("/api/pair/redeem", json={"code": code}).status_code == 200
        c.post("/api/pair/redeem", json={"code": code})
    assert code not in caplog.text and "ody_secret" not in caplog.text


def test_pair_page_is_served(monkeypatch):
    c = make_app(monkeypatch)
    r = c.get("/modules/sound/pair")
    assert r.status_code == 200 and "Pair a phone" in r.text and "phonePairing.js" in r.text
    assert "<script src=\"http" not in r.text  # no CDN scripts
