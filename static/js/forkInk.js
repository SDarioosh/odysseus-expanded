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
//   edit (409), its version replaces this one, and the view says so. Each signed-in
//   user has their own IndexedDB database ("fork-ink:<user>"), so a shared browser
//   never shows or uploads another user's notes.

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
// The hub's limits (src/ink_notes.py): the view never builds a note it would refuse.
const MAX_PAGES = 60;
const MAX_STROKE_POINTS = 5000;     // a longer stroke continues as a new one (hub: 6000)
const MAX_STROKES_PER_PAGE = 6000;
const MAX_NOTE_POINTS = 590000;     // hub: 600,000
const CLOSE_WAIT_MS = 4000;         // Back waits this long for the hub before listing
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
//
// handlers: drawStart(e, {eraser}), drawMove(e), drawEnd(e), drawSwitch(e, {eraser})
// (the barrel button was pressed or released mid-stroke), gesture(from, to) (touch
// pan/zoom: move the page point under `from`'s midpoint to `to`'s, scaled by the
// change in finger distance), gestureEnd().

// A pen's barrel button: Chrome reports it as the secondary button (2) on most
// Android pens, as the eraser button (32) on pens with an eraser end.
const BARREL = 2 | 32;
const barrelDown = (e) => e.pointerType === 'pen' && ((e.buttons || 0) & BARREL) !== 0;

export function createInputRouter(handlers, clock = () => performance.now()) {
  const h = handlers;
  let lastPen = -Infinity;
  const pens = new Set();       // pen pointers that are down
  let draw = null;              // { id, eraser, pen }
  const touches = new Map();    // pointerId -> {x, y}, at most two accepted touches
  let gesture = null;           // { x, y, dist, n } of the accepted touches
  const muted = new Set();      // pointers whose stroke was dropped: ignored until lifted

  const penNear = () => pens.size > 0 || clock() - lastPen < PALM_MS;
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

  function endDraw(e) {
    const d = draw;
    draw = null;
    if (d && d.pen) pens.delete(d.id);
    h.drawEnd(e);
  }

  function down(e) {
    seenPen(e);
    if (e.pointerType === 'touch') {
      // a palm, a stroke being drawn, or a third finger
      if (penNear() || draw || touches.size >= 2) return 'ignored';
      touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
      gesture = snapshot();
      return 'gesture';
    }
    if (e.pointerType === 'mouse' && e.button !== 0) return 'ignored';
    muted.delete(e.pointerId); // a new contact
    // The barrel pressed while the pen hovers: not a touch on the glass (yet).
    if (e.pointerType === 'pen' && e.button !== 0 && e.button !== 5 && !((e.buttons || 0) & 1)) return 'hover';
    if (draw && e.pointerId === draw.id) endDraw(e); // its pointerup was lost
    if (draw) return 'ignored';                       // one stroke at a time
    const pen = e.pointerType === 'pen';
    if (pen) pens.add(e.pointerId);
    dropTouches(); // the palm landed before the pen: the pen wins
    const eraser = barrelDown(e) || (pen && (e.button === 5 || e.button === 2));
    draw = { id: e.pointerId, eraser, pen, type: e.pointerType };
    h.drawStart(e, { eraser });
    return 'draw';
  }

  function move(e) {
    seenPen(e);
    if (muted.has(e.pointerId)) {
      if (!((e.buttons || 0) & 1)) muted.delete(e.pointerId); // lifted (hovering again)
      return 'ignored';
    }
    if (draw && e.pointerId === draw.id) {
      if (draw.pen && barrelDown(e) !== draw.eraser && h.drawSwitch) {
        draw.eraser = barrelDown(e);
        h.drawSwitch(e, { eraser: draw.eraser });
      } else {
        h.drawMove(e);
      }
      return 'draw';
    }
    if (e.pointerType === 'pen' && !e.buttons) pens.clear(); // hovering: no pen is down
    // The tip touched after the barrel was pressed in the air: Chrome sends no second
    // pointerdown, only a move with the tip's bit set.
    if (e.pointerType === 'pen' && !draw && ((e.buttons || 0) & 1)) return down(e);
    if (e.pointerType !== 'touch') return 'hover';
    if (!touches.has(e.pointerId)) return 'ignored';
    if (penNear()) { dropTouches(); return 'ignored'; }
    const from = gesture;
    touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
    gesture = snapshot();
    if (from && gesture && gesture.n === from.n && h.gesture) h.gesture(from, gesture);
    return 'gesture';
  }

  function up(e) {
    seenPen(e);
    if (muted.delete(e.pointerId)) return 'ignored';
    if (draw && e.pointerId === draw.id) {
      endDraw(e);
      return 'draw';
    }
    if (e.pointerType === 'pen') pens.delete(e.pointerId);
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
      case 'pointercancel':
      // Android system gestures (back swipe, notification shade) and dialogs can take
      // the pointer without a pointerup. After a normal pointerup this is a no-op.
      case 'lostpointercapture': return up(e);
      case 'pointerover':
      case 'pointerenter':
      case 'pointerleave':
      case 'pointerout': seenPen(e); return 'hover';
      default: return 'ignored';
    }
  }

  // The window lost focus or was hidden: finish the stroke being drawn, forget
  // pointers whose release we may never see.
  function release() {
    if (draw) endDraw({ type: 'release', pointerType: draw.type, pointerId: draw.id });
    pens.clear();
    dropTouches();
  }

  // The view replaced the note under the pen: forget the stroke without ending it;
  // the rest of it (until the pen lifts) isn't drawn either.
  function reset() {
    if (draw) muted.add(draw.id);
    draw = null;
    pens.clear();
    touches.clear();
    gesture = null;
  }

  return { handle, penNear, drawing: () => !!draw, notePen: seenPen, release, reset };
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
// IndexedDB "fork-ink:<user>": notes (the local copy, with `synced`) and deletes (to
// send). Falls back to memory when IndexedDB isn't available (private mode).
const storeName = (user) => `fork-ink:${user || '_'}`;

