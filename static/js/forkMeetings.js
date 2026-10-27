// Fork: the Meetings window (static/js/forkModuleWindows.js): "Send Athena to a meeting"
// (static/js/meetingBot.js) and "Record it on this computer" (static/js/callRecorder.js).
// The markup is read from the standalone Meetings page (static/call-recorder.html,
// served at /modules/sound/record), so the page and the window can't drift apart; the
// two scripts then find their elements in the window. The page can't simply be framed:
// Odysseus sends `frame-ancestors 'none'` for it (core/middleware.py).
//
// Closing the window only hides it (forkModuleWindows.js keeps the view), so a recording
// or a meeting goes on while the owner uses the chat.

const PAGE = '/modules/sound/record';

let mounted = null;

/**
 * Builds the Meetings view into `root`.
 * - openConversations(): opens the Conversations window (the page links to /modules/sound).
 * Returns { refresh }.
 */
export async function mountMeetings(root, { openConversations = null } = {}) {
  if (mounted) throw new Error('the Meetings view is already mounted');
  const res = await fetch(PAGE, { credentials: 'same-origin' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
  const wrap = doc.querySelector('.bs-wrap');
  if (!wrap) throw new Error('the Meetings page has no content');
  wrap.querySelector('.bs-top')?.remove(); // the window has its own title bar
  wrap.querySelectorAll('script').forEach((s) => s.remove());
  wrap.querySelectorAll('[data-in-app]').forEach((n) => { n.hidden = false; });
  root.classList.add('fm-root', 'fm-meetings');
  root.replaceChildren(...[...wrap.childNodes].map((n) => document.importNode(n, true)));
  if (openConversations) {
    root.addEventListener('click', (e) => {
      const a = e.target.closest && e.target.closest('a[data-conversations]');
      if (!a || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      openConversations();
    });
  }
  mounted = root;
  // Both scripts look their elements up when they load, so they load after the markup.
  await import('./callRecorder.js');
  await import('./meetingBot.js');
  return { refresh() {} };
}
