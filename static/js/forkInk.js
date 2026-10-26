// Fork: the Write view, handwritten notes stage H1 (docs/fork/handwritten-notes.md).
// Opened as a fork window by static/js/forkModuleWindows.js (full screen on a tablet).
//
// - Pointer Events. The pen (and the mouse, for desktop testing) draws. Touch never
//   draws: it is ignored while a pen is down or was seen (hovering or touching) in the
//   last PALM_MS (palm rejection); otherwise one finger scrolls and two fingers zoom.
//   The canvas has touch-action: none, so pan and zoom are done here.
// - Preset pens (black, blue, red, green, yellow highlighter drawn under the ink), a
//   thin/medium width, pressure ignored. A stroke eraser; the pen's barrel button
//   erases while held, when Chrome reports it. Unlimited undo/redo on strokes.
// - Strokes are points [x, y, t] in page units (a page is PAGE_W wide), so zoom and
//   rotation keep them aligned. getCoalescedEvents() and a desynchronized 2D context
//   for the stroke being drawn.
// - Autosave every AUTOSAVE_MS and when the app is hidden: IndexedDB first, then the
//   hub (PUT /api/ink/notes/<id>, routes/ink_routes.py). Notes that couldn't reach the
//   hub wait in IndexedDB and sync later. Last edit wins: when the hub has a newer
//   edit (409), its version replaces this one, and the view says so.

export const PAGE_W = 1000;
export const PAGE_H = 1414;
export const PALM_MS = 1500;
const PAGE_GAP = 40;
const SIDE = 24;          // page units around the page at zoom 1
const MAX_PAGE_PX = 1100; // CSS px: widest a page gets at zoom 1 on a big screen
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 4;
const AUTOSAVE_MS = 3000;
const RETRY_MS = 30000;
const ERASER_PX = 14;
const RULE_TOP = 120;
const RULE_STEP = 44;
const API = '/api/ink/notes';
const STYLESHEET = '/static/css/fork-ink.css';

export const PENS = [
  { key: 'black', label: 'Black', color: '#1f1f1f' },
  { key: 'blue', label: 'Blue', color: '#1f5fbf' },
  { key: 'red', label: 'Red', color: '#c62828' },
  { key: 'green', label: 'Green', color: '#2e7d32' },
  { key: 'highlighter', label: 'Highlighter', color: '#ffd400', highlighter: true },
];
const WIDTHS = { thin: { ink: 2, highlighter: 14 }, medium: { ink: 3.5, highlighter: 22 } };
const HIGHLIGHT_ALPHA = 0.35;

// ------------------------------------------------------------------ input router
// Pure logic, no DOM: decides what each pointer event does. Exported for tests
// (tests/test_ink_palm_rejection.py feeds it synthetic events).
export function createInputRouter(handlers, clock = () => performance.now()) {
  const h = handlers;
  let lastPen = -Infinity;
  let pensDown = 0;
  let draw = null;              // { id, eraser }
  const touches = new Map();    // pointerId -> {x, y}, only accepted touches
  let gesture = null;           // { x, y, dist } of the accepted touches

  const penNear = () => pensDown > 0 || clock() - lastPen < PALM_MS;
  const seenPen = (e) => { if (e.pointerType === 'pen') lastPen = clock(); };

  function snapshot() {
    const pts = [...touches.values()];
    if (!pts.length) return null;
    const x = pts.reduce((s, p) => s + p.x, 0) / pts.length;
    const y = pts.reduce((s, p) => s + p.y, 0) / pts.length;
    const dist = pts.length > 1 ? Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) : 0;
    return { x, y, dist, n: pts.length };
  }

  function dropTouches() {
    if (!touches.size) return;
    touches.clear();
    gesture = null;
    if (h.gestureEnd) h.gestureEnd();
  }

  function down(e) {
    seenPen(e);
    if (e.pointerType === 'touch') {
      if (penNear() || draw) return 'ignored'; // palm, or a stroke is being drawn
      touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
      gesture = snapshot();
      return 'gesture';
    }
    if (e.pointerType === 'pen') pensDown += 1;
    if (e.pointerType === 'mouse' && e.button !== 0) return 'ignored';
    if (draw) return 'ignored';
    dropTouches(); // the palm landed before the pen: the pen wins
    const eraser = e.pointerType === 'pen' && (((e.buttons || 0) & 32) !== 0 || e.button === 5);
    draw = { id: e.pointerId, eraser };
    h.drawStart(e, { eraser });
    return 'draw';
  }

  function move(e) {
    seenPen(e);
    if (draw && e.pointerId === draw.id) { h.drawMove(e); return 'draw'; }
    if (e.pointerType === 'pen' && !e.buttons) pensDown = 0; // hovering: no pen is down
    if (e.pointerType !== 'touch') return 'hover';
    if (!touches.has(e.pointerId)) return 'ignored';
    if (penNear()) { dropTouches(); return 'ignored'; }
    touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const now = snapshot();
    if (gesture && now && now.n === gesture.n) {
      if (now.n > 1 && gesture.dist > 0 && h.zoom) h.zoom(now.dist / gesture.dist, now.x, now.y);
      if (h.pan) h.pan(now.x - gesture.x, now.y - gesture.y);
    }
    gesture = now;
    return 'gesture';
  }

  function up(e) {
    seenPen(e);
    if (e.pointerType === 'pen') pensDown = Math.max(0, pensDown - 1);
    if (draw && e.pointerId === draw.id) {
      draw = null;
      h.drawEnd(e);
      return 'draw';
    }
    if (touches.delete(e.pointerId)) {
      gesture = snapshot();
      if (!touches.size && h.gestureEnd) h.gestureEnd();
      return 'gesture';
    }
    return 'ignored';
  }

  function handle(e) {
    switch (e.type) {
      case 'pointerdown': return down(e);
      case 'pointermove': return move(e);
      case 'pointerup':
      case 'pointercancel': return up(e);
      case 'pointerover':
      case 'pointerenter':
      case 'pointerleave':
      case 'pointerout': seenPen(e); return 'hover';
      default: return 'ignored';
    }
  }

  return { handle, penNear, drawing: () => !!draw, notePen: seenPen };
}

