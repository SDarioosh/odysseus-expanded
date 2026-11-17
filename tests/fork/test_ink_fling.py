"""Fork: flick to scroll in the Write view (static/js/forkInkFling.js, static/js/forkInk.js).

The pure logic through `node`: the finger's speed when it lifts, the exponential
coasting, and that the input router says when a palm or pen cancelled a gesture (no
flick then). Skips when node isn't available. Fictional numbers only.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_FLING = (_REPO / "static" / "js" / "forkInkFling.js").as_posix()
_INK = (_REPO / "static" / "js" / "forkInk.js").as_posix()
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")


def _node(body: str) -> dict:
    js = (f"const F = await import('{_FLING}');\nconst out = {{}};\n{body}\n"
          "console.log(JSON.stringify(out));")
    proc = subprocess.run(["node", "--input-type=module"], input=js, capture_output=True,
                          text=True, cwd=str(_REPO), timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@needs_node
def test_speed_is_the_fingers_over_its_last_moves_and_has_its_direction():
    got = _node("""
    const down = F.createVelocityTracker();           // 1000 px/s upwards, a move every 16 ms
    for (let i = 0; i <= 10; i++) down.add(i * 16, 300, 800 - i * 16);
    out.up = down.release(160 + 10);
    const right = F.createVelocityTracker();
    for (let i = 0; i <= 10; i++) right.add(i * 8, 100 + i * 12, 500);   // 1500 px/s to the right
    out.right = right.release(80 + 5);
    // It sped up: only the last 100 ms count (slow for 300 ms, then 2000 px/s).
    const fast = F.createVelocityTracker();
    for (let i = 0; i <= 20; i++) fast.add(i * 16, 0, 1000 - i * 2);              // slow
    for (let i = 1; i <= 6; i++) fast.add(320 + i * 16, 0, 1000 - 40 - i * 32);   // 2000 px/s
    out.fast = fast.release(320 + 96 + 5);
    """)
    assert got["up"]["vx"] == pytest.approx(0, abs=1e-6)
    assert got["up"]["vy"] == pytest.approx(-1000, rel=0.02)
    assert got["right"]["vx"] == pytest.approx(1500, rel=0.02)
    assert got["right"]["vy"] == pytest.approx(0, abs=1e-6)
    assert got["fast"]["vy"] == pytest.approx(-2000, rel=0.1)


@needs_node
def test_no_flick_when_the_finger_rested_was_slow_or_was_a_single_move():
    got = _node("""
    const t = F.createVelocityTracker();
    for (let i = 0; i <= 10; i++) t.add(i * 16, 0, 800 - i * 16);
    out.rested = t.release(160 + 120);                // lifted 120 ms after its last move
    out.justLifted = t.release(160 + 40) !== null;
    const slow = F.createVelocityTracker();
    for (let i = 0; i <= 10; i++) slow.add(i * 16, 0, 800 - i * 0.4);   // 25 px/s
    out.slow = slow.release(165);
    const one = F.createVelocityTracker();
    one.add(0, 0, 0);
    out.one = one.release(10);
    const none = F.createVelocityTracker();
    out.none = none.release(10);
    const reset = F.createVelocityTracker();
    for (let i = 0; i <= 10; i++) reset.add(i * 16, 0, 800 - i * 16);
    reset.reset();
    out.reset = reset.release(170);
    """)
    assert got["rested"] is None
    assert got["justLifted"] is True
    assert got["slow"] is None
    assert got["one"] is None
    assert got["none"] is None
    assert got["reset"] is None


@needs_node
def test_a_very_fast_flick_is_capped_keeping_its_direction():
    got = _node("""
    out.capped = F.startSpeed({ vx: -30000, vy: 40000 });
    out.slow = F.startSpeed({ vx: 20, vy: 20 });
    out.edge = F.startSpeed({ vx: F.FLING.MIN_SPEED, vy: 0 });
    out.max = F.FLING.MAX_SPEED;
    """)
    assert (got["capped"]["vx"] ** 2 + got["capped"]["vy"] ** 2) ** 0.5 == pytest.approx(got["max"], rel=1e-9)
    assert got["capped"]["vx"] < 0 < got["capped"]["vy"]
    assert got["capped"]["vx"] / got["capped"]["vy"] == pytest.approx(-0.75)
    assert got["slow"] is None
    assert got["edge"] == {"vx": 50, "vy": 0}


@needs_node
def test_coasting_goes_the_same_distance_at_any_frame_rate_and_stops():
    got = _node("""
    const run = (hz) => {
      let v = { vx: 0, vy: -2000 }, y = 0, t = 0;
      const dt = 1000 / hz;
      while (!F.flingDone(v) && t < 10000) { const s = F.flingStep(v, dt); y += s.dy; v = { vx: s.vx, vy: s.vy }; t += dt; }
      return { y: Math.round(y), ms: Math.round(t) };
    };
    out.hz60 = run(60); out.hz120 = run(120); out.hz30 = run(30);
    out.analytic = Math.round(F.flingDistance(2000));
    out.duration = Math.round(F.flingDuration(2000));
    out.tau = F.FLING.TAU_MS;
    """)
    # Total distance = speed x tau (2000 px/s x 0.3 s = 600 px), less what is left at the stop.
    assert got["analytic"] == 600
    for key in ("hz60", "hz120", "hz30"):
        assert got[key]["y"] == pytest.approx(-600, abs=15)
    assert abs(got["hz60"]["y"] - got["hz120"]["y"]) <= 2
    # About a second for a hard flick; never long.
    assert 1000 <= got["hz60"]["ms"] <= 1700
    assert got["hz60"]["ms"] == pytest.approx(got["duration"], abs=40)


@needs_node
def test_a_late_frame_counts_as_a_short_one_and_speed_never_grows():
    got = _node("""
    const v = { vx: 500, vy: 0 };
    const late = F.flingStep(v, 5000);          // the app was stalled for 5 s
    const capped = F.flingStep(v, F.FLING.MAX_STEP_MS);
    out.same = late.dx === capped.dx && late.vx === capped.vx;
    out.negative = F.flingStep(v, -30).dx;
    out.slower = late.vx < v.vx && late.vx > 0;
    out.done = [F.flingDone({ vx: 5, vy: 5 }), F.flingDone({ vx: 100, vy: 0 })];
    """)
    assert got["same"] is True
    assert got["negative"] == 0
    assert got["slower"] is True
    assert got["done"] == [True, False]


@needs_node
def test_router_says_when_a_pen_or_palm_cancelled_the_gesture():
    js = f"""
