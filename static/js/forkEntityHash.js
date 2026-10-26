// Fork: open a calendar event or a note named in the URL hash on page load.
// Loaded by one hook in static/index.html. Used by links from the
// Athena page and its phone pushes ("In calendar", "In to-dos").
//
// Upstream opens `#event-<uid>` and `#open=notes&note=<id>` only when such a
// link is clicked inside the app (document-level click handler in
// chatRenderer.js). On a fresh load the hash is ignored. This script waits for
// the app to start, then clicks a hidden link with the same hash, so upstream's
// own handler opens the event or note. Only these two read-only views.

const READY_TIMEOUT_MS = 20000;
const POLL_MS = 100;
const ENTITY_HASH = /^#(?:event-[A-Za-z0-9._:@-]{1,200}|open=notes&note=[A-Za-z0-9-]{1,80})$/;

function _sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function _waitFor(check, timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try { if (check()) return true; } catch (_) { /* not ready yet */ }
    await _sleep(POLL_MS);
  }
  return false;
}

// Same readiness test as forkChatAsk.js: the boot loader is gone once the
// session list has hydrated.
function _appReady() {
  return !!(window.sessionModule && document.getElementById('message')
    && !document.getElementById('app-loader'));
}

async function _run() {
  const hash = window.location.hash || '';
  if (!ENTITY_HASH.test(hash)) return;
  if (!await _waitFor(_appReady, READY_TIMEOUT_MS)) return;
  const a = document.createElement('a');
  a.href = hash;
  a.hidden = true;
  document.body.appendChild(a);
  try { a.click(); } finally { a.remove(); }
}

_run();
