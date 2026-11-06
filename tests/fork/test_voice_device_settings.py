"""Fork: settings the hub manages per Echo (fork/voice/device_settings.py and
device_settings_routes.py; contract C6). Fictional devices only."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from fork.voice import announcements, device_settings, device_settings_routes, devices
from fork.voice.routes import PREFIX

REPO = Path(__file__).resolve().parents[2]
VOICE = {"x-test-scopes": "voice"}
ADMIN = {"x-test-user": "alice"}
GOOD = {"home.enabled": True, "home.idle_seconds": 0, "home.weather_enabled": False,
        "today.enabled": True, "kiosk.return_seconds": 600, "kiosk.auto_reload": True,
        "record.enabled": False, "record.clock_button": True,
        "record.privacy_button": "mute", "followup.enabled": True, "followup.seconds": 8}


def hello(owner="alice", device_id="echo-1", name="Kitchen Echo", area="Kitchen"):
    devices.seen(owner, device_id, name=name, area=area, app_version_code=1400030)


# ------------------------------------------------------------------------ validation
def test_the_keys_are_c6s():
    assert [k.name for k in device_settings.KEYS] == list(GOOD)


def test_every_c6_value_is_accepted_and_null_means_the_device_decides():
    assert device_settings.validate(GOOD) == GOOD
    assert device_settings.validate({"home.enabled": None, "followup.seconds": 3}) == {
        "followup.seconds": 3}
    assert device_settings.validate({}) == {}


@pytest.mark.parametrize("bad", [
    {"home.enabled": 1},
    {"home.enabled": "true"},
    {"home.idle_seconds": -1},
    {"home.idle_seconds": True},
    {"home.idle_seconds": 1.5},
    {"home.idle_seconds": "60"},
    {"kiosk.return_seconds": device_settings.MAX_SECONDS + 1},
    {"followup.seconds": 2},
    {"followup.seconds": 16},
    {"record.privacy_button": "off"},
    {"record.privacy_button": True},
    {"volume": 3},
    [],
    "home.enabled",
])
def test_a_bad_value_or_unknown_key_is_refused(bad):
    with pytest.raises(device_settings.BadSettings):
        device_settings.validate(bad)


# ----------------------------------------------------------------------------- store
def test_nothing_set_is_version_0(data_dir):
    assert device_settings.get("alice", "echo-1") == {"version": 0, "settings": {}}


def test_each_change_bumps_the_version_and_feeds_the_device(data_dir, caplog):
    hello()
    caplog.set_level(logging.INFO, logger="fork.voice.device_settings")
    out = device_settings.replace("Alice", "echo-1", {"today.enabled": True}, now=1000.0)
    assert out == {"version": 1, "settings": {"today.enabled": True}}
    assert device_settings.get("alice", "echo-1") == out
    # the same set again changes nothing: no new version, no feed item
    assert device_settings.replace("alice", "echo-1", {"today.enabled": True},
                                   now=1001.0)["version"] == 1
    out = device_settings.replace("alice", "echo-1",
                                  {"today.enabled": None, "followup.seconds": 10}, now=1002.0)
    assert out == {"version": 2, "settings": {"followup.seconds": 10}}
    # leaving everything to the device is a change too, and the version never goes back
    assert device_settings.replace("alice", "echo-1", {}, now=1003.0) == {
        "version": 3, "settings": {}}
    feed = announcements.feed("alice", "echo-1", now=1004.0)
    assert [(a["kind"], a["ref"], a["target"]) for a in feed] == [
        ("settings", "settings:1", {"device_id": "echo-1"}),
        ("settings", "settings:2", {"device_id": "echo-1"}),
        ("settings", "settings:3", {"device_id": "echo-1"})]
    # only that device's feed; expires soon (the device refetches at start anyway)
    hello(device_id="echo-2", name="Lounge", area="Kitchen")
    assert announcements.feed("alice", "echo-2", now=1004.0) == []
    assert announcements.feed("alice", "echo-1",
                              now=1003.0 + device_settings.FEED_SHOW_SECONDS + 1) == []
    # logged with owner, device and keys, never values
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "followup.seconds" in logged and "echo-1" in logged and "10" not in logged


def test_owners_are_apart(data_dir):
    device_settings.replace("alice", "echo-1", {"home.enabled": False})
    assert device_settings.get("bob", "echo-1") == {"version": 0, "settings": {}}


def test_a_bad_set_changes_nothing(data_dir):
    device_settings.replace("alice", "echo-1", {"home.enabled": False})
    with pytest.raises(device_settings.BadSettings):
        device_settings.replace("alice", "echo-1", {"home.enabled": True, "x": 1})
    assert device_settings.get("alice", "echo-1") == {"version": 1,
                                                      "settings": {"home.enabled": False}}


# ---------------------------------------------------------------------------- routes
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

    def require_admin(request):
        if request.headers.get("x-test-user") != "alice":
            raise HTTPException(403, "Admin only")

    monkeypatch.setattr("core.middleware.require_admin", require_admin)
    monkeypatch.setattr("src.auth_helpers.get_current_user",
                        lambda request: request.headers.get("x-test-user"))
    app.include_router(device_settings_routes.setup_device_settings_routes())
    with TestClient(app) as client:
        yield client


def put(api, device_id, settings, headers=ADMIN):
    return api.put(f"/api/fork/voice/devices/{device_id}/settings",
                   content=json.dumps({"settings": settings}),
                   headers={**headers, "content-type": "application/json"})


def test_the_device_reads_its_settings(api):
    hello()
    assert put(api, "echo-1", {"kiosk.return_seconds": 600, "today.enabled": True}).json() == {
        "version": 1, "settings": {"kiosk.return_seconds": 600, "today.enabled": True}}
    res = api.get(PREFIX + "/devices/echo-1/settings", headers=VOICE)
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    assert res.json() == {"version": 1,
                          "settings": {"kiosk.return_seconds": 600, "today.enabled": True}}


def test_the_device_route_is_the_token_owners_only(api):
    hello()
    put(api, "echo-1", {"home.enabled": False})
    other = {**VOICE, "x-test-owner": "bob"}
    assert api.get(PREFIX + "/devices/echo-1/settings", headers=other).json() == {
        "version": 0, "settings": {}}
    assert api.get(PREFIX + "/devices/echo-1/settings").status_code == 403  # no token
    assert api.get(PREFIX + "/devices/echo-1/settings",
                   headers={"x-test-scopes": "chat"}).status_code == 403
    assert api.get(PREFIX + "/devices/" + "x" * 200 + "/settings",
                   headers=VOICE).status_code == 422


def test_the_admin_routes_are_admin_only(api):
    hello()
    assert api.get("/api/fork/voice/devices", headers={"x-test-user": "bob"}).status_code == 403
    assert api.get("/api/fork/voice/devices").status_code == 403
    assert api.get("/api/fork/voice/devices", headers={**VOICE, **ADMIN}).status_code == 403
    assert put(api, "echo-1", {"home.enabled": True},
               headers={"x-test-user": "bob"}).status_code == 403
    assert put(api, "echo-1", {"home.enabled": True}, headers={**VOICE, **ADMIN}).status_code == 403
    assert device_settings.get("alice", "echo-1")["version"] == 0


def test_the_admin_lists_devices_with_their_settings(api):
    hello()
    hello(device_id="echo-2", name="Bedroom Echo", area="Bedroom")
    hello(owner="bob", device_id="echo-9", name="Bob's Echo", area="Study")
    put(api, "echo-1", {"followup.seconds": 12})
    data = api.get("/api/fork/voice/devices", headers=ADMIN).json()
    rows = {d["device_id"]: d for d in data["devices"]}
    assert set(rows) == {"echo-1", "echo-2"}
    assert rows["echo-1"]["name"] == "Kitchen Echo" and rows["echo-1"]["area"] == "Kitchen"
    assert rows["echo-1"]["app_version_code"] == 1400030 and rows["echo-1"]["last_seen"] > 0
    assert (rows["echo-1"]["version"], rows["echo-1"]["settings"]) == (1, {"followup.seconds": 12})
    assert (rows["echo-2"]["version"], rows["echo-2"]["settings"]) == (0, {})
    keys = {k["key"]: k for k in data["keys"]}
    assert list(keys) == list(GOOD)
    assert keys["followup.seconds"] == {"key": "followup.seconds", "type": "int",
                                        "label": "Listen for an answer for (seconds)",
                                        "min": 3, "max": 15}
    assert keys["record.privacy_button"]["choices"] == ["record", "mute"]


@pytest.mark.parametrize("body", [
    {"settings": {"volume": 3}},
    {"settings": {"followup.seconds": 20}},
    {"settings": {"home.enabled": "yes"}},
    {"settings": []},
    {"home.enabled": True},
    {"settings": {}, "extra": 1},
])
def test_the_admin_route_refuses_bad_settings(api, body):
    hello()
    res = api.put("/api/fork/voice/devices/echo-1/settings", content=json.dumps(body),
                  headers={**ADMIN, "content-type": "application/json"})
    assert res.status_code == 400
    assert device_settings.get("alice", "echo-1")["version"] == 0


def test_the_admin_route_refuses_unknown_devices_and_bad_json(api):
    assert put(api, "echo-404", {"home.enabled": True}).status_code == 404
    hello(owner="bob", device_id="echo-9")
    assert put(api, "echo-9", {"home.enabled": True}).status_code == 404  # bob's, not alice's
    hello()
    res = api.put("/api/fork/voice/devices/echo-1/settings", content=b"{",
                  headers={**ADMIN, "content-type": "application/json"})
    assert res.status_code == 400


def test_a_change_arrives_in_the_devices_feed(api):
    hello()
    put(api, "echo-1", {"record.privacy_button": "record"})
    feed = announcements.feed("alice", "echo-1")
    assert [(a["kind"], a["ref"]) for a in feed] == [("settings", "settings:1")]
    # the normal feed shape: a unique id, its time and that device as the target
    item = feed[0]
    assert item["id"].startswith("an_") and item["at"] > 0
    assert item["target"] == {"device_id": "echo-1"}
    assert set(item) == {"id", "at", "kind", "title", "text", "target", "ref", "expires_at"}


def test_mounted_after_the_other_fork_routers():
    from fork.routes import ROUTERS

    assert ROUTERS[-1] == ("fork.voice.device_settings_routes", "setup_device_settings_routes")


# --------------------------------------------------------------------- the card's JS
@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_card_parses_values_like_the_route():
    script = """
