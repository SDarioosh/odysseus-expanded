"""Fork: announcements to a room or device and the devices' feed
(fork/voice/devices.py, announcements.py, the hello and feed routes in
satellite_routes.py, the reminder hook in announce.py). A fake Home Assistant over
httpx.MockTransport; fictional devices and messages."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fork.voice import announce, announcements, devices, satellite, satellite_routes
from fork.voice.routes import PREFIX

VOICE = {"x-test-scopes": "voice"}
SATELLITES = [
    {"entity_id": "assist_satellite.kitchen_echo", "area_id": "kitchen", "area": "Kitchen",
     "name": "Kitchen Echo Assist satellite"},
    {"entity_id": "assist_satellite.living_room_echo", "area_id": "living_room",
     "area": "Living Room", "name": "Living Room Echo"},
    {"entity_id": "assist_satellite.office", "area_id": "office", "area": "Office",
     "name": "Office"},
]


@pytest.fixture
def ha(monkeypatch, data_dir):
    monkeypatch.setenv("FORK_HA_URL", "http://192.168.1.20:8123/")
    monkeypatch.setenv("FORK_HA_TOKEN", "ha-test-token")
    monkeypatch.setenv("FORK_VOICE_ANNOUNCE_ENTITIES", "assist_satellite.everywhere")
    state = SimpleNamespace(spoken=[], templates=[], satellites=list(SATELLITES),
                            template_status=200)

    def handler(request):
        body = json.loads(request.read())
        assert request.headers["authorization"] == "Bearer ha-test-token"
        if request.url.path == announcements.TEMPLATE_PATH:
            state.templates.append(body["template"])
            if state.template_status != 200:
                return httpx.Response(state.template_status)
            return httpx.Response(200, text=json.dumps(state.satellites))
        assert request.url.path == announce.SERVICE_PATH
        state.spoken.append(body)
        return httpx.Response(200, json=[])

    state.transport = httpx.MockTransport(handler)
    return state


def hello(owner="alice", device_id="echo-1", name="Kitchen Echo", area="Kitchen"):
    devices.seen(owner, device_id, name=name, area=area)


# ------------------------------------------------------------------- the registry
def test_seen_keeps_what_a_later_turn_leaves_out(data_dir):
    devices.seen("Alice", "echo-1", name="Kitchen Echo", area="Kitchen", app_version_code=7)
    devices.seen("alice", "echo-1", name="", area=None)
    d = devices.get("alice", "echo-1")
    assert (d.name, d.area, d.app_version_code) == ("Kitchen Echo", "Kitchen", 7)
    devices.seen("alice", "echo-1", area="Pantry")
    assert devices.get("ALICE", "echo-1").area == "Pantry"
    assert devices.get("bob", "echo-1") is None


@pytest.mark.parametrize("said, expected", [
    ("the kitchen", {"area": "Kitchen"}),
    ("Kitchen", {"area": "Kitchen"}),
    ("in the living room", {"area": "Living Room"}),
    ("kitchen echo", {"device_id": "echo-1"}),
    ("everywhere", "all"),
    ("", "all"),
    ("the garage", None),
    ("office", {"area": "Office"}),  # a room only Home Assistant knows
])
def test_match(data_dir, said, expected):
    hello()
    hello(device_id="echo-2", name="Lounge", area="Living Room")
    assert devices.match("alice", said, areas=("Office",)) == expected


# ------------------------------------------------------------- speaking on a target
async def test_an_area_is_resolved_through_home_assistant(ha):
    out = await announcements.deliver("alice", "Dinner is ready", {"area": "kitchen"},
                                      transport=ha.transport)
    assert out.spoken and out.entities == ["assist_satellite.kitchen_echo"]
    assert ha.spoken == [{"entity_id": ["assist_satellite.kitchen_echo"],
                          "message": "Dinner is ready"}]
    (template,) = ha.templates
    assert template == announcements.SATELLITES_TEMPLATE and "area_entities" in template


async def test_a_room_name_with_spaces_matches_its_area_id(ha):
    out = await announcements.deliver("alice", "Hi", {"area": "Living room"},
                                      transport=ha.transport)
    assert out.entities == ["assist_satellite.living_room_echo"]


async def test_a_room_with_no_satellite_is_kept_for_its_feed_but_spoken_nowhere(ha):
    hello(device_id="echo-g", name="Garage Echo", area="Garage")
    out = await announcements.deliver("alice", "Hi", {"area": "Garage"},
                                      transport=ha.transport)
    assert not out.spoken and out.no_satellite and out.entities == []
    assert out.problem == "no Echo in the Garage can speak it"
    assert ha.spoken == []  # never the configured "everywhere" list instead
    assert [i["text"] for i in announcements.feed("alice", "echo-g")] == ["Hi"]
    assert announcements.feed("alice", "echo-1") == []


async def test_a_device_with_no_satellite_is_spoken_nowhere(ha):
    hello(device_id="echo-7", name="Attic Echo", area="Attic")
    out = await announcements.deliver("alice", "Hi", {"device_id": "echo-7"},
                                      transport=ha.transport)
    assert not out.spoken and out.problem == "no Echo for Attic Echo can speak it"
    out = await announcements.deliver("alice", "Hi", {"device_id": "never-seen"},
                                      transport=ha.transport)
    assert out.problem == "no Echo for that device can speak it"
    assert ha.spoken == [] and out.item is not None


async def test_home_assistant_unable_to_list_satellites_speaks_nowhere(ha):
    ha.template_status = 500
    out = await announcements.deliver("alice", "Hi", {"area": "Kitchen"},
                                      transport=ha.transport)
    assert not out.spoken and out.no_satellite and ha.spoken == []
    assert out.problem.startswith("no Echo in the Kitchen can speak it (Home Assistant")


async def test_a_reminder_to_all_still_uses_the_configured_list(ha):
    out = await announcements.deliver("alice", "Reminder: Bins", "all", kind="reminder",
                                      ref="todo:41", transport=ha.transport)
    assert out.spoken and not out.no_satellite and ha.templates == []
    assert ha.spoken == [{"entity_id": ["assist_satellite.everywhere"],
                          "message": "Reminder: Bins"}]


async def test_all_uses_the_configured_list_without_asking(ha):
    await announcements.deliver("alice", "Hi", "all", transport=ha.transport)
    assert ha.templates == [] and ha.spoken[0]["entity_id"] == ["assist_satellite.everywhere"]


async def test_a_device_is_found_by_name_else_by_its_room(ha):
    hello(device_id="echo-1", name="Kitchen Echo", area="Kitchen")
    hello(device_id="echo-9", name="Spare", area="Office")
    out = await announcements.deliver("alice", "Hi", {"device_id": "echo-1"},
                                      transport=ha.transport)
    assert out.entities == ["assist_satellite.kitchen_echo"]
    out = await announcements.deliver("alice", "Hi", {"device_id": "echo-9"},
                                      transport=ha.transport)
    assert out.entities == ["assist_satellite.office"]


async def test_home_assistant_off_the_network_is_never_asked(ha, monkeypatch):
    async def public(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(announce, "_resolve", public)
    monkeypatch.setenv("FORK_HA_URL", "https://ha.example.com")
    out = await announcements.deliver("alice", "Hi", {"area": "Kitchen"},
                                      transport=ha.transport)
    assert not out.spoken and ha.spoken == [] and ha.templates == []
    assert out.item is not None  # still on the devices' screens


async def test_without_home_assistant_it_is_only_kept_for_the_feed(ha, monkeypatch):
    monkeypatch.delenv("FORK_HA_TOKEN")
    out = await announcements.deliver("alice", "Hi", "all", transport=ha.transport)
    assert not out.spoken and out.problem and out.item["text"] == "Hi"
    assert ha.spoken == []


# ------------------------------------------------------------------------ the feed
def test_the_feed_gives_each_device_what_is_meant_for_it(data_dir):
    hello(device_id="echo-1", area="Kitchen")
    hello(device_id="echo-2", name="Bedroom", area="Bedroom")
    a = announcements.store("alice", "For everyone", "all")
    k = announcements.store("alice", "For the kitchen", {"area": "kitchen"})
    d = announcements.store("alice", "For echo 2", {"device_id": "echo-2"})
    announcements.store("bob", "Someone else's", "all")
    ids = lambda dev: [x["id"] for x in announcements.feed("alice", dev)]  # noqa: E731
    assert ids("echo-1") == [a["id"], k["id"]]
    assert ids("echo-2") == [a["id"], d["id"]]
    assert ids("unknown") == [a["id"]]
    assert k["target"] == {"area": "kitchen"} and a["target"] == {"all": True}
    assert a["kind"] == "announcement" and a["title"] == "For everyone"
    assert a["expires_at"] - a["at"] == announcements.SHOW_SECONDS * 1000
    assert set(a) == {"id", "at", "kind", "title", "text", "target", "ref", "expires_at"}
    assert announcements.feed("alice", "echo-1", since_ms=k["at"]) == []


def test_the_feed_drops_expired_and_old_ones(data_dir):
    now = 1_762_434_000.0
    old = announcements.store("alice", "Old", "all", now=now - 2 * 3600)
    fresh = announcements.store("alice", "Fresh", "all", now=now - 60)
    assert [x["id"] for x in announcements.feed("alice", "e", now=now)] == [fresh["id"]]
    assert old["expires_at"] < now * 1000
    announcements.store("alice", "Later", "all", now=now + announcements.KEEP_SECONDS + 1)
    with announcements._connect() as db:  # older than 24 h: deleted on the next write
        assert db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 1


def test_bad_targets_are_refused(data_dir):
    with pytest.raises(ValueError):
        announcements.store("alice", "Hi", {"room": "kitchen"})
    with pytest.raises(ValueError):
        announcements.store("alice", "  ", "all")


# --------------------------------------------------------------------- reminders
async def test_the_reminder_hook_keeps_a_reminder_for_every_device(ha):
    hello()
    announce.announce_reminder("Take the bins out", owner="alice", ref="todo:41")
    await asyncio.sleep(0.05)
    for _ in range(50):
        if announcements.feed("alice", "echo-1"):
            break
        await asyncio.sleep(0.02)
    (item,) = announcements.feed("alice", "echo-1")
    assert item["kind"] == "reminder" and item["ref"] == "todo:41"
    assert item["title"] == "Take the bins out"
    assert item["text"] == "Reminder: Take the bins out" and item["target"] == {"all": True}


# ------------------------------------------------------------------------ routes
@pytest.fixture
def api(data_dir, monkeypatch):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        request.state.api_token_owner = request.headers.get("x-test-owner", "alice") \
            if scopes is not None else None
        request.state.api_token_scopes = scopes.split(",") if scopes else []
        return await call_next(request)

    async def no_identify(audio, **_):
        return None

    monkeypatch.setattr(satellite, "identify", no_identify)
    satellite.forget_all()
    app.include_router(satellite_routes.setup_satellite_routes())
    with TestClient(app) as client:
        yield client
    satellite.forget_all()


def test_hello_records_the_device(api):
    form = {"device_id": "echo-1", "device_name": "Kitchen Echo", "area": "Kitchen",
            "app_version_code": "1400030"}
    assert api.post(PREFIX + "/satellite/hello", data=form, headers=VOICE).json() == {
        "ok": True}
    d = devices.get("alice", "echo-1")
    assert (d.name, d.area, d.app_version_code) == ("Kitchen Echo", "Kitchen", 1400030)
    assert api.post(PREFIX + "/satellite/hello", data=form).status_code == 403
    assert api.post(PREFIX + "/satellite/hello", data={"area": "x"},
                    headers=VOICE).status_code == 422
    assert api.post(PREFIX + "/satellite/hello", data={**form, "app_version_code": "x"},
                    headers=VOICE).status_code == 422


def test_a_turn_records_the_device_too(api):
    api.post(PREFIX + "/satellite/turn", headers=VOICE,
             data={"device_id": "echo-2", "device_name": "Bedroom Echo", "area": "Bedroom"})
    assert devices.get("alice", "echo-2").area == "Bedroom"


def test_the_feed_route(api):
    api.post(PREFIX + "/satellite/hello", headers=VOICE,
             data={"device_id": "echo-1", "area": "Kitchen"})
    k = announcements.store("alice", "Dinner is ready", {"area": "Kitchen"})
    announcements.store("alice", "Bedroom only", {"area": "Bedroom"})
    res = api.get(PREFIX + "/announcements", params={"device_id": "echo-1"}, headers=VOICE)
    assert res.json() == {"announcements": [k]}
    res = api.get(PREFIX + "/announcements", params={"device_id": "echo-1", "since": k["at"]},
                  headers=VOICE)
    assert res.json() == {"announcements": []}
    other = api.get(PREFIX + "/announcements", params={"device_id": "echo-1"},
                    headers={**VOICE, "x-test-owner": "bob"})
    assert other.json() == {"announcements": []}
    assert api.get(PREFIX + "/announcements", headers=VOICE).status_code == 422
    assert api.get(PREFIX + "/announcements", params={"device_id": "echo-1"}).status_code == 403


def test_the_admin_test_announcement_to_one_device(ha, monkeypatch):
    """scripts/echo_verify.py: a test to a made-up device reaches only that device's feed
    and is spoken only when asked."""
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.api_token = request.headers.get("x-token") == "1"
        request.state.current_user = "alice"
        return await call_next(request)

    async def fake_send(message, entities=None, **_):
        ha.spoken.append({"message": message, "entity_id": entities})

    monkeypatch.setattr(announce, "send", fake_send)
    app.include_router(satellite_routes.setup_satellite_routes())
    from core.middleware import require_admin

    app.dependency_overrides[require_admin] = lambda: None
    client = TestClient(app)
    res = client.post("/api/fork/voice/announce",
                      json={"message": "Verification test", "device_id": "echo-verify-1"})
    body = res.json()
    assert body["ok"] is True and body["spoken"] is False and body["id"].startswith("an_")
    assert ha.spoken == []
    feed = announcements.feed("alice", "echo-verify-1")
    assert [(i["id"], i["text"], i["target"], i["title"]) for i in feed] == [
        (body["id"], "Verification test", {"device_id": "echo-verify-1"},
         "Test announcement")]
    assert announcements.feed("alice", "echo-kitchen") == []  # no real device sees it
    # speak: through deliver, to that device's satellite; with none it is said nowhere
    monkeypatch.setattr(announcements, "satellites", _no_satellites)
    res = client.post("/api/fork/voice/announce",
                      json={"device_id": "echo-verify-1", "speak": True})
    assert res.json()["spoken"] is False and res.json()["ok"] is False
    assert res.json()["problem"] == "no Echo for that device can speak it"
    assert ha.spoken == []

    async def one_satellite(**_):
        return [announcements.Satellite("assist_satellite.kitchen_echo", "kitchen",
                                        "Kitchen", "Kitchen Echo")]

    monkeypatch.setattr(announcements, "satellites", one_satellite)
    hello(device_id="echo-verify-2", name="Kitchen Echo", area="Kitchen")
    res = client.post("/api/fork/voice/announce",
                      json={"device_id": "echo-verify-2", "speak": True})
    assert res.json()["spoken"] is True and res.json()["ok"] is True
    assert ha.spoken[-1] == {"message": satellite_routes.TEST_MESSAGE,
                             "entity_id": ["assist_satellite.kitchen_echo"]}
    assert client.post("/api/fork/voice/announce", json={"device_id": "x"},
                       headers={"x-token": "1"}).status_code == 403
    assert client.post("/api/fork/voice/announce",
                       json={"device_id": "x" * 129}).status_code == 422


async def _no_satellites(**_):
    return []


# ------------------------------------------------------------------- edge cases
@pytest.mark.parametrize("code", ["²", "１２", "٣"])
def test_hello_refuses_non_ascii_digits(api, code):
    res = api.post(PREFIX + "/satellite/hello", headers=VOICE,
                   data={"device_id": "echo-1", "app_version_code": code})
    assert res.status_code == 422


@pytest.mark.parametrize("since", [str(10 ** 30), str(-(10 ** 30)), "-5"])
def test_the_feed_survives_any_since(api, since):
    a = announcements.store("alice", "For everyone", "all")
    res = api.get(PREFIX + "/announcements", headers=VOICE,
                  params={"device_id": "echo-1", "since": since})
    assert res.status_code == 200
    expected = [] if since == str(10 ** 30) else [a["id"]]
    assert [x["id"] for x in res.json()["announcements"]] == expected


def test_the_feed_keeps_the_newest_fifty(data_dir):
    now = 1_762_434_000.0
    made = [announcements.store("alice", f"Item {i}", "all", now=now + i)["id"]
            for i in range(60)]
    got = [x["id"] for x in announcements.feed("alice", "echo-1", now=now + 61)]
    assert got == made[-announcements.MAX_FEED:]


def test_targets_match_rooms_by_slug_and_devices_exactly(data_dir):
    hello(device_id="echo-1", name="Kids' Room", area="Kids’ Room")
    hello(device_id="echo-2", name="Kitchen", area="kitchen")
    k = announcements.store("alice", "Bedtime", {"area": "kids room"})
    d = announcements.store("alice", "Only one", {"device_id": "ECHO-2"})
    assert [x["id"] for x in announcements.feed("alice", "echo-1")] == [k["id"]]
    assert announcements.feed("alice", "echo-2") == []  # device ids are exact
    assert d["target"] == {"device_id": "ECHO-2"}


def test_the_registry_is_written_off_the_event_loop(api, monkeypatch):
    threads = []
    real = devices.seen

    def seen(*a, **kw):
        try:
            asyncio.get_running_loop()
            threads.append("on the loop")
        except RuntimeError:
            threads.append("worker")
        return real(*a, **kw)

    monkeypatch.setattr(devices, "seen", seen)
    api.post(PREFIX + "/satellite/hello", headers=VOICE, data={"device_id": "echo-1"})
    api.post(PREFIX + "/satellite/turn", headers=VOICE, data={"device_id": "echo-1"})
    assert threads == ["worker", "worker"]
