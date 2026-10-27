"""static/js/meetingBot.js against a stubbed DOM and fetch in node (fork: QA BUG-26).

Each test loads the real module with a fetch whose answers the test releases one by
one, so a slow GET can be made to land after a Join or Leave the owner clicked meanwhile.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")

HARNESS = r"""
const els = {};
function el(id) {
  const handlers = {};
  const node = {
    id, value: '', disabled: false, hidden: false, children: [],
    addEventListener(type, fn) { handlers[type] = fn; },
    fire(type, ev = {}) { return handlers[type] && handlers[type](ev); },
    focus() {},
    appendChild(child) { node.children.push(child); },
    get textContent() { return node._text !== undefined ? node._text
      : node.children.map((c) => c.textContent).join('\n'); },
    set textContent(v) { node._text = v === '' ? undefined : v; node.children = []; },
  };
  return node;
}
for (const id of ['mb-link', 'mb-pass', 'mb-join', 'mb-leave', 'mb-state']) els[id] = el(id);
const docHandlers = {};
globalThis.document = {
  hidden: false,
  querySelector: (sel) => els[sel.slice(1)],
  createElement: () => ({ className: '', textContent: '' }),
  addEventListener: (type, fn) => { docHandlers[type] = fn; },
};
globalThis.window = { addEventListener() {} };
const pending = [];
globalThis.fetch = (path, init = {}) => new Promise((resolve) => {
  pending.push({ method: init.method || 'GET', path, resolve });
});
function answer(i, status, body) {
  const [req] = pending.splice(i, 1);
  req.resolve({ ok: status >= 200 && status < 300, status, json: async () => body });
}
const tick = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
const state = () => ({
  leaveHidden: els['mb-leave'].hidden,
  joinDisabled: els['mb-join'].disabled,
  text: els['mb-state'].textContent,
  pending: pending.map((p) => `${p.method} ${p.path}`),
});
await import('./static/js/meetingBot.js');
await tick();
"""

API = "/api/modules/sound/meetings"


def _run(scenario: str) -> dict:
    src = HARNESS + scenario
    r = subprocess.run(["node", "--input-type=module", "-e", src], cwd=ROOT,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_slow_current_does_not_undo_a_busy_answer():
    """loadCurrent()'s GET started before Join must not clear the busy state Join set."""
    out = _run(r"""
    // pending[0] is the start-up GET /current, still in flight.
    els['mb-link'].value = 'https://zoom.us/j/85012345678';
    els['mb-join'].fire('click');
    await tick();
    answer(1, 409, { code: 'busy', active: 'zoom-other', detail: 'Athena is already in a meeting.' });
    await tick();
    answer(0, 200, { meeting: null });  // the stale GET lands last
    await tick();
    console.log(JSON.stringify(state()));
    """)
    assert out["leaveHidden"] is False
    assert "busy in another meeting" in out["text"]


def test_slow_current_does_not_replace_the_joined_meeting():
    out = _run(r"""
    els['mb-link'].value = 'https://zoom.us/j/85012345678';
    els['mb-join'].fire('click');
    await tick();
    answer(1, 200, { meeting: { id: 'new', state: 'starting', active: true,
                                text: 'Athena is joining.' } });
    await tick();
    answer(0, 200, { meeting: { id: 'old', state: 'ended', active: false,
                                text: 'The meeting ended.' } });
    await tick();
    console.log(JSON.stringify(state()));
    """)
    assert "Athena is joining." in out["text"]
    assert "ended" not in out["text"]
    assert out["leaveHidden"] is False and out["joinDisabled"] is True


def test_leave_of_a_gone_busy_meeting_clears_it_and_reloads():
    """A 404 on Leave of the other meeting: forget busyId and read the current state."""
    out = _run(rf"""
    answer(0, 200, {{ meeting: null }});
    await tick();
    els['mb-link'].value = 'https://zoom.us/j/85012345678';
    els['mb-join'].fire('click');
    await tick();
    answer(0, 409, {{ code: 'busy', active: 'zoom-stale', detail: 'busy' }});
    await tick();
    const before = state();
    els['mb-leave'].fire('click');
    await tick();
    const del = pending.findIndex((p) => p.method === 'DELETE');
    answer(del, 404, {{ detail: 'Athena has no meeting zoom-stale.' }});
    await tick();
    const reload = state().pending.includes('GET {API}/current');
    const cur = pending.findIndex((p) => p.path === '{API}/current');
    if (cur >= 0) answer(cur, 200, {{ meeting: null }});
    await tick();
    console.log(JSON.stringify({{ before, reload, after: state() }}));
    """)
    assert out["before"]["leaveHidden"] is False
    assert out["reload"] is True
    assert out["after"]["leaveHidden"] is True
    assert "busy in another meeting" not in out["after"]["text"]
    assert out["after"]["pending"] == []