const m = await import(process.argv[1]);
const keys = [
  {key: 'home.enabled', type: 'bool', label: 'Clock'},
  {key: 'followup.seconds', type: 'int', label: 'Listen', min: 3, max: 15},
  {key: 'record.privacy_button', type: 'enum', label: 'Privacy', choices: ['record', 'mute']},
];
const out = {ok: m.settingsFrom(keys, {'home.enabled': 'false', 'followup.seconds': '8',
                                       'record.privacy_button': ''}), bad: []};
for (const raw of ['2', '16', '1.5', 'x']) {
  try { m.settingsFrom(keys, {'followup.seconds': raw}); out.bad.push(null); }
  catch (e) { out.bad.push(e.message); }
}
out.line = m.deviceLine({area: 'Kitchen', app_version_code: 1400030, last_seen: 1000}, 1000 + 5 * 60000);
console.log(JSON.stringify(out));
"""
    url = (REPO / "static" / "js" / "forkVoiceDevices.js").as_uri()
    done = subprocess.run(["node", "--input-type=module", "-e", script, url],
                          capture_output=True, text=True, check=True, timeout=30)
    out = json.loads(done.stdout)
    assert out["ok"] == {"home.enabled": False, "followup.seconds": 8}
    assert out["bad"] == ["Listen: a whole number from 3 to 15"] * 4
    assert out["line"] == "Kitchen · app 1400030 · seen 5 min ago"


# ------------------------------------------------------------------- edge cases
@pytest.mark.parametrize("stored", ["not json", "[1, 2]", "7", "null"])
def test_a_damaged_row_reads_as_nothing_set_and_can_be_replaced(data_dir, stored):
    device_settings.replace("alice", "echo-1", {"home.enabled": False}, notify=False)
    with device_settings._connect(write=True) as db:
        db.execute("UPDATE device_settings SET settings = ?", (stored,))
    assert device_settings.get("alice", "echo-1") == {"version": 1, "settings": {}}
    out = device_settings.replace("alice", "echo-1", {"home.enabled": True}, notify=False)
    assert out == {"version": 2, "settings": {"home.enabled": True}}


def test_saves_at_once_each_get_their_own_version(data_dir):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as pool:
        versions = list(pool.map(
            lambda s: device_settings.replace("alice", "echo-1", {"followup.seconds": s},
                                              notify=False)["version"], range(3, 15)))
    assert sorted(versions) == list(range(1, 13))
    assert device_settings.get("alice", "echo-1")["version"] == 12


@pytest.mark.parametrize("value", [1.0, "8", 8.5, [8], {"v": 8}, float("nan")])
def test_ints_must_be_whole_json_numbers(value):
    with pytest.raises(device_settings.BadSettings):
        device_settings.validate({"followup.seconds": value})
