// Fork: "Send Athena to a meeting" box on the Record a call page
// (static/call-recorder.html). The owner pastes a Zoom link, an invite or a meeting ID;
// routes/sound_module_routes.py checks it and asks Athena to send its Zoom bot
// (src/meeting_bot.py). The state is fetched only while this page is open and visible
// and a meeting is on: nothing polls when the tab is hidden or closed, or once the
// meeting has ended or failed.

const API = '/api/modules/sound/meetings';
const POLL_JOINING_MS = 3000; // starting / waiting room: show the change soon
const POLL_RECORDING_MS = 6000;
const POLL_ERROR_MAX_MS = 30000;
const WRITE_HEADERS = { 'Content-Type': 'application/json', 'X-Odysseus-Recorder': '1' };

const $ = (sel) => document.querySelector(sel);
const ui = {
  link: $('#mb-link'), pass: $('#mb-pass'), join: $('#mb-join'), leave: $('#mb-leave'),
  state: $('#mb-state'),
};

let meeting = null; // the meeting shown: {id, state, reason, active, text, ...}
let busyId = null; // the other meeting Athena is in, after a "busy" answer
let working = ''; // 'join' | 'leave' while a request is in flight
let message = ''; // a problem to show under the state
let timer = null;
let errorDelay = POLL_JOINING_MS;

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

function render() {
  const active = !!(meeting && meeting.active);
  ui.join.disabled = !!working || active;
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
  return meeting && meeting.state === 'recording' ? POLL_RECORDING_MS : POLL_JOINING_MS;
}

async function refresh() {
  timer = null;
  if (!meeting || working) return;
  const id = meeting.id;
  const res = await call('GET', `${API}/${encodeURIComponent(id)}`);
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
  const res = await call('GET', `${API}/current`);
  if (working) return;
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
  working = 'leave';
  message = '';
  render();
  const res = await call('DELETE', `${API}/${encodeURIComponent(id)}`);
  working = '';
  if (res.ok && res.data.meeting) {
    meeting = res.data.meeting;
    busyId = null;
  } else {
    message = res.data.detail;
  }
  render();
  schedule(POLL_JOINING_MS); // only if it still says active
}

// ------------------------------------------------------------------ start
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
