// Fork: show the saved result of a run that ended while the page was away.
//
// Chat runs keep going on the server when the phone sleeps or the tab closes
// (src/agent_runs.py), and a finished run saves its answer to the chat. When the
// page came back it tried to re-attach to the run (/api/chat/resume), got 404
// because the run had already ended, and printed "Connection lost. The existing
// run could not be resumed." over the chat instead of showing the saved answer.
//
// reloadEndedRun() checks that the run really ended (/api/chat/stream_status
// answers 404 once nothing is streaming) and reloads the chat from the server.
// If the run ended without saving an answer, it says so instead.
// Hook: static/js/chat.js (_tryAutoRecover).

const NO_ANSWER = 'This run ended without an answer while you were away. Send the message again to retry.';

async function runEnded(apiBase, sessionId) {
  try {
    const res = await fetch(`${apiBase}/api/chat/stream_status/${encodeURIComponent(sessionId)}`);
    if (res.status === 404) return true;
    if (!res.ok) return false;
    const info = await res.json().catch(() => ({}));
    return !(info && info.status === 'streaming');
  } catch (_) {
    return false; // still offline: keep upstream's message
  }
}

function lastMessageIsUser() {
  const box = document.getElementById('chat-history');
  const messages = box ? box.querySelectorAll('.msg') : [];
  const last = messages.length ? messages[messages.length - 1] : null;
  return Boolean(last && last.classList.contains('msg-user'));
}

function addNote(text) {
  const box = document.getElementById('chat-history');
  if (!box) return;
  const note = document.createElement('div');
  note.className = 'msg msg-ai';
  const body = document.createElement('div');
  body.className = 'body';
  body.style.opacity = '0.6';
  body.style.fontStyle = 'italic';
  body.textContent = text;
  note.appendChild(body);
  box.appendChild(note);
}

// True when it handled the failed resume (reloaded the chat), false to let the
// caller show its own message (offline, or the run is somehow still going).
export async function reloadEndedRun({ apiBase = '', sessionModule, sessionId } = {}) {
  if (!sessionId || !sessionModule || typeof sessionModule.selectSession !== 'function') return false;
  if (!(await runEnded(apiBase, sessionId))) return false;
  const current = typeof sessionModule.getCurrentSessionId === 'function'
    ? sessionModule.getCurrentSessionId()
    : sessionId;
  if (current !== sessionId) return true; // the owner moved on; that chat loads fresh later
  await sessionModule.selectSession(sessionId, { showLoading: false });
  if (sessionModule.getCurrentSessionId?.() === sessionId && lastMessageIsUser()) addNote(NO_ANSWER);
  return true;
}
