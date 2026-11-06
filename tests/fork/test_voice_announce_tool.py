"""Fork: the agent's announce tool (fork/voice/announce_tool.py) in a chat turn (asks on a
card, runs only with that exact approval) and in a voice turn (kept for a spoken "yes",
fork/voice/confirm.py). The brain's fakes; delivery and Home Assistant faked; fictional
devices."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fork.brain import tools as brain_tools
from fork.brain import toolset
from fork.voice import announce, announce_tool, announcements, confirm, devices, ledger
from tests.fork.brain_fakes import Brain, calls, of, text
from tests.fork.test_voice_assistant import _offered, _tool_results, _voice

NAME = announce_tool.NAME


@pytest.fixture
def brain(monkeypatch, data_dir):
    monkeypatch.setenv("FORK_HA_URL", "http://192.168.1.20:8123")
    monkeypatch.setenv("FORK_HA_TOKEN", "ha-test-token")
    confirm.forget_all()
    b = Brain(monkeypatch, data_dir)
    b.delivered = []

    async def fake_deliver(owner, message, target="all", **kw):
        b.delivered.append((owner, message, target))
        return announcements.Delivery(item={"id": "an_1"}, spoken=True)

    async def no_satellites(**_):
        raise announce.Refused("not in tests")

    monkeypatch.setattr(announcements, "deliver", fake_deliver)
    monkeypatch.setattr(announcements, "satellites", no_satellites)
    devices.seen("alice", "echo-1", name="Kitchen Echo", area="Kitchen")
    devices.seen("alice", "echo-2", name="Bedroom Echo", area="Bedroom")
    yield b
    confirm.forget_all()


DINNER = {"message": "Dinner is ready", "to": "the kitchen"}


async def test_chat_asks_then_runs_the_approved_call(brain):
    sess = brain.session()
    brain.llm.rounds([calls((NAME, DINNER))])
    events = await brain.run(sess, "Tell the kitchen dinner is ready")
    assert NAME in _offered(brain.llm.requests[0])
    (ask,) = of(events, "ask_user")
    assert brain.delivered == []  # not on the model's word
    brain.llm.rounds([text("Announced in the kitchen.")])
    await brain.resume(ask["data"]["approval_id"], sess)
    assert brain.delivered == [("alice", "Dinner is ready", {"area": "Kitchen"})]
    assert "Announced in the Kitchen" in _tool_results(brain.llm.requests[-1])[-1]


@pytest.mark.parametrize("why", ["no home assistant", "not an admin", "group off"])
async def test_not_offered(brain, monkeypatch, why):
    owner = "alice"
    if why == "no home assistant":
        monkeypatch.delenv("FORK_HA_TOKEN")
    elif why == "not an admin":
        owner = "guest"
    else:
        monkeypatch.setenv("FORK_BRAIN_TOOL_GROUPS", "web,workspace")
    assert NAME not in toolset.names(toolset.resolve(owner=owner))


async def test_without_an_exact_approval_it_never_speaks(brain):
    run = SimpleNamespace(owner="alice", session_id="s")
    desc, result = await announce_tool.run_approved(run, json.dumps(DINNER), None)
    assert result["blocked"] and brain.delivered == []
    other = SimpleNamespace(claim=lambda **kw: False)  # approval of another call
    desc, result = await announce_tool.run_approved(run, json.dumps(DINNER), other)
    assert result["blocked"] and brain.delivered == []


async def test_an_unknown_room_is_an_error_naming_the_known_ones(brain):
    desc, result = await announce_tool.run("alice", json.dumps({"message": "Hi",
                                                                "to": "garage"}))
    assert result["exit_code"] == 1 and "Bedroom, Kitchen" in result["error"]
    assert brain.delivered == []


async def test_everywhere_and_a_device_name(brain):
    await announce_tool.run("alice", json.dumps({"message": "Hi"}))
    await announce_tool.run("alice", json.dumps({"message": "Hi", "to": "bedroom echo"}))
    assert [d[2] for d in brain.delivered] == ["all", {"device_id": "echo-2"}]


async def test_voice_keeps_it_for_a_spoken_yes(brain):
    sess = brain.session()
    brain.llm.rounds([calls((NAME, DINNER))],
                     [text("Announce “Dinner is ready” in the Kitchen?")])
    await _voice(brain, sess, "Tell the kitchen dinner is ready")
    assert brain.delivered == []
    assert _tool_results(brain.llm.requests[1])[-1] == confirm.PROPOSED
    p = confirm.take(sess.owner, sess.id)
    (call,) = p.calls
    assert call.change.kind == "announcement" and call.change.where == "Kitchen"
    assert p.question() == "Announce “Dinner is ready” in the Kitchen?"
    outcomes = await confirm.run(p)
    assert [o.ok for o in outcomes] == [True]
    assert brain.delivered == [("alice", "Dinner is ready", {"area": "Kitchen"})]
    assert confirm.reply(outcomes) == "Done. Announced it in the Kitchen."
    row = ledger.add_action("t", "alice", action=call.change.action, kind=call.change.kind,
                            title=call.change.title, where=call.change.where)
    assert row["kind"] == "announcement"


async def test_voice_refuses_an_unknown_room(brain):
    sess = brain.session()
    brain.llm.rounds([calls((NAME, {"message": "Hi", "to": "the garage"}))],
                     [text("I don't know that room.")])
    await _voice(brain, sess, "Tell the garage hi")
    assert "can only read" in _tool_results(brain.llm.requests[1])[-1]
    assert confirm.pending(sess.owner, sess.id) is None and brain.delivered == []


def test_announce_is_a_write():
    assert not toolset.is_read(NAME) and not toolset.can_read(NAME)
    assert brain_tools.toolset.ANNOUNCE == NAME
