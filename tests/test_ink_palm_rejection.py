"""Fork: Write view input (static/js/forkInk.js): touch never draws while a pen is active.

Two levels:
* the input router (pure logic) through `node`, with a fake clock;
* the whole view in a real Chromium through Playwright (tests/helpers/ink_pointer_e2e.mjs),
  driven with synthetic pointer events, including the save to IndexedDB and the hub.
Each skips when node, or Playwright and its browser, aren't available.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_INK = (_REPO / "static" / "js" / "forkInk.js").as_posix()
_HAS_NODE = shutil.which("node") is not None


ROUTER = f"""
const {{ createInputRouter, PALM_MS }} = await import('{_INK}');
let t = 1000;
const log = [];
const r = createInputRouter({{
  drawStart: (e, o) => log.push(['start', e.pointerType, o.eraser]),
  drawMove: (e) => log.push(['move', e.pointerType]),
  drawEnd: (e) => log.push(['end', e.pointerType]),
  pan: (dx, dy) => log.push(['pan', dx, dy]),
  zoom: (f) => log.push(['zoom', Math.round(f * 100) / 100]),
  gestureEnd: () => log.push(['gestureEnd']),
}}, () => t);
const ev = (type, pointerType, pointerId, x = 0, y = 0, extra = {{}}) =>
  r.handle({{ type, pointerType, pointerId, clientX: x, clientY: y, buttons: 0, button: 0, ...extra }});
