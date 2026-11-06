"""Fork: voice satellites (fork/voice/satellite.py, satellite_routes.py) and Home
Assistant announcements (fork/voice/announce.py). Fake Athena and Home Assistant over
httpx.MockTransport; fictional data, a few bytes of silent WAV."""
from __future__ import annotations

import asyncio
import json
import struct
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fork.athena import client as athena
from fork.voice import announce, satellite, satellite_routes
from fork.voice.routes import PREFIX

URL = PREFIX + "/satellite/turn"
VOICE = {"x-test-scopes": "voice"}
OWNER_VOICE = satellite.Speaker(verified=True, is_owner=True, name="Sam", score=0.8)


def wav(seconds: float = 0.5) -> bytes:
    frames = b"\x00\x00" * int(16000 * seconds)
    header = (b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
              + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
              + b"data" + struct.pack("<I", len(frames)))
    return header + frames


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    satellite.forget_all()
    monkeypatch.delenv("FORK_VOICE_PRIVATE", raising=False)
    yield
    satellite.forget_all()


@pytest.fixture
def api(monkeypatch):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        request.state.api_token_owner = request.headers.get("x-test-owner", "alice") \
            if scopes is not None else None
        request.state.api_token_scopes = scopes.split(",") if scopes else []
        request.state.current_user = "api" if scopes is not None else "alice"
        return await call_next(request)

    seen: list[bytes] = []

    async def fake_identify(audio, **_):
        seen.append(audio)
        return OWNER_VOICE

    monkeypatch.setattr(satellite, "identify", fake_identify)
    app.include_router(satellite_routes.setup_satellite_routes())
    with TestClient(app) as client:
        client.seen = seen
        yield client


FIELDS = {"device_id": "echo-kitchen", "device_name": "Kitchen Echo", "area": "Kitchen",
          "wake_word": "okay_nabu", "started_at": "1762199530000"}


# ------------------------------------------------------------------------ the route
def test_only_a_voice_token_may_post(api):
    assert api.post(URL, data=FIELDS).status_code == 403
    assert api.post(URL, data=FIELDS, headers={"x-test-scopes": "chat"}).status_code == 403


def test_a_turn_without_audio_keeps_the_context(api):
    res = api.post(URL, data=FIELDS, headers=VOICE)
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["speaker"] is None and body["context_id"]
    ctx = asyncio.run(satellite.claim("alice", wait_seconds=0))
    assert ctx == satellite.SatelliteContext(
        context_id=body["context_id"], device_id="echo-kitchen", device_name="Kitchen Echo",
        area="Kitchen", wake_word="okay_nabu", started_at_ms=1762199530000, speaker=None)
    assert api.seen == []


def test_audio_is_checked_and_answered(api, data_dir, caplog):
    audio = wav()
    res = api.post(URL, data=FIELDS, files={"audio": ("c.wav", audio, "audio/wav")},
                   headers=VOICE)
    assert res.status_code == 200
    assert res.json()["speaker"] == {"verified": True, "is_owner": True, "name": "Sam",
                                     "score": 0.8}
    assert api.seen == [audio]
    ctx = asyncio.run(satellite.claim("alice", wait_seconds=0))
    assert ctx.speaker == OWNER_VOICE
    # never stored or logged
    # (the device registry, fork_voice.db, keeps the device's name and room only)
    assert not any(p.is_file() and not p.name.startswith("fork_voice.db")
                   for p in data_dir.rglob("*"))
    assert "RIFF" not in caplog.text
    assert not any(isinstance(v, (bytes, bytearray)) for v in vars(ctx).values())


@pytest.mark.parametrize(("change", "status"), [
    ({"device_id": ""}, 422),
    ({"device_id": "x" * 129}, 422),
    ({"area": "a" * 101}, 422),
    ({"started_at": "yesterday"}, 422),
    ({"started_at": "-5"}, 422),
])
def test_bad_fields_are_refused(api, change, status):
    assert api.post(URL, data={**FIELDS, **change}, headers=VOICE).status_code == status
    assert asyncio.run(satellite.claim("alice", wait_seconds=0)) is None


def test_missing_device_id_and_json_bodies_are_refused(api):
    data = {k: v for k, v in FIELDS.items() if k != "device_id"}
    assert api.post(URL, data=data, headers=VOICE).status_code == 422
    assert api.post(URL, json=FIELDS, headers=VOICE).status_code == 415


