"""Fork: PDF datasheets in the Write view (static/js/forkInkPdf.js, static/js/forkInk.js).

Two levels, like test_ink_palm_rejection.py:
* the plain logic of forkInkPdf.js through `node`: width tiers, the layout of pages of
  different heights, which pages' images load and stay, the background image cache with
  fake images, mark grouping and the `#open=write&...` deep link;
* the whole view in a real Chromium through Playwright (the fake hub of
  tests/helpers/ink_view_harness.mjs, plus the PDF routes faked here): search marks in the
  list, open the result, write on a PDF page, add a page between PDF pages, save, open
  the Marks panel and tap a mark.
Each skips when node, or Playwright and its browser, aren't available. Fictional data only.
"""
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PDF = (_REPO / "static" / "js" / "forkInkPdf.js").as_posix()
_LINKS = (_REPO / "static" / "js" / "forkInkLinks.js").as_posix()
_HARNESS = (_REPO / "tests" / "helpers" / "ink_view_harness.mjs").as_posix()
_HAS_NODE = shutil.which("node") is not None


def _node(body: str) -> dict:
    js = f"const P = await import('{_PDF}');\nconst out = {{}};\n{body}\nconsole.log(JSON.stringify(out));"
    proc = subprocess.run(["node", "--input-type=module"], input=js, capture_output=True,
                          text=True, cwd=str(_REPO), timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _playwright_ready() -> bool:
    if not _HAS_NODE:
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True,
                          cwd=str(_REPO), timeout=30).returncode == 0


needs_node = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


# ------------------------------------------------------------------ pure logic
@needs_node
def test_tier_is_the_smallest_at_least_as_wide_as_the_page_on_screen():
    got = _node("""
    out.tiers = [1, 600, 601, 1200, 1500, 2000, 2999, 3000, 5000].map(P.pickTier);
    out.dpr = [P.tierFor(860, 1), P.tierFor(860, 2), P.tierFor(860, 3), P.tierFor(430, 1)];
    """)
    assert got["tiers"] == [600, 600, 1200, 1200, 2000, 2000, 3000, 3000, 3000]
    assert got["dpr"] == [1200, 2000, 3000, 600]


@needs_node
def test_layout_with_pages_of_different_heights():
    got = _node("""
    const pages = [{height: 1294}, {height: 1414}, {height: 707}];
    const tops = P.pageTops(pages, 40);
    out.tops = tops;
    out.at = [-500, 0, 1313, 1314, 1500, 2767, 2768, 9999].map((y) => P.pageIndexAt(tops, y, 40));
    // Every page PAGE_H tall: the same answer as the old fixed-height formula.
    const same = Array.from({length: 7}, () => ({height: 1414}));
    const t2 = P.pageTops(same, 40);
    out.same = true;
    for (let y = -100; y < 7 * 1454 + 200; y += 37) {
      const old = Math.max(0, Math.min(6, Math.floor((y + 20) / 1454)));
      if (P.pageIndexAt(t2, y, 40) !== old) out.same = false;
    }
    out.empty = P.pageIndexAt(P.pageTops([], 40), 100, 40);
    """)
    assert got["tops"] == [0, 1334, 2788, 3535]
    assert got["at"] == [0, 0, 0, 1, 1, 1, 2, 2]
    assert got["same"] is True
    assert got["empty"] == 0


@needs_node
def test_plan_loads_visible_pages_sharp_neighbours_softer_and_keeps_only_near_ones():
    got = _node("""
    const pages = Array.from({length: 10}, (_, i) => (i === 4 ? {id: 'blank'} : {id: 'pd' + i, pdf: {page: i + 1}}));
    out.plan = P.planPdfPages(pages, 5, 6, 3000);
    out.none = P.planPdfPages([], 0, 0, 600);
    """)
    plan = got["plan"]
    assert plan["load"] == [["pd5", 3000], ["pd6", 3000], ["pd7", 1200]]  # page 4 is blank: not loaded
    assert plan["keep"] == ["pd5", "pd6", "pd7", "pd3", "pd8"]           # nearest first, +-2
    assert got["none"] == {"load": [], "keep": []}


