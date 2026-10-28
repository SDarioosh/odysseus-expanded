// Fork: the Meetings window (static/js/forkModuleWindows.js): "Send Athena to a meeting"
// (static/js/meetingBot.js) and "Record it on this computer" (static/js/callRecorder.js).
// The cards come from the standalone Meetings page (static/call-recorder.html, served at
// /modules/sound/record) through static/js/forkPageView.js; their look is in
// static/css/fork-modules.css.
//
// Closing the window only hides it (forkModuleWindows.js keeps the view), so a recording
// or a meeting goes on while the owner uses the chat.

import { mountPageView } from './forkPageView.js';

const PAGE = '/modules/sound/record';

let mounted = false;

/**
 * Builds the Meetings view into `root`.
 * - openConversations(): opens the Records window (the page links to /modules/sound).
 * Returns { refresh }.
 */
export async function mountMeetings(root, { openConversations = null } = {}) {
  if (mounted) throw new Error('the Meetings view is already mounted');
  mounted = true;
  try {
    await mountPageView(root, {
      page: PAGE,
      className: 'fm-root fm-meetings',
      pick(doc) {
        const wrap = doc.querySelector('.bs-wrap');
        if (!wrap) return null;
        wrap.querySelector('.bs-top')?.remove(); // the window has its own title bar
        wrap.querySelectorAll('[data-in-app]').forEach((n) => { n.hidden = false; });
        return [...wrap.childNodes];
      },
      scripts: ['./callRecorder.js', './meetingBot.js'],
    });
  } catch (e) {
    mounted = false;
    throw e;
  }
  if (openConversations) {
    root.addEventListener('click', (e) => {
      const a = e.target.closest && e.target.closest('a[data-conversations]');
      if (!a || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      openConversations();
    });
  }
  return { refresh() {} };
}
