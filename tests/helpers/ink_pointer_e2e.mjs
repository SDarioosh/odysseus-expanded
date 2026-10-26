// Fork: the Write view in a real Chromium, driven with synthetic pointer events
// (tests/test_ink_palm_rejection.py). Serves static/ from disk and stands in for the
// hub's ink routes. Prints one JSON line with what happened; "SKIP <reason>" when no
// browser can be started.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const require = createRequire(import.meta.url);
const REPO = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) { console.log('SKIP no playwright'); process.exit(0); }

const PAGE = `<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/fork-modules.css">
<style>:root{--bg:#fff;--fg:#222;--panel:#f6f6f6;--border:#ddd;--red:#c33}
html,body{margin:0;height:100%}#host{width:900px;height:760px;display:flex;flex-direction:column}</style>
</head><body><div id="host"></div>
<script type="module">
  import { mountInk } from '/static/js/forkInk.js';
  window.view = mountInk(document.getElementById('host'));
  window.ready = true;
</script></body></html>`;

// Playwright's own browser, else one named by PLAYWRIGHT_CHROMIUM (or a sandbox's
// preinstalled /opt/pw-browsers/chromium) when the pinned version isn't installed.
let browser;
const fallback = process.env.PLAYWRIGHT_CHROMIUM || '/opt/pw-browsers/chromium';
try { browser = await chromium.launch(); } catch (e) {
  try {
    if (!fs.existsSync(fallback)) throw e;
    browser = await chromium.launch({ executablePath: fallback });
  } catch (e2) { console.log('SKIP ' + String(e2.message).split('\n')[0]); process.exit(0); }
}
const page = await browser.newPage({ viewport: { width: 1000, height: 800 } });
const saves = [];
await page.route('**/*', async (route) => {
  const url = new URL(route.request().url());
  if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: PAGE });
  if (url.pathname === '/api/ink/notes') return route.fulfill({ json: { notes: [] } });
  if (url.pathname.startsWith('/api/ink/notes/')) {
    if (route.request().method() === 'PUT') {
      const body = JSON.parse(route.request().postData());
      saves.push(body);
      return route.fulfill({ json: { ...body, note_id: url.pathname.split('/').pop(), image_url: null } });
    }
    return route.fulfill({ status: 404, json: { detail: 'Note not found' } });
  }
  if (url.pathname.startsWith('/static/')) {
    const file = path.join(REPO, url.pathname);
    if (!file.startsWith(path.join(REPO, 'static')) || !fs.existsSync(file)) return route.fulfill({ status: 404, body: '' });
    const type = file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'application/octet-stream';
    return route.fulfill({ contentType: type, body: fs.readFileSync(file) });
  }
  return route.fulfill({ status: 404, body: '' });
});
await page.goto('http://hub.test/');
await page.waitForFunction(() => window.ready === true);
await page.evaluate(() => window.view.newNote());
await page.waitForFunction(() => document.querySelector('.ink-live').width > 0);

// Synthetic pointer events on the writing area, relative to its top-left corner.
await page.evaluate(() => {
  const c = document.querySelector('.ink-live');
  window.fire = (type, pointerType, pointerId, x, y, extra = {}) => {
    const r = c.getBoundingClientRect();
    c.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, pointerType, pointerId,
      isPrimary: true, clientX: r.left + x, clientY: r.top + y, ...extra }));
  };
  window.drag = (pointerType, pointerId, pts, extra = {}) => {
    const down = { buttons: 1, button: 0, ...extra };
    fire('pointerdown', pointerType, pointerId, pts[0][0], pts[0][1], down);
    for (const [x, y] of pts.slice(1)) fire('pointermove', pointerType, pointerId, x, y, { ...down, button: -1 });
    const last = pts[pts.length - 1];
    fire('pointerup', pointerType, pointerId, last[0], last[1], { buttons: 0, button: 0 });
  };
  window.snap = () => ({ strokes: view.strokeCount(), sy: view._state.sy, zoom: view._state.zoom });
});
const line = (y) => [[150, y], [200, y + 5], [260, y], [320, y + 5], [380, y]];
const snap = () => page.evaluate(() => window.snap());
const out = {};