IMAGES = """
const made = [];
const closed = [];
function makeImage() {
  const img = { naturalWidth: 0, naturalHeight: 0, _src: '' };
  Object.defineProperty(img, 'src', { get() { return this._src; }, set(v) { this._src = v; } });
  made.push(img);
  return img;
}
const loadImg = (img, w) => { img.naturalWidth = w; img.naturalHeight = Math.round(w * 1.3); img.onload(); };
const changes = [];
const imgs = P.createPdfImages({ urlOf: (id, t) => `/x/${id}.png?w=${t}`, onChange: (id) => changes.push(id),
  makeImage, toBitmap: (img) => Promise.resolve({ w: img.naturalWidth, close() { closed.push(img.naturalWidth); } }) });
const tick = () => new Promise((r) => setTimeout(r, 0));
"""


@needs_node
def test_image_cache_keeps_the_lower_tier_until_the_sharper_one_loads():
    got = _node(IMAGES + """
    imgs.want('a', 600);
    out.loading = imgs.status('a');
    loadImg(made[0], 600); await tick(); await tick();
    out.first = imgs.best('a').tier;
    imgs.want('a', 600);                 // already shown: nothing new
    out.made1 = made.length;
    imgs.want('a', 2000);
    out.whileLoading = imgs.best('a').tier;   // the lower tier is still shown
    out.url = made[1].src;
    loadImg(made[1], 2000); await tick(); await tick();
    out.after = imgs.best('a').tier;
    out.closed = closed.slice();         // the 600 px image was freed
    imgs.want('a', 1200);                // zoomed out: the sharper image stays
    out.made2 = made.length;
    out.changes = changes.length;
    """)
    assert got["loading"] == "loading"
    assert got["first"] == 600
    assert got["made1"] == 1
    assert got["whileLoading"] == 600
    assert got["url"] == "/x/a.png?w=2000"
    assert got["after"] == 2000
    assert got["closed"] == [600]
    assert got["made2"] == 2
    assert got["changes"] == 2


@needs_node
def test_image_cache_drops_far_pages_and_stays_in_budget_and_does_not_retry_failures():
    got = _node(IMAGES + """
    for (const id of ['a', 'b', 'c']) imgs.want(id, 1200);
    made.forEach((img) => loadImg(img, 1200)); await tick(); await tick();
    out.decoded = imgs.decoded();
    imgs.retain(['b']);
    out.after = [imgs.status('a'), imgs.status('b'), imgs.status('c')];
    out.closed = closed.length;
    // Budget: two 3000 px pages don't fit in 15 M pixels; the nearest (listed first) stays.
    const small = P.createPdfImages({ urlOf: () => 'u', makeImage, budget: 15e6, toBitmap: null });
    const before = made.length;
    small.want('x', 3000); small.want('y', 3000);
    loadImg(made[before], 3000); loadImg(made[before + 1], 3000);
    small.retain(['y', 'x']);
    out.budget = [small.status('x'), small.status('y')];
    // A failed page says so and is not asked again until reload() (back online).
    const n = made.length;
    imgs.want('f', 600);
    made[n].onerror();
    out.failed = imgs.status('f');
    imgs.want('f', 600);
    out.retried = made.length - n;
    imgs.reload();
    imgs.want('f', 600);
    out.reloaded = made.length - n;
    """)
    assert got["decoded"] == 3
    assert got["after"] == ["none", "ok", "none"]
    assert got["closed"] == 2
    assert got["budget"] == ["none", "ok"]
    assert got["failed"] == "failed"
    assert got["retried"] == 1
    assert got["reloaded"] == 2