// The signed-in user: the hub's answer, else the one upstream's init.js last saw
// (localStorage "odysseus-auth-user"), so an offline start still knows whose notes
// to show.
function cachedUser() {
  try { return localStorage.getItem('odysseus-auth-user') || ''; } catch (_) { return ''; }
}

async function liveUser() {
  try {
    const ctl = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => ctl.abort(), 3000) : null;
    const res = await fetch('/api/auth/status', { credentials: 'same-origin', signal: ctl ? ctl.signal : undefined });
    if (timer) clearTimeout(timer);
    if (!res.ok) return null;
    const data = await res.json();
    return (data && data.username) || '';
  } catch (_) { return null; }  // offline: unknown
}

function openStore(user) {
  const memory = { notes: new Map(), deletes: new Map() };
  let dbp = null;
  if (globalThis.indexedDB) {
    dbp = new Promise((resolve) => {
      let req;
      try { req = indexedDB.open(storeName(user), 1); } catch (_) { resolve(null); return; }
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

function drawStroke(ctx, stroke, map, k, { opaque = false } = {}) {
  ctx.save();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.strokeStyle = stroke.pen.color;
  ctx.lineWidth = Math.max(0.6, stroke.pen.width * k);
  if (stroke.pen.highlighter && !opaque) ctx.globalAlpha = HIGHLIGHT_ALPHA;
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
  let user = cachedUser();
  let store = options.store || openStore(user);
  const now = options.clock || (() => performance.now());
  // Each screen change gets a number; an answer that arrives after the next change is
  // dropped (a slow note load must not replace the note being written).
  let nav = 0;
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
  const syncing = new Map();  // note id -> { promise, again }
  let timers = [];
  let suspended = false;

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
  const backBtn = iconBtn('back', 'All notes', () => { if (!state.live) closeNote(); });
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
      if (!state.note || state.live) return;
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
  // One visible canvas, opaque and desynchronized (low latency). The pages are drawn
  // off-screen on `base` and copied in, with the stroke being drawn on top. A
  // transparent desynchronized canvas over another canvas showed black on Android
  // while writing: Chrome moves it to a hardware overlay that ignores transparency.
  const base = document.createElement('canvas');
  const live = el('canvas', { class: 'ink-canvas ink-live', 'aria-label': 'Writing area' });
  stage.append(live);
  editor.append(bar, stage);
  // Upstream's mobile sheets close when swiped down from anywhere in a window that
  // is scrolled to the top (static/js/ui.js), and the pen fires touch events too: a
  // downward stroke closed Write. Only the window's header keeps that swipe.
  wrap.setAttribute('data-no-swipe-dismiss', '');

  const bctx = base.getContext('2d');
  let lctx = null;
  try { lctx = live.getContext('2d', { desynchronized: true, alpha: false }); } catch (_) { lctx = null; }
  if (!lctx) lctx = live.getContext('2d', { alpha: false });

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
    }
    live.style.width = `${cssW}px`;
    live.style.height = `${cssH}px`;
    clampScroll();
    renderNow(); // resizing cleared the canvases: draw now, not a frame later (no flash)
  }

  // ------------------------------------------------------------ render
  let frame = 0;
  function render() {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; renderNow(); });
  }

  // Highlighter strokes are drawn opaque on their own layer, which is then laid on the
  // pages at HIGHLIGHT_ALPHA: overlapping marks don't get darker, like the hub's image.
  const marks = document.createElement('canvas');
  const mctx = marks.getContext('2d');

  function renderNow() {
    const ctx = bctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // Opaque: the stage's own background (a theme color) around the pages.
    ctx.fillStyle = getComputedStyle(stage).backgroundColor || '#808080';
    ctx.fillRect(0, 0, cssW, cssH);
    if (!state.note) { present(); return; }
    const k = scale();
    const top = state.sy;
    const bottom = state.sy + cssH / k;
    const visible = [];
    state.note.pages.forEach((page, i) => {
      const y0 = pageTop(i);
      if (y0 > bottom || y0 + page.height < top) return;
      visible.push([page, i, y0]);
    });
    const clipTo = (c, y0, page) => {
      const [px, py] = toScreen(0, y0);
      c.beginPath();
      c.rect(px, py, page.width * k, page.height * k);
      c.clip();
    };
    for (const [page, , y0] of visible) {
      const [px, py] = toScreen(0, y0);
      const pw = page.width * k;
      const ph = page.height * k;
      ctx.save();
      ctx.fillStyle = '#ffffff';
      ctx.shadowColor = 'rgba(0,0,0,0.18)';
      ctx.shadowBlur = 6;
      ctx.fillRect(px, py, pw, ph);
      ctx.restore();
      if (state.note.ruled) {
        ctx.save();
        clipTo(ctx, y0, page);
        ctx.strokeStyle = '#d6e0ea';
        ctx.lineWidth = 1;
        for (let ry = RULE_TOP; ry < page.height - 30; ry += RULE_STEP) {
          const sy = Math.round(py + ry * k) + 0.5;
          ctx.beginPath();
          ctx.moveTo(px, sy);
          ctx.lineTo(px + pw, sy);
          ctx.stroke();
        }
        ctx.restore();
      }
    }
    if (visible.some(([page]) => page.strokes.some((st) => st.pen.highlighter))) {
      if (marks.width !== base.width || marks.height !== base.height) {
        marks.width = base.width;
        marks.height = base.height;
      }
      mctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      mctx.clearRect(0, 0, cssW, cssH);
      for (const [page, , y0] of visible) {
        mctx.save();
        clipTo(mctx, y0, page);
        const map = (p) => toScreen(p[0], p[1] + y0);
        for (const st of page.strokes) if (st.pen.highlighter) drawStroke(mctx, st, map, k, { opaque: true });
        mctx.restore();
      }
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.globalAlpha = HIGHLIGHT_ALPHA;
      ctx.drawImage(marks, 0, 0);
      ctx.restore();
    }
    for (const [page, , y0] of visible) {
      ctx.save();
      clipTo(ctx, y0, page);
      const map = (p) => toScreen(p[0], p[1] + y0);
      for (const st of page.strokes) if (!st.pen.highlighter) drawStroke(ctx, st, map, k);
      ctx.restore();
    }
    present();
  }

  // The visible canvas: the pages, then the stroke being drawn (or the eraser ring).
  function present() {
    lctx.setTransform(1, 0, 0, 1, 0, 0);
    lctx.drawImage(base, 0, 0);
    lctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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

  const renderLive = present;

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

  // A point on the stroke's page, kept inside the page: ink outside it would be
  // clipped away when the stroke ends.
  function pointOf(e, lv) {
    const [x, y] = toDoc(e.clientX, e.clientY);
    if (lv.eraser) lv.pageIndex = pageAt(y); // the eraser follows the pen onto another page
    const page = state.note.pages[lv.pageIndex];
    const cx = Math.max(0, Math.min(page.width, x));
    const cy = Math.max(0, Math.min(page.height, y - pageTop(lv.pageIndex)));
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

  const notePoints = () => state.note.pages.reduce(
    (n, pg) => n + pg.strokes.reduce((m, st) => m + st.points.length, 0), 0);

  function startLive(e, eraser) {
    if (!state.note) return;
    const [, docY] = toDoc(e.clientX, e.clientY);
    const pageIndex = pageAt(docY);
    const isEraser = eraser || state.eraser;
    if (!isEraser) {
      const page = state.note.pages[pageIndex];
      if (page.strokes.length >= MAX_STROKES_PER_PAGE || notePoints() >= MAX_NOTE_POINTS) {
        setStatus('This note is full: start a new note.', true);
        return;
      }
    }
    const lv = { pageIndex, eraser: isEraser, t0: e.timeStamp, removed: [], cursor: null,
      stroke: isEraser ? null : { id: shortId(), pen: currentPen(), at: Date.now(), points: [] } };
    state.live = lv;
    const p = pointOf(e, lv);
    if (isEraser) { lv.cursor = p; eraseAt(lv, p); } else lv.stroke.points.push(p);
    syncTools();
    renderLive();
  }

  // Commit the stroke being drawn (or the strokes the eraser took) as one undo step.
  // `dropShort`: a stroke of a point or two, cut short by the barrel button.
  function endLive({ dropShort = false } = {}) {
    const lv = state.live;
    state.live = null;
    if (lv && lv.eraser) {
      if (lv.removed.length) pushAction({ type: 'erase', removed: lv.removed });
    } else if (lv && lv.stroke.points.length && !(dropShort && lv.stroke.points.length <= 2)) {
      const page = state.note.pages[lv.pageIndex];
      page.strokes.push(lv.stroke);
      pushAction({ type: 'add', pageId: page.id, stroke: lv.stroke });
      render();
    }
    syncTools();
    renderLive();
  }

  const router = createInputRouter({
    drawStart(e, { eraser }) {
      try { live.setPointerCapture(e.pointerId); } catch (_) { /* synthetic */ }
      startLive(e, eraser);
    },
    drawMove(e) {
      const lv = state.live;
      if (!lv) return;
      let events = typeof e.getCoalescedEvents === 'function' ? e.getCoalescedEvents() : [];
      if (!events || !events.length) events = [e];
      for (const ev of events) {
        const cur = state.live;
        const p = pointOf(ev, cur);
        if (cur.eraser) { cur.cursor = p; eraseAt(cur, p); continue; }
        const pts = cur.stroke.points;
        const last = pts[pts.length - 1];
        if (last && Math.abs(last[0] - p[0]) < 0.3 && Math.abs(last[1] - p[1]) < 0.3) continue;
        if (pts.length >= MAX_STROKE_POINTS) {
          // A very long stroke goes on as a new one, from the same point.
          const { pageIndex, t0 } = cur;
          endLive();
          state.live = { pageIndex, eraser: false, t0, removed: [], cursor: null,
            stroke: { id: shortId(), pen: { ...cur.stroke.pen }, at: Date.now(), points: [last] } };
          state.live.stroke.points.push(p);
          continue;
        }
        pts.push(p);
      }
      renderLive();
    },
    drawEnd() { endLive(); },
    drawSwitch(e, { eraser }) {
      endLive({ dropShort: eraser });
      startLive(e, eraser);
    },
    gesture(from, to) {
      // The page point under the fingers' old midpoint moves to their new midpoint,
      // zoomed by how much the fingers spread: one step, so pinching doesn't drift.
      const [dx, dy] = toDoc(from.x, from.y);
      if (to.n > 1 && from.dist > 0) {
        state.zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, state.zoom * (to.dist / from.dist)));
      }
      const r = live.getBoundingClientRect();
      const k = scale();
      state.sx = dx - (to.x - r.left) / k;
      state.sy = dy - (to.y - r.top) / k;
      clampScroll();
      render();
    },
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

  for (const type of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel', 'lostpointercapture',
    'pointerover', 'pointerenter', 'pointerleave', 'pointerout']) {
    live.addEventListener(type, (e) => {
      const what = router.handle(e);
      if (what === 'draw' || what === 'gesture' || (type === 'pointerdown' && e.pointerType === 'touch')) {
        e.preventDefault();
      }
    });
  }
  // A pen hovering anywhere in the window counts for palm rejection (listening only
  // while Write is open: see suspend/resume).
  const notePen = (e) => router.notePen(e);
  const onBlur = () => router.release();
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

  // Not while the pen is down: the stroke being drawn belongs to the page as it is.
  function undo() {
    if (state.live) return;
    const a = state.undo.pop();
    if (!a) return;
    apply(a, true);
    state.redo.push(a);
    markDirty();
    render();
  }

  function redo() {
    if (state.live) return;
    const a = state.redo.pop();
    if (!a) return;
    apply(a, false);
    state.undo.push(a);
    markDirty();
    render();
  }

  function addPage() {
    if (!state.note || state.live || state.note.pages.length >= MAX_PAGES) return;
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
    undoBtn.disabled = !state.undo.length || !!state.live;
    redoBtn.disabled = !state.redo.length || !!state.live;
    addPageBtn.disabled = !!(state.note && state.note.pages.length >= MAX_PAGES);
    addPageBtn.title = addPageBtn.disabled ? `A note has at most ${MAX_PAGES} pages` : 'Add a page';
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
    if (state.live) return; // mid-stroke or mid-erase: saved once the pen lifts
    state.dirty = false;
    state.needsFinal = !final;
    const copy = snapshotNote();
    copy.synced = false;
    copy.final = final;
    delete copy.blocked; // an edit may fix what the hub refused: try again
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
    const db = store;
    const note = await db.get('notes', id);
    if (!note || note.synced || note.blocked) return;
    const isOpen = () => state.note && state.note.id === id;
    if (globalThis.navigator && navigator.onLine === false) {
      if (isOpen()) setStatus('Offline: saved on this device, syncs later');
      return;
    }
    let res;
    try {
      res = await api(`${API}/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(payload(note)) });
    } catch (_) {
      if (isOpen()) setStatus('Hub not reachable: saved on this device, syncs later');
      return;
    }
    if (db !== store) return; // another user signed in meanwhile
    const latest = await db.get('notes', id);
    if (!latest) return;      // deleted on this device meanwhile: don't bring it back
    if (res.ok) {
      const image = (res.body && res.body.image_url) || null;
      latest.image_url = image;
      if (isOpen()) state.note.image_url = image;
      if (latest.edited_at === note.edited_at) {
        latest.synced = true;
        latest.synced_at = Date.now();
        if (isOpen() && !state.dirty) {
          state.note.synced = true;
          setStatus('Saved');
        }
      }
      await db.put('notes', latest);
      if (state.screen === 'list') refreshListSoon();
    } else if (res.status === 409 && res.body && res.body.note) {
      const theirs = fromServer(res.body.note);
      if ((latest.edited_at || 0) > theirs.edited_at) {
        // Written on here while the request was out, after the other device's edit:
        // this one is the last edit, so send it again.
        const running = syncing.get(id);
        if (running) running.again = true;
        return;
      }
      if (isOpen() && state.dirty && state.note.edited_at > theirs.edited_at) return; // the autosave sends it
      await db.put('notes', theirs);
      if (isOpen()) {
        if (state.live) { state.live = null; router.reset(); }
        openLoaded(theirs);
        setStatus('Another device saved a newer version; it replaced this one (last edit wins).', true);
      }
    } else if (res.status === 410) {
      await db.del('notes', id);
      if (isOpen()) {
        state.live = null;
        router.reset();
        state.note = null;
        await showList('This note was deleted elsewhere.');
      } else if (state.screen === 'list') refreshListSoon();
    } else if (res.status === 401 || res.status === 403) {
      if (isOpen()) setStatus('Sign in to sync. Saved on this device.', true);
    } else if (res.status >= 400 && res.status < 500 && res.status !== 408 && res.status !== 429) {
      // Refused for good (not this user's note, too large, malformed): keep it on this
      // device, stop retrying until it's edited again.
      latest.blocked = res.status;
      await db.put('notes', latest);
      const why = (res.body && res.body.detail) || `HTTP ${res.status}`;
      if (isOpen()) setStatus(`The hub refused this note (${why}). Kept on this device.`, true);
    } else if (isOpen()) {
      setStatus(`Hub said ${res.status}: saved on this device, syncs later`, true);
    }
  }

  let listTimer = 0;
  function refreshListSoon() {
    clearTimeout(listTimer);
    listTimer = setTimeout(() => { if (state.screen === 'list' && !suspended) showList(); }, 300);
  }

  async function flushQueue() {
    const db = store;
    let pending = [];
    try { pending = await db.all('notes'); } catch (_) { return; }
    for (const n of pending) if (!n.synced && !n.blocked && db === store) await sync(n.id);
    let deletes = [];
    try { deletes = await db.all('deletes'); } catch (_) { return; }
    for (const d of deletes) {
      if (globalThis.navigator && navigator.onLine === false) return;
      try {
        if (db !== store) return;
        const res = await api(`${API}/${encodeURIComponent(d.id)}`, { method: 'DELETE' });
        if (res.ok || res.status === 404) await db.del('deletes', d.id);
      } catch (_) { return; }
    }
  }

  function fromServer(n) {
    return { id: n.note_id, title: n.title || '', ruled: n.ruled !== false, edited_at: n.edited_at || 0,
      image_url: n.image_url || null, synced: true,
      pages: (n.pages || []).map((p) => ({ id: p.id, width: p.width, height: p.height, strokes: p.strokes || [] })) };
  }

  // -------------------------------------------------------------- screens
  function openLoaded(note, { keepView = false } = {}) {
    state.note = note;
    state.screen = 'editor';
    state.undo = [];
    state.redo = [];
    state.dirty = false;
    state.needsFinal = false;
    if (state.live) router.reset();
    state.live = null;
    if (!keepView) {
      state.zoom = 1;
      state.sy = -PAGE_GAP;
    }
    renderLive();
    titleInput.value = note.title || '';
    listView.hidden = true;
    editor.hidden = false;
    syncTools();
    setStatus(note.synced ? 'Saved' : 'Saved on this device');
    requestAnimationFrame(resize);
    resize();
  }

  async function openNote(id) {
    const my = ++nav;
    const db = store;
    let local = null;
    try { local = await db.get('notes', id); } catch (_) { local = null; }
    let remote = null;
    try {
      const res = await api(`${API}/${encodeURIComponent(id)}`);
      if (res.ok && res.body) remote = fromServer(res.body);
    } catch (_) { remote = null; }
    if (my !== nav || db !== store) return; // the owner moved on meanwhile
    let note = local;
    if (remote && (!local || (local.synced && remote.edited_at >= local.edited_at)
      || remote.edited_at > local.edited_at)) {
      note = remote;
      try { await db.put('notes', remote); } catch (_) { /* memory only */ }
    }
    if (my !== nav) return;
    if (!note) { await showList('That note could not be loaded.'); return; }
    openLoaded(note);
    if (!note.synced) sync(note.id);
  }

  async function newNote() {
    ++nav;
    const note = { id: uuid(), title: defaultTitle(), ruled: true, edited_at: Date.now(), synced: false,
      pages: [{ id: `p${shortId()}`, width: PAGE_W, height: PAGE_H, strokes: [] }] };
    try { await store.put('notes', note); } catch (_) { /* memory only */ }
    openLoaded(note);
    state.dirty = true;
    persist();
  }

  async function closeNote() {
    const id = state.note && state.note.id;
    const my = ++nav;
    await flush();
    // Wait a moment for the hub, so the list shows the saved note and its new image.
    const running = id && syncing.get(id);
    if (running) await Promise.race([running.promise, new Promise((r) => setTimeout(r, CLOSE_WAIT_MS))]);
    if (my !== nav) return;
    state.note = null;
    await showList();
  }

  // Remove a note from this device and queue its delete for the hub.
  async function deleteLocal(id) {
    try {
      await store.del('notes', id);
      await store.put('deletes', { id });
    } catch (_) { /* memory only */ }
  }

  async function deleteNote(id, title) {
    const ok = await confirmDelete(`Delete "${title || 'this note'}"? Its handwriting is removed too.`);
    if (!ok) return;
    await deleteLocal(id);
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
    const my = ++nav;
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

    await checkUser();
    if (my !== nav) return;
    const db = store;
    const asked = Date.now();
    let remote = null;
    try {
      const res = await api(API);
      if (res.ok && res.body) remote = res.body.notes || [];
    } catch (_) { remote = null; }
    let local = [];
    try { local = await db.all('notes'); } catch (_) { local = []; }
    if (my !== nav || db !== store) return;
    const byId = new Map();
    if (remote) {
      const remoteIds = new Set(remote.map((n) => n.id));
      // A synced note the hub no longer lists was deleted elsewhere; one synced after
      // the list was asked for may just be missing from it.
      const gone = (n) => n.synced && !remoteIds.has(n.id) && !((n.synced_at || 0) >= asked);
      for (const n of local) {
        if (gone(n)) { try { await db.del('notes', n.id); } catch (_) { /* */ } }
      }
      local = local.filter((n) => !gone(n));
      for (const n of remote) byId.set(n.id, { ...n, waiting: false });
    }
    for (const n of local) {
      const known = byId.get(n.id);
      if (!known || !n.synced) {
        byId.set(n.id, { id: n.id, title: n.title, image_url: n.image_url || (known && known.image_url),
          edited_at: n.edited_at, pages: n.pages.length, waiting: !n.synced, blocked: n.blocked });
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
      n.blocked ? `not synced: the hub refused it (${n.blocked})` : (n.waiting ? 'waiting to sync' : '')]
      .filter(Boolean).join(' · ');
    const thumb = el('div', { class: 'ink-thumb' });
    if (n.image_url) thumb.append(el('img', { src: n.image_url, alt: '', loading: 'lazy' }));
    const del = el('button', { type: 'button', class: 'bs-btn bs-icon-btn', title: 'Delete', 'aria-label': `Delete ${n.title}` });
    del.innerHTML = ICONS.trash;
    del.addEventListener('click', (e) => { e.stopPropagation(); deleteNote(n.id, n.title); });
    const card = el('div', { class: 'bs-card ink-card', role: 'button', tabindex: '0' },
      thumb, el('div', { class: 'ink-card-text' }, el('h2', { text: n.title || 'Untitled' }), el('div', { class: 'bs-meta', text: meta })),
      del);
    card.addEventListener('click', () => openNote(n.id));
    card.addEventListener('keydown', (e) => {
      if (e.target !== card) return; // a key on its Delete button is the button's
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openNote(n.id); }
    });
    return card;
  }

  titleInput.addEventListener('input', () => {
    if (!state.note) return;
    state.note.title = titleInput.value.slice(0, 200);
    markDirty();
  });

  // A different user signed in on this browser: switch to their notes.
  async function checkUser() {
    if (options.store) return;
    const who = await liveUser();
    if (who === null || who === user) return;
    user = who;
    store = openStore(user);
    syncing.clear();
    if (state.note) {
      state.live = null;
      router.reset();
      state.note = null;
      state.screen = 'list';
    }
  }

  // --------------------------------------------------------- lifecycle
  async function flush() {
    if (state.live) router.release(); // the stroke being drawn is part of the save
    if (!state.note || !(state.dirty || state.needsFinal)) return;
    state.dirty = true;
    await persist(true);
  }
  const onHidden = () => { if (document.visibilityState === 'hidden') { router.release(); flush(); } };
  const onOnline = () => flushQueue();
  document.addEventListener('visibilitychange', onHidden);
  window.addEventListener('pagehide', onHidden);
  window.addEventListener('online', onOnline);
  let observer = null;
  if (globalThis.ResizeObserver) {
    observer = new ResizeObserver(() => { if (state.screen === 'editor') resize(); });
    observer.observe(stage);
  }
  window.addEventListener('orientationchange', resize);

  // Write is open: timers and the window-wide pen listener run.
  function resume() {
    suspended = false;
    if (timers.length) return;
    timers.push(setInterval(() => { if (state.dirty) persist(); }, AUTOSAVE_MS));
    timers.push(setInterval(() => flushQueue(), RETRY_MS));
    document.addEventListener('pointermove', notePen, { passive: true, capture: true });
    window.addEventListener('blur', onBlur);
  }

  // Write is closed (its window hidden): save now, stop the timers and listeners. A
  // note still waiting for the hub syncs when the browser is back online.
  function suspend() {
    suspended = true;
    router.release();
    flush();
    timers.forEach(clearInterval);
    timers = [];
    document.removeEventListener('pointermove', notePen, { capture: true });
    window.removeEventListener('blur', onBlur);
  }

  // Reopened: the list again, or the open note as the hub has it now when it was
  // changed elsewhere and not here.
  async function refresh() {
    resume();
    if (state.screen === 'list') return showList();
    if (!state.note || state.dirty || state.live) return undefined;
    const my = nav;
    const id = state.note.id;
    try {
      const res = await api(`${API}/${encodeURIComponent(id)}`);
      if (my !== nav || !state.note || state.note.id !== id || state.dirty || state.live) return undefined;
      if (res.ok && res.body) {
        const remote = fromServer(res.body);
        if (remote.edited_at > (state.note.edited_at || 0)) {
          await store.put('notes', remote);
          openLoaded(remote, { keepView: true });
        }
      } else if (res.status === 404) {
        await store.del('notes', id);
        state.note = null;
        return showList('This note was deleted elsewhere.');
      }
    } catch (_) { /* offline: keep what is on screen */ }
    return undefined;
  }

  syncTools();
  resume();
  showList();
  flushQueue();

  return {
    refresh,
    flush,
    suspend,
    resume,
    newNote,
    openNote,
    fit() { state.zoom = 1; state.sy = -PAGE_GAP; clampScroll(); render(); },
    destroy() {
      suspend();
      observer?.disconnect();
      document.removeEventListener('visibilitychange', onHidden);
      window.removeEventListener('pagehide', onHidden);
      window.removeEventListener('online', onOnline);
      window.removeEventListener('orientationchange', resize);
    },
    // For tests.
    _state: state,
    _router: router,
    _persist: () => persist(),
    _clamp: () => clampScroll(),
    _base: base,
    _deleteLocal: deleteLocal,
    _timers: () => timers.length,
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
