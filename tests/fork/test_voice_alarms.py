"""Fork: the Echo alarms (fork/voice/alarms.py, alarm_tool.py, alarm_routes.py): the store,
the routes the Echo reads, the feed item, the tool by voice (runs at once on the asking
device, recorded on the turn) and in chat (asks on a card). Fictional devices only."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fork.brain import toolset
from fork.voice import alarm_routes, alarm_tool, alarms, announcements, confirm, devices, ledger
from fork.voice.routes import PREFIX
from tests.fork.brain_fakes import Brain, calls, of, text
from tests.fork.test_voice_assistant import _offered, _token_request, _tool_results

TZ = ZoneInfo("America/New_York")
# Monday 16 November 2026, 08:00 in New York
NOW = datetime(2026, 11, 16, 8, 0, tzinfo=TZ).timestamp()
VOICE = {"x-test-scopes": "voice"}
NAME = alarm_tool.NAME


@pytest.fixture
def echos(data_dir, monkeypatch):
    monkeypatch.setattr(alarms, "owner_tz", lambda owner: TZ)
    devices.seen("alice", "echo-1", name="Kitchen Echo", area="Kitchen")
    devices.seen("alice", "echo-2", name="Bedroom Echo", area="Bedroom")
    return data_dir


def _run(content, *, asked_from=None, turn_id=None, now=NOW, owner="alice"):
    return alarm_tool.run_sync(owner, json.dumps(content), asked_from=asked_from,
                               turn_id=turn_id, now=now, tz=TZ)


def _feed(device_id, owner="alice"):
    return [a for a in announcements.feed(owner, device_id, now=NOW + 1)
            if a["kind"] == "alarms"]


# ------------------------------------------------------------------------- the store
def test_set_list_and_the_body_the_echo_reads(echos):
    desc, out = _run({"op": "set", "time": "07:30"}, asked_from="echo-2")
    assert out["exit_code"] == 0
    assert out["output"] == "Alarm set for 7:30 AM tomorrow on the Bedroom Echo."
    desc, out = _run({"op": "set", "time": "6:15", "days": ["weekdays"], "label": "Gym"},
                     asked_from="echo-2")
    assert out["output"] == ("Alarm (Gym) set for 6:15 AM on weekdays on the Bedroom Echo; "
                             "it next rings at 6:15 AM tomorrow.")
    body = alarms.body("alice", "echo-2", now=NOW, tz=TZ)
    assert [(a["time"], a["days"], a["date"], a["label"]) for a in body["alarms"]] == [
        ("06:15", [1, 2, 3, 4, 5], None, "Gym"), ("07:30", [], "2026-11-17", None)]
    assert all(a["id"].startswith("al_") for a in body["alarms"])
    assert body["updated_at"].endswith("+00:00")
    # only that device's, only that owner's
    assert alarms.body("alice", "echo-1", now=NOW, tz=TZ)["alarms"] == []
    assert alarms.body("bob", "echo-2", now=NOW, tz=TZ)["alarms"] == []
    assert alarms.body("alice", "nobody", now=NOW, tz=TZ) == {
        "alarms": [], "updated_at": alarms.NEVER}


def test_each_change_feeds_that_device_only(echos):
    _run({"op": "set", "time": "07:30"}, asked_from="echo-2")
    (item,) = _feed("echo-2")
    assert item["target"] == {"device_id": "echo-2"} and item["title"] == "Alarms"
    assert item["ref"].startswith("alarms:")
    assert _feed("echo-1") == []


def test_a_passed_one_off_is_left_out_and_pruned(echos):
    _run({"op": "set", "time": "09:00"}, asked_from="echo-1")  # today 9:00
    assert len(alarms.alarms("alice", "echo-1", now=NOW, tz=TZ)) == 1
    later = NOW + 2 * 3600
    assert alarms.alarms("alice", "echo-1", now=later, tz=TZ) == []
    assert alarms.alarms("alice", "echo-1", now=NOW, tz=TZ) == []  # deleted, not hidden


def test_at_most_20_per_device(echos):
    for minute in range(alarms.MAX_PER_DEVICE):
        assert _run({"op": "set", "time": f"10:{minute:02d}", "days": "every day"},
                    asked_from="echo-1")[1]["exit_code"] == 0
    desc, out = _run({"op": "set", "time": "11:00"}, asked_from="echo-1")
    assert out["exit_code"] == 1 and "20 alarms" in out["error"]


def test_bad_input_is_said_to_the_model(echos):
    assert "time" in _run({"op": "set"}, asked_from="echo-1")[1]["error"]
    assert "Can't read the time" in _run({"op": "set", "time": "25:00"},
                                         asked_from="echo-1")[1]["error"]
    assert "Can't read the day" in _run({"op": "set", "time": "7", "days": ["funday"]},
                                        asked_from="echo-1")[1]["error"]
    assert "already passed" in _run({"op": "set", "time": "7", "date": "2026-11-16"},
                                     asked_from="echo-1")[1]["error"]
    assert "op must be" in _run({"op": "snooze"}, asked_from="echo-1")[1]["error"]


# ------------------------------------------------------------------------ the device
def test_which_device(echos):
    # a name, a room, the asking device; in chat with two Echos it must be named
    assert "Kitchen Echo" in _run({"op": "list", "device": "kitchen echo"})[1]["output"]
    assert "Bedroom Echo" in _run({"op": "list", "device": "the bedroom"})[1]["output"]
    assert "Bedroom Echo" in _run({"op": "list"}, asked_from="echo-2")[1]["output"]
    out = _run({"op": "list"})[1]
    assert out["exit_code"] == 1 and "Bedroom Echo, Kitchen Echo" in out["error"]
    assert "No Echo called" in _run({"op": "list", "device": "garage"})[1]["error"]
    devices.seen("alice", "echo-3", name="Bedside Echo", area="Bedroom")
    assert "say which one" in _run({"op": "list", "device": "bedroom"})[1]["error"]


def test_with_one_echo_chat_needs_no_device(data_dir, monkeypatch):
    devices.seen("carol", "echo-9", name="Office Echo", area="Office")
    assert "Office Echo" in _run({"op": "list"}, owner="carol")[1]["output"]


# ----------------------------------------------------------------- cancel and ledger
def test_cancel_by_time_id_all_and_the_only_one(echos):
    _run({"op": "set", "time": "07:30"}, asked_from="echo-1")
    out = _run({"op": "cancel"}, asked_from="echo-1")[1]  # the only one
    assert out["output"].startswith("Cancelled the alarm on the Kitchen Echo: 7:30 AM tomorrow")
    _run({"op": "set", "time": "07:30"}, asked_from="echo-1")
    _run({"op": "set", "time": "07:30", "days": "weekends"}, asked_from="echo-1")
    _run({"op": "set", "time": "08:30"}, asked_from="echo-1")
    out = _run({"op": "cancel"}, asked_from="echo-1")[1]
    assert out["exit_code"] == 1 and "[id al_" in out["error"]  # which one?
    out = _run({"op": "cancel", "time": "7:30"}, asked_from="echo-1")[1]
    assert out["output"].startswith("Cancelled the 2 alarms")
    (left,) = alarms.alarms("alice", "echo-1", now=NOW, tz=TZ)
    assert left.time == "08:30"
    assert "nothing was cancelled" in _run({"op": "cancel", "alarm_id": "al_x"},
                                           asked_from="echo-1")[1]["output"]
    _run({"op": "set", "time": "09:30"}, asked_from="echo-1")
    out = _run({"op": "cancel", "all": True}, asked_from="echo-1")[1]
    assert out["output"].startswith("Cancelled the 2 alarms")
    assert "no alarms" in _run({"op": "cancel"}, asked_from="echo-1")[1]["output"]


def test_set_and_cancel_are_recorded_on_the_turn(echos):
    turn = ledger.start_turn("alice", "Wake me at 6:15 on weekdays")
    _run({"op": "set", "time": "06:15", "days": ["weekdays"]}, asked_from="echo-2",
         turn_id=turn)
    _run({"op": "set", "time": "07:30", "label": "Flight"}, asked_from="echo-2", turn_id=turn)
    _run({"op": "cancel", "time": "06:15"}, asked_from="echo-2", turn_id=turn)
    _run({"op": "list"}, asked_from="echo-2", turn_id=turn)  # not recorded
    ledger.finish_turn(turn, answer="Done.")
    acts = ledger.latest_turn("alice")["actions"]
    got = sorted((a["action"], a["kind"], a["title"], a["detail"], a["due"], a["where"])
                 for a in acts)
    assert got == [
        ("cancelled", "alarm", "Alarm", "Weekdays", "2026-11-17T06:15:00-05:00", "Bedroom Echo"),
        ("created", "alarm", "Alarm", "Weekdays", "2026-11-17T06:15:00-05:00", "Bedroom Echo"),
        ("created", "alarm", "Flight", None, "2026-11-17T07:30:00-05:00", "Bedroom Echo"),
    ]


# -------------------------------------------------------------------------- routes
@pytest.fixture
def api(echos):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        request.state.api_token_owner = request.headers.get("x-test-owner", "alice") \
            if scopes is not None else None
        request.state.api_token_scopes = scopes.split(",") if scopes else []
        return await call_next(request)

    app.include_router(alarm_routes.setup_alarm_routes())
    with TestClient(app) as client:
        yield client


def test_the_routes(api):
    alarm = alarms.add("alice", "echo-1", "07:30", days=[1, 3], label="Gym")
    r = api.get(PREFIX + "/alarms", params={"device_id": "echo-1"}, headers=VOICE)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["alarms"] == [{"id": alarm.id, "time": "07:30", "days": [1, 3],
                                   "date": None, "label": "Gym"}]
    assert api.get(PREFIX + "/alarms", params={"device_id": "unknown"},
                   headers=VOICE).json()["alarms"] == []
    # another owner's token sees none and can't delete it
    assert api.get(PREFIX + "/alarms", params={"device_id": "echo-1"},
                   headers={**VOICE, "x-test-owner": "bob"}).json()["alarms"] == []
    assert api.delete(PREFIX + f"/alarms/{alarm.id}", params={"device_id": "echo-1"},
                      headers={**VOICE, "x-test-owner": "bob"}).json() == {
        "ok": True, "already": True}
    def items():
        return [a for a in announcements.feed("alice", "echo-1") if a["kind"] == "alarms"]

    before = len(items())  # 1: the add
    assert api.delete(PREFIX + f"/alarms/{alarm.id}", params={"device_id": "echo-1"},
                      headers=VOICE).json() == {"ok": True}
    assert len(items()) == before + 1
    assert api.delete(PREFIX + f"/alarms/{alarm.id}", params={"device_id": "echo-1"},
                      headers=VOICE).json() == {"ok": True, "already": True}


def test_the_routes_need_the_voice_token_and_a_device(api):
    assert api.get(PREFIX + "/alarms", params={"device_id": "echo-1"}).status_code == 403
    assert api.get(PREFIX + "/alarms", params={"device_id": "echo-1"},
                   headers={"x-test-scopes": "chat"}).status_code == 403
    assert api.get(PREFIX + "/alarms", headers=VOICE).status_code == 422
    assert api.delete(PREFIX + "/alarms/al_1", headers=VOICE).status_code == 422


# ---------------------------------------------------------------- the brain's tool
@pytest.fixture
def brain(monkeypatch, echos):
    confirm.forget_all()
    b = Brain(monkeypatch, echos)
    yield b
    confirm.forget_all()


async def _voice(brain, sess, message, *, device_id="echo-2", turn_id="turn-1"):
    from fork.brain import turn as brain_turn
    from fork.voice.chat_prefix import EMPTY
    from fork.voice.policy import VoiceGate

    gate = VoiceGate(owner=sess.owner, session_id=sess.id, turn_id=turn_id,
                     device_id=device_id)
    events = brain_turn.read_only_turn(
        _token_request(sess.owner), sess=sess, session_id=sess.id, message=message,
        owner=sess.owner, instructions="Speak briefly.", preset=EMPTY, gate=gate,
        shared_prefix=True, context_lines=("Asked from: Bedroom Echo, Bedroom",))
    return [c async for c in events]


async def test_voice_sets_it_at_once_on_the_asking_echo(brain):
    sess = brain.session()
    turn = ledger.start_turn("alice", "Set an alarm for 7:30")
    brain.llm.rounds([calls((NAME, {"op": "set", "time": "07:30"}))],
                     [text("Alarm set for 7:30 tomorrow.")])
    await _voice(brain, sess, "Set an alarm for 7:30", turn_id=turn)
    assert NAME in _offered(brain.llm.requests[0])
    result = _tool_results(brain.llm.requests[1])[-1]
    assert "Alarm set for 7:30 AM" in result and "Bedroom Echo" in result
    assert confirm.pending(sess.owner, sess.id) is None  # no spoken "yes"
    (alarm,) = alarms.alarms("alice", "echo-2")
    assert alarm.time == "07:30"
    ledger.finish_turn(turn, answer="Alarm set.")
    (act,) = ledger.latest_turn("alice")["actions"]
    assert (act["kind"], act["action"], act["where"]) == ("alarm", "created", "Bedroom Echo")


async def test_chat_lists_without_asking_and_asks_before_setting(brain):
    sess = brain.session()
    brain.llm.rounds([calls((NAME, {"op": "list", "device": "bedroom"}))],
                     [text("No alarms.")])
    events = await brain.run(sess, "What alarms are on the bedroom echo?")
    assert NAME in _offered(brain.llm.requests[0])
    assert not of(events, "ask_user")
    assert "no alarms on the Bedroom Echo" in _tool_results(brain.llm.requests[-1])[-1]

    sess = brain.session(sid="chat-2")
    brain.llm.rounds([calls((NAME, {"op": "set", "time": "06:00", "device": "kitchen"}))])
    events = await brain.run(sess, "Set an alarm for 6 in the kitchen")
    (ask,) = of(events, "ask_user")
    assert alarms.alarms("alice", "echo-1") == []  # not on the model's word
    brain.llm.rounds([text("Set.")])
    await brain.resume(ask["data"]["approval_id"], sess)
    (alarm,) = alarms.alarms("alice", "echo-1")
    assert alarm.time == "06:00"


async def test_without_an_exact_approval_it_changes_nothing(brain):
    run = SimpleNamespace(owner="alice", session_id="s")
    content = json.dumps({"op": "set", "time": "06:00", "device": "kitchen"})
    desc, result = await alarm_tool.run_approved(run, content, None)
    assert result["blocked"] and alarms.alarms("alice", "echo-1") == []


@pytest.mark.parametrize("why", ["no echo", "not an admin", "group off"])
def test_not_offered(data_dir, monkeypatch, why):
    owner = "alice"
    if why != "no echo":
        devices.seen("alice", "echo-1", name="Kitchen Echo", area="Kitchen")
    if why == "not an admin":
        owner = "guest"
    elif why == "group off":
        monkeypatch.setenv("FORK_BRAIN_TOOL_GROUPS", "web,workspace")
    Brain(monkeypatch, data_dir)
    assert NAME not in toolset.names(toolset.resolve(owner=owner))


def test_list_reads_the_rest_writes():
    assert toolset.is_read(NAME, json.dumps({"op": "list"}))
    assert not toolset.is_read(NAME, json.dumps({"op": "set", "time": "7"}))
    assert not toolset.is_read(NAME, json.dumps({"op": "cancel", "all": True}))
    assert toolset.can_read(NAME)
    assert toolset.GROUPS["home"] == ("announce", NAME)