@needs_node
def test_marks_group_by_page_in_page_order_and_quote_their_text():
    got = _node("""
    const pages = [{id: 'p1'}, {id: 'pd2'}, {id: 'pd3'}];
    const marks = [
      {id: 'm-c', page_id: 'pd3', page_no: 2, kind: 'ink', box: [10, 500, 5, 5], text: '', near: 'VDD 3.3 V'},
      {id: 'm-a', page_id: 'pd2', page_no: 1, kind: 'highlight', box: [50, 300, 10, 10], text: 'Supply  voltage'},
      {id: 'm-b', page_id: 'pd2', page_no: 1, kind: 'underline', box: [20, 100, 10, 10], text: 'Pinout'},
      {id: 'm-x', page_id: 'gone', page_no: 9, kind: 'ink', box: [0, 0, 1, 1], text: ''},
    ];
    out.groups = P.groupMarks(marks, pages).map((g) => [g.pageId, g.index, g.pageNo, g.marks.map((m) => m.id)]);
    out.quotes = marks.map((m) => P.markQuote(m));
    out.long = P.markQuote({text: 'x'.repeat(300)}, 20).text.length;
    out.kinds = ['highlight', 'underline', 'ink', 'odd'].map(P.markKindLabel);
    out.scroll = P.scrollForBox([0, 1334], 1, [100, 300, 400, 30], 800, 40);
    out.top = P.scrollForBox([0, 1334], 1, null, 800, 40);
    out.count = P.inkPageCount([{pdf: {}}, {}, {pdf: {}}, {}]);
    out.isPdf = [P.isPdfNote({pages: [{}]}), P.isPdfNote({pages: [{pdf: {page: 1}}]}), P.isPdfNote({pdf: {}, pages: []})];
    """)
    assert got["groups"] == [["pd2", 1, 1, ["m-b", "m-a"]], ["pd3", 2, 2, ["m-c"]]]
    assert got["quotes"][0] == {"text": "VDD 3.3 V", "near": True}
    assert got["quotes"][1] == {"text": "Supply voltage", "near": False}
    assert got["quotes"][3] == {"text": "", "near": False}
    assert got["long"] == 20
    assert got["kinds"] == ["Highlight", "Underline", "Note", "Mark"]
    assert got["scroll"] == 1334 + 315 - 400
    assert got["top"] == 1314
    assert got["count"] == 2
    assert got["isPdf"] == [False, True, True]


@needs_node
def test_write_deep_link_round_trips_and_refuses_junk():
    got = _node(f"""
    const L = await import('{_LINKS}');
    const h = P.writeHash({{note: 'n-1', page: 'pd0123456789ab', mark: 'm-s1'}});
    out.hash = h;
    out.back = P.parseWriteHash(h);
    out.noteOnly = P.parseWriteHash('#open=write&note=abc');
    out.junk = ['#open=notes&note=abc', '#open=write&note=a/b', '#open=write', '', '#open=write&note=x&page=a b']
      .map(P.parseWriteHash);
    const loc = {{ href: 'http://hub.test/', origin: 'http://hub.test', pathname: '/' }};
    out.link = L.writeLinkTarget('/' + h, loc);
    out.other = L.writeLinkTarget('http://elsewhere.test/' + h, loc);
    """)
    assert got["hash"] == "#open=write&note=n-1&page=pd0123456789ab&mark=m-s1"
    assert got["back"] == {"note": "n-1", "page": "pd0123456789ab", "mark": "m-s1"}
    assert got["noteOnly"] == {"note": "abc", "page": None, "mark": None}
    assert got["junk"][:4] == [None, None, None, None]
    assert got["junk"][4] == {"note": "x", "page": None, "mark": None}
    assert got["link"] == got["back"]
    assert got["other"] is None


