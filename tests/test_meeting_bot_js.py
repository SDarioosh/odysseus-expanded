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
  querySelector: (sel) => els[sel.slice(1)] || (els[sel.slice(1)] = el(sel.slice(1))),
  createElement: () => ({ className: '', textContent: '' }),
  addEventListener: (type, fn) => { docHandlers[type] = fn; },
};
const opened = [];
globalThis.window = {
  addEventListener() {},
  open(url, target) {
    if (globalThis.BLOCK_POPUPS) return null;
    const tab = { url, target, opener: 'page', closed: false, location: { href: url },
                  close() { tab.closed = true; } };
    opened.push(tab);
    return tab;
  },
};
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
// The start-up sign-in read: answered "signed in" unless the scenario wants it.
const si = pending.findIndex((p) => p.path === '/api/modules/sound/zoom-signin');
if (si >= 0 && !globalThis.KEEP_SIGNIN) {
  answer(si, 200, { configured: true, signed_in: true, join_token: 'obf', missing: [] });
  await tick();
}
const signinState = () => ({
  shown: !els['mb-signin'].hidden,
  text: els['mb-signin-text'].textContent || '',
  button: !els['mb-signin-btn'].hidden,
  paste: !els['mb-signin-paste'].hidden,
  link: els['mb-signin-open'].hidden ? '' : els['mb-signin-link'].href,
  msg: els['mb-signin-msg'].textContent || '',
  joinDisabled: els['mb-join'].disabled,
  pending: pending.map((p) => `${p.method} ${p.path}`),
});
const find = (method, path) => pending.findIndex((p) => p.method === method && p.path === path);
"""

API = "/api/modules/sound/meetings"


SIGNIN = "/api/modules/sound/zoom-signin"
KEEP = "globalThis.KEEP_SIGNIN = true;\n"
NOT_SIGNED_IN = "{ configured: true, signed_in: false, join_token: 'none', missing: [] }"
AUTHORIZE = "https://zoom.us/oauth/authorize?client_id=FictionalClient&state=FictionalState1"


def _run(scenario: str, prefix: str = "") -> dict:
    src = prefix + HARNESS + scenario
    r = subprocess.run(["node", "--input-type=module", "-e", src], cwd=ROOT,
                       capture_output=True, text=True, timeout=30, check=False)
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


# ------------------------------------------------------------------ Zoom sign-in
def test_signed_in_hides_the_button():
    out = _run(r"""
    answer(0, 200, { meeting: null });
    await tick();
    console.log(JSON.stringify(signinState()));
    """)
    assert out["shown"] is True
    assert out["text"] == "Athena is signed in to Zoom."
    assert out["button"] is False and out["paste"] is False
    assert out["joinDisabled"] is False


def test_not_signed_in_shows_the_button_and_join_stays_enabled():
    out = _run(rf"""
    answer(find('GET', '{SIGNIN}'), 200, {NOT_SIGNED_IN});
    answer(0, 200, {{ meeting: null }});
    await tick();
    console.log(JSON.stringify(signinState()));
    """, KEEP)
    assert out["shown"] is True and out["button"] is True
    assert out["text"] == ("Sign in once so Athena can join meetings other people host. "
                           "Meetings you host work without it.")
    assert out["joinDisabled"] is False


def test_not_configured_names_the_missing_settings():
    out = _run(rf"""
    answer(find('GET', '{SIGNIN}'), 200, {{ configured: false, signed_in: false,
      join_token: 'none', missing: ['CONVO_ZOOM_CLIENT_ID', 'CONVO_ZOOM_CLIENT_SECRET'] }});
    await tick();
    console.log(JSON.stringify(signinState()));
    """, KEEP)
    assert out["text"] == ("Athena's Zoom app isn't set up yet: CONVO_ZOOM_CLIENT_ID, "
                           "CONVO_ZOOM_CLIENT_SECRET in Athena's server/.env.")
    assert out["button"] is False


def test_older_athena_says_update():
    out = _run(rf"""
    answer(find('GET', '{SIGNIN}'), 502, {{ code: 'update',
      detail: 'Update Athena to sign in to Zoom from here: its MCP server has no sign-in routes yet.' }});
    await tick();
    console.log(JSON.stringify(signinState()));
    """, KEEP)
    assert out["shown"] is True and out["button"] is False
    assert out["msg"].startswith("Update Athena to sign in to Zoom from here")


def test_sign_in_opens_zoom_then_finish_signs_in():
    out = _run(rf"""
    answer(find('GET', '{SIGNIN}'), 200, {NOT_SIGNED_IN});
    answer(0, 200, {{ meeting: null }});
    await tick();
    els['mb-signin-btn'].fire('click');
    await tick();
    const startCall = pending.find((p) => p.path === '{SIGNIN}/start');
    const started = !!startCall && startCall.method === 'POST';
    const openedEarly = opened.length;
    answer(find('POST', '{SIGNIN}/start'), 200, {{ url: '{AUTHORIZE}', expires_in: 600 }});
    await tick();
    const pasteStep = signinState();
    els['mb-signin-input'].value = 'http://localhost:9/zoom?code=FictionalCode&state=FictionalState1';
    els['mb-signin-finish'].fire('click');
    await tick();
    const fin = find('POST', '{SIGNIN}/finish');
    answer(fin, 200, {{ ok: true, signed_in: true, join_token: 'obf' }});
    await tick();
    console.log(JSON.stringify({{ started, openedEarly, tab: opened[0], pasteStep,
                                  after: signinState(), input: els['mb-signin-input'].value }}));
    """, KEEP)
    assert out["started"] is True
    assert out["openedEarly"] == 1  # opened inside the click, before Athena answered
    assert out["tab"]["opener"] is None and out["tab"]["target"] == "_blank"
    assert out["tab"]["location"]["href"] == AUTHORIZE
    assert out["pasteStep"]["paste"] is True and out["pasteStep"]["link"] == AUTHORIZE
    assert out["after"]["text"] == "Athena is signed in to Zoom."
    assert out["after"]["button"] is False and out["after"]["paste"] is False
    assert out["after"]["joinDisabled"] is False
    assert out["input"] == ""


def test_finish_sends_the_paste_and_shows_a_refusal():
    out = _run(rf"""
    answer(find('GET', '{SIGNIN}'), 200, {NOT_SIGNED_IN});
    await tick();
    els['mb-signin-btn'].fire('click');
    await tick();
    answer(find('POST', '{SIGNIN}/start'), 200, {{ url: '{AUTHORIZE}', expires_in: 600 }});
    await tick();
    const sent = [];
    const realFetch = globalThis.fetch;
    globalThis.fetch = (path, init = {{}}) => {{ sent.push({{ path, init }}); return realFetch(path, init); }};
    els['mb-signin-input'].value = '  not an address  ';
    els['mb-signin-finish'].fire('click');
    await tick();
    answer(find('POST', '{SIGNIN}/finish'), 400, {{ code: 'bad_paste',
      detail: 'That address has no Zoom code in it.' }});
    await tick();
    console.log(JSON.stringify({{ sent: sent.map((s) => [s.path, s.init.body,
      s.init.headers['X-Odysseus-Recorder']]), after: signinState() }}));
    """, KEEP)
    assert out["sent"] == [[f"{SIGNIN}/finish", '{"pasted":"not an address"}', "1"]]
    assert out["after"]["msg"] == "That address has no Zoom code in it."
    assert out["after"]["paste"] is True and out["after"]["joinDisabled"] is False


def test_blocked_popup_still_shows_the_link():
    out = _run(rf"""
    answer(find('GET', '{SIGNIN}'), 200, {NOT_SIGNED_IN});
    await tick();
    els['mb-signin-btn'].fire('click');
    await tick();
    answer(find('POST', '{SIGNIN}/start'), 200, {{ url: '{AUTHORIZE}', expires_in: 600 }});
    await tick();
    console.log(JSON.stringify(signinState()));
    """, KEEP + "globalThis.BLOCK_POPUPS = true;\n")
    assert out["paste"] is True and out["link"] == AUTHORIZE


def test_failed_start_closes_the_tab():
    out = _run(rf"""
    answer(find('GET', '{SIGNIN}'), 200, {NOT_SIGNED_IN});
    await tick();
    els['mb-signin-btn'].fire('click');
    await tick();
    answer(find('POST', '{SIGNIN}/start'), 502, {{ code: 'zoom',
      detail: "Athena couldn't reach Zoom; try again in a minute." }});
    await tick();
    console.log(JSON.stringify({{ closed: opened[0].closed, s: signinState() }}));
    """, KEEP)
    assert out["closed"] is True
    assert out["s"]["paste"] is False and out["s"]["button"] is True
    assert "couldn't reach Zoom" in out["s"]["msg"]


def test_join_answering_signin_shows_the_button_and_rereads():
    out = _run(rf"""
    answer(0, 200, {{ meeting: null }});
    await tick();
    els['mb-link'].value = 'https://zoom.us/j/85012345678';
    els['mb-join'].fire('click');
    await tick();
    answer(find('POST', '{API}'), 503, {{ code: 'signin',
      detail: "Athena's Zoom sign-in is missing or expired — sign in to Zoom on the Meetings page." }});
    await tick();
    const reread = find('GET', '{SIGNIN}') >= 0;
    answer(find('GET', '{SIGNIN}'), 200, {NOT_SIGNED_IN});
    await tick();
    console.log(JSON.stringify({{ reread, s: signinState(), text: els['mb-state'].textContent }}));
    """)
    assert out["reread"] is True
    assert out["s"]["button"] is True and out["s"]["joinDisabled"] is False
    assert "Sign in to Zoom, then press Join again" in out["text"]
