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
//   rotation keep them aligned. Low latency: pointerrawupdate where Chrome sends it,
//   getCoalescedEvents(), Chrome's predicted points drawn ahead of the pen, one opaque
//   desynchronized canvas, and only what changed is redrawn (see "render").
// - Autosave every AUTOSAVE_MS and when the app is hidden: IndexedDB first, then the
//   hub (PUT /api/ink/notes/<id>, routes/ink_routes.py). Notes that couldn't reach the
//   hub wait in IndexedDB and sync later. Last edit wins: when the hub has a newer
//   edit (409), its version replaces this one, and the view says so. Each signed-in
//   user has their own IndexedDB database ("fork-ink:<user>"), so a shared browser
//   never shows or uploads another user's notes.
// - Stage H2 (transcription and review): a Text tab per page shows the page above its
//   transcript, editable, with unreadable words "[?]" marked. Transcribe asks the hub's
//   vision model (POST /api/ink/notes/<id>/transcribe, read in the background); leaving
//   a page whose strokes changed (scrolling to another page, the Text tab, Back, the app
//   hidden) asks for it too. Save review stores the checked text in the note. Edits not
//   saved yet stay in IndexedDB ("drafts"). The list shows the pages that need review.
// - Stage H4 (into Athena): Send to Athena in the Text tab sends the note's reviewed text
//   (POST /api/ink/notes/<id>/send, src/ink_athena.py). It works only when no page with
//   writing waits for review, and says which pages wait. The tab shows not sent / sent /
//   changed since sent; after an edit is reviewed, Send again replaces the note in Athena
//   (never by itself).

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
// The paper under the ink (not part of it): lines for writing, a dot grid for drafting
// designs, or plain. The hub stores it as `paper` (src/ink_notes.py).
export const PAPERS = [
  { key: 'lined', label: 'Lined' },
  { key: 'dotted', label: 'Dotted' },
  { key: 'blank', label: 'Blank' },
];
// A note saved before `paper` has only `ruled` (true or missing: lined; false: blank).
export const paperOf = (note) => (PAPERS.some((p) => p.key === note.paper) ? note.paper
  : (note.ruled === false ? 'blank' : 'lined'));
