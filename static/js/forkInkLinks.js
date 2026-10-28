// Fork: links to a handwritten note open it in Write (static/js/forkInk.js).
// Wired by static/js/forkModuleWindows.js.
//
// Handwritten notes (label `handwritten`) are shown in Write only: the Notes window
// leaves them out (hook in features/notes/application/workflows.js). A link to one,
// `#open=notes&note=<id>` or `#note-<id>` (Athena's "Written by hand", a chat answer,
// a fresh load through forkEntityHash.js), would open the Notes window on a card that
// isn't there, so it opens the note in Write instead. Telling a handwritten note apart
// needs its label: the click waits for `GET /api/notes/<id>`. Any other note, or a
// failed lookup, goes on to upstream's handler with the same link (a hidden copy of it,
// marked so this listener lets it pass).
//
// Upstream's handler (chatRenderer.js) listens on document in the capture phase, so
// this one listens on window, which comes first.

const NOTE_HASH = /^#(?:open=notes&note=|note-)([A-Za-z0-9-]{1,80})$/;
const CHECKED = 'forkNoteChecked';

const plainClick = (e) => e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;

// The note id in a link to a note on this page (`#…`, or the page's own URL with that
// hash), else null.
export function noteLinkId(raw, location = window.location) {
  let hash = '';
  try {
    const url = new URL(String(raw || ''), location.href);
    if (url.origin === location.origin && url.pathname === location.pathname) hash = url.hash;
  } catch (_) { return null; }
  const m = NOTE_HASH.exec(hash);
  return m ? m[1] : null;
}

export function wireHandwrittenNoteLinks({ offered, openInWrite, fetchNote = defaultFetchNote }) {
  window.addEventListener('click', (e) => {
    if (e.defaultPrevented || !plainClick(e)) return;
    const a = e.target && e.target.closest && e.target.closest('a[href]');
    if (!a || a.dataset[CHECKED] === '1') return;
    const id = noteLinkId(a.getAttribute('href'));
    if (!id || !offered()) return;
    e.preventDefault();
    e.stopPropagation();
    const hash = new URL(a.getAttribute('href'), window.location.href).hash;
    const toUpstream = () => {
      const again = document.createElement('a');
      again.href = hash;
      again.hidden = true;
      again.dataset[CHECKED] = '1';
      document.body.appendChild(again);
      try { again.click(); } finally { again.remove(); }
    };
    Promise.resolve()
      .then(() => fetchNote(id))
      .then((note) => {
        if (!note || note.label !== 'handwritten') { toUpstream(); return; }
        if (NOTE_HASH.test(window.location.hash || '')) {
          try { history.replaceState(null, '', window.location.pathname + window.location.search); } catch (_) { /* keep it */ }
        }
        return Promise.resolve(openInWrite(id)).catch(() => {});
      })
      .catch(toUpstream);
  }, true);
}

async function defaultFetchNote(id) {
  const res = await fetch(`/api/notes/${encodeURIComponent(id)}`, { credentials: 'same-origin' });
  return res.ok ? res.json() : null;
}