def test_audio_must_be_small_wav(api):
    files = {"audio": ("c.mp3", b"ID3" + b"\x00" * 100, "audio/mpeg")}
    assert api.post(URL, data=FIELDS, files=files, headers=VOICE).status_code == 415
    for seconds in (33, 40):  # over 1 MiB of audio; over the whole body's limit
        files = {"audio": ("c.wav", wav(seconds), "audio/wav")}
        assert api.post(URL, data=FIELDS, files=files, headers=VOICE).status_code == 413
    assert api.seen == []


def test_an_empty_audio_file_means_no_audio(api):
    files = {"audio": ("c.wav", b"", "audio/wav")}
    res = api.post(URL, data=FIELDS, files=files, headers=VOICE)
    assert res.status_code == 200 and res.json()["speaker"] is None
    assert api.seen == []


def test_contexts_are_per_owner(api):
    api.post(URL, data=FIELDS, headers={**VOICE, "x-test-owner": "bob"})
    assert asyncio.run(satellite.claim("alice", wait_seconds=0)) is None
    assert asyncio.run(satellite.claim("bob", wait_seconds=0)).device_id == "echo-kitchen"


# ----------------------------------------------------------------------- the seam
async def test_claim_once_newest_first():
    """The newest is claimed; an older unclaimed one is dropped with it, never used by a
    later turn (it belongs to a turn Home Assistant answered itself)."""
    satellite.register("alice", device_id="a")
    satellite.register("alice", device_id="b")
    assert (await satellite.claim("alice", wait_seconds=0)).device_id == "b"
    assert await satellite.claim("alice", wait_seconds=0) is None
    satellite.register("alice", device_id="c")  # a new turn's context is still taken
    assert (await satellite.claim("alice", wait_seconds=0)).device_id == "c"


