// Fork: open a new chat with prefilled composer text from `/?ask=<text>`.
// Loaded by one hook in static/index.html. See docs/fork (Ask in chat).
//
// A page (e.g. the sound module view) links to `/?ask=<urlencoded text>`.
// This script removes `ask` from the URL, waits for the app to finish
// starting, makes sure a NEW chat is showing, and puts the text into the
// composer with the cursor at the end. It never sends: a link must not be
// able to start a turn or trigger tool calls. The owner presses Send.

const ASK_PARAM = 'ask';
const MAX_ASK_CHARS = 2000;
const READY_TIMEOUT_MS = 20000;
const POLL_MS = 100;

function _takeAskParam() {
  let url;
  try { url = new URL(window.location.href); } catch (_) { return ''; }
  if (!url.searchParams.has(ASK_PARAM)) return '';
  const raw = url.searchParams.get(ASK_PARAM) || '';
  url.searchParams.delete(ASK_PARAM);
  try {
    history.replaceState(history.state, '', url.pathname + url.search + url.hash);
  } catch (_) { /* ignore */ }
  return _clean(raw);
}

function _clean(raw) {
  if (raw.length > MAX_ASK_CHARS) return '';
  // One line only: a line break in the composer can submit on some mobile
  // paths, and the composer is a single prompt anyway.
  return raw.replace(/[\u0000-\u001f\u007f]+/g, ' ');
}

function _sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function _waitFor(check, timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try { if (check()) return true; } catch (_) { /* not ready yet */ }
    await _sleep(POLL_MS);
  }
  return false;
}

function _composer() { return document.getElementById('message'); }

// The boot loader is removed once the session list has hydrated and the
// startup session (if any) has been chosen; before that, startup code may
// still switch chats and clear the composer.
function _appReady() {
  const msg = _composer();
  return !!(msg && window.sessionModule && !document.getElementById('app-loader'));
}

function _currentSessionId() {
  try { return window.sessionModule?.getCurrentSessionId?.() || null; } catch (_) { return null; }
}

async function _ensureNewChat() {
  if (!_currentSessionId()) return true;
  const btn = document.getElementById('sidebar-new-chat-btn')
    || document.getElementById('rail-new-session')
    || document.getElementById('mobile-new-chat-btn');
  if (!btn) return false;
  btn.click();
  return _waitFor(() => !_currentSessionId(), 5000);
}

function _fill(text) {
  const msg = _composer();
  if (!msg) return false;
  msg.disabled = false;
  msg.value = text;
  // Lets the app auto-resize the composer, update the send button and mark
  // the composer as user-edited (so startup code does not clear it).
  msg.dispatchEvent(new Event('input', { bubbles: true }));
  msg.focus();
  const end = msg.value.length;
  try { msg.setSelectionRange(end, end); } catch (_) { /* ignore */ }
  return true;
}

async function _run() {
  await _prefill(_takeAskParam());
}

/** Opens a new chat with `text` in the composer, like `/?ask=` but without a page load.
 *  Used by the module windows (static/js/forkModuleWindows.js). Never sends. */
export function askInChat(text) {
  return _prefill(_clean(String(text || '')));
}

async function _prefill(text) {
  if (!text.trim()) return;
  if (!await _waitFor(_appReady, READY_TIMEOUT_MS)) return;
  if (!await _ensureNewChat()) return;
  // Let the new-chat flow finish its own composer reset before filling.
  await _waitFor(() => !_composer()?.disabled, 3000);
  await _sleep(50);
  if (!_fill(text)) return;
  // A late startup repaint may still clear the composer; restore once if the
  // owner has not typed anything meanwhile.
  await _sleep(1500);
  const msg = _composer();
  if (msg && !msg.value && !_currentSessionId()) _fill(text);
}

_run();