@needs_node
def test_ink_marks_show_what_was_written_and_how_the_reading_goes():
    got = _node("""
    const near = 'Input voltage range 2.7 V to 5.5 V';
    const ink = (extra) => ({kind: 'ink', text: '', near, ...extra});
    out.read = P.markDisplay(ink({reading: 'read', written: '  check EN   pull-up '}));
    out.reviewed = P.markDisplay(ink({reading: 'reviewed', written: 'check EN pull-down'}));
    out.pending = P.markDisplay(ink({reading: 'pending', written: ''}));
    out.waiting = P.markDisplay(ink({reading: 'waiting'}));
    out.failed = P.markDisplay(ink({reading: 'failed', error: 'No vision model is set'}));
    out.failedPlain = P.markDisplay(ink({reading: 'failed'}));
    out.drawing = P.markDisplay(ink({reading: 'read', written: ''}));
    out.circled = P.markDisplay({kind: 'ink', text: '3.3 V', near: '', reading: 'read', written: 'ok'});
    out.oldHub = P.markDisplay({kind: 'ink', text: '', near: 'VDD'});
    out.highlight = P.markDisplay({kind: 'highlight', text: 'Supply voltage', reading: 'none', written: ''});
    out.bare = P.markDisplay({kind: 'underline', text: '', near: ''});
    out.long = P.markDisplay(ink({reading: 'read', written: 'w'.repeat(300)}), 20).main.length;
    out.polling = [P.marksStillReading({reading: true}), P.marksStillReading({reading: false}),
                   P.marksStillReading({}), P.marksStillReading(null)];
    """)
    assert got["read"]["main"] == "“check EN pull-up”" and got["read"]["quoted"] is True
    assert got["read"]["sub"] == "by “Input voltage range 2.7 V to 5.5 V”"
    assert got["read"]["label"] == "Note" and got["read"]["editable"] is True
    assert got["reviewed"]["main"] == "“check EN pull-down”" and got["reviewed"]["title"] == "Corrected by you"
    for k in ("pending", "waiting"):
        assert got[k]["main"] == "Reading…" and got[k]["quiet"] and got[k]["busy"], got[k]
    assert got["failed"]["main"] == "Could not read it" and got["failed"]["title"] == "No vision model is set"
    assert got["failedPlain"]["title"]
    assert got["drawing"]["main"] == "a drawing (no words)" and got["drawing"]["quiet"] is True
    assert got["circled"]["sub"] == "on “3.3 V”"
    assert got["oldHub"]["main"] == "near “VDD”" and got["oldHub"]["quiet"] is True
    assert got["highlight"]["main"] == "“Supply voltage”" and got["highlight"]["editable"] is False
    assert got["highlight"]["sub"] == ""
    assert got["bare"]["main"] == "no PDF text under it"
    assert got["long"] == 22                                  # 20 characters in quotes
    assert got["polling"] == [True, False, False, False]


# ------------------------------------------------------------------ in a browser
NOTE_ID = "0d5a7c1e-2b3f-4a6d-9e8f-1a2b3c4d5e6f"
P1 = "pd111111111111"
P2 = "pd222222222222"

