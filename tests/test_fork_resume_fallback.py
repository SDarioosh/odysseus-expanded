"""Fork: a run that ended while away shows its saved answer (static/js/forkResumeFallback.js).

Runs the module through `node --input-type=module` with a stub DOM, fetch and
session module; skips without node.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_MODULE = (_REPO / "static" / "js" / "forkResumeFallback.js").as_posix()
_HAS_NODE = shutil.which("node") is not None


def _run(*, status, body=None, current="chat-1", last_after_reload="msg-ai", offline=False):
    js = f"""
    const calls = {{ fetched: [], selected: [], notes: [] }};
    let current = {json.dumps(current)};
    const msg = (cls) => ({{ classList: {{ contains: (c) => c === cls }} }});
    let messages = [msg('msg-user')];
    globalThis.document = {{
      getElementById: () => ({{
        querySelectorAll: () => messages,
        appendChild: (node) => calls.notes.push(node.children[0].textContent),
      }}),
      createElement: () => ({{ style: {{}}, children: [], appendChild(c) {{ this.children.push(c); }} }}),
    }};
    globalThis.fetch = async (url) => {{
      calls.fetched.push(url);
      if ({json.dumps(offline)}) throw new TypeError('offline');
      return {{ status: {status}, ok: {status} >= 200 && {status} < 300,
                json: async () => ({json.dumps(body or {})}) }};
    }};
    const sessionModule = {{
      getCurrentSessionId: () => current,
      selectSession: async (id, opts) => {{
        calls.selected.push([id, opts]);
        messages = [msg('msg-user'), msg({json.dumps(last_after_reload)})];
      }},
    }};
    const {{ reloadEndedRun }} = await import('{_MODULE}');
    const handled = await reloadEndedRun({{ apiBase: '', sessionModule, sessionId: 'chat-1' }});
    console.log(JSON.stringify({{ handled, ...calls }}));
    """
    proc = subprocess.run(["node", "--input-type=module"], input=js, capture_output=True,
                          text=True, cwd=str(_REPO), timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


pytestmark = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


def test_ended_run_reloads_the_chat():
    got = _run(status=404)
    assert got["handled"] is True
    assert got["fetched"] == ["/api/chat/stream_status/chat-1"]
    assert got["selected"] == [["chat-1", {"showLoading": False}]]
    assert got["notes"] == []


def test_ended_run_without_an_answer_says_so():
    got = _run(status=404, last_after_reload="msg-user")
    assert got["handled"] is True
    assert got["notes"] and "without an answer" in got["notes"][0]


def test_still_streaming_or_offline_keeps_upstreams_message():
    assert _run(status=200, body={"status": "streaming"})["handled"] is False
    assert _run(status=500)["handled"] is False
    got = _run(status=404, offline=True)
    assert got["handled"] is False and got["selected"] == []


def test_other_chat_open_is_left_alone():
    got = _run(status=404, current="chat-2")
    assert got["handled"] is True and got["selected"] == []


def test_chat_js_hook_is_in_place():
    source = (_REPO / "static" / "js" / "chat.js").read_text()
    hook = "import('./forkResumeFallback.js')"
    assert hook in source
    assert source.index(hook) < source.index("Connection lost. The existing run could not be resumed.")