async def test_a_leftover_context_lends_its_speaker_to_no_later_turn(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(satellite, "_now", lambda: now[0])
    satellite.register("alice", device_id="kitchen")  # HA answered this turn itself
    now[0] += satellite.CLAIM_SECONDS + 0.1  # still within the 30 s it is kept
    assert await satellite.claim("alice", wait_seconds=0) is None
    now[0] += 1.0
    satellite.register("alice", device_id="bedroom")
    assert (await satellite.claim("alice", wait_seconds=0)).device_id == "bedroom"


async def test_contexts_expire_after_30_seconds(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(satellite, "_now", lambda: now[0])
    satellite.register("alice", device_id="a")
    now[0] += satellite.TTL_SECONDS + 0.1
    assert await satellite.claim("alice", wait_seconds=0) is None


async def test_contexts_are_bounded():
    for n in range(satellite.MAX_PER_OWNER + 5):
        satellite.register("alice", device_id=f"d{n}")
    for n in range(satellite.MAX_OWNERS + 3):
        satellite.register(f"owner{n}", device_id="x")
    assert len(satellite._contexts) == satellite.MAX_OWNERS
    found = []
    for n in range(satellite.MAX_OWNERS + 3):
        ctx = await satellite.claim(f"owner{n}", wait_seconds=0)
        found.append(ctx is not None)
    assert found.count(True) == satellite.MAX_OWNERS and found[:3] == [False] * 3


async def test_claim_waits_for_the_speaker_check(monkeypatch):
    release = asyncio.Event()

    async def slow(audio, **_):
        await release.wait()
        return OWNER_VOICE

    monkeypatch.setattr(satellite, "identify", slow)
    satellite.register("alice", device_id="a", audio=b"x")
    asyncio.get_running_loop().call_later(0.05, release.set)
    ctx = await satellite.claim("alice", wait_seconds=2)
    assert ctx.speaker == OWNER_VOICE


async def test_claim_gives_up_waiting(monkeypatch):
    async def never(audio, **_):
        await asyncio.sleep(10)

    monkeypatch.setattr(satellite, "identify", never)
    entry = satellite.register("alice", device_id="a", audio=b"x")
    ctx = await satellite.claim("alice", wait_seconds=0.05)
    assert ctx.speaker is None
    assert await satellite.claim("alice", wait_seconds=0) is None  # claimed once anyway
    entry.task.cancel()


@pytest.mark.parametrize(("mode", "speaker", "allowed"), [
    (None, None, True),
    ("off", None, True),
    ("bogus", None, True),
    ("owner", None, False),
    ("owner", satellite.Speaker(False, False, None, None), False),
    ("owner", satellite.Speaker(True, False, "Lee", 0.7), False),
    ("owner", satellite.Speaker(False, True, "Sam", None), False),
    ("owner", OWNER_VOICE, True),
    (" OWNER ", OWNER_VOICE, True),
])
def test_personal_allowed(monkeypatch, mode, speaker, allowed):
    if mode is not None:
        monkeypatch.setenv("FORK_VOICE_PRIVATE", mode)
    ctx = satellite.SatelliteContext("c", "d", "", "", "", 1, speaker)
    assert satellite.personal_allowed(ctx) is allowed
    if mode == "owner":
        assert satellite.personal_allowed(None) is False


# ---------------------------------------------------------------- Athena's answer
def _athena(monkeypatch, handler):
    monkeypatch.setattr(athena.RECORDER, "link",
                        lambda: athena.Link("http://127.0.0.1:8001", "mcp-test-token"))
    return httpx.MockTransport(handler)


async def test_identify_sends_the_audio_to_the_relay_route(monkeypatch):
    got = {}

    def handler(request):
        got["url"] = str(request.url)
        got["auth"] = request.headers.get("authorization")
        got["body"] = request.read()
        return httpx.Response(200, json={
            "speaker": {"id": "p1", "name": "Sam", "is_owner": True}, "score": 0.71,
            "threshold": 0.6, "speech_seconds": 2.4})

    speaker = await satellite.identify(wav(), transport=_athena(monkeypatch, handler))
    assert speaker == satellite.Speaker(True, True, "Sam", 0.71)
    assert got["url"] == "http://127.0.0.1:8001/speakers/identify"
    assert got["auth"] == "Bearer mcp-test-token"
    assert b'name="audio"' in got["body"] and b"RIFF" in got["body"]


@pytest.mark.parametrize(("answer", "expected"), [
    ({"speaker": None, "score": 0.3, "threshold": 0.6, "speech_seconds": 3},
     satellite.Speaker(True, False, None, 0.3)),
    ({"speaker": None, "score": None, "speech_seconds": 0.4}, None),
    ({"speaker": {"name": "Lee", "is_owner": True}, "score": 0.9, "speech_seconds": 0.5}, None),
    ({"speaker": {"name": "Lee", "is_owner": False}, "score": 0.9},
     satellite.Speaker(True, False, "Lee", 0.9)),
    ("nonsense", None),
])
def test_speaker_from_answer(answer, expected):
    assert satellite.speaker_from_answer(answer) == expected


@pytest.mark.parametrize("status", [404, 405, 401, 500])
async def test_an_older_or_failing_athena_means_no_speaker(monkeypatch, status):
    transport = _athena(monkeypatch, lambda r: httpx.Response(status, json={"detail": "x"}))
    assert await satellite.identify(wav(), transport=transport) is None


async def test_an_athena_timeout_or_no_setup_means_no_speaker(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert await satellite.identify(wav(), transport=_athena(monkeypatch, handler)) is None

    def no_link():
        raise athena.AthenaError(503, "No MCP server", "setup")

    monkeypatch.setattr(athena.RECORDER, "link", no_link)
    assert await satellite.identify(wav()) is None


# ------------------------------------------------------------------ announcements
@pytest.fixture
def ha(monkeypatch):
    monkeypatch.setenv("FORK_HA_URL", "http://192.168.1.20:8123/")
    monkeypatch.setenv("FORK_HA_TOKEN", "ha-test-token")
    monkeypatch.setenv("FORK_VOICE_ANNOUNCE_ENTITIES",
                       "assist_satellite.kitchen_echo, assist_satellite.office")
    sent = []

    def handler(request):
        sent.append(SimpleNamespace(url=str(request.url),
                                    auth=request.headers.get("authorization"),
                                    body=json.loads(request.read())))
        return httpx.Response(200, json=[])

    return SimpleNamespace(sent=sent, transport=httpx.MockTransport(handler))


async def test_announce_posts_the_service_call(ha):
    assert await announce.announce("Reminder:  call   the dentist", transport=ha.transport)
    (call,) = ha.sent
    assert call.url == "http://192.168.1.20:8123/api/services/assist_satellite/announce"
    assert call.auth == "Bearer ha-test-token"
    assert call.body == {"entity_id": ["assist_satellite.kitchen_echo",
                                       "assist_satellite.office"],
                         "message": "Reminder: call the dentist"}
    assert await announce.announce("Hi", ["assist_satellite.bedroom"], transport=ha.transport)
    assert ha.sent[1].body["entity_id"] == ["assist_satellite.bedroom"]


@pytest.mark.parametrize("url", [
    "http://8.8.8.8:8123", "https://ha.example.com", "ftp://192.168.1.20",
    "http://user:pw@192.168.1.20:8123", "http://100.20.0.1:8123",
])
async def test_announce_refuses_hosts_off_the_network(ha, monkeypatch, url):
    async def public(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(announce, "_resolve", public)
    monkeypatch.setenv("FORK_HA_URL", url)
    assert await announce.announce("Hi", transport=ha.transport) is False
    assert ha.sent == []


@pytest.mark.parametrize("url", ["http://127.0.0.1:8123", "http://100.101.102.103:8123",
                                 "http://homeassistant.local:8123"])
async def test_announce_allows_local_and_tailscale_hosts(ha, monkeypatch, url):
    async def lan(host):
        return ["192.168.1.20"]

    monkeypatch.setattr(announce, "_resolve", lan)
    monkeypatch.setenv("FORK_HA_URL", url)
    assert await announce.announce("Hi", transport=ha.transport) is True


async def test_a_name_with_one_public_address_is_refused(ha, monkeypatch):
    async def mixed(host):
        return ["192.168.1.20", "93.184.216.34"]

    monkeypatch.setattr(announce, "_resolve", mixed)
    monkeypatch.setenv("FORK_HA_URL", "http://ha.lan:8123")
    assert await announce.announce("Hi", transport=ha.transport) is False


async def test_announce_never_raises(ha, monkeypatch):
    def broken(request):
        raise httpx.ConnectError("down", request=request)

    assert await announce.announce("Hi", transport=httpx.MockTransport(broken)) is False
    assert await announce.announce("Hi", transport=httpx.MockTransport(
        lambda r: httpx.Response(401))) is False
    assert await announce.announce("   ", transport=ha.transport) is False
    monkeypatch.delenv("FORK_HA_TOKEN")
    assert await announce.announce("Hi", transport=ha.transport) is False
    assert ha.sent == []


async def test_the_reminder_hook_announces_in_the_background(ha, monkeypatch):
    said = []

    async def fake(message, entities=None, **_):
        said.append(message)
        return True

    monkeypatch.setattr(announce, "announce", fake)
    announce.announce_reminder("Call the dentist")
    announce.announce_reminder("Reminder: take the bins out")
    await asyncio.sleep(0.01)
    assert said == ["Reminder: Call the dentist", "Reminder: take the bins out"]
    monkeypatch.delenv("FORK_HA_URL")
    announce.announce_reminder("Quiet")  # unconfigured: nothing
    await asyncio.sleep(0.01)
    assert len(said) == 2
    announce.announce_reminder(object())  # never raises, whatever it is given


def test_the_reminder_hook_without_a_loop_does_nothing(ha):
    announce.announce_reminder("No loop here")  # no running loop: skipped, no error


def test_the_admin_test_route(ha, monkeypatch):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.api_token = request.headers.get("x-token") == "1"
        return await call_next(request)

    messages = []

    async def fake_send(message, entities=None, **_):
        messages.append(message)
        if message == "fail":
            raise announce.Refused("Home Assistant answered HTTP 401")

    monkeypatch.setattr(announce, "send", fake_send)
    app.include_router(satellite_routes.setup_satellite_routes())
    from core.middleware import require_admin

    app.dependency_overrides[require_admin] = lambda: None
    client = TestClient(app)
    assert client.post("/api/fork/voice/announce", json={}).json() == {"ok": True,
                                                                       "problem": None}
    assert messages == [satellite_routes.TEST_MESSAGE]
    assert client.post("/api/fork/voice/announce", json={"message": "fail"}).json() == {
        "ok": False, "problem": "Home Assistant answered HTTP 401"}
    assert client.post("/api/fork/voice/announce", json={},
                       headers={"x-token": "1"}).status_code == 403


def test_the_router_is_mounted():
    from fork.routes import ROUTERS

    assert ("fork.voice.satellite_routes", "setup_satellite_routes") in ROUTERS
