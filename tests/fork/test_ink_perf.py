"""Fork: the Write view keeps up with a fast pen and eraser on a dense page.

tests/helpers/ink_perf.mjs fills a page with about 1,500 fictional strokes, throttles
Chrome's CPU 4x (a stand-in for a tablet) and moves the pen, then the eraser, fast
(4 samples a frame). It reports the main-thread time per frame: event handlers plus
the drawing done in animation frames.

Before the fix the eraser took about 220 ms a frame on this page (every point of every
stroke tested per sample, the whole page redrawn per hit) and now takes under 10 ms.
The limits here are loose on purpose, so a slow CI machine doesn't fail them, but a
return to the old behaviour does. Skips without node, Playwright or a browser.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _ready() -> bool:
    if shutil.which("node") is None:
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True,
                          cwd=str(_REPO), timeout=30).returncode == 0


@pytest.mark.skipif(not _ready(), reason="node or playwright not available")
def test_pen_and_eraser_keep_up_on_a_dense_page():
    proc = subprocess.run(["node", str(_REPO / "tests" / "helpers" / "ink_perf.mjs")],
                          capture_output=True, text=True, cwd=str(_REPO), timeout=300)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip().splitlines()[-1]
    if out.startswith("SKIP"):
        pytest.skip(out)
    got = json.loads(out)
    assert got["strokes"] > 1000
    assert got["pen"]["meanMs"] < 15, got
    assert got["eraser"]["meanMs"] < 45, got
    assert got["eraser"]["p95Ms"] < 120, got
