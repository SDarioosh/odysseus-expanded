"""Fork: voice turn timings (fork/voice/timings.py, timing_routes.py,
scripts/voice_timings.py). Fictional devices and numbers only."""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fork.voice import timing_routes, timings
from fork.voice.routes import PREFIX

URL = PREFIX + "/satellite/timing"
VOICE = {"x-test-scopes": "voice"}
WAKE_MS = int(time.time() * 1000) - 3_600_000  # an hour ago: older rows are pruned
MARKS = {"started": 120, "listening": 180, "voice_started": 600, "voice_stopped": 2600,
         "awaiting_response": 3400, "awaiting_tts": 9400, "starting_tts": 9700,
         "streaming_tts": 9800, "completed_tts": 13800, "ended": 14000}


def sse(data: dict) -> str:
    return "data: " + json.dumps(data) + "\n\n"


def report(**over) -> dict:
    return {"device_id": "echo-kitchen", "started_at": WAKE_MS, "marks": dict(MARKS),
            "flags": {"thinking_sound": True}, "start_mode": "wake_word_detected", **over}


def hub_timer(received: float) -> timings.HubTimer:
    t = timings.HubTimer(now=received)
    t.marks.update({"ready": 0.05, "first_token": 2.0, "first_text": 2.5,
                    "first_spoken": 3.1, "done": 5.0})
    t.tools = [{"name": "search_conversations", "seconds": 1.2}]
    return t


# ----------------------------------------------------------------------- the hub's clock
def test_the_timer_reads_the_brain_events():
    t = timings.HubTimer()
    for chunk in (sse({"delta": "hm", "thinking": True}),
                  sse({"type": "tool_start", "tool": "search_conversations"}),
                  sse({"type": "tool_output", "tool": "search_conversations"}),
                  sse({"type": "agent_step", "round": 2}),
                  sse({"delta": "Priya brings the cake."}),
                  sse({"type": "metrics", "data": {"input_tokens": 9, "output_tokens": 4}}),
                  "data: [DONE]\n\n", "event: ping\n\n", "data: {not json\n\n"):
        t.observe(chunk)
    t.finish("answered")
    out = t.as_json()
    assert set(out["marks"]) == {"first_token", "first_text", "done"}
    assert out["marks"]["first_token"] <= out["marks"]["first_text"] <= out["marks"]["done"]
    assert [x["name"] for x in out["tools"]] == ["search_conversations"]
    assert out["rounds"] == 2 and out["tokens"] == {"input": 9, "output": 4}
    assert out["outcome"] == "answered"


def test_a_mark_keeps_its_first_time():
    t = timings.HubTimer()
    t.mark("ready")
    first = t.marks["ready"]
    t.mark("ready")
    assert t.marks["ready"] == first


def test_a_tool_still_running_at_the_end_is_counted():
    t = timings.HubTimer()
    t.observe(sse({"type": "tool_start", "tool": "web_search"}))
    t.finish()
    assert [x["name"] for x in t.tools] == ["web_search"]


# --------------------------------------------------------------------- joining the halves
def test_a_device_report_joins_its_turn(data_dir):
    timings.save_hub("Alice", "turn-1", "echo-kitchen", hub_timer(WAKE_MS / 1000 + 3.5))
    assert timings.save_device("alice", timings.clean_device(report())) == "turn-1"
    [row] = timings.rows("alice")
    assert row["hub"] and row["device"]["marks"]["streaming_tts"] == 9800
    steps = timings.steps_of(row)
    assert steps["Speech to text (HA)"] == pytest.approx(0.8)
    assert steps["Text to speech: first audio"] == pytest.approx(0.4)
    assert steps["Silence after you stopped"] == pytest.approx(7.2)
    assert steps["Odysseus: model's first token"] == pytest.approx(1.95)
    assert steps["Odysseus: tool calls (all)"] == pytest.approx(1.2)


def test_a_report_arriving_before_the_hub_finished_still_joins(data_dir):
    t = timings.HubTimer(now=WAKE_MS / 1000 + 3.5)
    row_id = timings.save_hub("alice", "turn-1", "echo-kitchen", t)
    assert timings.save_device("alice", timings.clean_device(report())) == "turn-1"
    t.finish("interrupted")
    timings.update_hub(row_id, t)
    [row] = timings.rows("alice")
    assert row["hub"]["outcome"] == "interrupted" and row["device"]


def test_another_device_or_owner_or_time_does_not_join(data_dir):
    timings.save_hub("alice", "turn-1", "echo-bedroom", hub_timer(WAKE_MS / 1000 + 3.5))
    timings.save_hub("bob", "turn-2", "echo-kitchen", hub_timer(WAKE_MS / 1000 + 3.5))
    timings.save_hub("alice", "turn-3", "echo-kitchen", hub_timer(WAKE_MS / 1000 + 600))
    assert timings.save_device("alice", timings.clean_device(report())) is None
    found = timings.rows("alice")
    assert len(found) == 3 and sum(1 for r in found if r["device"] and not r["hub"]) == 1


