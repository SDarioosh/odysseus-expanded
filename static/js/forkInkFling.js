// Fork: flick-to-scroll for the Write view (static/js/forkInk.js).
//
// Pure logic, no DOM (tests/fork/test_ink_fling.py runs it through node). Like the
// platform's own scrolling (iOS decelerates exponentially, Android's OverScroller is a
// similar curve): the finger's speed over its last ~100 ms is kept when it lifts and
// decays exponentially. A finger that rested before lifting gives no fling, a slow one
// gives none, a very fast one is capped, and a new touch or the edge of the page stops it.

export const FLING = {
  WINDOW_MS: 100,   // the speed is measured over the last this-many ms of the drag
  STALE_MS: 60,     // the last move was longer ago than this when the finger lifted: it rested
  MIN_SPEED: 50,    // px/s: slower than this is a drag, not a flick (Android's minimum)
  MAX_SPEED: 8000,  // px/s: a cap (Android's maximum)
  STOP_SPEED: 20,   // px/s: the coasting stops below this
  TAU_MS: 300,      // the speed falls to 1/e of itself every TAU_MS (a flick coasts about 1 s)
  MAX_STEP_MS: 48,  // a frame later than this (a hiccup) counts as this long
};

// Remembers the last moves of one finger: add(t, x, y) per move, release(t) when it lifts.
export function createVelocityTracker({ windowMs = FLING.WINDOW_MS, staleMs = FLING.STALE_MS } = {}) {
  let samples = [];
  return {
    add(t, x, y) {
      samples.push({ t, x, y });
      while (samples.length > 2 && t - samples[0].t > windowMs) samples.shift();
    },
    reset() { samples = []; },
    // {vx, vy} in px/s at time t (the finger lifts), or null when it didn't flick.
    release(t) {
      const n = samples.length;
      if (n < 2) return null;
      const last = samples[n - 1];
      if (t - last.t > staleMs) return null;                  // it rested before lifting
      let first = samples[0];
      for (const s of samples) { if (last.t - s.t <= windowMs) { first = s; break; } }
      const dt = last.t - first.t;
      if (dt < 8) return null;                                // too short to tell a speed
      return startSpeed({ vx: (last.x - first.x) / dt * 1000, vy: (last.y - first.y) / dt * 1000 });
    },
  };
}

// The speed to coast with: capped, or null when it is too slow to be a flick.
export function startSpeed({ vx, vy }) {
  const speed = Math.hypot(vx, vy);
  if (!(speed >= FLING.MIN_SPEED)) return null;
  const scale = speed > FLING.MAX_SPEED ? FLING.MAX_SPEED / speed : 1;
  return { vx: vx * scale, vy: vy * scale };
}

// One step of dtMs: the distance moved (px, the same sign as the speed) and the speed
// left. Exact for the exponential curve, so the total distance doesn't depend on the
// frame rate (60 or 120 Hz).
export function flingStep(v, dtMs, tau = FLING.TAU_MS) {
  const dt = Math.min(Math.max(0, dtMs), FLING.MAX_STEP_MS);
  const keep = Math.exp(-dt / tau);
  const reach = (tau / 1000) * (1 - keep);
  return { dx: v.vx * reach, dy: v.vy * reach, vx: v.vx * keep, vy: v.vy * keep };
}

export const flingDone = (v) => Math.hypot(v.vx, v.vy) < FLING.STOP_SPEED;

// ---- the coast as one slide (the view draws a strip once and the compositor moves it)
//
// Redrawing the whole canvas every frame of a coast makes the GPU miss vsyncs on a
// tablet. The distance of a flick is known when the finger lifts, so the view draws the
// pages once over that distance and slides the picture along the same curve.

// Where the coast of `v` ({vx, vy}, px/s, the finger's) ends, per axis. `room` is how far
// the content can still move in each direction ({xPos, xNeg, yPos, yNeg}, px: Pos is the
// way of a positive speed). An axis that meets its edge stops there, earlier than the
// other. null when nothing would move. {x: {dist, ms}, y: {dist, ms}, ms}.
export function slidePlan(v, room, { tau = FLING.TAU_MS, stopSpeed = FLING.STOP_SPEED, maxTravel = Infinity,
  minTravel = 1 } = {}) {
  const speed = Math.hypot(v.vx, v.vy);
  if (!(speed > stopSpeed)) return null;
  const natural = tau * Math.log(speed / stopSpeed);   // ms until the speed is stopSpeed
  const axis = (s, pos, neg) => {
    const a = Math.abs(s);
    if (a < 1e-6) return { dist: 0, ms: 0 };
    const total = a * tau / 1000;                      // px it would coast in all
    const reach = total * (1 - Math.exp(-natural / tau));
    const limit = Math.min(s > 0 ? pos : neg, maxTravel);
    const sign = s > 0 ? 1 : -1;
    if (reach <= limit) return reach < minTravel ? { dist: 0, ms: 0 } : { dist: sign * reach, ms: natural };
    if (!(limit >= minTravel)) return { dist: 0, ms: 0 };
    return { dist: sign * limit, ms: -tau * Math.log(1 - limit / total) };   // meets its edge
  };
  const x = axis(v.vx, room.xPos, room.xNeg);
  const y = axis(v.vy, room.yPos, room.yNeg);
  if (!x.dist && !y.dist) return null;
  return { x, y, ms: Math.max(x.ms, y.ms) };
}

// The share of an axis's distance covered `tMs` into a coast that lasts `ms`.
export function slideFraction(tMs, ms, tau = FLING.TAU_MS) {
  if (!(ms > 0)) return 1;
  const t = Math.min(Math.max(0, tMs), ms);
  return (1 - Math.exp(-t / tau)) / (1 - Math.exp(-ms / tau));
}

// The same curve as a CSS easing function, for an animation of `ms`.
export function slideEasing(ms, tau = FLING.TAU_MS, points = 96) {
  const out = [];
  for (let i = 0; i <= points; i++) out.push(Math.round(slideFraction(ms * i / points, ms, tau) * 1e5) / 1e5);
  return `linear(${out.join(', ')})`;
}

// How far a flick of `speed` px/s coasts in all (px), and for how long (ms).
export const flingDistance = (speed, tau = FLING.TAU_MS) => speed * tau / 1000;
export const flingDuration = (speed, tau = FLING.TAU_MS) =>
  speed > FLING.STOP_SPEED ? tau * Math.log(speed / FLING.STOP_SPEED) : 0;
