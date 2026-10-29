"""Fork: Conversations / Today as Odysseus windows (static/js/forkModuleWindows.js).

Pins the module-transcript link parser that decides which chat links open the
Conversations reader (static/js/soundModule.js `parseModuleLink`), the shortcut data
spread into upstream's tables (static/js/forkModuleShortcuts.js), and that each hook
is in place. The parser runs through `node --input-type=module`; skips without node.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
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
    assert got["FORK_SHORTCUT_ACTIONS"] == [
        "open_conversations", "open_meetings", "open_today", "open_write", "open_beliefs"]
    # unbound by default, like upstream's other open-tool shortcuts
    assert got["FORK_SHORTCUT_DEFAULTS"] == {
        "open_conversations": "", "open_meetings": "", "open_today": "", "open_write": "",
        "open_beliefs": ""}
    assert got["FORK_SHORTCUT_BUTTONS"] == {
        "open_conversations": "tool-sound-module-btn", "open_meetings": "tool-meetings-btn",
        "open_today": "tool-today-insight-btn", "open_write": "tool-ink-btn",
        "open_beliefs": "tool-beliefs-btn"}
    assert got["FORK_WINDOW_TRIGGERS"] == {
        "fork-sound-modal": "tool-sound-module-btn", "fork-meetings-modal": "tool-meetings-btn",
        "fork-today-modal": "tool-today-insight-btn", "fork-ink-modal": "tool-ink-btn",
        "fork-beliefs-modal": "tool-beliefs-btn"}
    assert got["FORK_OPEN_TARGETS"] == {
        "records": ["tool-sound-module-btn", "rail-sound-module"],
        "conversations": ["tool-sound-module-btn", "rail-sound-module"],
        "meetings": ["tool-meetings-btn", "rail-meetings"],
        "calls": ["tool-meetings-btn", "rail-meetings"],
        "today": ["tool-today-insight-btn", "rail-today-insight"],
        "write": ["tool-ink-btn", "rail-ink"],
        "ink": ["tool-ink-btn", "rail-ink"],
        "handwriting": ["tool-ink-btn", "rail-ink"],
        "wisdom": ["tool-beliefs-btn", "rail-beliefs"],
        "beliefs": ["tool-beliefs-btn", "rail-beliefs"],
    }
    windows = (_JS / "forkModuleWindows.js").read_text()
    index = (_REPO / "static" / "index.html").read_text()
    for window_id, button in got["FORK_WINDOW_TRIGGERS"].items():
        assert f"id: '{window_id}'" in windows
        assert f"sidebarBtn: '{button}'" in windows
        # Every sidebar entry is added by forkModuleWindows.js; none is in index.html.
        assert f'id="{button}"' not in index
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


def test_meetings_page_and_window_mount_one_view():
    # forkMeetings.js holds the Meetings cards and mounts them on the page (after its top
    # row) and in the window; the scripts find their elements by id there, so every id
    # they look up must be in its markup.
    page = (_REPO / "static" / "call-recorder.html").read_text()
    assert "<h1>Meetings</h1>" in page and '<div class="bs-wrap">' in page
    assert "mountMeetings(document.querySelector('.bs-wrap'));" in page
    meetings = (_JS / "forkMeetings.js").read_text()
    assert "const SCRIPTS = ['./callRecorder.js', './meetingBot.js'];" in meetings
    assert 'href="/modules/sound" data-conversations' in meetings
    assert "data-in-app hidden" in meetings
    for script in ("callRecorder.js", "meetingBot.js"):
        src = (_JS / script).read_text()
        for el_id in re.findall(r"\$\('#([a-z0-9-]+)'\)", src):
            assert f'id="{el_id}"' in meetings, (script, el_id)
    windows = (_JS / "forkModuleWindows.js").read_text()
    assert "href: '/modules/sound/record'" in windows
    assert "mountMeetings(body, { inApp: true, openConversations: () => open(WINDOWS.sound) });" in windows
    assert "addSidebarEntry(WINDOWS.meetings, WINDOWS.sound.sidebarBtn);" in windows
    # Recording moved out of the Conversations window's toolbar.
    sound = (_JS / "soundModule.js").read_text()
    assert "'Record a call'" not in sound
    assert "ctx.embedded ? null : el('a', { className: 'bs-btn', href: '/modules/sound/record' }" in sound


def test_athena_group_holds_the_module_entries():
    # The sidebar's collapsible "Athena" group (static/js/forkAthenaMenu.js) is built
    # above Tools from the existing entries; it must not be a `.section`, or upstream's
    # section toggle (static/js/section-management.js) binds a second handler to it.
    menu = (_JS / "forkAthenaMenu.js").read_text()
    assert "export function buildAthenaMenu(itemIds, beforeId = 'tools-section')" in menu
    assert "group.className = 'fork-athena-section';" in menu
    assert "'section'" not in menu
    # same open/close cascade as Tools, from upstream's keyframes
    assert "a.animationName === 'section-domino-out'" in menu
    css = (_REPO / "static" / "css" / "fork-modules.css").read_text()
    assert "animation: section-domino-in" in css and "animation: section-domino-out" in css
    windows = (_JS / "forkModuleWindows.js").read_text()
    assert "import { buildAthenaMenu } from './forkAthenaMenu.js';" in windows
    assert ("buildAthenaMenu([WINDOWS.sound.sidebarBtn, WINDOWS.meetings.sidebarBtn, "
            "WINDOWS.today.sidebarBtn,\n                   WINDOWS.ink.sidebarBtn, "
            "WINDOWS.beliefs.sidebarBtn]);") in windows
    # built after the entries it moves are added
    assert windows.index("addSidebarEntry(WINDOWS.ink") < windows.index("buildAthenaMenu([")
    index = (_REPO / "static" / "index.html").read_text()
    assert 'id="tools-section"' in index


def test_the_sidebar_entries_come_from_the_windows_script():
    # index.html only loads forkModuleWindows.js; the script adds the five entries after
    # upstream's Notes entry, in this order, before it moves them into the Athena group.
    windows = (_JS / "forkModuleWindows.js").read_text()
    index = (_REPO / "static" / "index.html").read_text()
    assert "const NOTES_ENTRY = 'tool-notes-btn';" in windows
    assert 'id="tool-notes-btn"' in index
    adds = ["addSidebarEntry(WINDOWS.sound, NOTES_ENTRY);",
            "addSidebarEntry(WINDOWS.meetings, WINDOWS.sound.sidebarBtn);",
            "addSidebarEntry(WINDOWS.today, WINDOWS.meetings.sidebarBtn);",
            "addSidebarEntry(WINDOWS.ink, WINDOWS.today.sidebarBtn);",
            "addSidebarEntry(WINDOWS.beliefs, WINDOWS.ink.sidebarBtn);"]
    at = [windows.index(line) for line in adds]
    assert at == sorted(at) and at[-1] < windows.index("buildAthenaMenu([")
    # the entries with a standalone page are links to it (a modified click opens it)
    for href in ("/modules/sound", "/modules/sound/record", "/today", "/beliefs"):
        assert f"href: '{href}'," in windows
    assert "Fork: Athena conversations page" not in index


def test_beliefs_page_and_window_mount_one_view():
    page = (_REPO / "static" / "beliefs.html").read_text()
    assert "mountBeliefs(document.body);" in page
    src = (_JS / "beliefs.js").read_text()
    assert "export function mountBeliefs(root, { inApp = false } = {})" in src
    markup = src[src.index("const MARKUP = `"):]
    assert '<div class="bs-wrap">' in markup and '<div class="bs-top">' in markup
    assert '<a href="/" title="Back to Odysseus">' in markup  # dropped in the window
    for el_id in set(re.findall(r"\$\('#([a-z0-9-]+)'\)", src)):
        assert f'id="{el_id}"' in markup, el_id
    windows = (_JS / "forkModuleWindows.js").read_text()
    assert "id: 'fork-beliefs-modal'" in windows and "sidebarBtn: 'tool-beliefs-btn'" in windows
    assert "const { mountBeliefs } = await import('./beliefs.js');" in windows
    assert "return mountBeliefs(body, { inApp: true });" in windows


_PAGES = ("beliefs.html", "call-recorder.html", "phone-pairing.html", "sound-module.html", "today.html")


def test_standalone_pages_are_thin_shells():
    # One theme snippet for every page, its look from fork-modules.css: no inline styles,
    # and no script but the snippet and the one that mounts the view.
    snippet = (_JS / "forkPageTheme.js").read_text()
    assert "localStorage.getItem('odysseus-theme')" in snippet
    for name in _PAGES:
        html = (_REPO / "static" / name).read_text()
        assert '<html lang="en" class="fm-page">' in html, name
        assert '<script src="/static/js/forkPageTheme.js"></script>' in html, name
        assert html.index("forkPageTheme.js") < html.index("00-tokens.css") < html.index("fork-modules.css")
        assert html.index('name="theme-color"') < html.index("forkPageTheme.js"), name
        assert "<style" not in html and "odysseus-theme" not in html, name
        assert html.count("<script") == 2, name


def test_fork_styles_stay_under_fork_classes():
    # fork-modules.css is loaded into the app: every rule must start at a fork class or id
    # (or the fork pages' html.fm-page), so it can't restyle upstream's views.
    css = (_REPO / "static" / "css" / "fork-modules.css").read_text()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"@media[^{]*\{", "", css)
    selectors = [sel.strip() for block in re.findall(r"([^{}]+)\{[^{}]*\}", css)
                 for sel in block.split(",") if sel.strip()]
    assert len(selectors) > 200
    fork = re.compile(r"^(html\.fm-page\b|\.fm-[\w-]+|#fork-[\w-]+|\.fork-athena-[\w-]+)")
    assert [s for s in selectors if not fork.match(s)] == []
