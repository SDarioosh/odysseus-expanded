// Fork: a harness for the Write view (static/js/forkInk.js) in a real Chromium, with
// the hub's ink routes faked inside the page (window.hub), so tests can set delays,
// failures and the signed-in user. Used by tests/helpers/ink_review_e2e.mjs.
// H2: the fake hub also transcribes (hub.read(noteId, pageIndex) gives the text, after
// hub.readMs; hub.noModel answers 503) and stores reviewed text (hub.content).
// H4: POST .../send stands for Athena (hub.athena[noteId] = the text it has; hub.sends).
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
    online: true, delay: {}, answer: {}, tr: {}, content: {}, athena: {}, sends: [], readMs: 200, noModel: false,
    read: (id, i) => ['Line one of page ' + (i + 1), 'the [?] word'].join(String.fromCharCode(10)) };
  // Transcripts as the hub keeps them: hub.tr[noteId][pageId] = {status, text, hash, ...}
  const hashOf = (pg) => JSON.stringify(pg.strokes.map((st) => st.id));
  const trOf = (n) => (hub.tr[n.note_id] = hub.tr[n.note_id] || {});
  const pageViews = (n) => n.pages.map((pg, i) => {
    const t = trOf(n)[pg.id] || { status: 'none', text: '' };
    const ink = pg.strokes.some((st) => !st.pen.highlighter);
    return { id: pg.id, index: i, status: t.status, text: t.text || '', model: t.model || null, at: t.at || null,
      error: t.error || null, ink, unreadable: (t.text || '').split('[?]').length - 1,
      changed: (t.status === 'machine' || t.status === 'reviewed') && t.hash !== hashOf(pg),
      ...(t.previous ? { previous: t.previous } : {}) };
  });
  const contentOf = (n) => n.pages.map((x) => trOf(n)[x.id]).filter((t) => t && t.status === 'reviewed' && t.text).map((t) => t.text).join(String.fromCharCode(10, 10));
  const athenaOf = (n, pages) => {
    const waiting = pages.filter((p) => p.status === 'machine' || p.status === 'failed' || p.status === 'pending'
      || (p.status === 'none' && p.ink) || (p.status === 'reviewed' && p.changed))
      .map((p) => ({ page: p.index + 1, why: p.status === 'none' ? 'not transcribed' : p.status }));
    const text = contentOf(n);
    const sent = hub.athena[n.note_id];
    const state = sent == null ? 'not_sent' : (sent === text ? 'sent' : 'changed');
    return { state, waiting, has_text: !!text.trim(), sending: false, sent_at: sent == null ? null : '2026-10-26T10:00:00Z',
      transcript_link: sent == null ? null : 'http://athena.local/view/c1?sig=x' };
  };
  const views = (n) => { const pages = pageViews(n); return { note_id: n.note_id, pages, running: pages.some((p) => p.status === 'pending'), athena: athenaOf(n, pages) }; };
  const summary = (n) => { const v = pageViews(n); return { review: v.filter((p) => p.status === 'machine').map((p) => p.index),
    failed: v.filter((p) => p.status === 'failed' && p.ink).map((p) => p.index), pending: v.filter((p) => p.status === 'pending').length }; };
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
        pages: n.pages.length, edited_at: n.edited_at, transcripts: summary(n), athena: athenaOf(n, pageViews(n)).state })) });
    }
    // /api/ink/notes/<id>/transcripts | transcribe | pages/<page>/transcript
    const parts = u.pathname.split('/');
    const sub = parts.slice(5).join('/');
    const m = parts.length >= 6 && parts[3] === 'notes' && (sub === 'transcripts' || sub === 'transcribe' || sub === 'send'
      || (parts.length === 8 && parts[5] === 'pages' && parts[7] === 'transcript'))
      ? [null, parts[4], sub === 'transcripts' || sub === 'transcribe' || sub === 'send' ? sub : 'page', parts[6]] : null;
    if (m) {
      const n = hub.notes[decodeURIComponent(m[1])];
      if (!n || n.owner !== hub.user) return json(404, { detail: 'Note not found' });
      if (m[2] === 'transcripts' && method === 'GET') return json(200, views(n));
      if (m[2] === 'send' && method === 'POST') {
        const v = views(n);
        hub.sends.push(n.note_id);
        if (v.athena.waiting.length) return json(409, { detail: 'Waiting: ' + v.athena.waiting.map((w) => 'page ' + w.page).join(', ') });
        const text = contentOf(n);
        if (!text.trim()) return json(400, { detail: 'No page has reviewed text yet' });
        const before = hub.athena[n.note_id];
        hub.athena[n.note_id] = text;
        const result = before == null ? 'created' : (before === text ? 'unchanged' : 'replaced');
        return json(200, { ...views(n), sent: { result, conversation_id: 'c1', transcript_link: 'http://athena.local/view/c1?sig=x' } });
      }
      if (m[2] === 'transcribe' && method === 'POST') {
        const body = JSON.parse(opts.body || '{}');
        const want = body.pages || n.pages.map((pg) => pg.id);
        if (want.some((pid) => !n.pages.find((pg) => pg.id === pid))) return json(404, { detail: 'Page not found' });
        hub.transcribeCalls = (hub.transcribeCalls || []).concat([{ id: n.note_id, pages: want, only_changed: !!body.only_changed }]);
        if (hub.noModel) return json(503, { detail: 'No vision model is set: choose one in Settings, Vision.' });
        for (const pid of want) {
          const pg = n.pages.find((x) => x.id === pid);
          const t = trOf(n)[pid] || { status: 'none', text: '' };
          const changed = t.hash !== hashOf(pg);
          if (t.status === 'pending') continue;
          if ((body.only_changed || !body.pages) && !(t.status === 'none' || t.status === 'failed' || changed)) continue;
          if (!pg.strokes.length) { trOf(n)[pid] = { status: 'none', text: '', hash: hashOf(pg) }; continue; }
          const prev = t.status === 'reviewed' && t.text ? { text: t.text, at: t.at } : t.previous;
          const index = n.pages.indexOf(pg);
          const hash = hashOf(pg);
          trOf(n)[pid] = { ...t, status: 'pending', previous: prev };
          setTimeout(() => {
            const cur = trOf(n)[pid];
            if (!cur || cur.status !== 'pending') return;
            const text = hub.read(n.note_id, index);
            trOf(n)[pid] = text === null ? { ...cur, status: 'failed', error: 'The model could not read this page: boom' }
              : { ...cur, status: 'machine', text, model: 'fake-vl', hash, at: new Date().toISOString(), error: null };
          }, hub.readMs);
        }
        return json(200, views(n));
      }
      if (m[3] && method === 'PUT') {
        const pid = decodeURIComponent(m[3]);
        const pg = n.pages.find((x) => x.id === pid);
        if (!pg) return json(404, { detail: 'Page not found' });
        const body = JSON.parse(opts.body || '{}');
        if (typeof body.text !== 'string') return json(400, { detail: 'text must be a string' });
        trOf(n)[pid] = { status: 'reviewed', text: body.text.trim(), hash: hashOf(pg), at: new Date().toISOString() };
        hub.content[n.note_id] = contentOf(n);
        return json(200, views(n));
      }
      return json(405, {});
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
