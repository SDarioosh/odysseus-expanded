// Fork: "Send Athena to a meeting" box on the Record a call page
// (static/call-recorder.html). The owner pastes a Zoom link, an invite or a meeting ID;
// routes/sound_module_routes.py checks it and asks Athena to send its Zoom bot
// (src/meeting_bot.py). The state is fetched only while this page is open and visible
// and a meeting is on: nothing polls when the tab is hidden or closed, or once the
// meeting has ended or failed.
//
// The box's Zoom sign-in line (Athena joins on the owner's behalf, so it needs the owner's
// Zoom sign-in once) reads the sign-in when the page opens and after a Join answers
// "signin", never on a timer. Sign in to Zoom opens Zoom's page in a new tab; the owner
// pastes back the address Zoom ends on and Athena finishes the sign-in.

const API = '/api/modules/sound/meetings';
const POLL_JOINING_MS = 3000; // starting / waiting room: show the change soon
const POLL_RECORDING_MS = 6000; // recording, or waiting for the host to allow it
const POLL_ERROR_MAX_MS = 30000;
const WRITE_HEADERS = { 'Content-Type': 'application/json', 'X-Odysseus-Recorder': '1' };
const SIGNIN_API = '/api/modules/sound/zoom-signin';
const SIGNED_IN = 'Athena is signed in to Zoom.';
const JOIN_SIGNIN = "Athena's Zoom sign-in is missing or expired. Sign in to Zoom, then press Join again.";
// Answers that the meeting box already explains (no module, no token, unreachable).
const SIGNIN_QUIET = ['setup', 'token', 'unreachable', 'timeout'];

const $ = (sel) => document.querySelector(sel);
const ui = {
  link: $('#mb-link'), pass: $('#mb-pass'), join: $('#mb-join'), leave: $('#mb-leave'),
  state: $('#mb-state'),
  signin: $('#mb-signin'), signinText: $('#mb-signin-text'), signinBtn: $('#mb-signin-btn'),
  paste: $('#mb-signin-paste'), pasteInput: $('#mb-signin-input'),
  finish: $('#mb-signin-finish'), open: $('#mb-signin-open'), openLink: $('#mb-signin-link'),
  signinMsg: $('#mb-signin-msg'),
};

let meeting = null; // the meeting shown: {id, state, reason, active, text, ...}
let busyId = null; // the other meeting Athena is in, after a "busy" answer
let working = ''; // 'join' | 'leave' while a request is in flight
let message = ''; // a problem to show under the state
let timer = null;
let errorDelay = POLL_JOINING_MS;
// Bumped by every Join and Leave: a state read (loadCurrent, refresh) that started
// before one of them answers with old news, so it is dropped (QA BUG-26).
let seq = 0;
// The Zoom sign-in as last read: shown once read (or once a Join answered "signin").
const signin = {
  shown: false, known: false, configured: false, signedIn: false, missing: [],
  step: '', // 'starting' | 'paste' | 'finishing'
  url: '', message: '',
};
let signinSeq = 0; // bumped by Sign in and Finish: an older status read is dropped

// ------------------------------------------------------------------ helpers
async function call(method, path, body) {
  const init = { method, credentials: 'same-origin' };
  if (method !== 'GET') init.headers = WRITE_HEADERS;
  if (body !== undefined) init.body = JSON.stringify(body);
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    return { ok: false, status: 0, data: { detail: 'Odysseus is not reachable.' } };
  }
  let data = {};
  try {
    data = await res.json();
  } catch (e) { /* not JSON */ }
  if (!res.ok && typeof data.detail !== 'string') {
    data = { ...data, detail: `Odysseus HTTP ${res.status}` };
  }
  return { ok: res.ok, status: res.status, data };
}

function meetingNumber(id) {
  const s = String(id || '');
  if (s.length === 11) return `${s.slice(0, 3)} ${s.slice(3, 7)} ${s.slice(7)}`;
  if (s.length === 10) return `${s.slice(0, 3)} ${s.slice(3, 6)} ${s.slice(6)}`;
  return s;
}

function needsSignin() {
  return signin.known && signin.configured && !signin.signedIn;
}

function signinLine() {
  if (!signin.known) return '';
  if (signin.signedIn) return SIGNED_IN;
  if (!signin.configured) {
    return signin.missing.length
      ? `Athena's Zoom app isn't set up yet: ${signin.missing.join(', ')} in Athena's server/.env.`
      : "Athena's Zoom app isn't set up yet (Athena's server/.env).";
  }
  return "Athena isn't signed in to Zoom, or its sign-in expired.";
}

