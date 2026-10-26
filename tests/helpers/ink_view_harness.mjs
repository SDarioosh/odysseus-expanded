// Fork: a harness for the Write view (static/js/forkInk.js) in a real Chromium, with
// the hub's ink routes faked inside the page (window.hub), so tests can set delays,
// failures and the signed-in user. Used by tests/helpers/ink_review_e2e.mjs.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const require = createRequire(import.meta.url);
export const REPO = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..');

const PAGE = `<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/fork-modules.css">
<style>:root{--bg:#fff;--fg:#222;--panel:#f6f6f6;--border:#ddd;--red:#c33}
html,body{margin:0;height:100%}#host{width:900px;height:760px;display:flex;flex-direction:column}</style>
</head><body><div id="host"></div>
<script>
  // The fake hub: notes by id, per user; options per test.
  window.hub = { user: localStorage.getItem('odysseus-auth-user') || 'alice', notes: {}, calls: [],
    online: true, delay: {}, answer: {} };
  const realFetch = window.fetch.bind(window);
  window.fetch = async (url, opts = {}) => {
    const u = new URL(url, location.href);
    if (!u.pathname.startsWith('/api/')) return realFetch(url, opts);
    const method = (opts.method || 'GET').toUpperCase();
    const hub = window.hub;
    hub.calls.push(method + ' ' + u.pathname);
    if (!hub.online) throw new TypeError('Failed to fetch');
    const wait = hub.delay[method + ' ' + u.pathname] || hub.delay[method] || 0;
    if (wait) await new Promise((r) => setTimeout(r, wait));
    const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
    const forced = hub.answer[method + ' ' + u.pathname];
    if (forced) return json(forced.status, typeof forced.body === 'function' ? forced.body() : forced.body);
    if (u.pathname === '/api/auth/status') return json(200, { username: hub.user });
    const mine = Object.values(hub.notes).filter((n) => n.owner === hub.user);
    if (u.pathname === '/api/ink/notes' && method === 'GET') {
      return json(200, { notes: mine.map((n) => ({ id: n.note_id, title: n.title, image_url: n.image_url,
        pages: n.pages.length, edited_at: n.edited_at })) });
    }
    const id = u.pathname.split('/').pop();
    const n = hub.notes[id];
    if (method === 'GET') return n && n.owner === hub.user ? json(200, n) : json(404, { detail: 'Note not found' });
    if (method === 'DELETE') { if (!n || n.owner !== hub.user) return json(404, {}); delete hub.notes[id]; return json(200, { ok: true }); }
    if (method === 'PUT') {
      const body = JSON.parse(opts.body);
      if (n && n.owner !== hub.user) return json(404, { detail: 'Note not found' });
      if (n && body.edited_at < n.edited_at) return json(409, { detail: 'newer', note: n });
      const strokes = body.pages[0].strokes.length;
      const saved = { ...body, note_id: id, owner: hub.user, image_url: strokes ? '/api/upload/img-' + id.slice(0, 4) + '-' + strokes + '.png' : null };
      hub.notes[id] = saved;
      return json(200, saved);
    }
    return json(404, {});
  };
</script>
<script type="module">
  import { mountInk } from '/static/js/forkInk.js';
  window.mountInk = mountInk;
  window.view = mountInk(document.getElementById('host'));
  window.errors = [];
  window.addEventListener('error', (e) => window.errors.push(String(e.message)));
  window.addEventListener('unhandledrejection', (e) => window.errors.push(String(e.reason && e.reason.message || e.reason)));
  window.ready = true;
</script></body></html>`;

export async function launch() {
  let chromium;
  try { ({ chromium } = require('playwright')); } catch (e) { return { skip: 'no playwright' }; }
  const fallback = process.env.PLAYWRIGHT_CHROMIUM || '/opt/pw-browsers/chromium';
  let browser;
  try { browser = await chromium.launch(); } catch (e) {
    try {
      if (!fs.existsSync(fallback)) throw e;
      browser = await chromium.launch({ executablePath: fallback });
    } catch (e2) { return { skip: String(e2.message).split('\n')[0] }; }
  }
  return { browser };
}

export async function openPage(browser, { user = 'alice', context = null } = {}) {
  const ctx = context || await browser.newContext({ viewport: { width: 1000, height: 800 } });
  const page = await ctx.newPage();
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: PAGE });
    if (url.pathname.startsWith('/static/')) {
      const file = path.join(REPO, url.pathname);
      if (!file.startsWith(path.join(REPO, 'static')) || !fs.existsSync(file)) return route.fulfill({ status: 404, body: '' });
      const type = file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'application/octet-stream';
      return route.fulfill({ contentType: type, body: fs.readFileSync(file) });
    }
    return route.fulfill({ status: 404, body: '' });
  });
  await page.addInitScript((u) => { localStorage.setItem('odysseus-auth-user', u); }, user);
  await page.goto('http://hub.test/');
  await page.waitForFunction(() => window.ready === true);
  await page.evaluate(() => {
    window.canvas = () => document.querySelector('.ink-live');
    window.fire = (type, pointerType, pointerId, x, y, extra = {}) => {
      const c = window.canvas();
      const r = c.getBoundingClientRect();
      c.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, pointerType, pointerId,
        isPrimary: true, clientX: r.left + x, clientY: r.top + y, ...extra }));
    };
    window.stroke = (pointerType, pointerId, pts, extra = {}) => {
      const down = { buttons: 1, button: 0, ...extra };
      fire('pointerdown', pointerType, pointerId, pts[0][0], pts[0][1], down);
      for (const [x, y] of pts.slice(1)) fire('pointermove', pointerType, pointerId, x, y, { ...down, button: -1 });
      const last = pts[pts.length - 1];
      fire('pointerup', pointerType, pointerId, last[0], last[1], { buttons: 0, button: 0 });
    };
    window.idbNotes = (name) => new Promise((resolve) => {
      const req = indexedDB.open(name);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('notes')) { resolve([]); return; }
        const get = db.transaction('notes').objectStore('notes').getAll();
        get.onsuccess = () => resolve(get.result);
      };
      req.onerror = () => resolve([]);
    });
    window.settle = (ms = 50) => new Promise((r) => setTimeout(r, ms));
  });
  return page;
}
