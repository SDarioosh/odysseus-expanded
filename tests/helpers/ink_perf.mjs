// Fork: how much main-thread time the Write view (static/js/forkInk.js) needs per frame
// while the pen or the eraser moves fast over a dense page. Chrome's CPU is throttled
// (default 4x) to stand in for a tablet. Prints one JSON line:
// {pen: {meanMs, p95Ms, maxMs}, eraser: {...}, strokes}. "SKIP <reason>" without a browser.
// Run by tests/test_ink_perf.py; also useful by hand: node tests/helpers/ink_perf.mjs
import { launch, openPage } from './ink_view_harness.mjs';

const THROTTLE = Number(process.env.INK_PERF_THROTTLE || 4);
const { browser, skip } = await launch();
if (skip) { console.log('SKIP ' + skip); process.exit(0); }

const page = await openPage(browser, { user: 'perf' });
const cdp = await page.context().newCDPSession(page);
await page.evaluate(async () => { await view.newNote(); await settle(100); });

// A dense page of fictional handwriting: 30 lines of short wavy strokes.
const strokes = await page.evaluate(() => {
  const st = view._state;
  const pg = st.note.pages[0];
  let n = 0;
  for (let line = 0; line < 30; line++) {
    for (let w = 0; w < 50; w++) {
      const x0 = 30 + w * 19;
      const y0 = 130 + line * 44;
      const pts = [];
      for (let i = 0; i < 40; i++) pts.push([x0 + i * 0.4, y0 + Math.sin(i / 3 + w) * 8, i * 8]);
      pg.strokes.push({ id: `s${n++}`, pen: { color: '#1f1f1f', width: 2, highlighter: false }, at: 0, points: pts });
    }
  }
  pg.strokes.push({ id: 'hl', pen: { color: '#ffd400', width: 14, highlighter: true }, at: 0,
    points: [[40, 140, 0], [600, 140, 50]] });
  view.fit();
  return pg.strokes.length;
});
await page.evaluate(() => settle(200));
await cdp.send('Emulation.setCPUThrottlingRate', { rate: THROTTLE });

// Count only main-thread work: the event handlers plus the drawing done in animation
// frames (requestAnimationFrame callbacks are timed), not the wait for the next frame.
await page.evaluate(() => {
  const raf = window.requestAnimationFrame.bind(window);
  window.__rafMs = 0;
  window.requestAnimationFrame = (cb) => raf((t) => { const t0 = performance.now(); try { cb(t); } finally { window.__rafMs += performance.now() - t0; } });
});

// Fast movement: 90 frames, 4 pointer samples per frame (a 240 Hz pen at 60 fps),
// each sample `step` px apart. Returns the main-thread time each frame took.
async function run(eraser) {
  return page.evaluate(async ({ eraser }) => {
    const c = canvas();
    const r = c.getBoundingClientRect();
    const buttons = eraser ? 32 : 1;
    const at = (i) => [60 + (i * 9) % (r.width - 120), 120 + Math.floor((i * 9) / (r.width - 120)) * 40 + (i % 7)];
    const ev = (type, x, y, b) => c.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true,
      pointerType: 'pen', pointerId: 5, clientX: r.left + x, clientY: r.top + y, buttons: b, button: type === 'pointermove' ? -1 : 0 }));
    const frame = () => new Promise((res) => requestAnimationFrame(() => setTimeout(res, 0)));
    let [x, y] = at(0);
    ev('pointerdown', x, y, buttons);
    await frame();
    const times = [];
    let i = 1;
    for (let f = 0; f < 90; f++) {
      window.__rafMs = 0;
      const t0 = performance.now();
      for (let s = 0; s < 4; s++, i++) { [x, y] = at(i); ev('pointermove', x, y, buttons); }
      const handlers = performance.now() - t0;
      await frame();                       // the frame's drawing happens here
      times.push(handlers + window.__rafMs);
    }
    ev('pointerup', x, y, 0);
    await frame();
    times.sort((a, b) => a - b);
    const mean = times.reduce((a, b) => a + b, 0) / times.length;
    return { meanMs: Math.round(mean * 10) / 10, p95Ms: Math.round(times[Math.floor(times.length * 0.95)] * 10) / 10,
      maxMs: Math.round(times[times.length - 1] * 10) / 10 };
  }, { eraser });
}

const pen = await run(false);
const eraser = await run(true);
await browser.close();
console.log(JSON.stringify({ throttle: THROTTLE, strokes, pen, eraser }));
