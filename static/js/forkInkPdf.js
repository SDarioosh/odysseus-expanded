// Fork: PDF datasheets in Write (static/js/forkInk.js), docs/fork/handwritten-notes.md.
//
// A PDF becomes one handwritten note whose pages are PDF pages: 1000 units wide, as tall
// as the PDF page (page.height), with the page rendered by the hub as the background
// (GET /api/ink/notes/<id>/pdf/<page id>.png?w=<tier>, fork/routes/ink_routes.py). They
// take pen ink and the highlighter. The hub groups the strokes on them into marks
// (highlight, underline, ink) with the PDF text under each (GET .../marks, fork/ink/
// pdf_marks.py), listed in the Marks panel and searchable from the list (GET
// /api/ink/marks?q=).
//
// This file holds the parts that are plain logic, so node tests can check them
// (tests/fork/test_ink_pdf_view.py): page layout with pages of different heights, the
// image width tier for a zoom, which pages' images to load and keep, the background
// image cache (bounded: only pages near the view keep a decoded image), mark grouping
// for the panel, and the deep link `#open=write&note=<id>&page=<page id>&mark=<mark id>`.

export const WIDTH_TIERS = [600, 1200, 2000, 3000]; // px (hub: fork/ink/pdf.py WIDTH_TIERS)
export const MAX_PDF_PAGES = 500;                    // hub: MAX_PDF_PAGES
export const PDF_LOAD_AROUND = 1;                    // pages loaded beyond the visible ones
export const PDF_KEEP_AROUND = 2;                    // pages beyond the visible ones that keep their image
export const PDF_PIXEL_BUDGET = 40e6;                // decoded pixels kept at most (~160 MB)
export const NEIGHBOUR_TIER = 1200;                  // a page off screen loads at most this sharp

export const isPdfPage = (page) => !!(page && page.pdf);
export const isPdfNote = (note) => !!(note && (note.pdf || (note.pages || []).some(isPdfPage)));
// Pages that count toward the hub's MAX_PAGES (60): every page but the PDF's own.
export const inkPageCount = (pages) => (pages || []).reduce((n, p) => n + (isPdfPage(p) ? 0 : 1), 0);

// The smallest tier at least `px` wide, else the largest (hub: pdf.tier).
export function pickTier(px) {
  const want = Math.max(1, Math.ceil(Number(px) || 0));
  for (const t of WIDTH_TIERS) if (t >= want) return t;
  return WIDTH_TIERS[WIDTH_TIERS.length - 1];
}

// The tier for a page `cssWidth` CSS px wide on a screen with `dpr` device px per CSS px.
export const tierFor = (cssWidth, dpr = 1) => pickTier(cssWidth * (dpr || 1));

// Page tops in document units: tops[i] is page i's top, tops[n] the end (after the last
// gap). Pages are `gap` apart, each as tall as its `height`.
export function pageTops(pages, gap, fallbackHeight = 1414) {
  const tops = new Array(pages.length + 1);
  let y = 0;
  for (let i = 0; i < pages.length; i++) {
    tops[i] = y;
    y += (Number(pages[i].height) || fallbackHeight) + gap;
  }
  tops[pages.length] = y;
  return tops;
}

// The page under a document y: page i owns [tops[i] - gap/2, tops[i+1] - gap/2), the
// first and last page everything above and below. The same answer as the old
// floor((y + gap/2) / (H + gap)) when every page is H tall.
export function pageIndexAt(tops, docY, gap) {
  const n = tops.length - 1;
  if (n <= 0) return 0;
  let lo = 0;
  let hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (tops[mid] - gap / 2 <= docY) lo = mid; else hi = mid - 1;
  }
  return lo;
}

// Which PDF pages' images to load (visible ones at `tier`, `around` pages on each side at
// most NEIGHBOUR_TIER) and which keep theirs, nearest first. `first`..`last`: the
// visible pages' indexes.
export function planPdfPages(pages, first, last, tier, { around = PDF_LOAD_AROUND, keep = PDF_KEEP_AROUND } = {}) {
  const load = [];
  const kept = [];
  if (!pages.length || first > last) return { load, keep: kept };
  const lo = Math.max(0, first - keep);
  const hi = Math.min(pages.length - 1, last + keep);
  const dist = (i) => (i < first ? first - i : i > last ? i - last : 0);
  const order = [];
  for (let i = lo; i <= hi; i++) if (isPdfPage(pages[i])) order.push(i);
  order.sort((a, b) => dist(a) - dist(b) || a - b);
  for (const i of order) {
    kept.push(pages[i].id);
    const d = dist(i);
    if (d === 0) load.push([pages[i].id, tier]);
    else if (d <= around) load.push([pages[i].id, Math.min(tier, NEIGHBOUR_TIER)]);
  }
  return { load, keep: kept };
}

