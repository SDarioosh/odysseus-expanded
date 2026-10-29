"""Fork: handwritten notes are shown in Write only.

The Notes window leaves out notes labelled ``handwritten`` (hook in
``features/notes/application/workflows.js``, source and published copy), and a link to
one opens it in Write (``static/js/forkInkLinks.js``). Runs through
``node --input-type=module``; skips without node.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_JS = _REPO / "static" / "js"
_HAS_NODE = shutil.which("node") is not None
pytestmark = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


def _node(js: str):
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js, capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def test_notes_window_leaves_out_handwritten_notes():
    got = _node(f"""
    const {{ createNotesWorkflows }} = await import('{(_JS / "features/notes/application/workflows.js").as_posix()}');
    let shown = null;
    const app = {{ state: {{ notes: [], showingArchived: false }}, setLoading() {{}},
                   replaceNotes(n) {{ shown = n; }} }};
    const api = {{ patchNote() {{}}, listNotes: async () => ({{ ok: true, json: async () => ({{ notes: [
      {{ id: 'a', label: null }}, {{ id: 'b', label: 'handwritten' }}, {{ id: 'c', label: 'work' }},
    ] }}) }}) }};
    const wf = createNotesWorkflows({{ app, api, effects: {{ render() {{}} }} }});
    await wf._fetchNotes();
    console.log(JSON.stringify(shown.map(n => n.id)));
    """)
    assert got == ["a", "c"]


def test_both_copies_of_the_hook_match():
    src = (_REPO / "frontend/src/features/notes/application/workflows.js").read_text()
    out = (_JS / "features/notes/application/workflows.js").read_text()
    assert src == out
    assert "// Fork: handwritten notes are shown in Write only (static/js/forkInk.js)." in out


def test_note_links_are_recognised():
    got = _node(f"""
    globalThis.window = {{ location: {{ href: 'http://hub.example:7000/', origin: 'http://hub.example:7000', pathname: '/' }} }};
    const {{ noteLinkId }} = await import('{(_JS / "forkInkLinks.js").as_posix()}');
    console.log(JSON.stringify([
      '#open=notes&note=abc-123', '#note-abc', 'http://hub.example:7000/#open=notes&note=n1',
      'https://other.example/#open=notes&note=n1', 'http://hub.example:7000/today#note-n1',
      '#open=notes&note=a b', '#event-1', '', null,
    ].map(h => noteLinkId(h))));
    """)
    assert got == ["abc-123", "abc", "n1", None, None, None, None, None, None]


def _click(href: str, label: str | None, offered: bool = True, fail: bool = False):
    return _node(f"""
    const listeners = [];
    const appended = [];
    globalThis.history = {{ replaceState() {{ log.push('replaceState'); }} }};
    const log = [];
    globalThis.window = {{
      location: {{ href: 'http://hub.example:7000/#open=notes&note=x', origin: 'http://hub.example:7000',
                  pathname: '/', search: '', hash: '#open=notes&note=x' }},
      addEventListener(type, fn, capture) {{ listeners.push({{ type, fn, capture }}); }},
    }};
    const anchor = (href, dataset = {{}}) => ({{ dataset, getAttribute: () => href, closest() {{ return this; }} }});
    globalThis.document = {{
      createElement: () => {{
        const a = {{ dataset: {{}}, hidden: false, href: '', click() {{
          log.push('upstream:' + a.href + ':' + a.dataset.forkNoteChecked);
          dispatch(anchor(a.href, a.dataset));
        }}, remove() {{}} }};
        return a;
      }},
      body: {{ appendChild(a) {{ appended.push(a); }} }},
    }};
    function dispatch(target) {{
      const e = {{ button: 0, target, defaultPrevented: false,
                   preventDefault() {{ this.defaultPrevented = true; log.push('prevented'); }},
                   stopPropagation() {{}} }};
      for (const l of listeners) l.fn(e);
      return e;
    }}
    const {{ wireHandwrittenNoteLinks }} = await import('{(_JS / "forkInkLinks.js").as_posix()}');
    wireHandwrittenNoteLinks({{
      offered: () => {json.dumps(offered)},
      openInWrite: async (id) => {{ log.push('write:' + id); }},
      fetchNote: async (id) => {{ if ({json.dumps(fail)}) throw new Error('offline');
                                   return {{ id, label: {json.dumps(label)} }}; }},
    }});
    console.assert(listeners.length === 1 && listeners[0].type === 'click' && listeners[0].capture);
    dispatch(anchor({json.dumps(href)}));
    await new Promise(r => setTimeout(r, 10));
    console.log(JSON.stringify(log));
    """)


def test_handwritten_note_link_opens_write():
    assert _click("#open=notes&note=n1", "handwritten") == ["prevented", "replaceState", "write:n1"]


def test_other_note_link_goes_to_upstream_once():
    # The re-sent copy is marked, so the listener lets it through to upstream's handler.
    assert _click("#open=notes&note=n1", "work") == ["prevented", "upstream:#open=notes&note=n1:1"]
    assert _click("http://hub.example:7000/#note-n1", None) == ["prevented", "upstream:#note-n1:1"]


def test_failed_lookup_goes_to_upstream():
    assert _click("#open=notes&note=n1", None, fail=True) == ["prevented", "upstream:#open=notes&note=n1:1"]


def test_left_alone_when_write_is_not_offered_or_not_a_note_link():
    assert _click("#open=notes&note=n1", "handwritten", offered=False) == []
    assert _click("#event-1", "handwritten") == []
    assert _click("https://other.example/#open=notes&note=n1", "handwritten") == []


def test_hook_is_in_place():
    windows = (_JS / "forkModuleWindows.js").read_text()
    assert "import { wireHandwrittenNoteLinks } from './forkInkLinks.js';" in windows
    assert "wireHandwrittenNoteLinks({" in windows
