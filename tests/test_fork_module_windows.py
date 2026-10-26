"""Fork: Conversations / Today as Odysseus windows (static/js/forkModuleWindows.js).

Pins the module-transcript link parser that decides which chat links open the
Conversations reader (static/js/soundModule.js `parseModuleLink`), the shortcut data
spread into upstream's tables (static/js/forkModuleShortcuts.js), and that each hook
is in place. The parser runs through `node --input-type=module`; skips without node.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_JS = _REPO / "static" / "js"
_HAS_NODE = shutil.which("node") is not None


def _node(js: str):
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js, capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def _parse(hrefs: list[str]):
    return _node(f"""
    globalThis.window = {{ location: {{ href: 'http://hub.example:7000/' }} }};
    const {{ parseModuleLink }} = await import('{(_JS / "soundModule.js").as_posix()}');
    console.log(JSON.stringify({json.dumps(hrefs)}.map(parseModuleLink)));
    """)


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_module_transcript_links_are_recognised():
    got = _parse([
        "http://127.0.0.1:8000/view/892317da90354a9ca980f50b9e745a33?sig=d73dd40e#t=24",
        "https://sound.example/view/abc-1.2:3?sig=ff",
        "https://sound.example/view/abc?sig=ff#t=12.5",
    ])
    assert got == [
        {"id": "892317da90354a9ca980f50b9e745a33", "t": 24},
        {"id": "abc-1.2:3", "t": None},
        {"id": "abc", "t": 12.5},
    ]


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_other_links_are_left_alone():
    got = _parse([
        "https://sound.example/view/abc",            # unsigned
        "https://sound.example/view/abc?sig=",       # empty signature
        "https://sound.example/other/abc?sig=ff",    # not a transcript page
        "https://sound.example/view/a%20b?sig=ff",   # id outside the allowed characters
        "javascript:alert(1)//view/abc?sig=ff",
        "/#event-123",
        "",
    ])
    assert got == [None] * 7


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_shortcut_data_matches_the_windows():
    got = _node(f"""
    const s = await import('{(_JS / "forkModuleShortcuts.js").as_posix()}');
    console.log(JSON.stringify(s));
    """)
    assert got["FORK_SHORTCUT_ACTIONS"] == ["open_conversations", "open_today", "open_write"]
    # unbound by default, like upstream's other open-tool shortcuts
    assert got["FORK_SHORTCUT_DEFAULTS"] == {
        "open_conversations": "", "open_today": "", "open_write": ""}
    assert got["FORK_SHORTCUT_BUTTONS"] == {
        "open_conversations": "tool-sound-module-btn", "open_today": "tool-today-insight-btn",
        "open_write": "tool-ink-btn"}
    assert got["FORK_WINDOW_TRIGGERS"] == {
        "fork-sound-modal": "tool-sound-module-btn", "fork-today-modal": "tool-today-insight-btn",
        "fork-ink-modal": "tool-ink-btn"}
    assert got["FORK_OPEN_TARGETS"] == {
        "conversations": ["tool-sound-module-btn", "rail-sound-module"],
        "today": ["tool-today-insight-btn", "rail-today-insight"],
        "write": ["tool-ink-btn", "rail-ink"],
        "ink": ["tool-ink-btn", "rail-ink"],
        "handwriting": ["tool-ink-btn", "rail-ink"],
    }
    windows = (_JS / "forkModuleWindows.js").read_text()
    index = (_REPO / "static" / "index.html").read_text()
    for window_id, button in got["FORK_WINDOW_TRIGGERS"].items():
        assert f"id: '{window_id}'" in windows
        assert f"sidebarBtn: '{button}'" in windows
        # Write's sidebar entry is added by forkModuleWindows.js, not by index.html.
        if button == "tool-ink-btn":
            assert f'id="{button}"' not in index
        else:
            assert f'id="{button}"' in index
    for _name, (button, rail) in got["FORK_OPEN_TARGETS"].items():
        assert f"railBtn: '{rail}'" in windows


def test_hooks_are_in_place():
    index = (_REPO / "static" / "index.html").read_text()
    assert '<script type="module" src="/static/js/forkModuleWindows.js"></script>' in index
    keys = (_JS / "keyboard-shortcuts.js").read_text()
    for name in ("FORK_SHORTCUT_DEFAULTS", "FORK_SHORTCUT_BUTTONS", "FORK_WINDOW_TRIGGERS"):
        assert f"...{name}," in keys
    settings = (_JS / "settings.js").read_text()
    for name in ("DEFAULTS", "ICONS", "LABELS", "ACTIONS"):
        assert f"..._forkShortcuts.FORK_SHORTCUT_{name}" in settings
    slash = (_JS / "slashCommands.js").read_text()
    assert "import { FORK_OPEN_TARGETS } from './forkModuleShortcuts.js';" in slash
    assert "...FORK_OPEN_TARGETS, // Fork:" in slash


def test_standalone_pages_mount_the_shared_views():
    for page, fn in (("sound-module.html", "mountSoundModule"), ("today.html", "mountToday")):
        html = (_REPO / "static" / page).read_text()
        assert '<link rel="stylesheet" href="/static/css/fork-modules.css">' in html
        assert f"{fn}(document.getElementById('fm-view'))" in html
        assert 'nonce="{{CSP_NONCE}}"' in html  # inline module script needs the CSP nonce
