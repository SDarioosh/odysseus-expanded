"""Write on a tablet held upright (static/js/forkInk.js, static/css/fork-ink.css).

Found on a Lenovo tablet in portrait with a datasheet open: its part numbers make the bar
wrap, so the pen group is at the left of the second row, and

- the pen menu, hung from the pen group's right edge, opened off the left of the screen;
- the pen row in the menu was wider than the menu (the eraser pick touched its edge);
- a mark opened from the Marks panel flashed under the panel when it is on the right of
  the page;
- the full-screen window kept its padding and border: a dead frame around the page.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_HARNESS = (_REPO / "tests" / "helpers" / "ink_view_harness.mjs").as_posix()
_HAS_NODE = shutil.which("node") is not None
INK_CSS = (_REPO / "static/css/fork-ink.css").read_text()

NOTE_ID = "1e6b8d2f-3c4a-4b7e-8f90-2b3c4d5e6f70"
P1 = "pd333333333333"


def _playwright_ready() -> bool:
    if not _HAS_NODE:
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True,
                          cwd=str(_REPO), timeout=30).returncode == 0


def test_an_open_note_has_no_frame_around_it():
    m = re.search(r"#fork-ink-modal:has\(\.ink-editor:not\(\[hidden\]\)\)[^{]*> \.modal-content\.fm-modal-content\s*\{([^}]+)\}",
                  INK_CSS)
    assert m, "the rule that drops the window's padding around an open note is missing"
    body = m.group(1)
    assert "border: 0 !important" in body
    assert "padding: env(safe-area-inset-top, 0px)" in body and "!important" in body.split("padding:")[1]


E2E = """
import { launch, openPage } from '%(harness)s';

const { browser, skip } = await launch();
if (skip) { console.log('SKIP ' + skip); process.exit(0); }
const NOTE = '%(note)s', P1 = '%(p1)s';
const pdfNote = { note_id: NOTE, owner: 'alice', title: 'Fictional regulator', paper: 'blank', edited_at: 1000,
  parts: ['ZX-100/ZX-200/ZX-300', 'ZX-1XX'],
  pdf: { file: 'pdfs/x.pdf', hash: 'f'.repeat(64), name: 'regulator.pdf', pages: 1, at: '2026-11-01T10:00:00Z' },
  pages: [{ id: P1, width: 1000, height: 1294, strokes: [], pdf: { page: 1, width_pt: 612, height_pt: 792 } }] };
const mk = (id, x) => ({ id, page_id: P1, page_no: 1, kind: 'highlight', color: '#ffd400', box: [x, 300, 150, 30],
  text: 'Supply ' + id, near: '', strokes: [id], at: 5 });
const answer = {
  ['GET /api/ink/notes/' + NOTE + '/marks']: { status: 200, body: { note_id: NOTE, marks: [mk('left', 40), mk('right', 800)] } },
};
const page = await openPage(browser, { hubState: JSON.stringify({ notes: { [NOTE]: pdfNote }, answer }) });
await page.route('**/api/ink/notes/*/pdf/*.png*', (route) => route.fulfill({ status: 404, body: '' }));
const out = {};
const until = (fn, arg) => page.waitForFunction(fn, arg, { timeout: 8000 });
const box = (sel) => page.evaluate((s) => { const r = document.querySelector(s).getBoundingClientRect();
  return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; }, sel);

// A tablet held upright: the bar wraps, the pen group starts the second row.
await page.evaluate(() => { document.getElementById('host').style.width = '600px'; });
await page.evaluate((id) => view.openNote(id), NOTE);
await until(() => view._state.screen === 'editor' && !document.querySelector('.ink-parts').hidden);
out.host = await box('#host');
out.group = await box('.ink-pens');
out.tabs = await box('.ink-tabs');
await page.click('.ink-tool-btn');
await until(() => document.querySelector('.ink-menu.open'));
out.menu = await box('.ink-menu');
out.row = await page.evaluate(() => { const m = document.querySelector('.ink-menu');
  const picks = [...m.querySelectorAll('.ink-pen')].map((b) => b.getBoundingClientRect().right);
  const r = m.querySelector('.ink-pen-row').getBoundingClientRect();
  return { fits: m.scrollWidth <= m.clientWidth, last: Math.max(...picks), rowRight: r.right }; });

// Wide again (the tablet turned): the open menu moves with the pen group.
await page.evaluate(() => { document.getElementById('host').style.width = '1200px'; });
await until(() => { const m = document.querySelector('.ink-menu').getBoundingClientRect();
  const g = document.querySelector('.ink-pens').getBoundingClientRect(); return Math.abs(m.right - g.right) < 2; });
out.wide = { menu: await box('.ink-menu'), group: await box('.ink-pens') };
await page.keyboard.press('Escape');

// The Marks panel covers the right of the pages: a mark there closes it, one at the left doesn't.
await page.evaluate(() => { document.getElementById('host').style.width = '600px'; });
await page.waitForTimeout(100);
await page.click('.ink-marks-btn');
await until(() => document.querySelectorAll('.ink-marks .ink-mark').length === 2);
await page.click('.ink-marks .ink-mark[data-mark="left"]');
await until(() => !document.querySelector('.ink-flash').hidden);
out.left = { panelHidden: await page.evaluate(() => document.querySelector('.ink-marks').hidden),
  flash: await box('.ink-flash'), panel: await box('.ink-marks') };
await page.click('.ink-marks .ink-mark[data-mark="right"]');
await until(() => document.querySelector('.ink-marks').hidden);
out.right = { flashHidden: await page.evaluate(() => document.querySelector('.ink-flash').hidden),
  flash: await box('.ink-flash'), pressed: await page.evaluate(() => document.querySelector('.ink-marks-btn').getAttribute('aria-pressed')) };
out.errors = await page.evaluate(() => window.errors);
await browser.close();
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not _playwright_ready(), reason="node or playwright not available")
def test_pen_menu_and_marks_on_a_narrow_screen(tmp_path):
    script = tmp_path / "tablet_layout_e2e.mjs"
    script.write_text(E2E % {"harness": _HARNESS, "note": NOTE_ID, "p1": P1})
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, cwd=str(_REPO), timeout=300)
    assert proc.returncode == 0, proc.stderr
    last = proc.stdout.strip().splitlines()[-1]
    if last.startswith("SKIP"):
        pytest.skip(last)
    got = json.loads(last)
    assert got["errors"] == []
    host, group, menu = got["host"], got["group"], got["menu"]
    # The setup is the one that failed: the pen group wrapped to the left of a second row.
    assert group["top"] > got["tabs"]["top"] + 20 and group["right"] < host["left"] + 288
    # The menu is on screen, under the pen group.
    assert menu["left"] >= host["left"] and menu["right"] <= host["right"], menu
    assert menu["top"] >= group["bottom"]
    # Every pick is inside it.
    assert got["row"]["fits"] and got["row"]["last"] <= got["row"]["rowRight"] + 0.5, got["row"]
    # With room, it hangs from the pen group's right edge as before.
    assert abs(got["wide"]["menu"]["right"] - got["wide"]["group"]["right"]) < 2
    # A mark the panel doesn't cover: the panel stays; one it covers: the panel closes.
    assert got["left"]["panelHidden"] is False
    assert got["left"]["flash"]["right"] <= got["left"]["panel"]["left"]
    assert got["right"]["flashHidden"] is False and got["right"]["pressed"] == "false"
    assert got["right"]["flash"]["right"] <= host["right"]