// The background images of PDF pages. Each page shows at most one decoded image (the
// sharpest loaded) and loads at most one more (a sharper tier); a page whose image is
// shown keeps it until a sharper one has loaded. `retain` drops every page not listed,
// and the farthest of those listed once their pixels pass the budget. Decoded images are
// ImageBitmaps where the browser has them (decoded off the main thread, freed at once
// with close()), so drawing one never decodes on the pen's thread.
//
// urlOf(pageId, tier) -> url; onChange(pageId) is called when an image arrives or fails.
export function createPdfImages({ urlOf, onChange = () => {}, makeImage = null, toBitmap = undefined,
  budget = PDF_PIXEL_BUDGET } = {}) {
  const make = makeImage || (() => new Image());
  const bitmap = toBitmap !== undefined ? toBitmap
    : (typeof createImageBitmap === 'function' ? (img) => createImageBitmap(img) : null);
  const pages = new Map(); // page id -> { shown: {tier, image, w, h} | null, loading: {tier, img} | null, failed: tier | 0 }
  let version = 0;

  const free = (shown) => {
    if (shown && shown.image && typeof shown.image.close === 'function') {
      try { shown.image.close(); } catch (_) { /* gone */ }
    }
  };
  const stop = (loading) => {
    if (!loading) return;
    loading.cancelled = true;
    const { img } = loading;
    img.onload = null;
    img.onerror = null;
    try { img.src = ''; } catch (_) { /* fake image */ }
  };

  function want(pageId, tier) {
    let e = pages.get(pageId);
    if (!e) { e = { shown: null, loading: null, failed: 0 }; pages.set(pageId, e); }
    if (e.shown && e.shown.tier >= tier) return;            // as sharp or sharper on screen
    if (e.loading && e.loading.tier === tier) return;
    if (e.failed && e.failed <= tier) return;              // failed: not again until reload()
    stop(e.loading);
    const img = make();
    const loading = { tier, img, cancelled: false };
    e.loading = loading;
    const done = (image, w, h) => {
      if (loading.cancelled || pages.get(pageId) !== e) { free({ image }); return; }
      e.loading = null;
      if (!e.shown || e.shown.tier < tier) { free(e.shown); e.shown = { tier, image, w, h }; } else free({ image });
      e.failed = 0;
      version++;
      onChange(pageId);
    };
    img.onload = () => {
      if (loading.cancelled) return;
      const w = img.naturalWidth || img.width || tier;
      const h = img.naturalHeight || img.height || 0;
      if (!bitmap) { done(img, w, h); return; }
      Promise.resolve().then(() => bitmap(img)).then((bmp) => {
        // A browser fires `error` on an <img> whose src is set to '': detach the handlers
        // first, or that error marks the page failed and no sharper tier is ever asked for.
        img.onload = null;
        img.onerror = null;
        try { img.src = ''; } catch (_) { /* fake image */ }
        done(bmp, w, h);
      }, () => done(img, w, h));
    };
    img.onerror = () => {
      if (loading.cancelled || pages.get(pageId) !== e) return;
      e.loading = null;
      e.failed = tier;
      version++;
      onChange(pageId);
    };
    img.src = urlOf(pageId, tier);
  }

  // Only `ids` keep their images (in priority order, nearest first), within the budget.
  function retain(ids) {
    const keep = new Set();
    let px = 0;
    for (const id of ids) {
      const e = pages.get(id);
      const s = e && e.shown;
      const cost = s ? (s.w || s.tier) * (s.h || s.tier * 1.414) : 0;
      if (keep.size && px + cost > budget) continue;
      px += cost;
      keep.add(id);
    }
    for (const [id, e] of pages) {
      if (keep.has(id)) continue;
      stop(e.loading);
      free(e.shown);
      pages.delete(id);
      if (e.shown) version++;
    }
  }

  function clear() { retain([]); }

  const best = (pageId) => { const e = pages.get(pageId); return e && e.shown ? e.shown : null; };
  // 'ok' (an image to show), 'loading', 'failed' or 'none'.
  function status(pageId) {
    const e = pages.get(pageId);
    if (!e) return 'none';
    if (e.shown) return 'ok';
    if (e.loading) return 'loading';
    return e.failed ? 'failed' : 'none';
  }
  // Failed pages may be tried again (back online).
  function reload() { for (const e of pages.values()) e.failed = 0; }
  const decoded = () => [...pages.values()].filter((e) => e.shown).length;

  return { want, retain, clear, best, status, reload, decoded, get version() { return version; } };
}

