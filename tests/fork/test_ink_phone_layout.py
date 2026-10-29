"""Write (static/css/fork-ink.css) is full screen on a phone, not upstream's drawer.

Upstream's phone rule in static/css/17-research.css sets every tool window to
`height: auto` at <= 768 px. Write's page sizes itself from its window, so under that rule
it was 0 px tall on a phone (a Galaxy Z Fold, folded and open) and the note never showed.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INK_CSS = (ROOT / "static/css/fork-ink.css").read_text()
UPSTREAM_CSS = (ROOT / "static/css/17-research.css").read_text()


def _specificity(selector: str) -> tuple[int, int]:
    """(ids, classes) of a simple selector; :not() counts its argument."""
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+", selector))
    return ids, classes


def _full_screen_rule() -> tuple[str, str, str]:
    m = re.search(r"@media (\(pointer: coarse\)[^{]*)\{\s*(#fork-ink-modal[^{]+)\{([^}]+)\}", INK_CSS)
    assert m, "Write's full-screen rule is missing"
    return m.group(1).strip(), m.group(2).strip(), m.group(3)


def test_full_screen_rule_covers_phone_width():
    media, _, body = _full_screen_rule()
    assert "max-width: 768px" in media and "pointer: coarse" in media
    assert "height: 100dvh !important" in body
    assert "max-height: none !important" in body


def test_full_screen_rule_outranks_upstream_drawer():
    drawer = ".modal:not(#theme-modal):not(#styled-confirm-overlay) > .modal-content"
    assert drawer in UPSTREAM_CSS, "upstream drawer rule changed: recheck Write on a phone"
    _, selector, _ = _full_screen_rule()
    assert _specificity(selector) > _specificity(drawer)
