// Fork: how the Today insight ranks (src/insight_weights.py): the weights table with
// Reset, and the answer to "why is this first?". Mounted by today.js.
// Routes: routes/insight_weights_routes.py (GET /api/insight/weights,
// POST /api/insight/weights/reset, GET /api/insight/today/why).

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'className') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children) {
    if (c === null || c === undefined) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: 'same-origin', ...options });
  let data = {};
  try {
    data = await res.json();
  } catch (e) {}
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}

const signed = (n) => (n > 0 ? `+${n}` : String(n || 0));

const RULES = 'Overdue and due-today items always come first, then the next 7 days, ' +
  'pinned notes, undated and later items; these tiers never change. Within a tier a ' +
  'higher weight goes first. A weight starts at 0 and only moves once that kind of item ' +
  'was shown 6 times in 4 weeks.';
const EMPTY = 'No weights yet: Today learns from what you tick, move up or skip.';

function table(data, onReset) {
  const rows = (data && data.rows) || [];
  if (!rows.length) return el('div', { className: 'bs-empty' }, EMPTY);
  const head = el('tr', {}, ...['Kind', 'Weight', 'Shown', 'Done', 'Moved up', 'Skipped', '']
    .map((h, i) => el('th', { scope: 'col', className: i >= 1 && i <= 5 ? 'tw-num' : null }, h)));
  const body = rows.map((r) => el('tr', {},
    el('td', { className: 'tw-label', title: r.key }, r.label || r.key,
       r.reset_at ? el('span', { className: 'bs-meta' }, ` reset ${r.reset_at}`) : null),
    el('td', { className: 'tw-num' }, signed(r.weight)),
    el('td', { className: 'tw-num' }, r.shown),
    el('td', { className: 'tw-num' }, r.done),
    el('td', { className: 'tw-num' }, r.promoted),
    el('td', { className: 'tw-num' }, r.dismissed),
    el('td', {}, el('button', {
      type: 'button', className: 'bs-btn', title: `Forget what Today learnt for ${r.label}`,
      onclick: () => onReset(r.key),
    }, 'Reset'))));
  return el('table', { className: 'tw-table' }, el('thead', {}, head), el('tbody', {}, ...body));
}

/** Builds the collapsed "How Today ranks" table into `container`. */
export function mountWeights(container) {
  if (!container) return;
  const msg = el('div', { className: 'bs-meta bs-msg' });
  const holder = el('div', { className: 'tw-scroll' }, el('div', { className: 'bs-status' }, 'Loading…'));
  const resetAll = el('button', { type: 'button', className: 'bs-btn', hidden: true }, 'Reset all');
  let loaded = false;

  function show(data) {
    holder.replaceChildren(table(data, (key) => reset({ key })));
    resetAll.hidden = !((data && data.rows) || []).length;
  }

  async function load() {
    try {
      show(await api('/api/insight/weights'));
      msg.textContent = '';
    } catch (e) {
      holder.replaceChildren(el('div', { className: 'bs-status error' },
                                `Couldn't load the weights: ${e.message}`));
    }
  }

  async function reset(body) {
    if (!body.key && !window.confirm('Reset every weight? Today starts learning again.')) return;
    try {
      show(await api('/api/insight/weights/reset', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }));
      msg.className = 'bs-meta bs-msg';
      msg.textContent = body.key ? 'Reset. It counts again from tomorrow.' : 'All weights reset.';
    } catch (e) {
      msg.className = 'bs-meta bs-msg error';
      msg.textContent = `Couldn't reset: ${e.message}`;
    }
  }

  resetAll.addEventListener('click', () => reset({}));
  const details = el('details', { className: 'tw-panel' },
    el('summary', { className: 'bs-section' }, 'How Today ranks'),
    el('p', { className: 'bs-meta tw-rules' }, RULES),
    holder,
    el('div', { className: 'bs-row' }, resetAll, msg));
  details.addEventListener('toggle', () => {
    if (details.open && !loaded) {
      loaded = true;
      load();
    }
  });
  container.replaceChildren(details);
}

/** Renders a `GET /api/insight/today/why` answer into `container`. */
export function renderWhy(container, data) {
  if (!container) return;
  const parts = [el('div', { className: 'bs-why' }, (data && data.text) || '')];
  const rows = ((data && data.contributions) || []).filter((c) => c.weight || c.shown);
  if (rows.length) {
    parts.push(el('ul', { className: 'bs-list tw-why' }, ...rows.map((c) => el('li', {},
      `${c.label || c.key}: ${signed(c.weight)}`,
      el('span', { className: 'bs-meta' },
         c.weight || c.shown >= 6
           ? ` (shown ${c.shown}, done ${c.done}, moved up ${c.promoted}, skipped ${c.dismissed})`
           : ` (shown ${c.shown} of the 6 it needs)`)))));
  }
  container.replaceChildren(...parts);
}