const API = '/api/ink/notes';
// The hub's limits (src/ink_notes.py): the view never builds a note it would refuse.
const MAX_PAGES = 60;
const MAX_STROKE_POINTS = 5000;     // a longer stroke continues as a new one (hub: 6000)
const MAX_STROKES_PER_PAGE = 6000;
const MAX_NOTE_POINTS = 590000;     // hub: 600,000
const CLOSE_WAIT_MS = 4000;         // Back waits this long for the hub before listing
const POLL_MS = 2500;               // the Text tab asks how a transcription is going
const UNREADABLE = '[?]';
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
    // Chrome sends pointerrawupdate as soon as a sample arrives, pointermove only at the
    // next frame: once raw updates come for the stroke being drawn, the moves are skipped.
    if (draw && e.pointerId === draw.id && e.type === 'pointermove' && draw.raw) return 'draw';
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
      case 'pointerrawupdate':
        if (draw && e.pointerId === draw.id) { draw.raw = true; return move(e); }
        seenPen(e);
        return 'hover';
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
  next: SVG('<path d="M9 18l6-6-6-6"/>'),
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
  const memory = { notes: new Map(), deletes: new Map(), drafts: new Map() };
  let dbp = null;
  if (globalThis.indexedDB) {
    dbp = new Promise((resolve) => {
      let req;
      try { req = indexedDB.open(storeName(user), 2); } catch (_) { resolve(null); return; }
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('notes')) db.createObjectStore('notes', { keyPath: 'id' });
        if (!db.objectStoreNames.contains('deletes')) db.createObjectStore('deletes', { keyPath: 'id' });
        // v2 (H2): transcript edits not saved yet, id "<note id>:<page id>"
        if (!db.objectStoreNames.contains('drafts')) db.createObjectStore('drafts', { keyPath: 'id' });
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

// The pieces of strokePath's curve that come near `box` (screen px), each run of them
// as one sub-path: a redraw clipped to a small box then doesn't build the whole of a
// long stroke. The ends of a run lie farther than `pad` (half the line width and a
// pixel) outside the box, so their round caps never reach into it. Returns whether
// anything was added.
export function strokePathNear(ctx, pts, map, box, pad) {
  const n = pts.length;
  if (n < 3) { strokePath(ctx, pts, map); return true; }
  const x0 = box.x0 - pad; const y0 = box.y0 - pad; const x1 = box.x1 + pad; const y1 = box.y1 + pad;
  const near = (a, b, c) => !(Math.max(a[0], b[0], c[0]) < x0 || Math.min(a[0], b[0], c[0]) > x1
    || Math.max(a[1], b[1], c[1]) < y0 || Math.min(a[1], b[1], c[1]) > y1);
  ctx.beginPath();
  let open = false;
  let any = false;
  let prev = map(pts[0]);           // where the next piece starts
  let cur = map(pts[1]);
  for (let i = 1; i < n - 1; i++) {
    const next = map(pts[i + 1]);
    const end = [(cur[0] + next[0]) / 2, (cur[1] + next[1]) / 2];
    if (near(prev, cur, end)) {
      if (!open) { ctx.moveTo(prev[0], prev[1]); open = true; }
      ctx.quadraticCurveTo(cur[0], cur[1], end[0], end[1]);
      any = true;
    } else open = false;
    prev = end;
    cur = next;
  }
  if (near(prev, cur, cur)) {         // the last piece: a line to the last point
    if (!open) ctx.moveTo(prev[0], prev[1]);
    ctx.lineTo(cur[0], cur[1]);
    any = true;
  }
  return any;
}

function drawStroke(ctx, stroke, map, k, { opaque = false, box = null } = {}) {
  ctx.save();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.strokeStyle = stroke.pen.color;
  ctx.lineWidth = Math.max(0.6, stroke.pen.width * k);
  if (stroke.pen.highlighter && !opaque) ctx.globalAlpha = HIGHLIGHT_ALPHA;
  if (!box) strokePath(ctx, stroke.points, map);
  else if (!strokePathNear(ctx, stroke.points, map, box, ctx.lineWidth / 2 + 1)) { ctx.restore(); return; }
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

export const unionBox = (a, b) => (!a ? b : !b ? a : {
  x0: Math.min(a.x0, b.x0), y0: Math.min(a.y0, b.y0), x1: Math.max(a.x1, b.x1), y1: Math.max(a.y1, b.y1) });

function segmentsCross(ax, ay, bx, by, cx, cy, dx, dy) {
  const d1 = (dx - cx) * (ay - cy) - (dy - cy) * (ax - cx);
  const d2 = (dx - cx) * (by - cy) - (dy - cy) * (bx - cx);
  const d3 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
  const d4 = (bx - ax) * (dy - ay) - (by - ay) * (dx - ax);
  return ((d1 > 0) !== (d2 > 0)) && ((d3 > 0) !== (d4 > 0));
}

// Shortest distance between segments AB and CD.
function segmentDistance(ax, ay, bx, by, cx, cy, dx, dy) {
  if (segmentsCross(ax, ay, bx, by, cx, cy, dx, dy)) return 0;
  return Math.min(distToSegment(ax, ay, cx, cy, dx, dy), distToSegment(bx, by, cx, cy, dx, dy),
    distToSegment(cx, cy, ax, ay, bx, by), distToSegment(dx, dy, ax, ay, bx, by));
}

// Whether an eraser of `radius` moving from (ax, ay) to (bx, by) touches the stroke:
// testing the path, not only its end, so a fast eraser doesn't jump over strokes.
export function strokeHitPath(stroke, ax, ay, bx, by, radius) {
  const r = radius + stroke.pen.width / 2;
  const pts = stroke.points;
  if (pts.length === 1) return distToSegment(pts[0][0], pts[0][1], ax, ay, bx, by) <= r;
  for (let i = 1; i < pts.length; i++) {
    if (segmentDistance(ax, ay, bx, by, pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]) <= r) return true;
  }
  return false;
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
    note: null,               // { id, title, paper, edited_at, pages, synced }
    pen: 'black',
    width: 'thin',
    eraser: false,
    undo: [],
    redo: [],
    dirty: false,
    zoom: 1,
    sx: -SIDE, sy: -PAGE_GAP, // scroll, in page units
    live: null,               // stroke being drawn: { pageIndex, stroke, eraser, removed }
    view: 'ink',              // the editor's tab: 'ink' (Write) or 'text' (H2)
    textPage: 0,              // the page shown in the Text tab
    curPage: null,            // the page in the middle of the Write tab (leaving it: H2)
  };
  // H2: transcripts of the open note as the hub has them, edits not saved yet (also in
  // IndexedDB), and the pages changed since the hub was last asked to transcribe them.
  let tr = null;              // { noteId, pages: Map(page id -> page view), running, missing }
  const drafts = new Map();   // "<note id>:<page id>" -> text
  const edited = new Set();   // page ids of the open note
  let pollTimer = 0;
  let autoChain = Promise.resolve();
  // Transcript answers: each question is numbered, and an answer to an older question
  // than the one shown is dropped (a slow GET must not undo a newer transcribe answer).
  let trAsked = 0;
  let trTaken = 0;
  // H4: the notes being sent to Athena (one send per note), and the last answer to show.
  const sendingIds = new Set();
  let athenaMsg = null;       // { noteId, text, error, link, key }: shown while `key` holds
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
  const paperBtn = el('button', { type: 'button', class: 'bs-btn ink-paper',
    title: 'Paper: lined, dotted or blank (not part of the ink)', onclick: () => {
      if (!state.note || state.live) return;
      const i = PAPERS.findIndex((p) => p.key === paperOf(state.note));
      state.note.paper = PAPERS[(i + 1) % PAPERS.length].key;
      markDirty(); syncTools(); render();
    } });
  const addPageBtn = iconBtn('page', 'Add a page', () => addPage());
  addPageBtn.append('Page');

  const tabInk = el('button', { type: 'button', class: 'bs-btn ink-tab', role: 'tab', text: 'Write',
    onclick: () => showInkTab() });
  const tabText = el('button', { type: 'button', class: 'bs-btn ink-tab', role: 'tab', text: 'Text',
    title: 'The text of each page: transcribe, check, save', onclick: () => showTextTab() });
  const penGroup = el('div', { class: 'ink-group ink-pens', role: 'group', 'aria-label': 'Pens' },
    ...penBtns, widthBtn, eraserBtn);
  const editGroup = el('div', { class: 'ink-group' }, undoBtn, redoBtn, paperBtn, addPageBtn);
  const bar = el('div', { class: 'ink-bar' },
    el('div', { class: 'ink-group' }, backBtn, titleInput),
    el('div', { class: 'ink-group ink-tabs', role: 'tablist', 'aria-label': 'View' }, tabInk, tabText),
    penGroup, editGroup, status);
  const stage = el('div', { class: 'ink-stage' });
  // One visible canvas, opaque and desynchronized (low latency). The pages are drawn
  // off-screen on `base` and copied in, with the stroke being drawn on top. A
  // transparent desynchronized canvas over another canvas showed black on Android
  // while writing: Chrome moves it to a hardware overlay that ignores transparency.
  // `base` sits under it, hidden, and is shown instead while fingers pan or zoom: a
  // desynchronized canvas redrawn whole every frame tears (it is drawn straight to the
  // screen), a normal one doesn't. Only `live` takes input.
  const base = el('canvas', { class: 'ink-canvas ink-base', 'aria-hidden': 'true' });
  const live = el('canvas', { class: 'ink-canvas ink-live', 'aria-label': 'Writing area' });
  stage.append(base, live);

  // H2: the Text tab, one page at a time: the page above, its transcript below.
  const tPrev = iconBtn('back', 'Previous page', () => textGo(-1));
  const tNext = iconBtn('next', 'Next page', () => textGo(1));
  const tLabel = el('span', { class: 'bs-meta ink-tpage' });
  const tState = el('div', { class: 'bs-meta ink-tstate', role: 'status', 'aria-live': 'polite' });
  const tTranscribe = el('button', { type: 'button', class: 'bs-btn', text: 'Transcribe',
    onclick: () => transcribeCurrent() });
  const tRestore = el('button', { type: 'button', class: 'bs-btn', text: 'Restore reviewed text', hidden: true,
    title: 'Put back the text you reviewed before this transcription', onclick: () => restorePrevious() });
  const tFind = el('button', { type: 'button', class: 'bs-btn', text: 'Next [?]',
    title: 'Select the next unreadable word', onclick: () => nextUnreadable() });
  const tSave = el('button', { type: 'button', class: 'bs-btn primary', text: 'Save review',
    title: 'Store this text in the note (it stays in the hub)', onclick: () => saveReview() });
  // H4: send the reviewed text of every page to Athena.
  const tSend = el('button', { type: 'button', class: 'bs-btn ink-send', text: 'Send to Athena',
    title: 'Send the reviewed text of every page to Athena (replaces what Athena has)', onclick: () => sendToAthena() });
  const tAthena = el('div', { class: 'bs-meta ink-athena', role: 'status', 'aria-live': 'polite' });
  const tImage = el('canvas', { class: 'ink-timage', role: 'img', 'aria-label': 'The handwritten page' });
  const tFigure = el('div', { class: 'ink-tfigure' }, tImage);
  const tHl = el('div', { class: 'ink-thl', 'aria-hidden': 'true' });
  const tArea = el('textarea', { class: 'ink-tarea', spellcheck: 'false', 'aria-label': 'Text of this page' });
  const tEdit = el('div', { class: 'ink-tedit' }, tHl, tArea);
  const textPanel = el('div', { class: 'ink-text', hidden: true },
    el('div', { class: 'ink-trow' }, tPrev, tLabel, tNext, el('span', { class: 'ink-tspacer' }),
      tRestore, tFind, tTranscribe, tSave, tSend),
    tState, tAthena, tFigure, tEdit);
  editor.append(bar, stage, textPanel);
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
  // The scroll on screen, rounded to whole device pixels: panning then moves the paper
  // and the ink by whole pixels together, so lines, dots and ink edges don't shimmer.
  const offX = () => Math.round(state.sx * scale() * dpr) / dpr;
  const offY = () => Math.round(state.sy * scale() * dpr) / dpr;
  const toScreen = (x, y) => { const k = scale(); return [x * k - offX(), y * k - offY()]; };
  function toDoc(clientX, clientY) {
    const r = live.getBoundingClientRect();
    const k = scale();
    return [(clientX - r.left + offX()) / k, (clientY - r.top + offY()) / k];
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
    renderNow(); // resizing cleared the canvases: draw now, not a frame later (no flash)
  }

  // ------------------------------------------------------------ render
  // The pages are drawn on `base` off-screen and copied to the visible canvas. Only what
  // changed is redrawn: everything after a scroll, zoom, undo or page change; a box
  // around the strokes the eraser took or a new highlighter mark; nothing at all for a
  // finished ink stroke (it is drawn straight onto the pages). The stroke being drawn
  // goes on the visible canvas only, a piece at a time.
  let frame = 0;
  let panning = false;           // fingers pan or zoom: `base` is shown (see startPan)
  let dirty = null;              // null, 'all', or a screen box {x0, y0, x1, y1}
  let bg = '#808080';

  function render() { noteCurrentPage(); dirty = 'all'; schedule(); }
  function renderBox(box) {
    if (!box || dirty === 'all') return;
    dirty = unionBox(dirty, box);
    schedule();
  }
  function schedule() {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      const d = dirty;
      dirty = null;
      if (d === 'all') renderNow();
      else if (d && state.live && state.live.eraser) renderErase(d);
      else if (d) renderPart(d);
    });
  }

  // Highlighter strokes are drawn opaque on their own layer, which is then laid on the
  // pages at HIGHLIGHT_ALPHA: overlapping marks don't get darker, like the hub's image.
  const marks = document.createElement('canvas');
  const mctx = marks.getContext('2d');

  // A stroke's box in page units, ink width included (strokes don't change once drawn).
  const boxes = new WeakMap();
  function bboxOf(st) {
    let b = boxes.get(st);
    if (b && b.n === st.points.length) return b;
    let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
    for (const [x, y] of st.points) {
      if (x < x0) x0 = x; if (x > x1) x1 = x;
      if (y < y0) y0 = y; if (y > y1) y1 = y;
    }
    const pad = st.pen.width / 2;
    b = { x0: x0 - pad, y0: y0 - pad, x1: x1 + pad, y1: y1 + pad, n: st.points.length };
    boxes.set(st, b);
    return b;
  }
  // A page-unit box on the page whose top is at y0, as a screen box (with a margin).
  function screenBox(b, y0, margin = 2) {
    const [ax, ay] = toScreen(b.x0, b.y0 + y0);
    const [bx, by] = toScreen(b.x1, b.y1 + y0);
    return { x0: ax - margin, y0: ay - margin, x1: bx + margin, y1: by + margin };
  }
  const meets = (a, b) => !(a.x1 < b.x0 || a.x0 > b.x1 || a.y1 < b.y0 || a.y0 > b.y1);

  function visiblePages(box) {
    const k = scale();
    const out = [];
    state.note.pages.forEach((page, i) => {
      const y0 = pageTop(i);
      const [px, py] = toScreen(0, y0);
      const rect = { x0: px, y0: py, x1: px + page.width * k, y1: py + page.height * k };
      if (rect.y1 < 0 || rect.y0 > cssH) return;
      if (box && !meets(rect, box)) return;
      out.push([page, i, y0, rect]);
    });
    return out;
  }
  const clipRect = (c, r) => { c.beginPath(); c.rect(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0); c.clip(); };
  // A box on whole device pixels: a redraw clipped to a box with fractional edges blends
  // the old and new pixels at the edge, which left a faint line around the box.
  const snapBox = (b) => ({ x0: Math.floor(b.x0 * dpr) / dpr, y0: Math.floor(b.y0 * dpr) / dpr,
    x1: Math.ceil(b.x1 * dpr) / dpr, y1: Math.ceil(b.y1 * dpr) / dpr });

  // The paper without ink (the stage, the pages with their shadow, lines or dots) for
  // the current view, kept so the eraser's redraws copy it instead of drawing it again.
  // Built when a part of the view is redrawn, not on a full redraw (a pan would build
  // it every frame for nothing).
  const paperCanvas = document.createElement('canvas');
  const pctx = paperCanvas.getContext('2d');
  let paperKey = '';
  function paperFor() {
    const n = state.note;
    const key = [state.sx, state.sy, state.zoom, cssW, cssH, dpr, bg, paperOf(n),
      n.pages.map((p) => `${p.width}x${p.height}`).join(',')].join('|');
    if (key === paperKey && paperCanvas.width === base.width && paperCanvas.height === base.height) return;
    paperCanvas.width = base.width;
    paperCanvas.height = base.height;
    pctx.save();
    pctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    pctx.fillStyle = bg;
    pctx.fillRect(0, 0, cssW, cssH);
    drawPaper(pctx, visiblePages(null));
    pctx.restore();
    paperKey = key;
  }

  // Draw the pages into `base` (or `ctx`): all of it, or only inside `box` (a box on
  // whole device pixels: see snapBox).
  function drawScene(box, ctx = bctx) {
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (box) clipRect(ctx, box);
    if (!state.note) {
      ctx.fillStyle = bg;
      ctx.fillRect(0, 0, cssW, cssH);
      ctx.restore();
      return;
    }
    const k = scale();
    const pages = visiblePages(box);
    const wanted = (st, y0) => !box || meets(screenBox(bboxOf(st), y0), box);
    if (box) {
      paperFor();
      const x = Math.max(0, Math.floor(box.x0 * dpr));
      const y = Math.max(0, Math.floor(box.y0 * dpr));
      const w = Math.min(base.width, Math.ceil(box.x1 * dpr)) - x;
      const h = Math.min(base.height, Math.ceil(box.y1 * dpr)) - y;
      if (w > 0 && h > 0) {
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0); // the clip set above still applies
        ctx.drawImage(paperCanvas, x, y, w, h, x, y, w, h);
        ctx.restore();
      }
    } else {
      ctx.fillStyle = bg;
      ctx.fillRect(0, 0, cssW, cssH);
      drawPaper(ctx, pages);
    }
    drawInk(ctx, box, pages, k, wanted);
    ctx.restore();
  }

  // The pages' paper (white sheet, shadow, lines or dots), in CSS px.
  function drawPaper(ctx, pages) {
    const k = scale();
    for (const [page, , , rect] of pages) {
      ctx.save();
      ctx.fillStyle = '#ffffff';
      ctx.shadowColor = 'rgba(0,0,0,0.18)';
      ctx.shadowBlur = 6;
      ctx.fillRect(rect.x0, rect.y0, rect.x1 - rect.x0, rect.y1 - rect.y0);
      ctx.restore();
      const paper = paperOf(state.note);
      if (paper === 'lined') {
        ctx.save();
        clipRect(ctx, rect);
        // About 1 CSS px, on whole device pixels so the lines stay sharp.
        const lw = Math.max(1, Math.round(dpr));
        ctx.strokeStyle = '#d6e0ea';
        ctx.lineWidth = lw / dpr;
        for (let ry = RULE_TOP; ry < page.height - 30; ry += RULE_STEP) {
          const sy = (Math.round((rect.y0 + ry * k) * dpr) + (lw % 2 ? 0.5 : 0)) / dpr;
          ctx.beginPath();
          ctx.moveTo(rect.x0, sy);
          ctx.lineTo(rect.x1, sy);
          ctx.stroke();
        }
        ctx.restore();
      } else if (paper === 'dotted') {
        // A square dot grid on the lines' spacing over the whole page, centred, at
        // least half a step from each edge.
        ctx.save();
        clipRect(ctx, rect);
        ctx.fillStyle = '#a9b8c7';
        const r = Math.max(0.8, 1.6 * k);
        const start = (dim) => ((dim - RULE_STEP) % RULE_STEP) / 2 + RULE_STEP / 2;
        const x0 = start(page.width);
        ctx.beginPath();
        for (let ry = start(page.height); ry < page.height - RULE_STEP / 2; ry += RULE_STEP) {
          const sy = rect.y0 + ry * k;
          if (sy < -r || sy > cssH + r) continue;
          for (let rx = x0; rx < page.width - RULE_STEP / 2; rx += RULE_STEP) {
            const sx = rect.x0 + rx * k;
            ctx.moveTo(sx + r, sy);
            ctx.arc(sx, sy, r, 0, Math.PI * 2);
          }
        }
        ctx.fill();
        ctx.restore();
      }
    }
  }

  // The strokes over the paper: highlighter marks on their own layer, then the ink.
  function drawInk(ctx, box, pages, k, wanted) {
    const hl = pages.map(([page, , y0, rect]) => [page.strokes.filter((st) => st.pen.highlighter && wanted(st, y0)), y0, rect]);
    if (hl.some(([list]) => list.length)) {
      if (marks.width !== base.width || marks.height !== base.height) {
        marks.width = base.width;
        marks.height = base.height;
      }
      mctx.save();
      mctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (box) clipRect(mctx, box);
      mctx.clearRect(0, 0, cssW, cssH);
      for (const [list, y0, rect] of hl) {
        mctx.save();
        clipRect(mctx, rect);
        const map = (p) => toScreen(p[0], p[1] + y0);
        for (const st of list) drawStroke(mctx, st, map, k, { opaque: true, box });
        mctx.restore();
      }
      mctx.restore();
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0); // the clip set above still applies
      ctx.globalAlpha = HIGHLIGHT_ALPHA;
      ctx.drawImage(marks, 0, 0);
      ctx.restore();
    }
    for (const [page, , y0, rect] of pages) {
      ctx.save();
      clipRect(ctx, rect);
      const map = (p) => toScreen(p[0], p[1] + y0);
      for (const st of page.strokes) if (!st.pen.highlighter && wanted(st, y0)) drawStroke(ctx, st, map, k, { box });
      ctx.restore();
    }
  }

  function renderNow() {
    // Opaque: the stage's own background (a theme color) around the pages.
    bg = getComputedStyle(stage).backgroundColor || bg;
    dirty = null;
    baseStale = null;
    drawScene(null);
    if (!panning) present(); // while panning `base` itself is on screen
  }

  // Fingers pan or zoom: `base` (a normal canvas, drawn in step with the screen) is
  // shown instead of the low-latency canvas, which would tear when redrawn whole every
  // frame. It holds the same picture (no stroke is being drawn during a gesture).
  function startPan() {
    if (panning) return;
    panning = true;
    stage.classList.add('ink-panning');
  }
  // The gesture ended or the pen came down: bring `base` up to date now (not a frame
  // later: the pen may be drawing), copy it in and show the low-latency canvas again.
  function endPan() {
    if (!panning) return;
    panning = false;
    if (frame) { cancelAnimationFrame(frame); frame = 0; }
    const d = dirty;
    if (d === 'all') renderNow();
    else {
      dirty = null;
      if (d) drawScene(snapBox(d));
      present();
    }
    stage.classList.remove('ink-panning');
  }

  // While the eraser moves, what it took is redrawn straight onto the visible canvas
  // (paper from its cache, then the ink), and `base` is left as it was until the pen
  // lifts (syncBase). Copying from a canvas that changes every frame is what the pen
  // never does: on some GPUs each such copy reads the whole canvas back.
  let baseStale = null;          // screen box where `base` still shows erased strokes
  function renderErase(rough) {
    const box = snapBox(rough);
    drawScene(box, lctx);
    baseStale = unionBox(baseStale, box);
    drawRing(state.live);
  }
  function syncBase() {
    if (!baseStale) return;
    const box = baseStale;
    baseStale = null;
    drawScene(box);
  }

  function renderPart(rough) {
    const box = snapBox(rough);
    drawScene(box);
    const wiped = presentBox(box);
    const lv = state.live;
    if (!lv || !wiped) return;
    if (lv.eraser) { if (lv.ringBox) drawRing(lv); return; }
    // The stroke being drawn, where the copy wiped it (exactly that box).
    lctx.save();
    lctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    clipRect(lctx, wiped);
    clipRect(lctx, pageRect(lv.pageIndex));
    drawStroke(lctx, lv.stroke, liveMap(lv), scale());
    lctx.restore();
  }

  // Copy a screen box of the pages onto the visible canvas. Returns the box actually
  // copied (grown to whole device pixels): whatever was on screen there is gone, so a
  // caller redrawing on top must cover exactly that box.
  function presentBox(box) {
    if (!box) return null;
    syncBase();
    const x = Math.max(0, Math.floor(box.x0 * dpr) - 1);
    const y = Math.max(0, Math.floor(box.y0 * dpr) - 1);
    const x1 = Math.min(live.width, Math.ceil(box.x1 * dpr) + 1);
    const y1 = Math.min(live.height, Math.ceil(box.y1 * dpr) + 1);
    if (x1 <= x || y1 <= y) return null;
    lctx.save();
    lctx.setTransform(1, 0, 0, 1, 0, 0);
    lctx.drawImage(base, x, y, x1 - x, y1 - y, x, y, x1 - x, y1 - y);
    lctx.restore();
    return { x0: x / dpr, y0: y / dpr, x1: x1 / dpr, y1: y1 / dpr };
  }

  // The whole visible canvas: the pages, then the stroke being drawn (or the eraser ring).
  function present() {
    syncBase();
    lctx.setTransform(1, 0, 0, 1, 0, 0);
    lctx.drawImage(base, 0, 0);
    lctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const lv = state.live;
    if (!lv) return;
    if (lv.eraser) { if (lv.cursor) drawRing(lv); return; }
    lctx.save();
    clipRect(lctx, pageRect(lv.pageIndex));
    drawStroke(lctx, lv.stroke, liveMap(lv), scale());
    lctx.restore();
    // Everything drawn so far is in place; the end of it stays open (see liveInk).
    const pts = lv.stroke.points;
    lv.drawn = Math.max(0, pts.length - 2);
    lv.tailBox = pts.length ? pointsBox(pts.slice(-3), lv) : null;
  }

  function pageRect(i) {
    const page = state.note.pages[i];
    const k = scale();
    const [px, py] = toScreen(0, pageTop(i));
    return { x0: px, y0: py, x1: px + page.width * k, y1: py + page.height * k };
  }
  const liveMap = (lv) => { const y0 = pageTop(lv.pageIndex); return (p) => toScreen(p[0], p[1] + y0); };
  // Screen box around page points of the live stroke, ink width included.
  function pointsBox(pts, lv) {
    const map = liveMap(lv);
    let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
    for (const p of pts) {
      const [x, y] = map(p);
      if (x < x0) x0 = x; if (x > x1) x1 = x;
      if (y < y0) y0 = y; if (y > y1) y1 = y;
    }
    const pad = (lv.stroke ? lv.stroke.pen.width * scale() : 0) / 2 + 2;
    return { x0: x0 - pad, y0: y0 - pad, x1: x1 + pad, y1: y1 + pad };
  }

  // The stroke being drawn, on the visible canvas. A curve piece through point i ends
  // at the middle of points i and i+1, so with n points pieces 1..n-2 are final; they
  // are drawn once. The end (a line to the last point, then the points Chrome predicts
  // the pen will reach next) is drawn each time and taken back the next time: the
  // pages under it are copied in again and the final pieces it covered redrawn.
  function liveInk(lv, predicted) {
    const st = lv.stroke;
    const pts = st.points;
    const n = pts.length;
    if (!n) return;
    const map = liveMap(lv);
    const k = scale();
    const page = pageRect(lv.pageIndex);
    const style = (c, alpha) => {
      c.lineCap = 'round';
      c.lineJoin = 'round';
      c.strokeStyle = st.pen.color;
      c.lineWidth = Math.max(0.6, st.pen.width * k);
      c.globalAlpha = alpha;
    };
    if (st.pen.highlighter) {
      // See-through: the whole mark is drawn again over the pages it covers.
      const box = pointsBox(pts.concat(predicted), lv);
      presentBox(unionBox(box, lv.tailBox));
      lctx.save();
      lctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      clipRect(lctx, page);
      style(lctx, HIGHLIGHT_ALPHA);
      strokePath(lctx, pts.concat(predicted), map);
      lctx.stroke();
      lctx.restore();
      lv.tailBox = box;
      return;
    }
    const endOf = (i) => {
      if (i <= 0) return map(pts[0]);
      const [ax, ay] = map(pts[i]);
      const [bx, by] = map(pts[i + 1]);
      return [(ax + bx) / 2, (ay + by) / 2];
    };
    const pieces = (from, to) => {
      if (to < from) return;
      const [sx, sy] = endOf(from - 1);
      lctx.beginPath();
      lctx.moveTo(sx, sy);
      for (let i = from; i <= to; i++) {
        const [ax, ay] = map(pts[i]);
        const [bx, by] = map(pts[i + 1]);
        lctx.quadraticCurveTo(ax, ay, (ax + bx) / 2, (ay + by) / 2);
      }
      lctx.stroke();
    };
    const done = lv.drawn || 0;
    const wiped = lv.tailBox ? presentBox(lv.tailBox) : null;
    lctx.save();
    lctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    clipRect(lctx, page);
    style(lctx, 1);
    if (wiped && done > 0) {
      // Taking back the open end wiped that box: redraw every final piece that crosses
      // it, clipped to exactly the box copied (a pen sends many tiny pieces, so this
      // can be far more than the last few).
      const half = (st.pen.width * k) / 2 + 1;
      const grown = { x0: wiped.x0 - half, y0: wiped.y0 - half, x1: wiped.x1 + half, y1: wiped.y1 + half };
      lctx.save();
      clipRect(lctx, wiped);
      lctx.beginPath();
      for (let i = 1; i <= done; i++) {
        const [sx, sy] = endOf(i - 1);
        const [ax, ay] = map(pts[i]);
        const [ex, ey] = endOf(i);
        if (Math.max(sx, ax, ex) < grown.x0 || Math.min(sx, ax, ex) > grown.x1
          || Math.max(sy, ay, ey) < grown.y0 || Math.min(sy, ay, ey) > grown.y1) continue;
        lctx.moveTo(sx, sy);
        lctx.quadraticCurveTo(ax, ay, ex, ey);
      }
      lctx.stroke();
      lctx.restore();
    }
    const last = Math.max(0, n - 2);
    pieces(done + 1, last);
    lv.drawn = last;
    // The open end: to the last point, then ahead.
    const tail = [endOf(last), map(pts[n - 1]), ...predicted.map(map)];
    lctx.beginPath();
    lctx.moveTo(tail[0][0], tail[0][1]);
    if (n === 1 && !predicted.length) lctx.lineTo(tail[0][0] + 0.01, tail[0][1]);
    for (const [x, y] of tail.slice(1)) lctx.lineTo(x, y);
    lctx.stroke();
    lctx.restore();
    let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
    for (const [x, y] of tail) {
      if (x < x0) x0 = x; if (x > x1) x1 = x;
      if (y < y0) y0 = y; if (y > y1) y1 = y;
    }
    const pad = (st.pen.width * k) / 2 + 2;
    lv.tailBox = { x0: x0 - pad, y0: y0 - pad, x1: x1 + pad, y1: y1 + pad };
  }

  // The eraser ring under the pen, drawn over the pages just redrawn (see liveEraser).
  function drawRing(lv) {
    if (!lv.cursor) return;
    const [x, y] = toScreen(lv.cursor[0], lv.cursor[1] + pageTop(lv.pageIndex));
    lctx.save();
    lctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    lctx.beginPath();
    lctx.arc(x, y, ERASER_PX, 0, Math.PI * 2);
    lctx.strokeStyle = 'rgba(120,120,120,0.9)';
    lctx.lineWidth = 1;
    lctx.stroke();
    lctx.restore();
    lv.ringBox = { x0: x - ERASER_PX - 2, y0: y - ERASER_PX - 2, x1: x + ERASER_PX + 2, y1: y + ERASER_PX + 2 };
  }
  // The ring moves with the strokes it takes, in the frame's one redraw (renderErase):
  // the old ring's box and the new one's are redrawn with the rest.
  function liveEraser(lv) {
    if (!lv.cursor) return;
    const [x, y] = toScreen(lv.cursor[0], lv.cursor[1] + pageTop(lv.pageIndex));
    const at = { x0: x - ERASER_PX - 2, y0: y - ERASER_PX - 2, x1: x + ERASER_PX + 2, y1: y + ERASER_PX + 2 };
    renderBox(unionBox(lv.ringBox, at));
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

  // Where Chrome predicts the pen will be next (a few ms ahead), as page points. Only
  // drawn, never stored.
  function predictedOf(e, lv) {
    let list = [];
    try { list = typeof e.getPredictedEvents === 'function' ? e.getPredictedEvents() : []; } catch (_) { list = []; }
    return (list || []).slice(0, 3).map((ev) => pointOf(ev, lv));
  }

  // Remove the strokes the eraser touched on its way from `from` to `p` (page units).
  // Strokes whose box is nowhere near are skipped without looking at their points.
  function eraseAt(lv, p, from) {
    const page = state.note.pages[lv.pageIndex];
    const r = ERASER_PX / scale();
    const a = from || p;
    const near = { x0: Math.min(a[0], p[0]) - r, y0: Math.min(a[1], p[1]) - r,
      x1: Math.max(a[0], p[0]) + r, y1: Math.max(a[1], p[1]) + r };
    const y0 = pageTop(lv.pageIndex);
    let box = null;
    for (let i = page.strokes.length - 1; i >= 0; i--) {
      const st = page.strokes[i];
      const b = bboxOf(st);
      if (!meets(b, near)) continue;
      if (strokeHitPath(st, a[0], a[1], p[0], p[1], r)) {
        lv.removed.push({ pageId: page.id, index: i, stroke: st });
        page.strokes.splice(i, 1);
        box = unionBox(box, screenBox(b, y0));
      }
    }
    if (box) renderBox(box);
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
      drawn: 0, tailBox: null, ringBox: null,
      stroke: isEraser ? null : { id: shortId(), pen: currentPen(), at: Date.now(), points: [] } };
    state.live = lv;
    const p = pointOf(e, lv);
    syncTools();
    if (isEraser) {
      lv.cursor = p;
      eraseAt(lv, p, null);
      liveEraser(lv);
    } else {
      lv.stroke.points.push(p);
      liveInk(lv, []);
    }
  }

  // Commit the stroke being drawn (or the strokes the eraser took) as one undo step.
  // `dropShort`: a stroke of a point or two, cut short by the barrel button.
  function endLive({ dropShort = false } = {}) {
    const lv = state.live;
    state.live = null;
    if (lv && lv.eraser) {
      if (lv.removed.length) pushAction({ type: 'erase', removed: lv.removed });
      presentBox(lv.ringBox);
    } else if (lv && lv.stroke.points.length && !(dropShort && lv.stroke.points.length <= 2)) {
      const page = state.note.pages[lv.pageIndex];
      const st = lv.stroke;
      page.strokes.push(st);
      const box = unionBox(screenBox(bboxOf(st), pageTop(lv.pageIndex)), lv.tailBox);
      if (st.pen.highlighter) {
        renderBox(box); // goes under the ink: redraw that part of the page
      } else {
        // Ink goes on top: draw it onto the pages, then show that part of them.
        bctx.save();
        bctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        clipRect(bctx, pageRect(lv.pageIndex));
        drawStroke(bctx, st, liveMap(lv), scale());
        bctx.restore();
        presentBox(box);
      }
      pushAction({ type: 'add', pageId: page.id, stroke: st });
    } else if (lv && lv.stroke && lv.stroke.points.length) {
      presentBox(unionBox(pointsBox(lv.stroke.points, lv), lv.tailBox)); // dropped
    }
    syncTools();
  }

  const router = createInputRouter({
    drawStart(e, { eraser }) {
      try { live.setPointerCapture(e.pointerId); } catch (_) { /* synthetic */ }
      endPan();
      startLive(e, eraser);
    },
    drawMove(e) {
      if (!state.live) return;
      let events = typeof e.getCoalescedEvents === 'function' ? e.getCoalescedEvents() : [];
      if (!events || !events.length) events = [e];
      for (const ev of events) {
        const cur = state.live;
        if (!cur) return;
        if (cur.eraser) {
          const before = cur.pageIndex;
          const p = pointOf(ev, cur);
          eraseAt(cur, p, before === cur.pageIndex ? cur.cursor : null);
          cur.cursor = p;
          continue;
        }
        const p = pointOf(ev, cur);
        const pts = cur.stroke.points;
        const last = pts[pts.length - 1];
        if (last && Math.abs(last[0] - p[0]) < 0.3 && Math.abs(last[1] - p[1]) < 0.3) continue;
        if (pts.length >= MAX_STROKE_POINTS) {
          // A very long stroke goes on as a new one, from the same point.
          const { pageIndex, t0 } = cur;
          liveInk(cur, []);
          endLive();
          state.live = { pageIndex, eraser: false, t0, removed: [], cursor: null, drawn: 0,
            tailBox: null, ringBox: null,
            stroke: { id: shortId(), pen: { ...cur.stroke.pen }, at: Date.now(), points: [last] } };
          state.live.stroke.points.push(p);
          continue;
        }
        pts.push(p);
      }
      const cur = state.live;
      if (!cur) return;
      if (cur.eraser) liveEraser(cur);
      else liveInk(cur, predictedOf(e, cur));
    },
    drawEnd() { endLive(); },
    drawSwitch(e, { eraser }) {
      endLive({ dropShort: eraser });
      startLive(e, eraser);
    },
    gesture(from, to) {
      // The page point under the fingers' old midpoint moves to their new midpoint,
      // zoomed by how much the fingers spread: one step, so pinching doesn't drift.
      startPan();
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
    gestureEnd() { endPan(); },
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

  for (const type of ['pointerdown', 'pointermove', 'pointerrawupdate', 'pointerup', 'pointercancel',
    'lostpointercapture', 'pointerover', 'pointerenter', 'pointerleave', 'pointerout']) {
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
    // Not in the Text tab: Ctrl+Z there undoes typing, never strokes.
    if (state.screen !== 'editor' || state.view !== 'ink' || e.target === titleInput) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === 'z') { e.preventDefault(); if (e.shiftKey) redo(); else undo(); }
    else if (mod && e.key.toLowerCase() === 'y') { e.preventDefault(); redo(); }
  });

  // ------------------------------------------------------------ undo
  function pushAction(action) {
    touchPages(action);
    state.undo.push(action);
    state.redo = [];
    markDirty();
  }

  function pageById(id) { return state.note.pages.find((p) => p.id === id); }

  // H2: the pages whose strokes this action changed need transcribing again.
  function touchPages(action) {
    if (action.type === 'add') edited.add(action.pageId);
    else if (action.type === 'erase') for (const r of action.removed) edited.add(r.pageId);
  }

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
    touchPages(a);
    state.redo.push(a);
    markDirty();
    render();
  }

  function redo() {
    if (state.live) return;
    const a = state.redo.pop();
    if (!a) return;
    apply(a, false);
    touchPages(a);
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
    const paper = state.note ? paperOf(state.note) : 'lined';
    paperBtn.textContent = PAPERS.find((p) => p.key === paper).label;
    undoBtn.disabled = !state.undo.length || !!state.live;
    redoBtn.disabled = !state.redo.length || !!state.live;
    addPageBtn.disabled = !!(state.note && state.note.pages.length >= MAX_PAGES);
    tabInk.setAttribute('aria-selected', String(state.view === 'ink'));
    tabText.setAttribute('aria-selected', String(state.view === 'text'));
    tabInk.disabled = !!state.live;
    tabText.disabled = !!state.live; // no tab change while the pen is down
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

  const payload = (note) => ({ title: note.title, paper: paperOf(note),
    ruled: paperOf(note) === 'lined', edited_at: note.edited_at,
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
      await dropNoteDrafts(id, db);
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
    return { id: n.note_id, title: n.title || '', paper: paperOf(n), edited_at: n.edited_at || 0,
      image_url: n.image_url || null, synced: true,
      pages: (n.pages || []).map((p) => ({ id: p.id, width: p.width, height: p.height, strokes: p.strokes || [] })) };
  }

  // ------------------------------------------------- H2: transcription and review
  const enc = encodeURIComponent;
  const draftKey = (noteId, pageId) => `${noteId}:${pageId}`;
  // Still the same screen, note and user as when an answer was asked for.
  const current = (my, id, db) => my === nav && db === store && !!state.note && state.note.id === id;
  const trPage = (pageId) => (tr && state.note && tr.noteId === state.note.id ? tr.pages.get(pageId) : null);
  const inkOf = (page) => page.strokes.some((st) => !st.pen.highlighter);

  function applyView() {
    const text = state.view === 'text';
    stage.hidden = text;
    textPanel.hidden = !text;
    penGroup.hidden = text;
    editGroup.hidden = text;
    syncTools();
    if (text) renderText();
    else {
      clearTimeout(pollTimer);
      pollTimer = 0;
      requestAnimationFrame(resize);
      resize();
    }
  }

  function currentPageIndex() {
    if (!state.note) return 0;
    return pageAt(state.sy + cssH / scale() / 2);
  }

  // Leaving a page in the Write tab (it scrolled out of the middle): read it again if
  // its strokes changed.
  function noteCurrentPage() {
    if (!state.note || state.view !== 'ink' || state.live || state.screen !== 'editor') return;
    const i = currentPageIndex();
    if (state.curPage == null || i === state.curPage) { state.curPage = i; return; }
    const left = state.note.pages[state.curPage];
    state.curPage = i;
    if (left && edited.has(left.id)) autoTranscribe([left.id]);
  }

  async function showTextTab() {
    if (!state.note || state.live || state.view === 'text') return;
    state.textPage = currentPageIndex();
    state.view = 'text';
    applyView();
    autoTranscribe([...edited]); // leaving the writing: its changed pages are read
    await loadTranscripts();
  }

  function showInkTab() {
    if (!state.note || state.live || state.view === 'ink') return;
    state.view = 'ink';
    state.sy = pageTop(state.textPage) - PAGE_GAP / 2;
    clampScroll();
    state.curPage = null;
    applyView();
    render();
  }

  function textGo(step) {
    if (!state.note) return;
    state.textPage = Math.max(0, Math.min(state.note.pages.length - 1, state.textPage + step));
    renderText();
  }

  function takeTranscripts(noteId, body, seq) {
    if (!body || !Array.isArray(body.pages) || seq < trTaken) return;
    trTaken = seq;
    tr = { noteId, pages: new Map(body.pages.map((p) => [p.id, p])), running: !!body.running, missing: false,
      athena: body.athena || null };
  }

  async function loadTranscripts() {
    if (!state.note) return;
    const my = nav;
    const id = state.note.id;
    const db = store;
    const seq = ++trAsked;
    let res = null;
    try { res = await api(`${API}/${enc(id)}/transcripts`); } catch (_) { res = null; }
    if (!current(my, id, db)) return;
    if (seq < trTaken) { schedulePoll(); return; } // a newer answer is on screen
    let message = '';
    if (res && res.ok) takeTranscripts(id, res.body, seq);
    else if (res && res.status === 404) {
      trTaken = seq;
      tr = { noteId: id, pages: new Map(), running: false, missing: true };
    }
    else message = res ? `The hub said ${res.status}.` : 'The hub is not reachable: the text can be read when it is back.';
    renderText(message);
    schedulePoll();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = 0;
    if (suspended || !tr || !tr.running || state.screen !== 'editor' || state.view !== 'text') return;
    pollTimer = setTimeout(() => {
      pollTimer = 0;
      if (!suspended && state.screen === 'editor' && state.view === 'text') loadTranscripts();
    }, POLL_MS);
  }

  let imageKey = '';
  function drawPageImage(page) {
    const width = Math.max(200, Math.floor(tFigure.clientWidth || 600) - 2);
    const key = `${state.note.id}:${page.id}:${width}:${page.strokes.length}:${state.note.edited_at}`;
    if (key === imageKey) return;
    imageKey = key;
    const k = width / page.width;
    const ratio = window.devicePixelRatio || 1;
    const h = page.height * k;
    tImage.width = Math.round(width * ratio);
    tImage.height = Math.round(h * ratio);
    tImage.style.width = `${width}px`;
    tImage.style.height = `${h}px`;
    const c = tImage.getContext('2d');
    c.setTransform(ratio, 0, 0, ratio, 0, 0);
    c.fillStyle = '#ffffff';
    c.fillRect(0, 0, width, h);
    const map = (p) => [p[0] * k, p[1] * k];
    const hl = page.strokes.filter((st) => st.pen.highlighter);
    if (hl.length) {
      const m = document.createElement('canvas');
      m.width = tImage.width;
      m.height = tImage.height;
      const mc = m.getContext('2d');
      mc.setTransform(ratio, 0, 0, ratio, 0, 0);
      for (const st of hl) drawStroke(mc, st, map, k, { opaque: true });
      c.save();
      c.setTransform(1, 0, 0, 1, 0, 0);
      c.globalAlpha = HIGHLIGHT_ALPHA;
      c.drawImage(m, 0, 0);
      c.restore();
    }
    for (const st of page.strokes) if (!st.pen.highlighter) drawStroke(c, st, map, k);
  }

  function setTState(text, error = false) {
    tState.textContent = text;
    tState.classList.toggle('error', !!error);
  }

  function renderText(message = '') {
    if (!state.note || state.view !== 'text') return;
    const pages = state.note.pages;
    state.textPage = Math.max(0, Math.min(pages.length - 1, state.textPage));
    const page = pages[state.textPage];
    const tp = trPage(page.id);
    const key = draftKey(state.note.id, page.id);
    tLabel.textContent = `Page ${state.textPage + 1} of ${pages.length}`;
    tPrev.disabled = state.textPage === 0;
    tNext.disabled = state.textPage >= pages.length - 1;
    drawPageImage(page);
    const pending = !!(tp && tp.status === 'pending');
    const draft = drafts.get(key);
    const want = draft != null ? draft : (tp ? tp.text : '');
    if (tArea.dataset.key !== key || tArea.value !== want) {
      tArea.value = want;
      tArea.scrollTop = 0;
    }
    tArea.dataset.key = key;
    tArea.readOnly = pending; // the model's text is on its way
    updateHighlight();
    // While the note is sent to Athena its text stays as it is (typing is kept as a draft).
    const sending = sendingIds.has(state.note.id);
    tTranscribe.textContent = tp && (tp.status === 'machine' || tp.status === 'reviewed') ? 'Transcribe again' : 'Transcribe';
    tTranscribe.disabled = pending || sending || !inkOf(page);
    tSave.disabled = pending || sending || !tr || tr.missing || !tp;
    tRestore.hidden = !(tp && tp.previous && tp.previous.text);
    tRestore.disabled = sending;
    renderSend();
    if (message) { setTState(message, true); return; }
    const unsaved = draft != null && draft !== (tp ? tp.text : '') ? ' Edits not saved yet.' : '';
    if (!tr) setTState('Loading...');
    else if (tr.missing || !tp) setTState('This page is not on the hub yet. It is saved there first, then it can be transcribed.');
    else if (pending) setTState('Transcribing... The model reads the page in parts, one at a time; chat goes first.');
    else if (tp.status === 'failed') setTState(`Transcription failed: ${tp.error || 'unknown error'}${unsaved}`, true);
    else if (!inkOf(page)) setTState(`This page has no writing.${unsaved}`);
    else if (tp.status === 'none') setTState(`Not transcribed yet: press Transcribe, or type the text.${unsaved}`);
    else {
      const n = (tArea.value.match(/\[\?\]/g) || []).length;
      const what = tp.status === 'reviewed' ? 'Reviewed.'
        : `Machine text${tp.model ? ` (${tp.model})` : ''}: check it against the page, then Save review.`;
      const marks = n ? ` ${n} unreadable word${n === 1 ? '' : 's'} marked [?].` : '';
      const changed = tp.changed ? ' The writing changed since this text was made: transcribe again.' : '';
      setTState(`${what}${marks}${changed}${unsaved}`, !!tp.changed);
    }
  }

  // H4: the Send button and what Athena has. Send works only when every page with
  // writing is reviewed for its strokes as they are now and no edit is left unsaved.
  function unsavedPages() {
    const id = state.note.id;
    const out = [];
    state.note.pages.forEach((page, i) => {
      const draft = drafts.get(draftKey(id, page.id));
      const tp = trPage(page.id);
      if (draft != null && draft !== (tp ? tp.text : '')) out.push(i + 1);
    });
    return out;
  }

  // What the Athena state is now: a Send answer is shown only until it changes.
  const athenaKey = (a) => (a ? `${a.state}|${a.sent_at}|${(a.waiting || []).length}|${a.has_text}` : '');

  function renderSend() {
    const a = tr && !tr.missing && tr.noteId === state.note.id ? tr.athena : null;
    const busy = sendingIds.has(state.note.id) || !!(a && a.sending);
    const unsaved = a ? unsavedPages() : [];
    const waiting = (a && a.waiting) || [];
    let line = '';
    let error = false;
    tSend.textContent = a && a.state !== 'not_sent' ? 'Send again' : 'Send to Athena';
    tSend.disabled = true;
    tSend.removeAttribute('data-state');
    tAthena.textContent = '';
    if (!a) return;
    tSend.setAttribute('data-state', a.state);
    if (busy) line = 'Sending to Athena...';
    else if (waiting.length) {
      line = `Send to Athena once every page is reviewed. Waiting: ${waiting.slice(0, 6)
        .map((w) => `page ${w.page} (${w.why})`).join(', ')}${waiting.length > 6 ? ` and ${waiting.length - 6} more` : ''}.`;
    } else if (unsaved.length) {
      line = `Save review first: edits not saved yet on page${unsaved.length === 1 ? '' : 's'} ${unsaved.join(', ')}.`;
    } else if (a.state === 'sent') {
      line = `Sent to Athena${a.sent_at ? ` ${new Date(a.sent_at).toLocaleString()}` : ''}.`;
    } else if (a.state === 'changed') {
      line = a.has_text ? 'Changed since it was sent: Send again replaces the note in Athena.'
        : 'No text left since it was sent: Send again removes the note from Athena.';
      tSend.disabled = false;
    } else if (a.has_text) {
      line = 'Not sent to Athena yet.';
      tSend.disabled = false;
    } else line = 'Nothing to send yet: review a page first.';
    // The last Send's answer, while Athena's state is the one it describes; pages waiting
    // or edits not saved yet are said instead.
    const quiet = !busy && !waiting.length && !unsaved.length;
    const msg = quiet && athenaMsg && athenaMsg.noteId === state.note.id && athenaMsg.key === athenaKey(a) ? athenaMsg : null;
    if (msg) { line = msg.text; error = !!msg.error; }
    tAthena.append(line);
    const link = msg ? msg.link : (a.state === 'sent' && quiet ? a.transcript_link : null);
    if (link && /^https?:\/\//.test(link)) {
      tAthena.append(' ', el('a', { href: link, target: '_blank', rel: 'noopener', text: 'Open in Athena' }));
    }
    tAthena.classList.toggle('error', error);
  }

  async function sendToAthena() {
    if (!state.note || state.view !== 'text' || state.live || sendingIds.has(state.note.id)) return;
    const my = nav;
    const db = store;
    const id = state.note.id;
    sendingIds.add(id);
    athenaMsg = null;
    renderText();
    const say = (text, error, link = null) => {
      athenaMsg = { noteId: id, text, error, link, key: athenaKey(tr && tr.noteId === id ? tr.athena : null) };
    };
    try {
      if (!(await pushStrokes(id))) {
        say('The writing is saved on this device but not on the hub yet: send when it syncs.', true);
        return;
      }
      if (!current(my, id, db)) return;
      let res = null;
      try { res = await api(`${API}/${enc(id)}/send`, { method: 'POST', body: '{}' }); } catch (_) { res = null; }
      if (!current(my, id, db)) return; // another note, screen or user meanwhile
      if (res && res.ok) {
        // The hub made this answer after the send, so it is newer than any transcripts
        // read that finished while the send was in flight: numbered on arrival.
        takeTranscripts(id, res.body, ++trAsked);
        const sent = (res.body && res.body.sent) || {};
        const words = { created: 'Sent to Athena.', replaced: 'Sent again: Athena replaced the note.',
          unchanged: 'Athena already had this text.', deleted: 'Removed from Athena: the note has no text now.',
          already: 'Athena no longer had this note.' };
        say(words[sent.result] || 'Sent to Athena.', false, sent.transcript_link || null);
      } else if (!res) say('Not sent: the hub is not reachable. Try again when it is back.', true);
      else say(`Not sent: ${(res.body && res.body.detail) || `the hub said ${res.status}`}`, true);
    } finally {
      if (db === store) sendingIds.delete(id);
      if (current(my, id, db)) renderText();
    }
  }

  // The marks behind the textarea: the same text, transparent, [?] highlighted.
  function updateHighlight() {
    tHl.textContent = '';
    tArea.value.split(UNREADABLE).forEach((part, i) => {
      if (i) tHl.append(el('mark', { text: UNREADABLE }));
      tHl.append(document.createTextNode(part));
    });
    tHl.append(document.createTextNode('\n'));
    tHl.scrollTop = tArea.scrollTop;
    tFind.disabled = !tArea.value.includes(UNREADABLE);
  }

  const draftTimers = new Map(); // one per page: typing on the next page keeps this one
  tArea.addEventListener('input', () => {
    if (!state.note || state.view !== 'text') return;
    const key = tArea.dataset.key;
    if (!key || !key.startsWith(`${state.note.id}:`)) return;
    updateHighlight();
    queueMicrotask(() => { if (state.note && state.view === 'text') renderText(); });
    const db = store;
    const tp = trPage(key.slice(state.note.id.length + 1));
    if (tp && tArea.value === tp.text) { // back to the hub's text: nothing to keep
      clearTimeout(draftTimers.get(key));
      draftTimers.delete(key);
      drafts.delete(key);
      Promise.resolve(db.del('drafts', key)).catch(() => {});
      return;
    }
    drafts.set(key, tArea.value);
    const text = tArea.value;
    clearTimeout(draftTimers.get(key));
    draftTimers.set(key, setTimeout(() => {
      draftTimers.delete(key);
      if (db !== store || drafts.get(key) !== text) return; // another user, or saved/typed since
      Promise.resolve(db.put('drafts', { id: key, text })).catch(() => {});
    }, 400));
  });
  tArea.addEventListener('scroll', () => { tHl.scrollTop = tArea.scrollTop; });

  async function loadDrafts(noteId) {
    const db = store;
    let rows = [];
    try { rows = (await db.all('drafts')) || []; } catch (_) { rows = []; }
    if (db !== store) return;
    for (const r of rows) {
      if (r && typeof r.id === 'string' && r.id.startsWith(`${noteId}:`) && !drafts.has(r.id)) drafts.set(r.id, r.text);
    }
    if (state.note && state.note.id === noteId) renderText();
  }

  // A note gone from this device: its drafts go too.
  async function dropNoteDrafts(noteId, db = store) {
    const mine = (k) => typeof k === 'string' && k.startsWith(`${noteId}:`);
    if (db === store) {
      for (const k of [...drafts.keys()]) if (mine(k)) drafts.delete(k);
      for (const [k, t] of [...draftTimers]) if (mine(k)) { clearTimeout(t); draftTimers.delete(k); }
    }
    let rows = [];
    try { rows = (await db.all('drafts')) || []; } catch (_) { rows = []; }
    for (const r of rows) {
      if (r && mine(r.id)) { try { await db.del('drafts', r.id); } catch (_) { /* memory only */ } }
    }
  }

  async function dropDraft(key, text) {
    if (drafts.get(key) !== text) return; // typed more meanwhile: keep that
    drafts.delete(key);
    try { await store.del('drafts', key); } catch (_) { /* memory only */ }
  }

  function nextUnreadable() {
    const v = tArea.value;
    let i = v.indexOf(UNREADABLE, tArea.selectionEnd);
    if (i < 0) i = v.indexOf(UNREADABLE);
    if (i < 0) return;
    tArea.focus();
    tArea.setSelectionRange(i, i + UNREADABLE.length);
  }

  function restorePrevious() {
    const page = state.note && state.note.pages[state.textPage];
    const tp = page && trPage(page.id);
    if (!tp || !tp.previous || tArea.readOnly || sendingIds.has(state.note.id)) return;
    tArea.value = tp.previous.text;
    tArea.dispatchEvent(new Event('input'));
    renderText();
  }

  // The hub has this note's strokes as they are here (saved and synced), or not.
  async function pushStrokes(id) {
    const db = store;
    if (state.note && state.note.id === id && state.dirty && !state.live) await persist();
    await sync(id);
    if (db !== store) return false;
    let local = null;
    try { local = await db.get('notes', id); } catch (_) { local = null; }
    return !local || !!local.synced;
  }

  // Ask the hub to read pages. 'ok', 'retry' (not reachable or not synced yet) or
  // 'refused' (no model, not this user's note).
  async function requestTranscribe(id, pageIds, onlyChanged, { quiet = false } = {}) {
    const my = nav;
    const db = store;
    const show = (msg) => { if (!quiet && current(my, id, db)) renderText(msg); };
    if (!(await pushStrokes(id))) { show('The writing is saved on this device but not on the hub yet: try again when it syncs.'); return 'retry'; }
    if (db !== store) return 'retry';
    let res = null;
    const seq = ++trAsked;
    try {
      res = await api(`${API}/${enc(id)}/transcribe`, { method: 'POST',
        body: JSON.stringify({ pages: pageIds, only_changed: onlyChanged }) });
    } catch (_) { res = null; }
    if (res && res.ok) {
      if (current(my, id, db)) { takeTranscripts(id, res.body, seq); renderText(); schedulePoll(); }
      return 'ok';
    }
    if (!res || (res.status >= 500 && res.status !== 503)) { show('The hub is not reachable: try again later.'); return 'retry'; }
    show((res.body && res.body.detail) || `The hub refused (${res.status}).`);
    return 'refused';
  }

  // Pages changed since they were last read: ask the hub once the strokes are there.
  function autoTranscribe(pageIds) {
    if (!state.note) return;
    const id = state.note.id;
    const db = store;
    const ids = pageIds.filter((p) => edited.has(p));
    if (!ids.length) return;
    for (const p of ids) edited.delete(p);
    autoChain = autoChain.then(async () => {
      if (db !== store) return;
      const got = await requestTranscribe(id, ids, true, { quiet: true });
      if (got === 'retry' && db === store && state.note && state.note.id === id) for (const p of ids) edited.add(p);
    }).catch(() => {});
  }

  async function transcribeCurrent() {
    if (!state.note || state.live || state.view !== 'text' || sendingIds.has(state.note.id)) return;
    const id = state.note.id;
    const page = state.note.pages[state.textPage];
    const tp = trPage(page.id);
    const key = draftKey(id, page.id);
    const draft = drafts.get(key);
    const edits = draft != null && draft !== (tp ? tp.text : '');
    if ((tp && tp.status === 'reviewed') || edits) {
      const ok = await ask('Replace the text of this page with a new transcription? '
        + 'Reviewed text can be restored until you save again.', 'Transcribe');
      if (!ok || !state.note || state.note.id !== id) return;
    }
    tTranscribe.disabled = true;
    if (draft != null) await dropDraft(key, draft);
    edited.delete(page.id);
    await requestTranscribe(id, [page.id], false);
  }

  async function saveReview() {
    if (!state.note || state.view !== 'text' || sendingIds.has(state.note.id)) return;
    const my = nav;
    const db = store;
    const id = state.note.id;
    const page = state.note.pages[state.textPage];
    const key = draftKey(id, page.id);
    const text = tArea.value;
    tSave.disabled = true;
    setTState('Saving...');
    const seq = ++trAsked;
    let res = null;
    try {
      res = await api(`${API}/${enc(id)}/pages/${enc(page.id)}/transcript`, { method: 'PUT', body: JSON.stringify({ text }) });
    } catch (_) { res = null; }
    if (db !== store) return;
    if (res && res.ok) {
      await dropDraft(key, text);
      if (current(my, id, db)) {
        if (athenaMsg && athenaMsg.noteId === id) athenaMsg = null; // the text changed since that Send
        takeTranscripts(id, res.body, seq);
        renderText();
        if (tArea.dataset.key === key && !drafts.has(key)) setTState('Reviewed and saved in the note. It stays in the hub.');
      }
    } else if (current(my, id, db)) {
      renderText(res ? `Not saved: ${(res.body && res.body.detail) || `the hub said ${res.status}`}. Your edits are kept on this device.`
        : 'Not saved: the hub is not reachable. Your edits are kept on this device.');
    }
  }

  function ask(message, confirmText) {
    const ui = window.uiModule;
    if (ui && typeof ui.styledConfirm === 'function') return ui.styledConfirm(message, { confirmText });
    return Promise.resolve(window.confirm(message));
  }

  function reviewList(items) {
    const rows = [];
    for (const n of items) {
      const t = n.transcripts;
      if (!t) continue;
      for (const i of t.review || []) rows.push({ n, i, what: 'check the text' });
      for (const i of t.failed || []) rows.push({ n, i, what: 'transcription failed' });
    }
    if (!rows.length) return null;
    const box = el('div', { class: 'bs-card ink-review' }, el('h2', { text: `Needs review (${rows.length})` }));
    for (const r of rows) {
      box.append(el('button', { type: 'button', class: 'bs-btn ink-review-item', 'data-note': r.n.id, 'data-page': String(r.i),
        onclick: () => openNote(r.n.id, { textPage: r.i }) },
      `${r.n.title || 'Untitled'} · page ${r.i + 1} · ${r.what}`));
    }
    return box;
  }

  // -------------------------------------------------------------- screens
  function openLoaded(note, { keepView = false, textPage = null } = {}) {
    const sameOpen = state.note && state.note.id === note.id;
    if (state.note && !sameOpen && state.screen === 'editor') autoTranscribe([...edited]); // left that note
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
      // The same note replaced by the hub's version stays on its tab and page.
      if (!sameOpen || textPage != null) {
        state.view = textPage == null ? 'ink' : 'text';
        state.textPage = textPage == null ? 0 : textPage;
      }
    }
    // A note replaced by the hub's version (409, or changed elsewhere) keeps its
    // transcripts and drafts; another note starts without them.
    if (!sameOpen) {
      tr = null;
      athenaMsg = null;
      edited.clear();
      loadDrafts(note.id);
    }
    state.curPage = null;
    render(); // a different note: draw it all (resize below draws at once when sized)
    titleInput.value = note.title || '';
    listView.hidden = true;
    editor.hidden = false;
    syncTools();
    setStatus(note.synced ? 'Saved' : 'Saved on this device');
    applyView();
    if (state.view === 'text') loadTranscripts();
  }

  async function openNote(id, { textPage = null } = {}) {
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
    openLoaded(note, { textPage });
    if (!note.synced) sync(note.id);
  }

  async function newNote() {
    ++nav;
    const note = { id: uuid(), title: defaultTitle(), paper: 'lined', edited_at: Date.now(), synced: false,
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
    autoTranscribe([...edited]); // leaving the note: read its changed pages
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
    await dropNoteDrafts(id);
  }

  async function deleteNote(id, title) {
    const ok = await confirmDelete(`Delete "${title || 'this note'}"? Its handwriting is removed too, and Athena's copy if it was sent.`);
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
        if (gone(n)) { try { await db.del('notes', n.id); } catch (_) { /* */ } await dropNoteDrafts(n.id, db); }
      }
      local = local.filter((n) => !gone(n));
      for (const n of remote) byId.set(n.id, { ...n, waiting: false });
    }
    for (const n of local) {
      const known = byId.get(n.id);
      if (!known || !n.synced) {
        byId.set(n.id, { id: n.id, title: n.title, image_url: n.image_url || (known && known.image_url),
          edited_at: n.edited_at, pages: n.pages.length, waiting: !n.synced, blocked: n.blocked,
          transcripts: known && known.transcripts, athena: known && known.athena });
      }
    }
    const items = [...byId.values()].sort((a, b) => (b.edited_at || 0) - (a.edited_at || 0)
      || String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    list.textContent = '';
    if (remote === null) list.append(el('div', { class: 'bs-notice', text: 'The hub is not reachable: showing the notes on this device.' }));
    const review = reviewList(items);
    if (review) list.before(review);
    if (!items.length) { list.append(el('div', { class: 'bs-empty', text: 'No handwritten notes yet.' })); return; }
    for (const n of items) list.append(noteCard(n));
  }

  function noteCard(n) {
    const when = n.edited_at ? new Date(n.edited_at) : (n.updated_at ? new Date(n.updated_at) : null);
    const toReview = (n.transcripts && n.transcripts.review && n.transcripts.review.length) || 0;
    const athena = { sent: 'sent to Athena', changed: 'changed since sent to Athena' }[n.athena] || '';
    const meta = [n.pages ? `${n.pages} page${n.pages === 1 ? '' : 's'}` : '', when ? when.toLocaleString() : '',
      toReview ? `${toReview} to review` : '', athena,
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
    drafts.clear(); // the last user's text never shows for this one
    tr = null;
    sendingIds.clear(); // the last user's sends answer to nobody here
    athenaMsg = null;
    edited.clear();
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
  const onHidden = () => {
    if (document.visibilityState !== 'hidden') return;
    router.release();
    flush();
    if (state.note) autoTranscribe([...edited]);
  };
  const onOnline = () => flushQueue();
  document.addEventListener('visibilitychange', onHidden);
  window.addEventListener('pagehide', onHidden);
  window.addEventListener('online', onOnline);
  // A new size (window, rotation): the Write canvas, and the Text tab's page image.
  const onResize = () => {
    if (state.screen !== 'editor') return;
    resize();
    const page = state.note && state.view === 'text' && state.note.pages[state.textPage];
    if (page) drawPageImage(page);
  };
  let observer = null;
  if (globalThis.ResizeObserver) {
    observer = new ResizeObserver(onResize);
    observer.observe(stage);
    observer.observe(textPanel);
  }
  window.addEventListener('orientationchange', onResize);

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
    if (state.note) autoTranscribe([...edited]); // like Back: read the pages changed
    clearTimeout(pollTimer);
    pollTimer = 0;
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
    if (state.note && state.view === 'text') loadTranscripts();
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
        await dropNoteDrafts(id);
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
      window.removeEventListener('orientationchange', onResize);
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