def test_a_hub_row_without_a_device_joins_when_nothing_better(data_dir):
    timings.save_hub("alice", "turn-1", None, hub_timer(WAKE_MS / 1000 + 3.5))
    assert timings.save_device("alice", timings.clean_device(report())) == "turn-1"
    assert timings.rows("alice")[0]["device_id"] == "echo-kitchen"


def test_two_turns_of_one_device_each_get_their_own_report(data_dir):
    timings.save_hub("alice", "turn-1", "echo-kitchen", hub_timer(WAKE_MS / 1000 + 3.5))
    timings.save_hub("alice", "turn-2", "echo-kitchen", hub_timer(WAKE_MS / 1000 + 20))
    second = report(started_at=WAKE_MS + 17_000)
    assert timings.save_device("alice", timings.clean_device(second)) == "turn-2"
    assert timings.save_device("alice", timings.clean_device(report())) == "turn-1"


@pytest.mark.parametrize("bad", [
    None, [], {"started_at": WAKE_MS, "marks": {}},
    {"device_id": "x", "started_at": "soon", "marks": {}},
    {"device_id": "x", "started_at": WAKE_MS, "marks": {"ended": -1}},
    {"device_id": "x", "started_at": WAKE_MS, "marks": {"ended": True}},
    {"device_id": "x" * 200, "started_at": WAKE_MS, "marks": {}},
])
def test_a_bad_report_is_refused(bad):
    with pytest.raises(timings.BadReport):
        timings.clean_device(bad)


def test_unknown_marks_and_flags_are_dropped():
    out = timings.clean_device(report(marks={"ended": 5, "question": 1},
                                      flags={"barge_in": 1, "secret": True}))
    assert out["marks"] == {"ended": 5} and out["flags"] == {"barge_in": True}


def test_summary_gives_median_p90_and_slowest(data_dir):
    for i, stt in enumerate((500, 700, 900, 3000)):
        marks = dict(MARKS, awaiting_response=MARKS["voice_stopped"] + stt)
        timings.save_device("alice", timings.clean_device(
            report(started_at=WAKE_MS + i * 600_000, marks=marks)))
    [stt] = [s for s in timings.summary(timings.rows()) if s["step"] == "Speech to text (HA)"]
    assert stt["turns"] == 4 and stt["median"] == pytest.approx(0.8)
    assert stt["max"] == pytest.approx(3.0) and 0.9 < stt["p90"] < 3.0


# ------------------------------------------------------------------------------ the routes
@pytest.fixture
def api(data_dir):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        request.state.api_token_owner = "alice" if scopes is not None else None
        request.state.api_token_scopes = scopes.split(",") if scopes else []
        return await call_next(request)

    app.include_router(timing_routes.setup_timing_routes())
    with TestClient(app) as client:
        yield client


def test_only_a_voice_token_may_report(api):
    assert api.post(URL, json=report()).status_code == 403
    assert api.post(URL, json=report(), headers={"x-test-scopes": "chat"}).status_code == 403


def test_the_device_reports_its_stages(api):
    timings.save_hub("alice", "turn-1", "echo-kitchen", hub_timer(WAKE_MS / 1000 + 3.5))
    res = api.post(URL, json=report(), headers=VOICE)
    assert res.status_code == 200 and res.json() == {"ok": True, "turn_id": "turn-1"}


def test_a_bad_or_large_report_is_refused(api):
    assert api.post(URL, content=b"{nope", headers=VOICE).status_code == 422
    assert api.post(URL, json=report(started_at=0), headers=VOICE).status_code == 422
    big = report(start_mode="x" * 9000)
    assert api.post(URL, json=big, headers=VOICE).status_code == 413


# ------------------------------------------------------------------------------ the script
def test_the_script_prints_the_steps(data_dir, capsys, tmp_path):
    from scripts import voice_timings

    assert voice_timings.main([]) == 0
    assert "No timings yet" in capsys.readouterr().out
    timings.save_hub("alice", "turn-1", "echo-kitchen", hub_timer(WAKE_MS / 1000 + 3.5))
    timings.save_device("alice", timings.clean_device(report()))
    out_csv = tmp_path / "t.csv"
    assert voice_timings.main(["--days", "1", "--last", "1", "--csv", str(out_csv)]) == 0
    out = capsys.readouterr().out
    assert "Speech to text (HA)" in out and "search_conversations" in out
    assert "Silence after you stopped" in out_csv.read_text()