const out = {{}};
const run = (name, fn) => {{ log.length = 0; const res = fn(); out[name] = {{ res, log: [...log] }}; }};
"""


def _router(body: str) -> dict:
    js = ROUTER + body + "\nconsole.log(JSON.stringify(out));"
    proc = subprocess.run(["node", "--input-type=module"], input=js, capture_output=True,
                          text=True, cwd=str(_REPO), timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_touch_is_ignored_while_a_pen_hovers_and_scrolls_after():
    got = _router("""
    run('hover', () => ev('pointermove', 'pen', 2, 5, 5));
    run('palm', () => [ev('pointerdown', 'touch', 10, 0, 100, {buttons: 1}),
                       ev('pointermove', 'touch', 10, 0, 50, {buttons: 1}),
                       ev('pointerup', 'touch', 10, 0, 50)]);
    t += PALM_MS + 1;
    run('finger', () => [ev('pointerdown', 'touch', 11, 0, 100, {buttons: 1}),
                         ev('pointermove', 'touch', 11, 0, 60, {buttons: 1}),
                         ev('pointerup', 'touch', 11, 0, 60)]);
    """)
    assert got["hover"]["res"] == "hover"
    assert got["palm"]["res"] == ["ignored", "ignored", "ignored"]
    assert got["palm"]["log"] == []
    assert got["finger"]["res"] == ["gesture", "gesture", "gesture"]
    assert got["finger"]["log"] == [["pan", 0, -40], ["gestureEnd"]]


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_pen_draws_and_touch_never_does():
    got = _router("""
    run('pen', () => [ev('pointerdown', 'pen', 2, 1, 1, {buttons: 1}),
                      ev('pointerdown', 'touch', 10, 5, 5, {buttons: 1}),
                      ev('pointermove', 'touch', 10, 9, 9, {buttons: 1}),
                      ev('pointermove', 'pen', 2, 2, 2, {buttons: 1}),
                      ev('pointerup', 'pen', 2, 2, 2)]);
    t += 10 * PALM_MS;  // the palm that landed during the stroke stays ignored
    run('lateMove', () => ev('pointermove', 'touch', 10, 40, 40, {buttons: 1}));
    """)
    assert got["pen"]["res"] == ["draw", "ignored", "ignored", "draw", "draw"]
    assert got["pen"]["log"] == [["start", "pen", False], ["move", "pen"], ["end", "pen"]]
    assert got["lateMove"] == {"res": "ignored", "log": []}


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_pen_landing_during_a_touch_gesture_wins():
    got = _router("""
    t += 10 * PALM_MS;
    run('both', () => [ev('pointerdown', 'touch', 10, 0, 0, {buttons: 1}),
                       ev('pointerdown', 'pen', 2, 1, 1, {buttons: 1}),
                       ev('pointermove', 'touch', 10, 0, 50, {buttons: 1}),
                       ev('pointerup', 'pen', 2, 1, 1)]);
    """)
    assert got["both"]["res"] == ["gesture", "draw", "ignored", "draw"]
    assert got["both"]["log"] == [["gestureEnd"], ["start", "pen", False], ["end", "pen"]]


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_two_fingers_zoom_and_mouse_and_barrel_button():
    got = _router("""
    t += 10 * PALM_MS;
    run('pinch', () => [ev('pointerdown', 'touch', 10, 0, 0, {buttons: 1}),
                        ev('pointerdown', 'touch', 11, 100, 0, {buttons: 1}),
                        ev('pointermove', 'touch', 11, 200, 0, {buttons: 1}),
                        ev('pointerup', 'touch', 10, 0, 0), ev('pointerup', 'touch', 11, 200, 0)]);
    run('mouseRight', () => ev('pointerdown', 'mouse', 1, 0, 0, {button: 2, buttons: 2}));
    run('mouse', () => [ev('pointerdown', 'mouse', 1, 0, 0, {buttons: 1}), ev('pointerup', 'mouse', 1)]);
    run('barrel', () => [ev('pointerdown', 'pen', 2, 0, 0, {buttons: 33}), ev('pointerup', 'pen', 2)]);
    run('eraserEnd', () => [ev('pointerdown', 'pen', 3, 0, 0, {button: 5, buttons: 32}), ev('pointerup', 'pen', 3)]);
    """)
    assert ["zoom", 2] in got["pinch"]["log"]
    assert all(entry[0] != "start" for entry in got["pinch"]["log"])
    assert got["mouseRight"] == {"res": "ignored", "log": []}
    assert got["mouse"]["log"][0] == ["start", "mouse", False]
    assert got["barrel"]["log"][0] == ["start", "pen", True]
    assert got["eraserEnd"]["log"][0] == ["start", "pen", True]


def _playwright_ready() -> bool:
    if not _HAS_NODE:
        return False
    proc = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True,
                          cwd=str(_REPO), timeout=30)
    return proc.returncode == 0


@pytest.mark.skipif(not _playwright_ready(), reason="node or playwright not available")
def test_write_view_in_a_browser_with_synthetic_pointer_events():
    proc = subprocess.run(["node", str(_REPO / "tests" / "helpers" / "ink_pointer_e2e.mjs")],
                          capture_output=True, text=True, cwd=str(_REPO), timeout=180)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip().splitlines()[-1]
    if out.startswith("SKIP"):
        pytest.skip(out)
    got = json.loads(out)
    start = got["start"]
    assert got["palmWhileHovering"] == start                       # palm: no ink, no scroll
    assert got["afterPen"]["strokes"] == 1
    assert got["palmAfterPen"] == got["afterPen"]                  # within the palm window
    assert got["penWithPalmDown"]["strokes"] == 2                  # pen wins over a palm
    assert got["penWithPalmDown"]["sy"] == start["sy"]
    assert got["fingerScroll"]["strokes"] == 2                     # a finger scrolls...
    assert got["fingerScroll"]["sy"] > start["sy"]                 # ...and draws nothing
    assert got["pinch"]["strokes"] == 2 and got["pinch"]["zoom"] > 1
    assert got["mouse"]["strokes"] == 3
    assert got["barrelErase"]["strokes"] == 2                      # whole stroke erased
    assert got["undo"]["strokes"] == 3 and got["redo"]["strokes"] == 2
    saved = got["saved"]
    assert saved and sum(len(p["strokes"]) for p in saved["pages"]) == 3
    assert all(not s["pen"]["highlighter"] for p in saved["pages"] for s in p["strokes"])
    assert all(len(pt) == 3 for p in saved["pages"] for s in p["strokes"] for pt in s["points"])
    assert got["local"] == [{"synced": True, "strokes": 3}]
    assert got["touchAction"] == "none"
    assert got["desynchronized"] in (True, None)
