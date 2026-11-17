"""Fork: flick to scroll in the Write view (static/js/forkInkFling.js, static/js/forkInk.js).

The pure logic through `node`: the finger's speed when it lifts, the exponential
coasting, and that the input router says when a palm or pen cancelled a gesture (no
flick then). Skips when node isn't available. Fictional numbers only.
"""
import json
import math
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
    out.gain = F.FLING.GAIN;
    """)
    g = got["gain"]   # the coast starts a little faster than the finger
    assert 1 <= g <= 1.3
    assert got["up"]["vx"] == pytest.approx(0, abs=1e-6)
    assert got["up"]["vy"] == pytest.approx(-1000 * g, rel=0.02)
    assert got["right"]["vx"] == pytest.approx(1500 * g, rel=0.02)
    assert got["right"]["vy"] == pytest.approx(0, abs=1e-6)
    assert got["fast"]["vy"] == pytest.approx(-2000 * g, rel=0.1)


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
    out.max = F.FLING.MAX_SPEED; out.min = F.FLING.MIN_SPEED; out.gain = F.FLING.GAIN;
    """)
    assert (got["capped"]["vx"] ** 2 + got["capped"]["vy"] ** 2) ** 0.5 == pytest.approx(got["max"], rel=1e-9)
    assert got["capped"]["vx"] < 0 < got["capped"]["vy"]
    assert got["capped"]["vx"] / got["capped"]["vy"] == pytest.approx(-0.75)
    assert got["slow"] is None
    assert got["edge"]["vx"] == pytest.approx(got["min"] * got["gain"]) and got["edge"]["vy"] == 0
    # A light flick counts: the threshold is low (more sensitive than Android's 50 px/s).
    assert got["min"] <= 50


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
    # Total distance = speed x tau (2000 px/s x tau), less what is left at the stop.
    full = 2000 * got["tau"] / 1000
    assert got["analytic"] == round(full)
    for key in ("hz60", "hz120", "hz30"):
        assert got[key]["y"] == pytest.approx(-full, abs=25)
    assert abs(got["hz60"]["y"] - got["hz120"]["y"]) <= 2
    # A long, gliding coast: a 2000 px/s flick goes on over two seconds, never forever.
    assert 450 <= got["tau"] <= 650
    assert 2000 <= got["hz60"]["ms"] <= 3500
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
def test_slide_plan_goes_the_distance_of_the_coast_on_each_axis():
    got = _node("""
    const room = { xPos: 1e6, xNeg: 1e6, yPos: 1e6, yNeg: 1e6 };
    out.up = F.slidePlan({ vx: 0, vy: -2000 }, room);             // a finger flicking up: the page moves up
    out.diag = F.slidePlan({ vx: 600, vy: 800 }, room);           // 1000 px/s along (0.6, 0.8)
    out.slow = F.slidePlan({ vx: 10, vy: 5 }, room);
    out.none = F.slidePlan({ vx: 0, vy: 0 }, room);
    out.tau = F.FLING.TAU_MS; out.stop = F.FLING.STOP_SPEED;
    """)
    # 2000 px/s x tau, less what is left at the stop (stop speed x tau).
    tau, stop = got["tau"], got["stop"]
    assert got["up"]["y"]["dist"] == pytest.approx(-(2000 - stop) * tau / 1000, abs=0.01)
    assert got["up"]["x"] == {"dist": 0, "ms": 0}
    assert got["up"]["ms"] == pytest.approx(tau * math.log(2000 / stop), rel=1e-9)   # until the speed is the stop speed
    # Both axes take the same time and split the distance by their speeds.
    assert got["diag"]["x"]["ms"] == got["diag"]["y"]["ms"] == got["diag"]["ms"]
    assert got["diag"]["x"]["dist"] / got["diag"]["y"]["dist"] == pytest.approx(0.75)
    assert got["slow"] is None and got["none"] is None


