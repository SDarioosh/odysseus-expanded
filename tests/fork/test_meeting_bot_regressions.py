"""Fork: regressions for "Send Athena to a meeting" and calendar auto-join
(src/meeting_bot.py, src/meeting_auto_join.py, static/js/forkMeetingAutoJoin.js).

Fictional meetings only: numbers, passcodes, hosts and titles below are made up.
"""
import json
import logging
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

import core.database as cdb
from src import meeting_auto_join as auto
from src import meeting_bot as mb
from tests.fork.test_meeting_auto_join import (  # noqa: F401  (fixtures)
    NOW, OWNER, add_event, env, module, tasks,
)
from tests.fork.test_meeting_bot import LINK, MID, TOKEN, Module, row, run

ROOT = Path(__file__).resolve().parents[2]
NUMBER = "85012345678"
PWD = "FictPwd1"


# ------------------------------------------------------------------ links in HTML text
@pytest.mark.parametrize("text, want_pwd", [
    # Google Calendar: the description is HTML, the link an <a href="…">.
    (f'Join Zoom Meeting<br><a href="https://us02web.zoom.us/j/{NUMBER}?pwd={PWD}">'
     f'https://us02web.zoom.us/j/{NUMBER}?pwd={PWD}</a>', PWD),
    (f"Join: <a href='https://acme.zoom.us/j/{NUMBER}?pwd={PWD}'>here</a>", PWD),
    # Outlook's plain text: the link, then the same link in <…> with no space between.
    (f"Join Zoom Meeting https://zoom.us/j/{NUMBER}?pwd={PWD}"
     f"<https://zoom.us/j/{NUMBER}?pwd={PWD}>", PWD),
    # An HTML-escaped "&" before pwd.
    (f"Join https://zoom.us/j/{NUMBER}?from=addon&amp;pwd={PWD} today", PWD),
    # No pwd in the link: the invite's "Passcode:" line, followed by a <br>.
    (f'Join: <a href="https://zoom.us/j/{NUMBER}">Join</a><br>Passcode: 445566<br>',
     "445566"),
])
def test_html_invite_gives_the_clean_link_and_passcode(text, want_pwd):
    got = mb.parse_join(text)
    assert got.meeting_id == NUMBER
    assert got.passcode == want_pwd
    assert all(c not in got.url for c in '<>"\'') and "&amp;" not in got.url


def test_calendar_event_with_an_html_description_is_eligible(env):
    add_event(env, "e1", NOW + timedelta(hours=2), location="",
              description=(f'<b>Join Zoom Meeting</b><br><a href="https://us06web.zoom.us/'
                           f'j/{NUMBER}?pwd={PWD}">https://us06web.zoom.us/j/{NUMBER}?pwd='
                           f'{PWD}</a><br>Meeting ID: 850 1234 5678'))
    req, why = auto.zoom_join(auto._event(env.db(), OWNER, "e1"))
    assert why is None and req is not None
    assert req.passcode == PWD and req.url == f"https://us06web.zoom.us/j/{NUMBER}?pwd={PWD}"


# ------------------------------------------------------------------ cancelled events
def test_cancelled_event_is_never_joined(env, module):
    from src.builtin_actions import TaskNoop

    fake = module()
    add_event(env, "e1", NOW + timedelta(hours=2))
    auto.set_toggle(OWNER, "e1", True)
    assert len(tasks(env)) == 1
    # An emailed CANCEL marks the event cancelled (src/email_calendar_import.py); no
    # calendar hook runs, so the task is still there when it comes due.
    with env.db() as db:
        db.query(cdb.CalendarEvent).filter_by(uid="e1").one().status = "cancelled"
        db.commit()
    auto._utcnow = lambda: NOW + timedelta(hours=1, minutes=59)  # the task's due time
    try:
        with pytest.raises(TaskNoop):
            run(auto.action_meeting_auto_join(owner=OWNER, prompt="e1"))
    finally:
        auto._utcnow = lambda: NOW
    assert fake.requests == []
    assert tasks(env) == []


# ------------------------------------------------------------------ the token
def test_module_calls_never_put_the_token_in_a_logged_url(caplog):
    caplog.set_level(logging.DEBUG)
    req = mb.parse_join(f"https://zoom.us/j/{NUMBER}?pwd={PWD}")
    run(mb.join(LINK, req, transport=Module(202, {"id": MID, "state": "starting"}).transport))
    run(mb.get(LINK, MID, transport=Module(200, row()).transport))
    run(mb.current(LINK, transport=Module(200, {"meetings": [row()]}).transport))
    run(mb.leave(LINK, MID, transport=Module(200, row(state="ended")).transport))
    run(mb.signin_status(LINK, transport=Module(200, {"configured": True}).transport))
    assert TOKEN not in caplog.text


def test_module_calls_send_the_token_as_a_bearer_header():
    module = Module(200, row())
    run(mb.get(LINK, MID, transport=module.transport))
    sent = module.requests[0]
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert "access_token" not in str(sent.url) and TOKEN not in str(sent.url)


# ------------------------------------------------------------------ event form toggle
TOGGLE_HARNESS = r"""
const box = { checked: false, disabled: false, handlers: {},
  addEventListener(t, fn) { this.handlers[t] = fn; } };
const text = { textContent: '' };
const row = { style: {}, set innerHTML(h) { this.html = h;
    box.checked = /\bchecked\b/.test(h); box.disabled = /\bdisabled\b/.test(h); },
  querySelector: (sel) => (sel.includes('toggle') ? box : text) };
globalThis.document = { createElement: () => row };
const form = { isConnected: true, querySelector: () => null, appendChild() {} };
const answers = [];
globalThis.fetch = async (path, init = {}) => {
  const [status, body] = answers.shift();
  return { ok: status < 300, status, json: async () => body };
};
const { attach } = await import('./static/js/forkMeetingAutoJoin.js');
"""


def _node(scenario: str) -> dict:
    r = subprocess.run(["node", "--input-type=module", "-e", TOGGLE_HARNESS + scenario],
                       cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_toggle_that_is_on_can_be_turned_off_when_the_link_stopped_working():
    """The link became a webinar (or the event all-day) after Send Athena was turned on:
    the toggle stays on in the hub, so the owner must still be able to turn it off."""
    out = _node(r"""
    const on = { uid: 'e1', zoom: true, eligible: false, on: true,
                 reason: "Webinars aren't supported yet." };
    answers.push([200, on]);
    await attach(form, 'e1');
    const first = { checked: box.checked, disabled: box.disabled };
    answers.push([500, { detail: 'Fictional failure' }]);  // a failed save keeps it on
    box.checked = false;
    await box.handlers.change();
    const afterFail = { checked: box.checked, disabled: box.disabled };
    console.log(JSON.stringify({ first, afterFail }));
    """)
    assert out["first"] == {"checked": True, "disabled": False}
    assert out["afterFail"] == {"checked": True, "disabled": False}