out.start = await snap();
// 1. A pen hovers (no buttons), then a palm drags: nothing drawn, nothing scrolled.
await page.evaluate(() => fire('pointermove', 'pen', 7, 300, 300, { buttons: 0 }));
await page.evaluate((pts) => drag('touch', 21, pts), [[300, 600], [300, 500], [300, 400]]);
out.palmWhileHovering = await snap();
// 2. The pen writes a stroke.
await page.evaluate((pts) => drag('pen', 7, pts), line(200));
out.afterPen = await snap();
// 3. Right after the pen lifts, a palm drag is still ignored.
await page.evaluate((pts) => drag('touch', 22, pts), [[300, 600], [300, 450], [320, 300]]);
out.palmAfterPen = await snap();
// 4. The palm is down first, then the pen writes while the palm moves: the pen wins.
await page.evaluate(() => fire('pointerdown', 'touch', 23, 600, 650, { buttons: 1, button: 0 }));
await page.evaluate((pts) => {
  fire('pointerdown', 'pen', 7, pts[0][0], pts[0][1], { buttons: 1, button: 0 });
  for (const [x, y] of pts.slice(1)) {
    fire('pointermove', 'touch', 23, 600, 650 - x / 10, { buttons: 1 });
    fire('pointermove', 'pen', 7, x, y, { buttons: 1 });
  }
  fire('pointerup', 'pen', 7, 380, 300, { buttons: 0, button: 0 });
  fire('pointerup', 'touch', 23, 600, 600, { buttons: 0, button: 0 });
}, line(300));
out.penWithPalmDown = await snap();
// 5. After the palm window has passed, one finger scrolls and still draws nothing.
await page.waitForTimeout(1700);
await page.evaluate((pts) => drag('touch', 24, pts), [[500, 600], [500, 500], [500, 400]]);
out.fingerScroll = await snap();
// 6. Two fingers zoom.
await page.evaluate(() => {
  fire('pointerdown', 'touch', 25, 400, 400, { buttons: 1, button: 0 });
  fire('pointerdown', 'touch', 26, 500, 400, { buttons: 1, button: 0 });
  fire('pointermove', 'touch', 25, 350, 400, { buttons: 1 });
  fire('pointermove', 'touch', 26, 550, 400, { buttons: 1 });
  fire('pointerup', 'touch', 25, 350, 400, { buttons: 0, button: 0 });
  fire('pointerup', 'touch', 26, 550, 400, { buttons: 0, button: 0 });
});
out.pinch = await snap();
// Back to zoom 1 at the top, so the strokes are where they were drawn.
await page.evaluate(() => view.fit());
// 7. A mouse draws (desktop testing).
await page.evaluate((pts) => drag('mouse', 1, pts), line(400));
out.mouse = await snap();
// 8. The barrel button erases while held: across the first stroke.
await page.evaluate((pts) => drag('pen', 7, pts, { buttons: 33 }), [[260, 160], [260, 210], [260, 260]]);
out.barrelErase = await snap();
// 9. Undo brings it back; redo erases again; undo again.
await page.evaluate(() => document.querySelector('[aria-label="Undo"]').click());
out.undo = await snap();
await page.evaluate(() => document.querySelector('[aria-label="Redo"]').click());
out.redo = await snap();
await page.evaluate(() => document.querySelector('[aria-label="Undo"]').click());
// 10. Saving: IndexedDB first, then the hub.
await page.evaluate(() => view.flush());
await page.waitForFunction(() => document.querySelector('.ink-status').textContent === 'Saved');
out.saved = saves.length ? saves[saves.length - 1] : null;
out.local = await page.evaluate(() => new Promise((resolve) => {
  const req = indexedDB.open('fork-ink', 1);
  req.onsuccess = () => {
    const get = req.result.transaction('notes').objectStore('notes').getAll();
    get.onsuccess = () => resolve(get.result.map((n) => ({ synced: n.synced,
      strokes: n.pages.reduce((s, p) => s + p.strokes.length, 0) })));
  };
}));
out.touchAction = await page.evaluate(() => getComputedStyle(document.querySelector('.ink-live')).touchAction);
out.desynchronized = await page.evaluate(() => {
  const ctx = document.querySelector('.ink-live').getContext('2d');
  return typeof ctx.getContextAttributes === 'function' ? !!ctx.getContextAttributes().desynchronized : null;
});
await browser.close();
console.log(JSON.stringify(out));