const {{ createInputRouter, PALM_MS }} = await import('{_INK}');
let t = 1000;
const ends = [];
const r = createInputRouter({{
  drawStart() {{}}, drawMove() {{}}, drawEnd() {{}}, drawSwitch() {{}},
  gesture() {{}},
  gestureEnd: (info) => ends.push(info === undefined ? 'lift' : JSON.stringify(info)),
}}, () => t);
const ev = (type, pointerType, pointerId, x = 0, y = 0, extra = {{}}) =>
  r.handle({{ type, pointerType, pointerId, clientX: x, clientY: y, buttons: 0, button: 0, ...extra }});
// a finger drags and lifts: a plain end (a flick may follow)
ev('pointerdown', 'touch', 1, 0, 100, {{ buttons: 1 }});
ev('pointermove', 'touch', 1, 0, 60, {{ buttons: 1 }});
ev('pointerup', 'touch', 1, 0, 60);
// a finger drags, then the pen comes near: the gesture is cancelled (no flick)
t += PALM_MS + 1;
ev('pointerdown', 'touch', 2, 0, 100, {{ buttons: 1 }});
ev('pointermove', 'touch', 2, 0, 60, {{ buttons: 1 }});
ev('pointermove', 'pen', 3, 5, 5);
ev('pointermove', 'touch', 2, 0, 20, {{ buttons: 1 }});
ev('pointerup', 'touch', 2, 0, 20);
console.log(JSON.stringify(ends));
"""
    proc = subprocess.run(["node", "--input-type=module"], input=js, capture_output=True,
                          text=True, cwd=str(_REPO), timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == ["lift", '{"dropped":true}']
