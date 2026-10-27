// Fork: the Beliefs window (static/js/forkModuleWindows.js), "What I know about you".
// The view comes from the standalone page (static/beliefs.html, served at /beliefs) through
// static/js/forkPageView.js, with the page's own styles scoped to the window, then the
// page's script (static/js/beliefs.js) runs as it does there.

import { mountPageView } from './forkPageView.js';

const PAGE = '/beliefs';

let view = null;

/** Builds the Beliefs view into `root`. Returns { refresh }. */
export async function mountBeliefs(root) {
  if (view) throw new Error('the Beliefs view is already mounted');
  view = { refresh() {} }; // "Sync now" in the view reloads it
  try {
    await mountPageView(root, {
      page: PAGE,
      className: 'fm-beliefs',
      scopeStyles: true,
      pick(doc) {
        const top = doc.querySelector('.bs-wrap .bs-top');
        if (!top) return null;
        // The window has its own title bar: keep only the Download and Sync buttons.
        top.querySelector('a[href="/"]')?.remove();
        const title = top.querySelector('h1');
        if (title) title.replaceChildren();
        return [...doc.body.childNodes]; // the view and its toast
      },
      scripts: ['./beliefs.js'],
    });
  } catch (e) {
    view = null;
    throw e;
  }
  return view;
}