// ------------------------------------------------------------------ marks
const KIND_LABEL = { highlight: 'Highlight', underline: 'Underline', ink: 'Note', circle: 'Circled', arrow: 'Arrow' };
export const markKindLabel = (kind) => KIND_LABEL[kind] || 'Mark';

// What the panel says for a mark: the PDF text under it, else the text line near it.
export function markQuote(mark, max = 160) {
  const cut = (s) => (s.length > max ? `${s.slice(0, max - 1).trimEnd()}…` : s);
  const text = String((mark && mark.text) || '').replace(/\s+/g, ' ').trim();
  if (text) return { text: cut(text), near: false };
  const near = String((mark && mark.near) || '').replace(/\s+/g, ' ').trim();
  if (near) return { text: cut(near), near: true };
  return { text: '', near: false };
}

// Stage 2: an ink mark carries what was written (`written`, read by the hub's vision
// model, fork/ink/mark_reading.py) and how its reading goes (`reading`: none, waiting,
// pending, read, failed, reviewed). What the panel and the search results show for a
// mark: `main` (the line itself; `quoted` puts it in quotes, `quiet` dims it), `sub` (a
// smaller line under it, '' for none), `title` (a tooltip, '' for none) and `editable`
// (only ink marks have writing to correct). `max` cuts long text.
export const MARK_READING_BUSY = ['waiting', 'pending'];
export function markDisplay(mark, max = 160) {
  const m = mark || {};
  const cut = (s) => (s.length > max ? `${s.slice(0, max - 1).trimEnd()}…` : s);
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const q = markQuote(m, max);
  const pdfLine = q.text ? (q.near ? `near “${q.text}”` : `“${q.text}”`) : '';
  const out = { label: markKindLabel(m.kind), main: '', quoted: false, quiet: false, sub: '', title: '',
    editable: m.kind === 'ink', busy: false };
  const nearLine = clean(m.near);
  if (m.kind === 'arrow') {
    // An arrow alone: the line at its head (`text`, else `near`).
    if (q.text) { out.main = `points to “${q.text}”`; out.quoted = true; }
    else { out.main = 'points to no PDF text'; out.quiet = true; }
    return out;
  }
  if (m.kind !== 'ink' || !m.reading || m.reading === 'none') {
    // Highlight, underline, circled words, or an ink mark from a hub that doesn't read marks.
    const text = clean(m.text);
    if (m.kind === 'circle' && m.points_to && nearLine) {
      out.main = text ? `“${cut(text)}”` : 'no PDF text inside it';
      out.quoted = !!text;
      out.quiet = !text;
      out.sub = `points to “${cut(nearLine)}”`;
      return out;
    }
    if (q.text) { out.main = pdfLine; out.quoted = !q.near; out.quiet = q.near; }
    else { out.main = m.kind === 'circle' ? 'no PDF text inside it' : 'no PDF text under it'; out.quiet = true; }
    return out;
  }
  // An ink mark: what was written first, the PDF text it is on, by, or points to under it
  // (`points_to`: `near` is the line an arrow drawn with the note points to).
  if (m.points_to && nearLine) out.sub = `points to “${cut(nearLine)}”`;
  else out.sub = q.text ? (q.near ? `by “${q.text}”` : `on “${q.text}”`) : '';
  const written = clean(m.written);
  if (MARK_READING_BUSY.includes(m.reading)) {
    out.main = 'Reading…';
    out.quiet = true;
    out.busy = true;
  } else if (m.reading === 'failed') {
    out.main = 'Could not read it';
    out.quiet = true;
    out.title = clean(m.error || m.reading_error) || 'The hub could not read this writing.';
  } else if (written) {
    out.main = `“${cut(written)}”`;
    out.quoted = true;
    if (m.reading === 'reviewed') out.title = 'Corrected by you';
  } else {
    out.main = 'a drawing (no words)';
    out.quiet = true;
  }
  return out;
}

// The Marks panel asks again while the hub is still reading marks of the note.
export const marksStillReading = (body) => !!(body && body.reading === true);

// The marks of a note grouped by page, in the note's page order (then y, then x as the
// hub sends them). Marks on a page the note no longer has are left out.
export function groupMarks(marks, pages) {
  const index = new Map((pages || []).map((p, i) => [p.id, i]));
  const groups = new Map();
  for (const m of marks || []) {
    if (!m || !index.has(m.page_id)) continue;
    let g = groups.get(m.page_id);
    if (!g) {
      g = { pageId: m.page_id, index: index.get(m.page_id), pageNo: m.page_no || null, marks: [] };
      groups.set(m.page_id, g);
    }
    g.marks.push(m);
  }
  const out = [...groups.values()].sort((a, b) => a.index - b.index);
  for (const g of out) {
    g.marks.sort((a, b) => ((a.box || [])[1] || 0) - ((b.box || [])[1] || 0) || ((a.box || [])[0] || 0) - ((b.box || [])[0] || 0));
  }
  return out;
}