function renderSignin() {
  ui.signin.hidden = !signin.shown;
  ui.signinText.textContent = signinLine();
  const step = signin.step;
  ui.signinBtn.hidden = !needsSignin();
  ui.signinBtn.disabled = step === 'starting' || step === 'finishing';
  ui.signinBtn.textContent = step === 'starting' ? 'Opening Zoom…' : 'Sign in to Zoom';
  ui.paste.hidden = !(step === 'paste' || step === 'finishing');
  ui.open.hidden = !signin.url;
  ui.openLink.href = signin.url || '#';
  ui.finish.disabled = step === 'finishing';
  ui.finish.textContent = step === 'finishing' ? 'Finishing…' : 'Finish';
  ui.pasteInput.disabled = step === 'finishing';
  ui.signinMsg.textContent = signin.message;
}

function render() {
  renderSignin();
  const active = !!(meeting && meeting.active);
  ui.join.disabled = !!working || active || needsSignin();
  ui.join.textContent = working === 'join' ? 'Joining…' : 'Join';
  ui.leave.hidden = !(active || busyId);
  ui.leave.disabled = !!working;
  ui.leave.textContent = working === 'leave' ? 'Leaving…' : 'Leave';
  ui.link.disabled = active;
  ui.pass.disabled = active;

  const lines = [];
  if (working === 'leave') {
    lines.push('Leaving… Athena is saying goodbye to Zoom (this can take a few seconds).');
  } else if (busyId && !active) {
    lines.push('Athena is busy in another meeting. Leave it first, or wait until it ends.');
  } else if (meeting) {
    let text = meeting.text || '';
    if (meeting.state === 'ended' && meeting.started_at) {
      text += ' The conversation appears in Conversations once Athena has transcribed it.';
    }
    lines.push(text);
    const what = meeting.title || (meeting.meeting_id
      ? `Zoom meeting ${meetingNumber(meeting.meeting_id)}` : '');
    if (what) lines.push(what);
  }
  if (message) lines.push(message);
  ui.state.textContent = '';
  lines.forEach((line, i) => {
    const div = document.createElement('div');
    if (i > 0) div.className = 'bs-meta';
    div.textContent = line;
    ui.state.appendChild(div);
  });
}

// ------------------------------------------------------------------ polling
function stopPolling() {
  if (timer) clearTimeout(timer);
  timer = null;
}

function schedule(ms) {
  stopPolling();
  if (document.hidden || !meeting || !meeting.active) return;
  timer = setTimeout(refresh, ms);
}

function pollDelay() {
  const slow = meeting && (meeting.state === 'recording' || meeting.state === 'waiting_permission');
  return slow ? POLL_RECORDING_MS : POLL_JOINING_MS;
}

async function refresh() {
  timer = null;
  if (!meeting || working) return;
  const id = meeting.id;
  const started = seq;
  const res = await call('GET', `${API}/${encodeURIComponent(id)}`);
  if (started !== seq || working) return; // a Join or Leave came meanwhile
  if (!meeting || meeting.id !== id) return; // replaced meanwhile
  if (res.ok && res.data.meeting) {
    meeting = res.data.meeting;
    message = '';
    errorDelay = POLL_JOINING_MS;
    render();
    schedule(pollDelay());
    return;
  }
  if (res.status === 404) { // the module forgot it (a wiped database): stop asking
    meeting = null;
    message = 'Athena no longer knows this meeting.';
    render();
    return;
  }
  message = res.data.detail;
  render();
  errorDelay = Math.min(errorDelay * 2, POLL_ERROR_MAX_MS);
  schedule(errorDelay);
}

async function loadCurrent() {
  const started = seq;
  const res = await call('GET', `${API}/current`);
  if (started !== seq || working) return; // a Join or Leave came meanwhile
  if (res.ok) {
    if (res.data.meeting) {
      meeting = res.data.meeting;
      busyId = null;
    } else if (meeting && meeting.active) {
      // The meeting shown ended while the tab was hidden: read its final state once.
      await refresh();
      return;
    }
    if (!res.data.meeting) busyId = null; // the other meeting is over
    message = '';
  } else {
    message = res.data.detail;
  }
  render();
  schedule(pollDelay());
}

// ------------------------------------------------------------------ actions
async function join() {
  const link = ui.link.value.trim();
  if (!link) {
    message = 'Paste a Zoom meeting link, or type the meeting ID.';
    render();
    ui.link.focus();
    return;
  }
  seq += 1;
  working = 'join';
  message = '';
  busyId = null;
  render();
  const body = { link };
  const pass = ui.pass.value.trim();
  if (pass) body.passcode = pass;
  const res = await call('POST', API, body);
  working = '';
  if (res.ok && res.data.meeting) {
    meeting = res.data.meeting;
    ui.link.value = '';
    ui.pass.value = '';
  } else if (res.status === 409 && res.data.code === 'busy') {
    busyId = res.data.active || null;
    if (!busyId) message = res.data.detail;
  } else if (res.data.code === 'signin') {
    message = JOIN_SIGNIN;
    signin.shown = true;
    signin.known = true;
    signin.signedIn = false;
    signin.configured = true; // the status read below corrects this
    render();
    loadSignin();
    return;
  } else {
    message = res.data.detail;
  }
  render();
  schedule(POLL_JOINING_MS);
}