E2E = """
import { launch, openPage } from '%(harness)s';
import fs from 'node:fs';

const { browser, skip } = await launch();
if (skip) { console.log('SKIP ' + skip); process.exit(0); }
const NOTE = '%(note)s', P1 = '%(p1)s', P2 = '%(p2)s';
const png = fs.readFileSync(process.env.PDF_PNG);
const pdfNote = { note_id: NOTE, owner: 'alice', title: 'Fictional regulator', paper: 'blank', edited_at: 1000,
  image_url: null, pdf: { file: 'pdfs/x.pdf', hash: 'f'.repeat(64), name: 'regulator.pdf', pages: 2, at: '2026-11-01T10:00:00Z' },
  pages: [
    { id: P1, width: 1000, height: 1294, strokes: [], pdf: { page: 1, width_pt: 612, height_pt: 792 } },
    { id: P2, width: 1000, height: 707, strokes: [], pdf: { page: 2, width_pt: 842, height_pt: 595 } },
  ] };
const mark = { id: 'm-s1', page_id: P2, page_no: 2, kind: 'highlight', color: '#ffd400', box: [100, 300, 400, 30],
  text: 'Supply voltage 3.3 V', near: '', strokes: ['s1'], at: 5 };
const answer = {
  ['GET /api/ink/notes']: { status: 200, body: { notes: [{ id: NOTE, title: 'Fictional regulator', pages: 2, edited_at: 1000,
    image_url: null, pdf: { name: 'regulator.pdf', pages: 2 } }] } },
  ['GET /api/ink/marks']: { status: 200, body: { q: 'supply', results: [
    { note_id: NOTE, title: 'Fictional regulator', pdf_name: 'regulator.pdf', mark }] } },
  ['GET /api/ink/notes/' + NOTE + '/marks']: { status: 200, body: { note_id: NOTE, title: 'Fictional regulator', marks: [mark] } },
};
const page = await openPage(browser, { hubState: JSON.stringify({ notes: { [NOTE]: pdfNote }, answer }) });
const images = [];
await page.route('**/api/ink/notes/*/pdf/*.png*', (route) => {
  images.push(new URL(route.request().url()).pathname + new URL(route.request().url()).search);
  return route.fulfill({ status: 200, contentType: 'image/png', body: png });
});
const out = {};
const until = (fn, arg) => page.waitForFunction(fn, arg, { timeout: 8000 });

// The list: the PDF badge, and Search marks finds the highlight; tapping it opens the note there.
await page.evaluate(() => view.refresh());
await until(() => document.querySelector('.ink-card .ink-badge'));
out.badge = await page.evaluate(() => document.querySelector('.ink-card .ink-badge').textContent);
out.meta = await page.evaluate(() => document.querySelector('.ink-card .bs-meta').textContent);
out.searchShown = await page.evaluate(() => !document.querySelector('.ink-mark-searchbox').hidden);
await page.fill('.ink-mark-search', 'supply');
await until(() => document.querySelector('.ink-mark-result'));
await page.click('.ink-mark-result');
await until(() => window.view._state.screen === 'editor' && !document.querySelector('.ink-flash').hidden);
out.fromSearch = await page.evaluate(() => ({ sy: view._state.sy, note: view._state.note.id }));

// Both backgrounds load at the tier for the page width on screen (about 860 px: 1200).
await until((ids) => ids.every((id) => view._pdfImages.status(id) === 'ok'), [P1, P2]);
out.images = images.slice();
out.marksBtn = await page.evaluate(() => !document.querySelector('.ink-marks-btn').hidden);

// Back to the top; the PDF page is drawn under the pages (the PNG is light blue).
await page.evaluate(async () => { view.fit(); await settle(150); });
out.pixel = await page.evaluate(() => [...view._base.getContext('2d').getImageData(450, 600, 1, 1).data]);

// A pen stroke on PDF page 1, a blank page added after it, then the save.
await page.evaluate(() => { stroke('pen', 3, [[200, 150], [260, 158], [320, 150], [400, 160]]); });
out.strokes = await page.evaluate(() => view._state.note.pages[0].strokes.length);
await page.evaluate(async () => { document.querySelector('.ink-editor button[title="Add a page"]').click(); await settle(50); });
await page.evaluate(() => view._persist());
await until((id) => hub.notes[id] && hub.notes[id].pages.length === 3 && hub.notes[id].pages[0].strokes.length === 1, NOTE);
out.saved = await page.evaluate((id) => hub.notes[id].pages.map((p) => ({ id: p.id, height: p.height, width: p.width,
  strokes: p.strokes.length, pen: p.strokes[0] ? p.strokes[0].pen.highlighter : null, pdf: 'pdf' in p })), NOTE);
const local = await page.evaluate(async (id) => (await idbNotes('fork-ink:alice')).find((n) => n.id === id), NOTE);
out.localPdf = local.pages.map((p) => (p.pdf ? p.pdf.page : null));
out.localNotePdf = !!local.pdf;

// The Marks panel: grouped by page; a tap scrolls to the mark and flashes it.
await page.evaluate(async () => { view.fit(); await settle(100); });
await page.click('.ink-marks-btn');
await until(() => document.querySelector('.ink-marks .ink-mark'));
out.panel = await page.evaluate(() => ({ head: document.querySelector('.ink-marks-page h3').textContent,
  text: document.querySelector('.ink-marks .ink-mark').textContent }));
await page.evaluate(() => { document.querySelector('.ink-flash').hidden = true; });
await page.click('.ink-marks .ink-mark');
await until(() => !document.querySelector('.ink-flash').hidden);
out.afterTap = await page.evaluate(() => ({ sy: view._state.sy, flash: document.querySelector('.ink-flash').getBoundingClientRect().height }));

// The Text tab: no Send to Athena for a datasheet.
await page.click('.ink-tab:nth-child(2)');
await page.waitForTimeout(200);
out.send = await page.evaluate(() => ({ hidden: document.querySelector('.ink-send').hidden,
  line: document.querySelector('.ink-athena').textContent }));
out.errors = await page.evaluate(() => window.errors);
await browser.close();
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not _playwright_ready(), reason="node or playwright not available")
def test_pdf_note_in_a_browser(tmp_path):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1200, 1553), (200, 220, 255)).save(buf, format="PNG")
    png = tmp_path / "page.png"
    png.write_bytes(buf.getvalue())
    script = tmp_path / "pdf_e2e.mjs"
    script.write_text(E2E % {"harness": _HARNESS, "note": NOTE_ID, "p1": P1, "p2": P2})
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, cwd=str(_REPO),
                          timeout=300, env={**os.environ, "PDF_PNG": str(png)})
    assert proc.returncode == 0, proc.stderr
    last = proc.stdout.strip().splitlines()[-1]
    if last.startswith("SKIP"):
        pytest.skip(last)
    got = json.loads(last)
    assert got["errors"] == []
    assert got["badge"] == "PDF"
    assert "PDF, 2 pages: regulator.pdf" in got["meta"]
    assert got["searchShown"] is True
    # Opened from the search at page 2 (top 1334): the mark in the middle of the view.
    assert got["fromSearch"]["note"] == NOTE_ID
    assert got["fromSearch"]["sy"] > 1000
    assert got["marksBtn"] is True
    assert all("?w=1200" in u for u in got["images"]), got["images"]
    assert {u.split("/")[-1].split(".")[0] for u in got["images"]} == {P1, P2}
    r, g, b, _ = got["pixel"]
    assert b > 230 and r < 215, got["pixel"]                 # the PDF page, not white paper
    assert got["strokes"] == 1
    saved = got["saved"]
    assert [p["height"] for p in saved] == [1294, 1414, 707]  # each PDF page keeps its height
    assert [p["id"] for p in saved][0::2] == [P1, P2]         # the blank page went after page 1
    assert saved[1]["id"].startswith("p") and not saved[1]["id"].startswith("pd")
    assert all(p["width"] == 1000 for p in saved)
    assert saved[0]["strokes"] == 1 and saved[0]["pen"] is False  # pen ink on a PDF page
    assert not any(p["pdf"] for p in saved)                   # the save never sends `pdf`
    assert got["localPdf"] == [1, None, 2]                    # this device's copy keeps it
    assert got["localNotePdf"] is True
    assert got["panel"]["head"] == "Page 2"
    assert "Highlight" in got["panel"]["text"] and "Supply voltage 3.3 V" in got["panel"]["text"]
    assert got["afterTap"]["sy"] > 2000                       # page 2 is now the third page (top 2788)
    assert got["afterTap"]["flash"] > 10
    assert got["send"]["hidden"] is True
    assert "Datasheets stay in Odysseus" in got["send"]["line"]


E2E_READING = """
import { launch, openPage } from '%(harness)s';