// --------------------------------------------------------------------- helpers
function uuid() {
  if (globalThis.crypto && typeof crypto.randomUUID === 'function') {
    try { return crypto.randomUUID(); } catch (_) { /* insecure context */ }
  }
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const hex = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const shortId = () => uuid().replace(/-/g, '').slice(0, 12);

function defaultTitle(date = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return `Handwritten ${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())} `
    + `${p(date.getHours())}:${p(date.getMinutes())}`;
}

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids) if (kid != null) node.append(kid);
  return node;
}

const SVG = (body) => `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" `
  + `stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
const ICONS = {
  back: SVG('<path d="M15 18l-6-6 6-6"/>'),
  undo: SVG('<path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>'),
  redo: SVG('<path d="M15 14l5-5-5-5"/><path d="M20 9H9a5 5 0 0 0 0 10h3"/>'),
  eraser: SVG('<path d="M7 21h10"/><path d="M5.5 13.5l7-7a2 2 0 0 1 2.8 0l3.2 3.2a2 2 0 0 1 0 2.8L13 18H8.5l-3-3a1 1 0 0 1 0-1.5z"/>'),
  page: SVG('<path d="M6 3h8l4 4v14H6z"/><path d="M12 11v6M9 14h6"/>'),
  plus: SVG('<path d="M12 5v14M5 12h14"/>'),
  trash: SVG('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>'),
};

// ----------------------------------------------------------------- local store
// IndexedDB "fork-ink": notes (the local copy, with `synced`) and deletes (to send).
// Falls back to memory when IndexedDB isn't available (private mode).
function openStore() {
  const memory = { notes: new Map(), deletes: new Map() };
  let dbp = null;
  if (globalThis.indexedDB) {
    dbp = new Promise((resolve) => {
      let req;
      try { req = indexedDB.open('fork-ink', 1); } catch (_) { resolve(null); return; }
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('notes')) db.createObjectStore('notes', { keyPath: 'id' });
        if (!db.objectStoreNames.contains('deletes')) db.createObjectStore('deletes', { keyPath: 'id' });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    });
  }
  async function tx(store, mode, fn) {
    const db = dbp ? await dbp : null;
    if (!db) return fn(null, memory[store]);
    return new Promise((resolve, reject) => {
      const t = db.transaction(store, mode);
      const os = t.objectStore(store);
      let result;
      const req = fn(os, null);
      if (req) req.onsuccess = () => { result = req.result; };
      t.oncomplete = () => resolve(result);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error);
    });
  }
  const clone = (v) => (v == null ? v : JSON.parse(JSON.stringify(v)));
  return {
    get: (store, id) => tx(store, 'readonly', (os, mem) => (os ? os.get(id) : clone(mem.get(id)))),
    all: (store) => tx(store, 'readonly', (os, mem) => (os ? os.getAll() : [...mem.values()].map(clone))),
    put: (store, value) => tx(store, 'readwrite', (os, mem) => (os ? os.put(value) : void mem.set(value.id, clone(value)))),
    del: (store, id) => tx(store, 'readwrite', (os, mem) => (os ? os.delete(id) : void mem.delete(id))),
  };
}

// ---------------------------------------------------------------- the hub calls
async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
    ...options,
  });
  let body = null;
  try { body = await res.json(); } catch (_) { /* empty */ }
  return { status: res.status, ok: res.ok, body };
}

// ------------------------------------------------------------------- drawing
function strokePath(ctx, pts, map) {
  const [x0, y0] = map(pts[0]);
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  if (pts.length === 1) { ctx.lineTo(x0 + 0.01, y0); return; }
  for (let i = 1; i < pts.length - 1; i++) {
    const [ax, ay] = map(pts[i]);
    const [bx, by] = map(pts[i + 1]);
    ctx.quadraticCurveTo(ax, ay, (ax + bx) / 2, (ay + by) / 2);
  }
  const [lx, ly] = map(pts[pts.length - 1]);
  ctx.lineTo(lx, ly);
}

function drawStroke(ctx, stroke, map, k) {
  ctx.save();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.strokeStyle = stroke.pen.color;
  ctx.lineWidth = Math.max(0.6, stroke.pen.width * k);
  if (stroke.pen.highlighter) ctx.globalAlpha = HIGHLIGHT_ALPHA;
  strokePath(ctx, stroke.points, map);
  ctx.stroke();
  ctx.restore();
}

function distToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const len = dx * dx + dy * dy;
  let t = len ? ((px - ax) * dx + (py - ay) * dy) / len : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

export function strokeHit(stroke, x, y, radius) {
  const r = radius + stroke.pen.width / 2;
  const pts = stroke.points;
  if (pts.length === 1) return Math.hypot(pts[0][0] - x, pts[0][1] - y) <= r;
  for (let i = 1; i < pts.length; i++) {
    if (distToSegment(x, y, pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]) <= r) return true;
  }
  return false;
}

// ===================================================================== the view
export function mountInk(root, options = {}) {
  loadStylesheet();
  const store = options.store || openStore();
  const now = options.clock || (() => performance.now());
  const state = {
    screen: 'list',
    note: null,               // { id, title, ruled, edited_at, pages, synced }
    pen: 'black',
    width: 'thin',
    eraser: false,
    undo: [],
    redo: [],
    dirty: false,
    zoom: 1,
    sx: -SIDE, sy: -PAGE_GAP, // scroll, in page units
    live: null,               // stroke being drawn: { pageIndex, stroke, eraser, removed }
  };
  const syncing = new Map();  // note id -> promise
  let timers = [];

  // ---------------------------------------------------------------- DOM
  root.textContent = '';
  const wrap = el('div', { class: 'fm-root ink-root', tabindex: '-1' });
  root.append(wrap);

  const listView = el('div', { class: 'ink-list' });
  const editor = el('div', { class: 'ink-editor', hidden: true });
  wrap.append(listView, editor);

  const status = el('span', { class: 'ink-status bs-meta', role: 'status', 'aria-live': 'polite' });
  const titleInput = el('input', { class: 'bs-input ink-title', type: 'text', maxlength: '200',
    'aria-label': 'Title', placeholder: 'Title' });
  const backBtn = iconBtn('back', 'All notes', () => closeNote());
  const penBtns = PENS.map((p) => {
    const b = el('button', { type: 'button', class: 'bs-btn ink-pen', title: p.label, 'aria-label': p.label,
      'data-pen': p.key, 'aria-pressed': 'false' });
    b.append(el('span', { class: `ink-swatch${p.highlighter ? ' hl' : ''}`, style: `--ink:${p.color}` }));
    b.addEventListener('click', () => { state.pen = p.key; state.eraser = false; syncTools(); });
    return b;
  });
  const widthBtn = el('button', { type: 'button', class: 'bs-btn ink-width', title: 'Pen width',
    onclick: () => { state.width = state.width === 'thin' ? 'medium' : 'thin'; syncTools(); } });
  const eraserBtn = iconBtn('eraser', 'Eraser (removes whole strokes)', () => {
    state.eraser = !state.eraser; syncTools();
  });
  const undoBtn = iconBtn('undo', 'Undo', () => undo());
  const redoBtn = iconBtn('redo', 'Redo', () => redo());
  const ruledBtn = el('button', { type: 'button', class: 'bs-btn', title: 'Ruled lines (not part of the ink)',
    text: 'Lines', onclick: () => {
      if (!state.note) return;
      state.note.ruled = !state.note.ruled;
      markDirty(); syncTools(); render();
    } });
  const addPageBtn = iconBtn('page', 'Add a page', () => addPage());
  addPageBtn.append('Page');

  const bar = el('div', { class: 'ink-bar' },
    el('div', { class: 'ink-group' }, backBtn, titleInput),
    el('div', { class: 'ink-group ink-pens', role: 'group', 'aria-label': 'Pens' }, ...penBtns, widthBtn, eraserBtn),
    el('div', { class: 'ink-group' }, undoBtn, redoBtn, ruledBtn, addPageBtn),
    status);
  const stage = el('div', { class: 'ink-stage' });
  const base = el('canvas', { class: 'ink-canvas ink-base', 'aria-hidden': 'true' });
  const live = el('canvas', { class: 'ink-canvas ink-live', 'aria-label': 'Writing area' });
  stage.append(base, live);
  editor.append(bar, stage);

  const bctx = base.getContext('2d');
  let lctx = null;
  try { lctx = live.getContext('2d', { desynchronized: true }); } catch (_) { lctx = null; }
  if (!lctx) lctx = live.getContext('2d');

  function iconBtn(icon, label, onclick) {
    const b = el('button', { type: 'button', class: 'bs-btn', title: label, 'aria-label': label, onclick });
    b.innerHTML = ICONS[icon]; // our own static SVG
    return b;
  }

  // ---------------------------------------------------------- geometry
  let cssW = 1;
  let cssH = 1;
  let dpr = 1;
  const baseScale = () => Math.min(cssW / (PAGE_W + 2 * SIDE), MAX_PAGE_PX / PAGE_W);
  const scale = () => baseScale() * state.zoom;
  const pageTop = (i) => i * (PAGE_H + PAGE_GAP);
  const docHeight = () => (state.note ? state.note.pages.length * (PAGE_H + PAGE_GAP) - PAGE_GAP : PAGE_H);
  const toScreen = (x, y) => [(x - state.sx) * scale(), (y - state.sy) * scale()];
  function toDoc(clientX, clientY) {
    const r = live.getBoundingClientRect();
    const k = scale();
    return [(clientX - r.left) / k + state.sx, (clientY - r.top) / k + state.sy];
  }

  function clampScroll() {
    const k = scale();
    const viewW = cssW / k;
    const viewH = cssH / k;
    if (PAGE_W + 2 * SIDE <= viewW) state.sx = -(viewW - PAGE_W) / 2;
    else state.sx = Math.min(Math.max(state.sx, -SIDE), PAGE_W + SIDE - viewW);
    const maxY = docHeight() + PAGE_GAP - viewH;
    state.sy = Math.min(Math.max(state.sy, -PAGE_GAP), Math.max(-PAGE_GAP, maxY));
  }

  function resize() {
    const r = stage.getBoundingClientRect();
    if (!r.width || !r.height) return;
    // Keep the top-left doc point in place (rotation keeps the writing where it was).
    cssW = r.width;
    cssH = r.height;
    dpr = window.devicePixelRatio || 1;
    for (const c of [base, live]) {
      c.width = Math.round(cssW * dpr);
      c.height = Math.round(cssH * dpr);
      c.style.width = `${cssW}px`;
      c.style.height = `${cssH}px`;
    }
    clampScroll();
    render();
  }

  // ------------------------------------------------------------ render
  let frame = 0;
  function render() {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; renderNow(); });
  }

  function renderNow() {
    const ctx = bctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    if (!state.note) return;
    const k = scale();
    const top = state.sy;
    const bottom = state.sy + cssH / k;
    state.note.pages.forEach((page, i) => {
      const y0 = pageTop(i);
      if (y0 > bottom || y0 + page.height < top) return;
      const [px, py] = toScreen(0, y0);
      const pw = page.width * k;
      const ph = page.height * k;
      ctx.save();
      ctx.fillStyle = '#ffffff';
      ctx.shadowColor = 'rgba(0,0,0,0.18)';
      ctx.shadowBlur = 6;
      ctx.fillRect(px, py, pw, ph);
      ctx.restore();
      ctx.save();
      ctx.beginPath();
      ctx.rect(px, py, pw, ph);
      ctx.clip();
      if (state.note.ruled) {
        ctx.strokeStyle = '#d6e0ea';
        ctx.lineWidth = 1;
        for (let ry = RULE_TOP; ry < page.height - 30; ry += RULE_STEP) {
          const sy = Math.round(py + ry * k) + 0.5;
          ctx.beginPath();
          ctx.moveTo(px, sy);
          ctx.lineTo(px + pw, sy);
          ctx.stroke();
        }
      }
      const map = (p) => toScreen(p[0], p[1] + y0);
      for (const s of page.strokes) if (s.pen.highlighter) drawStroke(ctx, s, map, k);
      for (const s of page.strokes) if (!s.pen.highlighter) drawStroke(ctx, s, map, k);
      ctx.restore();
    });
  }

  function renderLive() {
    lctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    lctx.clearRect(0, 0, cssW, cssH);
    const lv = state.live;
    if (!lv) return;
    const y0 = pageTop(lv.pageIndex);
    if (lv.eraser) {
      const last = lv.cursor;
      if (!last) return;
      const [x, y] = toScreen(last[0], last[1] + y0);
      lctx.beginPath();
      lctx.arc(x, y, ERASER_PX, 0, Math.PI * 2);
      lctx.strokeStyle = 'rgba(120,120,120,0.9)';
      lctx.lineWidth = 1;
      lctx.stroke();
      return;
    }
    drawStroke(lctx, lv.stroke, (p) => toScreen(p[0], p[1] + y0), scale());
  }

  // ------------------------------------------------------------- input
  function currentPen() {
    const p = PENS.find((x) => x.key === state.pen) || PENS[0];
    const w = WIDTHS[state.width] || WIDTHS.thin;
    return { color: p.color, width: p.highlighter ? w.highlighter : w.ink, highlighter: !!p.highlighter };
  }

  function pageAt(docY) {
    const pages = state.note.pages;
    const i = Math.floor((docY + PAGE_GAP / 2) / (PAGE_H + PAGE_GAP));
    return Math.max(0, Math.min(pages.length - 1, i));
  }

  function pointOf(e, lv) {
    const [x, y] = toDoc(e.clientX, e.clientY);
    const page = state.note.pages[lv.pageIndex];
    const cx = Math.max(-40, Math.min(page.width + 40, x));
    const cy = Math.max(-40, Math.min(page.height + 40, y - pageTop(lv.pageIndex)));
    return [Math.round(cx * 10) / 10, Math.round(cy * 10) / 10, Math.max(0, Math.round(e.timeStamp - lv.t0))];
  }

  function eraseAt(lv, p) {
    const page = state.note.pages[lv.pageIndex];
    const r = ERASER_PX / scale();
    let hit = false;
    for (let i = page.strokes.length - 1; i >= 0; i--) {
      if (strokeHit(page.strokes[i], p[0], p[1], r)) {
        lv.removed.push({ pageId: page.id, index: i, stroke: page.strokes[i] });
        page.strokes.splice(i, 1);
        hit = true;
      }
    }
    if (hit) render();
  }

  const router = createInputRouter({
    drawStart(e, { eraser }) {
      if (!state.note) return;
      try { live.setPointerCapture(e.pointerId); } catch (_) { /* synthetic */ }
      const [, docY] = toDoc(e.clientX, e.clientY);
      const pageIndex = pageAt(docY);
      const isEraser = eraser || state.eraser;
      const lv = { pageIndex, eraser: isEraser, t0: e.timeStamp, removed: [], cursor: null,
        stroke: isEraser ? null : { id: shortId(), pen: currentPen(), at: Date.now(), points: [] } };
      state.live = lv;
      const p = pointOf(e, lv);
      if (isEraser) { lv.cursor = p; eraseAt(lv, p); } else lv.stroke.points.push(p);
      renderLive();
    },
    drawMove(e) {
      const lv = state.live;
      if (!lv) return;
      let events = typeof e.getCoalescedEvents === 'function' ? e.getCoalescedEvents() : [];
      if (!events || !events.length) events = [e];
      for (const ev of events) {
        const p = pointOf(ev, lv);
        if (lv.eraser) { lv.cursor = p; eraseAt(lv, p); continue; }
        const pts = lv.stroke.points;
        const last = pts[pts.length - 1];
        if (last && Math.abs(last[0] - p[0]) < 0.3 && Math.abs(last[1] - p[1]) < 0.3) continue;
        pts.push(p);
      }
      renderLive();
    },
    drawEnd() {
      const lv = state.live;
      state.live = null;
      if (lv && lv.eraser) {
        if (lv.removed.length) pushAction({ type: 'erase', removed: lv.removed });
      } else if (lv && lv.stroke.points.length) {
        const page = state.note.pages[lv.pageIndex];
        page.strokes.push(lv.stroke);
        pushAction({ type: 'add', pageId: page.id, stroke: lv.stroke });
        render();
      }
      renderLive();
    },
    pan(dx, dy) {
      const k = scale();
      state.sx -= dx / k;
      state.sy -= dy / k;
      clampScroll();
      render();
    },
    zoom(factor, cx, cy) { zoomAt(factor, cx, cy); },
  }, now);

  function zoomAt(factor, clientX, clientY) {
    const [dx, dy] = toDoc(clientX, clientY);
    const r = live.getBoundingClientRect();
    state.zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, state.zoom * factor));
    const k = scale();
    state.sx = dx - (clientX - r.left) / k;
    state.sy = dy - (clientY - r.top) / k;
    clampScroll();
    render();
  }

  for (const type of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel', 'pointerover',
    'pointerenter', 'pointerleave', 'pointerout']) {
    live.addEventListener(type, (e) => {
      const what = router.handle(e);
      if (what === 'draw' || what === 'gesture' || (type === 'pointerdown' && e.pointerType === 'touch')) {
        e.preventDefault();
      }
    });
  }
  // A pen hovering anywhere in the window counts for palm rejection.
  const notePen = (e) => router.notePen(e);
  document.addEventListener('pointermove', notePen, { passive: true, capture: true });
  live.addEventListener('contextmenu', (e) => e.preventDefault());
  live.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (e.ctrlKey || e.metaKey) zoomAt(Math.exp(-e.deltaY / 300), e.clientX, e.clientY);
    else {
      const k = scale();
      state.sx += (e.shiftKey ? e.deltaY : e.deltaX) / k;
      state.sy += (e.shiftKey ? 0 : e.deltaY) / k;
      clampScroll();
      render();
    }
  }, { passive: false });
  wrap.addEventListener('keydown', (e) => {
    if (state.screen !== 'editor' || e.target === titleInput) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === 'z') { e.preventDefault(); if (e.shiftKey) redo(); else undo(); }
    else if (mod && e.key.toLowerCase() === 'y') { e.preventDefault(); redo(); }
  });

  // ------------------------------------------------------------ undo
  function pushAction(action) {
    state.undo.push(action);
    state.redo = [];
    markDirty();
  }

  function pageById(id) { return state.note.pages.find((p) => p.id === id); }

  function apply(action, reverse) {
    if (action.type === 'add') {
      const page = pageById(action.pageId);
      if (!page) return;
      if (reverse) page.strokes = page.strokes.filter((s) => s.id !== action.stroke.id);
      else page.strokes.push(action.stroke);
    } else if (action.type === 'erase') {
      // Each index was taken right after the earlier removals: put back in reverse order.
      if (reverse) {
        for (const r of [...action.removed].reverse()) pageById(r.pageId)?.strokes.splice(r.index, 0, r.stroke);
      } else {
        const gone = new Set(action.removed.map((r) => r.stroke.id));
        for (const page of state.note.pages) page.strokes = page.strokes.filter((s) => !gone.has(s.id));
      }
    } else if (action.type === 'page') {
      if (reverse) state.note.pages = state.note.pages.filter((p) => p.id !== action.page.id);
      else state.note.pages.push(action.page);
      clampScroll();
    }
  }

  function undo() {
    const a = state.undo.pop();
    if (!a) return;
    apply(a, true);
    state.redo.push(a);
    markDirty();
    render();
  }

  function redo() {
    const a = state.redo.pop();
    if (!a) return;
    apply(a, false);
    state.undo.push(a);
    markDirty();
    render();
  }

  function addPage() {
    if (!state.note) return;
    const page = { id: `p${shortId()}`, width: PAGE_W, height: PAGE_H, strokes: [] };
    state.note.pages.push(page);
    pushAction({ type: 'page', page });
    state.sy = pageTop(state.note.pages.length - 1) - PAGE_GAP / 2;
    clampScroll();
    render();
  }

  function syncTools() {
    for (const b of penBtns) b.setAttribute('aria-pressed', String(!state.eraser && b.dataset.pen === state.pen));
    eraserBtn.setAttribute('aria-pressed', String(state.eraser));
    widthBtn.textContent = state.width === 'thin' ? 'Thin' : 'Medium';
    ruledBtn.setAttribute('aria-pressed', String(!!(state.note && state.note.ruled)));
    undoBtn.disabled = !state.undo.length;
    redoBtn.disabled = !state.redo.length;
  }

  // -------------------------------------------------------------- saving
  function setStatus(text, error = false) {
    status.textContent = text;
    status.classList.toggle('error', !!error);
  }

  function markDirty() {
    if (!state.note) return;
    state.note.edited_at = Math.max(Date.now(), (state.note.edited_at || 0) + 1);
    state.note.synced = false;
    state.dirty = true;
    syncTools();
    setStatus('Not saved yet');
  }

  const payload = (note) => ({ title: note.title, ruled: !!note.ruled, edited_at: note.edited_at,
    final: !!note.final, pages: note.pages.map((p) => ({ id: p.id, width: p.width, height: p.height, strokes: p.strokes })) });

  function snapshotNote() {
    return JSON.parse(JSON.stringify({ ...state.note, ...payload(state.note) }));
  }

  // `final`: the page is being left (closed, back to the list, app hidden): the hub also
  // redraws the note's page image now instead of at most once a minute.
  async function persist(final = false) {
    if (!state.note || !state.dirty) return;
    state.dirty = false;
    state.needsFinal = !final;
    const copy = snapshotNote();
    copy.synced = false;
    copy.final = final;
    try {
      await store.put('notes', copy);
      setStatus('Saved on this device');
    } catch (e) {
      setStatus('Could not save on this device', true);
    }
    sync(copy.id);
  }

  function sync(id) {
    const running = syncing.get(id);
    if (running) { running.again = true; return running.promise; }
    const entry = { again: false, promise: null };
    entry.promise = (async () => {
      try {
        do { entry.again = false; await syncOnce(id); } while (entry.again);
      } finally { syncing.delete(id); }
    })();
    syncing.set(id, entry);
    return entry.promise;
  }

  async function syncOnce(id) {
    const note = await store.get('notes', id);
    if (!note || note.synced) return;
    const isOpen = state.note && state.note.id === id;
    if (globalThis.navigator && navigator.onLine === false) {
      if (isOpen) setStatus('Offline: saved on this device, syncs later');
      return;
    }
    let res;
    try {
      res = await api(`${API}/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(payload(note)) });
    } catch (_) {
      if (isOpen) setStatus('Hub not reachable: saved on this device, syncs later');
      return;
    }
    if (res.ok) {
      const latest = await store.get('notes', id);
      if (latest && latest.edited_at === note.edited_at) {
        latest.synced = true;
        latest.image_url = res.body && res.body.image_url;
        await store.put('notes', latest);
        if (state.note && state.note.id === id && !state.dirty) {
          state.note.synced = true;
          setStatus('Saved');
        }
      }
    } else if (res.status === 409 && res.body && res.body.note) {
      const theirs = fromServer(res.body.note);
      await store.put('notes', theirs);
      if (state.note && state.note.id === id) {
        openLoaded(theirs);
        setStatus('Another device saved a newer version; it replaced this one (last edit wins).', true);
      }
    } else if (res.status === 410) {
      await store.del('notes', id);
      if (state.note && state.note.id === id) {
        state.note = null;
        await showList('This note was deleted elsewhere.');
      }
    } else if (res.status === 401 || res.status === 403) {
      if (isOpen) setStatus('Sign in to sync. Saved on this device.', true);
    } else if (isOpen) {
      setStatus(`Hub said ${res.status}: saved on this device, syncs later`, true);
    }
  }

  async function flushQueue() {
    let pending = [];
    try { pending = await store.all('notes'); } catch (_) { return; }
    for (const n of pending) if (!n.synced) await sync(n.id);
    let deletes = [];
    try { deletes = await store.all('deletes'); } catch (_) { return; }
    for (const d of deletes) {
      if (globalThis.navigator && navigator.onLine === false) return;
      try {
        const res = await api(`${API}/${encodeURIComponent(d.id)}`, { method: 'DELETE' });
        if (res.ok || res.status === 404) await store.del('deletes', d.id);
      } catch (_) { return; }
    }
  }

  function fromServer(n) {
    return { id: n.note_id, title: n.title || '', ruled: n.ruled !== false, edited_at: n.edited_at || 0,
      image_url: n.image_url || null, synced: true,
      pages: (n.pages || []).map((p) => ({ id: p.id, width: p.width, height: p.height, strokes: p.strokes || [] })) };
  }

  // -------------------------------------------------------------- screens
  function openLoaded(note) {
    state.note = note;
    state.screen = 'editor';
    state.undo = [];
    state.redo = [];
    state.dirty = false;
    state.needsFinal = false;
    state.live = null;
    state.zoom = 1;
    state.sy = -PAGE_GAP;
    titleInput.value = note.title || '';
    listView.hidden = true;
    editor.hidden = false;
    syncTools();
    setStatus(note.synced ? 'Saved' : 'Saved on this device');
    requestAnimationFrame(resize);
    resize();
  }

  async function openNote(id) {
    let local = null;
    try { local = await store.get('notes', id); } catch (_) { local = null; }
    let remote = null;
    try {
      const res = await api(`${API}/${encodeURIComponent(id)}`);
      if (res.ok && res.body) remote = fromServer(res.body);
    } catch (_) { remote = null; }
    let note = local;
    if (remote && (!local || (local.synced && remote.edited_at >= local.edited_at)
      || remote.edited_at > local.edited_at)) {
      note = remote;
      try { await store.put('notes', remote); } catch (_) { /* memory only */ }
    }
    if (!note) { await showList('That note could not be loaded.'); return; }
    openLoaded(note);
    if (!note.synced) sync(note.id);
  }

  async function newNote() {
    const note = { id: uuid(), title: defaultTitle(), ruled: true, edited_at: Date.now(), synced: false,
      pages: [{ id: `p${shortId()}`, width: PAGE_W, height: PAGE_H, strokes: [] }] };
    try { await store.put('notes', note); } catch (_) { /* memory only */ }
    openLoaded(note);
    state.dirty = true;
    persist();
  }

  async function closeNote() {
    await flush();
    state.note = null;
    await showList();
  }

  async function deleteNote(id, title) {
    const ok = await confirmDelete(`Delete "${title || 'this note'}"? Its handwriting is removed too.`);
    if (!ok) return;
    try {
      await store.del('notes', id);
      await store.put('deletes', { id });
    } catch (_) { /* memory only */ }
    await flushQueue();
    await showList();
  }

  function confirmDelete(message) {
    const ui = window.uiModule;
    if (ui && typeof ui.styledConfirm === 'function') {
      return ui.styledConfirm(message, { confirmText: 'Delete', danger: true });
    }
    return Promise.resolve(window.confirm(message));
  }

  async function showList(message = '') {
    state.screen = 'list';
    editor.hidden = true;
    listView.hidden = false;
    listView.textContent = '';
    const head = el('div', { class: 'bs-row ink-list-head' },
      el('button', { type: 'button', class: 'bs-btn primary', onclick: () => newNote() }, 'New note'),
      el('span', { class: 'bs-meta', text: 'Write with the pen; one finger scrolls, two fingers zoom. '
        + 'Saved on this device first, then in your notes. If two devices change the same note, the last edit wins.' }));
    const msg = el('div', { class: `bs-msg${message ? ' error' : ''}`, text: message });
    const list = el('div', { class: 'ink-notes' }, el('div', { class: 'bs-status', text: 'Loading...' }));
    listView.append(head, msg, list);

    let local = [];
    try { local = await store.all('notes'); } catch (_) { local = []; }
    let remote = null;
    try {
      const res = await api(API);
      if (res.ok && res.body) remote = res.body.notes || [];
    } catch (_) { remote = null; }
    const byId = new Map();
    if (remote) {
      const remoteIds = new Set(remote.map((n) => n.id));
      for (const n of local) {
        // A synced note the hub no longer lists was deleted elsewhere.
        if (n.synced && !remoteIds.has(n.id)) { try { await store.del('notes', n.id); } catch (_) { /* */ } }
      }
      local = local.filter((n) => !n.synced || remoteIds.has(n.id));
      for (const n of remote) byId.set(n.id, { ...n, waiting: false });
    }
    for (const n of local) {
      const known = byId.get(n.id);
      if (!known || !n.synced) {
        byId.set(n.id, { id: n.id, title: n.title, image_url: n.image_url || (known && known.image_url),
          edited_at: n.edited_at, pages: n.pages.length, waiting: !n.synced });
      }
    }
    const items = [...byId.values()].sort((a, b) => (b.edited_at || 0) - (a.edited_at || 0)
      || String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    list.textContent = '';
    if (remote === null) list.append(el('div', { class: 'bs-notice', text: 'The hub is not reachable: showing the notes on this device.' }));
    if (!items.length) { list.append(el('div', { class: 'bs-empty', text: 'No handwritten notes yet.' })); return; }
    for (const n of items) list.append(noteCard(n));
  }

  function noteCard(n) {
    const when = n.edited_at ? new Date(n.edited_at) : (n.updated_at ? new Date(n.updated_at) : null);
    const meta = [n.pages ? `${n.pages} page${n.pages === 1 ? '' : 's'}` : '', when ? when.toLocaleString() : '',
      n.waiting ? 'waiting to sync' : ''].filter(Boolean).join(' · ');
    const thumb = el('div', { class: 'ink-thumb' });
    if (n.image_url) thumb.append(el('img', { src: n.image_url, alt: '', loading: 'lazy' }));
    const del = el('button', { type: 'button', class: 'bs-btn bs-icon-btn', title: 'Delete', 'aria-label': `Delete ${n.title}` });
    del.innerHTML = ICONS.trash;
    del.addEventListener('click', (e) => { e.stopPropagation(); deleteNote(n.id, n.title); });
    const card = el('div', { class: 'bs-card ink-card', role: 'button', tabindex: '0' },
      thumb, el('div', { class: 'ink-card-text' }, el('h2', { text: n.title || 'Untitled' }), el('div', { class: 'bs-meta', text: meta })),
      del);
    card.addEventListener('click', () => openNote(n.id));
    card.addEventListener('keydown', (e) => { if (e.key === 'Enter') openNote(n.id); });
    return card;
  }

  titleInput.addEventListener('input', () => {
    if (!state.note) return;
    state.note.title = titleInput.value.slice(0, 200);
    markDirty();
  });

  // --------------------------------------------------------- lifecycle
  async function flush() {
    if (!state.note || !(state.dirty || state.needsFinal)) return;
    state.dirty = true;
    await persist(true);
  }
  const onHidden = () => { if (document.visibilityState === 'hidden') flush(); };
  const onOnline = () => flushQueue();
  document.addEventListener('visibilitychange', onHidden);
  window.addEventListener('pagehide', onHidden);
  window.addEventListener('online', onOnline);
  timers.push(setInterval(() => { if (state.dirty) persist(); }, AUTOSAVE_MS));
  timers.push(setInterval(() => flushQueue(), RETRY_MS));
  let observer = null;
  if (globalThis.ResizeObserver) {
    observer = new ResizeObserver(() => { if (state.screen === 'editor') resize(); });
    observer.observe(stage);
  }
  window.addEventListener('orientationchange', resize);

  syncTools();
  showList();
  flushQueue();

  return {
    refresh: () => (state.screen === 'list' ? showList() : undefined),
    flush,
    newNote,
    openNote,
    fit() { state.zoom = 1; state.sy = -PAGE_GAP; clampScroll(); render(); },
    destroy() {
      timers.forEach(clearInterval);
      timers = [];
      observer?.disconnect();
      document.removeEventListener('visibilitychange', onHidden);
      document.removeEventListener('pointermove', notePen, { capture: true });
      window.removeEventListener('pagehide', onHidden);
      window.removeEventListener('online', onOnline);
      window.removeEventListener('orientationchange', resize);
    },
    // For tests.
    _state: state,
    _router: router,
    strokeCount: () => (state.note ? state.note.pages.reduce((n, p) => n + p.strokes.length, 0) : 0),
  };
}

function loadStylesheet() {
  if (typeof document === 'undefined' || document.querySelector(`link[href="${STYLESHEET}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = STYLESHEET;
  document.head.appendChild(link);
}