@needs_node
def test_slide_plan_stops_an_axis_at_its_edge_and_caps_the_travel():
    got = _node("""
    // 2000 px/s upwards would coast 594 px; only 200 px of page is left in that direction.
    out.edge = F.slidePlan({ vx: 0, vy: -2000 }, { xPos: 0, xNeg: 0, yPos: 1000, yNeg: 200 });
    // Nothing left that way at all: no slide.
    out.wall = F.slidePlan({ vx: 0, vy: -2000 }, { xPos: 0, xNeg: 0, yPos: 1000, yNeg: 0 });
    // The x axis meets its edge, y goes on.
    out.xedge = F.slidePlan({ vx: 1000, vy: 1000 }, { xPos: 50, xNeg: 0, yPos: 1e6, yNeg: 0 });
    // A cap on the travel (the strip's size).
    out.cap = F.slidePlan({ vx: 0, vy: 8000 }, { xPos: 0, xNeg: 0, yPos: 1e6, yNeg: 0 }, { maxTravel: 1000 });
    out.free = F.slidePlan({ vx: 0, vy: 8000 }, { xPos: 0, xNeg: 0, yPos: 1e6, yNeg: 0 });
    // Reaching the edge happens before the natural stop, and the curve agrees: the share
    // of the *natural* coast covered at that time is what the edge allows.
    const nat = out.free.y;
    out.edgeTime = out.edge.y.ms;
    out.shareAtEdge = F.slideFraction(out.edge.y.ms, nat.ms) ;
    """)
    assert got["edge"]["y"]["dist"] == -200
    assert 0 < got["edge"]["y"]["ms"] < got["edge"]["ms"] + 1
    assert got["wall"] is None
    assert got["xedge"]["x"]["dist"] == 50
    assert got["xedge"]["y"]["dist"] > 200
    assert got["xedge"]["x"]["ms"] < got["xedge"]["y"]["ms"] == got["xedge"]["ms"]
    assert got["cap"]["y"]["dist"] == 1000 and got["cap"]["y"].get("capped") is True
    assert "capped" not in got["edge"]["y"]
    assert got["free"]["y"]["dist"] > 2000


@needs_node
def test_slide_curve_matches_the_coast_and_the_easing_is_that_curve():
    got = _node("""
    // The frame by frame coast and the slide's curve must agree at every moment.
    const v0 = 3000, ms = 1500;
    let v = { vx: 0, vy: v0 }, y = 0, t = 0;
    const checks = [];
    for (let i = 0; i < 90; i++) { const s = F.flingStep(v, 1000 / 60); y += s.dy; v = { vx: 0, vy: s.vy }; t += 1000 / 60;
      if (i % 15 === 14) checks.push([t, y]); }
    const total = v0 * F.FLING.TAU_MS / 1000 * (1 - Math.exp(-t / F.FLING.TAU_MS));   // distance by time t
    out.steps = checks.map(([tt, yy]) => [yy / (v0 * F.FLING.TAU_MS / 1000 * (1 - Math.exp(-tt / F.FLING.TAU_MS))),
      F.slideFraction(tt, t) * (1 - Math.exp(-t / F.FLING.TAU_MS)) / (1 - Math.exp(-tt / F.FLING.TAU_MS)) ]);
    out.f0 = F.slideFraction(0, ms); out.f1 = F.slideFraction(ms, ms); out.over = F.slideFraction(ms * 3, ms);
    out.mid = F.slideFraction(ms / 2, ms);
    out.easing = F.slideEasing(ms);
    out.zero = F.slideFraction(100, 0);
    """)
    for ratio_coast, ratio_slide in got["steps"]:
        assert ratio_coast == pytest.approx(1.0, abs=1e-9)    # the stepper is exact
        assert ratio_slide == pytest.approx(1.0, abs=1e-9)    # and so is the slide's fraction
    assert got["f0"] == 0 and got["f1"] == 1 and got["over"] == 1 and got["zero"] == 1
    # Fast at the start: well over half the way in the first half of the time.
    assert 0.8 < got["mid"] < 1
    points = [float(p) for p in got["easing"][len("linear("):-1].split(",")]
    assert len(points) == 97 and points[0] == 0 and points[-1] == 1
    assert all(b >= a for a, b in zip(points, points[1:]))
    # The easing is the curve sampled: a mid point agrees to the rounding.
    assert points[48] == pytest.approx(got["mid"], abs=1e-4)


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


@needs_node
def test_a_coast_cut_short_by_the_strip_goes_on_with_the_speed_it_had():
    got = _node("""
    const room = { xPos: 0, xNeg: 0, yPos: 1e6, yNeg: 0 };
    const v = { vx: 0, vy: 8000 };
    const plan = F.slidePlan(v, room, { maxTravel: 1000 });
    out.rest = F.slideRest(v, plan);
    // The rest of the coast, chained until it stops, adds up to the uncut coast.
    let total = plan.y.dist, next = out.rest, n = 0;
    while (next && n < 20) { const p = F.slidePlan(next, room, { maxTravel: 1000 }); if (!p) break; total += p.y.dist; next = F.slideRest(next, p); n++; }
    out.total = total; out.n = n;
    out.free = F.slidePlan(v, room).y.dist;
    // An axis that met its edge has no rest.
    out.edge = F.slideRest(v, F.slidePlan(v, { xPos: 0, xNeg: 0, yPos: 300, yNeg: 0 }, { maxTravel: 1000 }));
    out.none = F.slideRest(v, null);
    """)
    assert 0 < got["rest"]["vy"] < 8000 and got["rest"]["vx"] == 0
    assert got["n"] >= 2
    assert got["total"] == pytest.approx(got["free"], rel=0.01)
    assert got["edge"] is None and got["none"] is None