const { browser, skip } = await launch();
if (skip) { console.log('SKIP ' + skip); process.exit(0); }
const NOTE = '%(note)s', P1 = '%(p1)s';
const pdfNote = { note_id: NOTE, owner: 'alice', title: 'Fictional regulator', paper: 'blank', edited_at: 1000,
  image_url: null, pdf: { file: 'pdfs/x.pdf', hash: 'f'.repeat(64), name: 'regulator.pdf', pages: 1, at: '2026-11-01T10:00:00Z' },
  pages: [{ id: P1, width: 1000, height: 1294, strokes: [], pdf: { page: 1, width_pt: 612, height_pt: 792 } }] };
const base = { id: 'm-s9', page_id: P1, page_no: 1, kind: 'ink', color: '#1f1f1f', box: [700, 200, 150, 60],
  text: '', near: 'Input voltage range 2.7 V to 5.5 V', strokes: ['s9'], at: 5 };
const page = await openPage(browser, { hubState: JSON.stringify({ notes: { [NOTE]: pdfNote } }) });
await page.route('**/api/ink/notes/*/pdf/*.png*', (route) => route.fulfill({ status: 404, body: '' }));
// The fake hub: the first listing is still reading the mark, the next ones have it read;
// a correction answers the merged mark. Request bodies of PUTs are kept.
await page.evaluate(([NOTE, base]) => {
  window.marksGets = 0;
  window.markPuts = [];
  window.written = { text: 'check EN pull-up', reading: 'read' };
  hub.answer['GET /api/ink/notes/' + NOTE + '/marks'] = { status: 200, body: () => {
    window.marksGets += 1;
    const first = window.marksGets === 1;
    return { note_id: NOTE, title: 'Fictional regulator', reading: first,
      marks: [{ ...base, written: first ? '' : window.written.text, reading: first ? 'pending' : window.written.reading }] };
  } };
  hub.answer['PUT /api/ink/notes/' + NOTE + '/marks/m-s9'] = { status: 200, body: () => {
    window.written = { text: window.markPuts[window.markPuts.length - 1].written, reading: 'reviewed' };
    return { ...base, written: window.written.text, reading: 'reviewed' };
  } };
  hub.answer['GET /api/ink/notes'] = { status: 200, body: { notes: [{ id: NOTE, title: 'Fictional regulator', pages: 1,
    edited_at: 1000, image_url: null, pdf: { name: 'regulator.pdf', pages: 1 } }] } };
  hub.answer['GET /api/ink/marks'] = { status: 200, body: { q: 'pull', results: [{ note_id: NOTE, title: 'Fictional regulator',
    pdf_name: 'regulator.pdf', mark: { ...base, written: 'check EN pull-up', reading: 'read' } }] } };
  const f = window.fetch;
  window.fetch = (u, o = {}) => {
    if ((o.method || '').toUpperCase() === 'PUT' && String(u).includes('/marks/')) window.markPuts.push(JSON.parse(o.body));
    return f(u, o);
  };
}, [NOTE, base]);
const out = {};
const until = (fn, arg, timeout = 8000) => page.waitForFunction(fn, arg, { timeout });
const panelText = () => page.evaluate(() => document.querySelector('.ink-marks .ink-mark-row').textContent);