// The scroll (document y, page units) that puts a box of page `index` in the middle of
// a view `viewH` units tall, or the page's top when it has no box.
export function scrollForBox(tops, index, box, viewH, gap) {
  const top = tops[index] || 0;
  if (!box || box.length < 4) return top - gap / 2;
  const mid = top + box[1] + box[3] / 2;
  return mid - viewH / 2;
}

// --------------------------------------------------------------- deep link
const ID = /^[A-Za-z0-9_-]{1,80}$/;

// `#open=write&note=<id>[&page=<page id>][&mark=<mark id>]` -> { note, page, mark }, else null.
export function parseWriteHash(hash) {
  const raw = String(hash || '');
  if (!raw.startsWith('#open=write&')) return null;
  let params;
  try { params = new URLSearchParams(raw.slice(1)); } catch (_) { return null; }
  if (params.get('open') !== 'write') return null;
  const note = params.get('note') || '';
  if (!ID.test(note)) return null;
  const page = params.get('page');
  const mark = params.get('mark');
  return { note, page: page && ID.test(page) ? page : null, mark: mark && ID.test(mark) ? mark : null };
}

export function writeHash({ note, page = null, mark = null }) {
  let h = `#open=write&note=${encodeURIComponent(note)}`;
  if (page) h += `&page=${encodeURIComponent(page)}`;
  if (mark) h += `&mark=${encodeURIComponent(mark)}`;
  return h;
}

// ------------------------------------------------------------- part numbers
// A datasheet's part-number tags (hub: fork/ink/parts.py): at most MAX_PARTS, each 1 to
// MAX_PART_LEN characters (trimmed, inner spaces collapsed), unique ignoring case (the
// first spelling kept), in the owner's order.
export const MAX_PARTS = 20;
export const MAX_PART_LEN = 40;
export const cleanPart = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();

// The tags with `raw` added: { parts, error } (error '' when added; parts unchanged on error).
export function addPart(parts, raw) {
  const list = Array.isArray(parts) ? parts.slice() : [];
  const p = cleanPart(raw);
  if (!p) return { parts: list, error: 'Type a part number first.' };
  if (p.length > MAX_PART_LEN) return { parts: list, error: `A part number has at most ${MAX_PART_LEN} characters.` };
  if (list.some((x) => String(x).toLowerCase() === p.toLowerCase())) return { parts: list, error: `${p} is already there.` };
  if (list.length >= MAX_PARTS) return { parts: list, error: `At most ${MAX_PARTS} part numbers.` };
  list.push(p);
  return { parts: list, error: '' };
}

export const removePart = (parts, p) => (Array.isArray(parts) ? parts.filter((x) => x !== p) : []);

// At most `max` tags to show, and how many more there are.
export function partsShown(parts, max = 3) {
  const list = Array.isArray(parts) ? parts.filter((x) => typeof x === 'string' && x) : [];
  return { shown: list.slice(0, max), more: Math.max(0, list.length - max) };
}

// --------------------------------------------------------------- downloads
// The datasheet as a file: the original, or a copy with the marks as PDF annotations
// (GET /api/ink/notes/<id>/pdf[?marks=1], fork/ink/pdf_export.py). Plain links, so the
// browser sends the sign-in cookie; the names are the hub's own (Content-Disposition).
export function pdfDownloads(noteId, name = '') {
  const base = `/api/ink/notes/${encodeURIComponent(noteId)}/pdf`;
  const file = cleanPart(name) || 'datasheet.pdf';
  const stem = file.replace(/\.pdf$/i, '') || 'datasheet';
  return { original: { href: base, name: `${stem}.pdf` },
    marked: { href: `${base}?marks=1`, name: `${stem} (marked).pdf` } };
}

// ---------------------------------------------------------------- Library
// A Library PDF in the From Library picker: its title and the day it last changed.
export function libraryItem(doc, locale = undefined) {
  const d = doc || {};
  const title = cleanPart(d.title) || 'Untitled PDF';
  let date = '';
  const t = d.updated_at ? Date.parse(d.updated_at) : NaN;
  if (Number.isFinite(t)) {
    try { date = new Date(t).toLocaleDateString(locale, { year: 'numeric', month: 'short', day: 'numeric' }); } catch (_) { date = ''; }
  }
  return { id: String(d.id == null ? '' : d.id), title, date };
}
