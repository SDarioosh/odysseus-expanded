// Fork: weekly vote and outcome counts for the Today insight (src/insight_trends.py).
// A collapsed "Last weeks" panel under "How Today ranks"; loads on first open.
// Route: routes/insight_trends_routes.py (GET /api/insight/trends?weeks=8).

const WEEKS = 8;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'className') node.className = v;
    else if (k === 'style') node.style.cssText = v;
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children) {
    if (c === null || c === undefined) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function weekLabel(w) {
  const [, m, d] = String(w.start || '').split('-').map(Number);
  return m && d ? `${MONTHS[m - 1]} ${d}` : String(w.week || '');
}

const COLS = [
  ['shown', 'Shown', 'Items the insight showed'],
  ['done', 'Done', 'Ticked off'],
  ['promoted', '↑ first', 'You moved it up'],
  ['dismissed', 'Not today', 'You skipped it for the day'],
  ['moved', 'Moved', 'Moved to a later day'],
  ['ignored', 'Ignored', 'Still open at the end of the day'],
];

function bar(w) {
  const shown = w.shown || 0;
  const pct = shown ? Math.round((100 * (w.done || 0)) / shown) : 0;
  return el('span', { className: 'tt-bar', title: shown ? `${pct}% done` : 'Nothing shown' },
            el('span', { className: 'tt-fill', style: `width: ${pct}%` }));
}

function table(data) {
  const weeks = (data && data.weeks) || [];
  const any = weeks.some((w) => w.shown || w.insights || w.votes_up || w.votes_down);
  if (!any) {
    return el('div', { className: 'bs-empty' },
              'Nothing to show yet: each week fills in as Today shows you items.');
  }
  const head = el('tr', {},
    el('th', { scope: 'col' }, 'Week of'),
    ...COLS.map(([, h, t]) => el('th', { scope: 'col', className: 'tw-num', title: t }, h)),
    el('th', { scope: 'col', className: 'tw-num', title: 'Votes on the insight' }, '👍/👎'),
    el('th', { scope: 'col', title: 'Done of shown' }, 'Done'));
  const body = weeks.map((w) => el('tr', { className: w.shown ? null : 'tt-quiet' },
    el('td', { title: `${w.week}, ${w.insights || 0} day(s) with an insight` }, weekLabel(w)),
    ...COLS.map(([k]) => el('td', { className: 'tw-num' }, w[k] || 0)),
    el('td', { className: 'tw-num' }, `${w.votes_up || 0}/${w.votes_down || 0}`),
    el('td', {}, bar(w))));
  return el('table', { className: 'tw-table tt-table' }, el('thead', {}, head),
            el('tbody', {}, ...body));
}

/** Builds the collapsed weekly trends panel into `container`. */
export function mountTrends(container) {
  if (!container) return;
  const holder = el('div', { className: 'tw-scroll' },
                    el('div', { className: 'bs-status' }, 'Loading…'));
  let loaded = false;

  async function load() {
    try {
      const res = await fetch(`/api/insight/trends?weeks=${WEEKS}`, { credentials: 'same-origin' });
      let data = {};
      try {
        data = await res.json();
      } catch (e) {}
      if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
      holder.replaceChildren(table(data));
    } catch (e) {
      holder.replaceChildren(el('div', { className: 'bs-status error' },
                                `Couldn't load the last weeks: ${e.message}`));
    }
  }

  const details = el('details', { className: 'tw-panel tt-panel' },
    el('summary', { className: 'bs-section' }, 'Last weeks'),
    el('p', { className: 'bs-meta tw-rules' },
       'What Today showed you each week and what became of it, newest first.'),
    holder);
  details.addEventListener('toggle', () => {
    if (details.open && !loaded) {
      loaded = true;
      load();
    }
  });
  container.replaceChildren(details);
}