// Search marks shows what was written.
await page.evaluate(() => view.refresh());
await until(() => !document.querySelector('.ink-mark-searchbox').hidden);
await page.fill('.ink-mark-search', 'pull');
await until(() => document.querySelector('.ink-mark-result'));
out.result = await page.evaluate(() => document.querySelector('.ink-mark-result').textContent);

// The panel: Reading... while the hub reads, then the writing, by polling.
await page.evaluate((id) => view.openNote(id), NOTE);
await until(() => !document.querySelector('.ink-marks-btn').hidden);
await page.click('.ink-marks-btn');
await until(() => document.querySelector('.ink-marks .ink-mark-text.busy'));
out.pending = await panelText();
out.editShown = await page.evaluate(() => !!document.querySelector('.ink-marks .ink-mark-edit'));
await until(() => document.querySelector('.ink-marks .ink-mark-row').textContent.includes('pull-up'), null, 9000);
out.read = await panelText();
const gets = await page.evaluate(() => window.marksGets);
await page.waitForTimeout(4600);
out.getsAfterDone = (await page.evaluate(() => window.marksGets)) - gets; // polling stopped

// Edit, then Escape: nothing sent.
await page.click('.ink-marks .ink-mark-edit');
await until(() => document.querySelector('.ink-mark-input'));
out.inputValue = await page.inputValue('.ink-mark-input');
await page.press('.ink-mark-input', 'Escape');
await until(() => !document.querySelector('.ink-mark-input'));
out.putsAfterEscape = await page.evaluate(() => window.markPuts.length);
// Enter saves the correction.
await page.click('.ink-marks .ink-mark-edit');
await page.fill('.ink-mark-input', 'check EN pull-down');
await page.press('.ink-mark-input', 'Enter');
await until(() => !document.querySelector('.ink-mark-input') && document.querySelector('.ink-marks .ink-mark-row').textContent.includes('pull-down'));
out.puts = await page.evaluate(() => window.markPuts);
out.saved = await panelText();
out.title = await page.evaluate(() => document.querySelector('.ink-marks .ink-mark-text').title);
out.errors = await page.evaluate(() => window.errors);
await browser.close();
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not _playwright_ready(), reason="node or playwright not available")
def test_marks_panel_reads_handwriting_polls_and_corrects_in_a_browser(tmp_path):
    script = tmp_path / "pdf_reading_e2e.mjs"
    script.write_text(E2E_READING % {"harness": _HARNESS, "note": NOTE_ID, "p1": P1})
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, cwd=str(_REPO), timeout=300)
    assert proc.returncode == 0, proc.stderr
    last = proc.stdout.strip().splitlines()[-1]
    if last.startswith("SKIP"):
        pytest.skip(last)
    got = json.loads(last)
    assert got["errors"] == []
    assert "check EN pull-up" in got["result"] and "by “Input voltage range" in got["result"]
    assert "Reading…" in got["pending"]
    assert got["editShown"] is True
    assert "“check EN pull-up”" in got["read"] and "by “Input voltage range 2.7 V to 5.5 V”" in got["read"]
    assert got["getsAfterDone"] == 0                          # reading: false stops the polling
    assert got["inputValue"] == "check EN pull-up"
    assert got["putsAfterEscape"] == 0
    assert got["puts"] == [{"written": "check EN pull-down"}]
    assert "“check EN pull-down”" in got["saved"]
    assert got["title"] == "Corrected by you"