async function leave() {
  const id = (meeting && meeting.active && meeting.id) || busyId;
  if (!id) return;
  stopPolling();
  seq += 1;
  working = 'leave';
  message = '';
  render();
  const res = await call('DELETE', `${API}/${encodeURIComponent(id)}`);
  working = '';
  if (res.ok && res.data.meeting) {
    meeting = res.data.meeting;
    busyId = null;
  } else if (res.status === 404 && id === busyId) {
    // The other meeting is already gone: forget it and show what is on now.
    busyId = null;
    render();
    await loadCurrent();
    return;
  } else {
    message = res.data.detail;
  }
  render();
  schedule(POLL_JOINING_MS); // only if it still says active
}

// ------------------------------------------------------------------ Zoom sign-in
async function loadSignin() {
  const started = signinSeq;
  const res = await call('GET', SIGNIN_API);
  if (started !== signinSeq) return; // a Sign in or Finish came meanwhile
  if (res.ok) {
    signin.shown = true;
    signin.known = true;
    signin.configured = !!res.data.configured;
    signin.signedIn = !!res.data.signed_in;
    signin.missing = Array.isArray(res.data.missing) ? res.data.missing.map(String) : [];
    if (signin.signedIn) {
      signin.step = '';
      signin.url = '';
      signin.message = '';
    }
  } else if (!SIGNIN_QUIET.includes(res.data.code)) {
    signin.shown = true; // e.g. "Update Athena to sign in to Zoom from here"
    signin.known = false;
    signin.message = res.data.detail;
  }
  render();
}

async function startSignin() {
  signinSeq += 1;
  signin.step = 'starting';
  signin.message = '';
  render();
  // Open the tab inside the click, before the request, so a popup blocker lets it through;
  // it is cut from this page (no opener) and sent to Zoom's page once Athena answers.
  let tab = null;
  try {
    tab = window.open('', '_blank');
    if (tab) tab.opener = null;
  } catch (e) {
    tab = null;
  }
  const res = await call('POST', `${SIGNIN_API}/start`);
  if (res.ok && typeof res.data.url === 'string' && res.data.url.startsWith('https://')) {
    signin.url = res.data.url;
    signin.step = 'paste';
    if (tab) {
      try {
        tab.location.href = signin.url;
      } catch (e) { /* the link below still works */ }
    }
    render();
    ui.pasteInput.focus();
    return;
  }
  if (tab) {
    try { tab.close(); } catch (e) { /* already gone */ }
  }
  signin.step = '';
  signin.url = '';
  signin.message = res.ok ? 'Athena sent an unreadable answer.' : res.data.detail;
  render();
  if (res.data.code === 'not_configured') loadSignin(); // name the missing settings
}

async function finishSignin() {
  const pasted = ui.pasteInput.value.trim();
  if (!pasted) {
    signin.message = 'Paste the address of the page Zoom opened after you clicked Allow.';
    render();
    ui.pasteInput.focus();
    return;
  }
  signinSeq += 1;
  signin.step = 'finishing';
  signin.message = '';
  render();
  const res = await call('POST', `${SIGNIN_API}/finish`, { pasted });
  if (res.ok && res.data.ok) {
    signin.known = true;
    signin.configured = true;
    signin.signedIn = true;
    signin.step = '';
    signin.url = '';
    ui.pasteInput.value = '';
    if (message === JOIN_SIGNIN) message = '';
  } else {
    signin.step = 'paste';
    signin.message = res.data.detail || 'Athena sent an unreadable answer.';
    if (res.data.code === 'not_configured') {
      signin.configured = false;
      signin.step = '';
    }
  }
  render();
}

// ------------------------------------------------------------------ start
ui.signinBtn.addEventListener('click', startSignin);
ui.finish.addEventListener('click', finishSignin);
ui.pasteInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    if (!ui.finish.disabled) finishSignin();
  }
});
ui.join.addEventListener('click', join);
ui.leave.addEventListener('click', leave);
ui.link.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (!ui.join.disabled) join();
  }
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopPolling();
  } else if (!working) {
    loadCurrent(); // another device may have started or stopped a meeting meanwhile
  }
});
window.addEventListener('pagehide', stopPolling);

render();
loadCurrent();
loadSignin();
