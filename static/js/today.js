// Fork: the Today view — the stored daily insight (src/today_insight.py) with
// tickable to-dos, events that open the calendar on their day, 👍/👎 and Refresh.
// Mounted by the Today window in the main app (static/js/forkModuleWindows.js) and by
// the standalone page (static/today.html). Data: routes/today_insight_routes.py. To-dos
// are ticked with the notes API's own toggle route
// (POST /api/notes/<id>/items/<index>/toggle), like the notes page.
// Phase 3 (src/insight_outcomes.py): each row has "↑ first", "Not today" and "Why first?"
// (open loops also "Not a task");
// ticks, marks and undo are posted to POST /api/insight/today/outcome. The server already
// puts promoted rows first and leaves dismissed ones out (listed under `dismissed`).

// Phase 3: the weights table and "why is this first?" (todayWeights.js), weekly trends
// (todayTrends.js). Contract: docs/fork/today-insight.md.
import { mountWeights, renderWhy } from './todayWeights.js';
import { mountTrends } from './todayTrends.js';

let root = null; // one mount per document
const $ = (sel) => root.querySelector(sel);

const SECTIONS = [
  ['focus', 'Do first'],
  ['coming_up', 'Coming up'],
  ['also', 'Also worth a look'],
];

const EVENT_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" ' +
  'height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/>' +
  '<line x1="3" y1="10" x2="21" y2="10"/></svg>';

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'className') node.className = v;
    else if (k === 'html') node.innerHTML = v; // only for our own static SVG strings
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
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('not logged in');
  }
  let data = {};
  try {
    data = await res.json();
  } catch (e) {}
  if (!res.ok) {
    const err = new Error(data.detail || data.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function postJSON(path, body) {
  return api(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

function status(text, error = false) {
  $('.ti-main').replaceChildren(el('div', { className: error ? 'bs-status error' : 'bs-status' }, text));
}

// ref -> {kind, noteId, index} or {kind, uid}
function parseRef(ref) {
  const todo = /^todo:(.+):(\d+)$/.exec(ref || '');
  if (todo) return { kind: 'todo', noteId: todo[1], index: Number(todo[2]) };
  const ev = /^event:(.+)$/.exec(ref || '');
  if (ev) return { kind: 'event', uid: ev[1] };
  if (/^loop:./.test(ref || '')) return { kind: 'loop' };
  if (/^belief:./.test(ref || '')) return { kind: 'plan' };
  return { kind: 'other' };
}

// The day's outcomes ({ref: outcome}) from the last load.
let outcomes = {};

function postOutcome(ref, outcome, extra = {}) {
  return postJSON('/api/insight/today/outcome', { ref, outcome, ...extra });
}

// "Not a task" on an open loop: the module item was never a to-do (a quick errand the
// summary model picked up). It is dismissed in Athena, so it doesn't come back
// (routes/sound_module_routes.py -> update_action_item), and hidden today with
// not_task in the outcome log. Undo reopens it.
async function setLoopStatus(ref, status) {
  const id = /^loop:(\d+)$/.exec(ref || '');
  if (!id) throw new Error('not an open loop');
  const res = await postJSON(`/api/modules/sound/action-items/${id[1]}`, { status });
  if (!res.ok) throw new Error(res.message || 'the module said no');
}

function safeLink(href) {
  const text = String(href || '');
  // a site path, not "//host" or "/\host" (browsers read both as another host)
  return /^(https?:\/\/|\/(?![\/\\]))/i.test(text) ? text : null;
}

function eventHref(uid) {
  // /#event-<uid> opens the calendar on the event's day (static/js/forkEntityHash.js).
  // A recurring occurrence's uid is "<series uid>::<YYYY-MM-DD[THH:MM]>"; the calendar
  // resolves that to the series' first day, so link to the occurrence's date instead.
  const occurrence = /::(\d{4}-\d{2}-\d{2})/.exec(uid || '');
  if (occurrence) return `/#event-${occurrence[1]}`;
  return /^[A-Za-z0-9._:@-]{1,200}$/.test(uid) ? `/#event-${uid}` : '/';
}

function noteHref(id) {
  return /^[A-Za-z0-9-]{1,80}$/.test(id) ? `/#open=notes&note=${id}` : '/';
}

// A line's text as the insight titles it: whitespace collapsed, its first 200
// characters (the server cuts candidate titles there), lower case.
function lineKey(s) {
  return String(s || '').replace(/\s+/g, ' ').trim().slice(0, 200).trim().toLowerCase();
}

function sameText(a, b) {
  return lineKey(a) === lineKey(b);
}

// The note as it is now, so a tick finds the right line even if lines moved.
const notes = new Map();
async function loadNote(id) {
  if (!notes.has(id)) notes.set(id, api(`/api/notes/${encodeURIComponent(id)}`).catch(() => null));
  return notes.get(id);
}

function itemIndex(note, ref, title) {
  const items = (note && Array.isArray(note.items)) ? note.items : [];
  const at = items[ref.index];
  if (at && sameText(at.text, title)) return ref.index;
  const found = items.findIndex(it => it && sameText(it.text, title));
  return found >= 0 ? found : null;
}

function todoRow(row, ref) {
  const li = el('li', { className: 'ti-item' });
  const box = el('input', { type: 'checkbox', className: 'bs-tick', 'aria-label': `Done: ${row.title}`,
                            disabled: true });
  const msg = el('div', { className: 'bs-meta bs-msg' });
  const title = el('a', { className: 'bs-title', href: noteHref(ref.noteId) }, row.title || row.ref);
  li.append(box, el('div', { className: 'bs-body' }, title,
                     row.why ? el('div', { className: 'bs-why' }, row.why) : null, msg));
  let index = null;
  const show = (done) => {
    box.checked = !!done;
    li.classList.toggle('bs-done', !!done);
  };
  loadNote(ref.noteId).then(note => {
    index = itemIndex(note, ref, row.title);
    if (index === null) {
      msg.textContent = note ? 'No longer on this list.' : 'This to-do is gone.';
      return;
    }
    show(note.items[index].done);
    box.disabled = false;
  });
  box.addEventListener('change', async () => {
    const want = box.checked;
    show(want); // at once; the server answer confirms it
    box.disabled = true;
    msg.textContent = '';
    msg.classList.remove('error');
    try {
      const r = await api(`/api/notes/${encodeURIComponent(ref.noteId)}/items/${index}/toggle`,
                          { method: 'POST' });
      const items = Array.isArray(r.items) ? r.items : [];
      const done = items[index] ? !!items[index].done : want;
      show(done);
      notes.delete(ref.noteId);
      // the outcome log; the 15-min pass sees the tick anyway, so a failure is quiet
      postOutcome(row.ref, done ? 'done' : 'open').catch(() => {});
    } catch (e) {
      show(!want);
      msg.textContent = `Couldn't save: ${e.message}`;
      msg.classList.add('error');
    } finally {
      box.disabled = false;
    }
  });
  return li;
}

function eventRow(row, ref) {
  return el('li', { className: 'ti-item' },
    el('span', { className: 'bs-kind', 'aria-hidden': 'true', html: EVENT_ICON }),
    el('div', { className: 'bs-body' },
      el('a', { className: 'bs-title', href: eventHref(ref.uid), title: 'Open in the calendar' },
         row.title || row.ref),
      row.why ? el('div', { className: 'bs-why' }, row.why) : null));
}

function loopRow(row) {
  const href = safeLink(row.link);
  const title = href
    ? el('a', { className: 'bs-title', href, target: '_blank', rel: 'noopener noreferrer',
                title: 'Open the conversation' }, row.title || row.ref)
    : el('span', { className: 'bs-title' }, row.title || row.ref);
  return el('li', { className: 'ti-item' }, el('div', { className: 'bs-body' }, title,
    row.person ? el('div', { className: 'bs-meta' }, `with ${row.person}`) : null,
    row.why ? el('div', { className: 'bs-why' }, row.why) : null));
}

function planRow(row) {
  return el('li', { className: 'ti-item' }, el('div', { className: 'bs-body' },
    el('a', { className: 'bs-title', href: '/beliefs', title: 'Open in Beliefs' }, row.title || row.ref),
    row.why ? el('div', { className: 'bs-why' }, row.why) : null));
}

// "↑ first", "Not today" and "Why first?" under a row.
function rowActions(row) {
  const promoted = outcomes[row.ref] === 'promoted';
  const msg = el('span', { className: 'bs-meta bs-msg' });
  const why = el('div', { className: 'ti-why', hidden: true });
  const mark = async (outcome, notTask = false) => {
    msg.textContent = '';
    msg.classList.remove('error');
    try {
      if (notTask) await setLoopStatus(row.ref, 'dismissed');
      await postOutcome(row.ref, outcome, notTask ? { not_task: true } : {});
      await load();
    } catch (e) {
      msg.textContent = `Couldn't save: ${e.message}`;
      msg.classList.add('error');
    }
  };
  const showWhy = async () => {
    if (!why.hidden) {
      why.hidden = true;
      return;
    }
    why.hidden = false;
    why.replaceChildren(el('span', { className: 'bs-meta' }, 'Loading…'));
    try {
      const data = await api(`/api/insight/today/why?ref=${encodeURIComponent(row.ref)}`);
      why.replaceChildren();
      renderWhy(why, data);
    } catch (e) {
      why.replaceChildren(el('span', { className: 'bs-meta' }, e.status === 404
        ? 'No explanation for this item.' : `Couldn't load the explanation: ${e.message}`));
    }
  };
  return el('div', {},
    el('div', { className: 'bs-row ti-actions' },
      el('button', { type: 'button', className: 'bs-btn ti-act', 'aria-pressed': String(promoted),
                     title: promoted ? 'Undo: back to the model\'s order' : 'Show this first today',
                     onclick: () => mark(promoted ? 'clear' : 'promoted') }, '↑ first'),
      el('button', { type: 'button', className: 'bs-btn ti-act', title: 'Hide this for today',
                     onclick: () => mark('dismissed') }, 'Not today'),
      parseRef(row.ref).kind === 'loop'
        ? el('button', { type: 'button', className: 'bs-btn ti-act',
                         title: 'This was never a to-do: remove it for good',
                         onclick: () => mark('dismissed', true) }, 'Not a task')
        : null,
      el('button', { type: 'button', className: 'bs-btn ti-act', title: 'Why is this here?',
                     onclick: showWhy }, 'Why first?'),
      msg),
    why);
}

function itemRow(row) {
  const ref = parseRef(row.ref);
  let li;
  if (ref.kind === 'todo') li = todoRow(row, ref);
  else if (ref.kind === 'event') li = eventRow(row, ref);
  else if (ref.kind === 'loop') li = loopRow(row);
  else if (ref.kind === 'plan') li = planRow(row);
  else {
    li = el('li', { className: 'ti-item' }, el('div', { className: 'bs-body' },
      el('span', { className: 'bs-title' }, row.title || row.ref),
      row.why ? el('div', { className: 'bs-why' }, row.why) : null));
  }
  const body = li.querySelector('.bs-body');
  if (body && row.ref) body.append(rowActions(row));
  return li;
}

// "Not today (n)": the rows the owner hid today, collapsed, each with Undo.
function dismissedBlock(rows) {
  const list = el('ul', { className: 'bs-items' }, ...rows.map(row => {
    const msg = el('span', { className: 'bs-meta bs-msg' });
    const undo = async () => {
      try {
        if (row.not_task) await setLoopStatus(row.ref, 'open');
        await postOutcome(row.ref, 'clear');
        await load();
      } catch (e) {
        msg.textContent = `Couldn't save: ${e.message}`;
        msg.classList.add('error');
      }
    };
    return el('li', { className: 'ti-item' }, el('div', { className: 'bs-body' },
      el('span', { className: 'bs-title' }, row.title || 'An item hidden today'),
      row.not_task ? el('div', { className: 'bs-meta' }, 'Not a task') : null,
      el('div', { className: 'bs-row ti-actions' },
        el('button', { type: 'button', className: 'bs-btn ti-act', onclick: undo,
                       title: 'Show it again today' }, 'Undo'), msg)));
  }));
  return el('details', { className: 'ti-dismissed' },
    el('summary', { className: 'bs-section' }, `Not today (${rows.length})`),
    el('div', { className: 'bs-card' }, list));
}

function lastVote(votes) {
  return Array.isArray(votes) && votes.length ? votes[votes.length - 1].vote : null;
}

function voteRow(insight) {
  const msg = el('span', { className: 'bs-meta bs-msg' });
  const buttons = {};
  const mark = (vote) => {
    for (const [k, b] of Object.entries(buttons)) b.setAttribute('aria-pressed', String(k === vote));
  };
  const send = async (vote) => {
    msg.textContent = '';
    msg.classList.remove('error');
    try {
      const r = await postJSON('/api/insight/today/vote', { vote });
      mark(lastVote(r.votes));
    } catch (e) {
      msg.textContent = `Couldn't save the vote: ${e.message}`;
      msg.classList.add('error');
    }
  };
  buttons.up = el('button', { type: 'button', className: 'bs-btn', title: 'Helpful',
                              'aria-label': 'Helpful', onclick: () => send('up') }, '👍');
  buttons.down = el('button', { type: 'button', className: 'bs-btn', title: 'Not helpful',
                                'aria-label': 'Not helpful', onclick: () => send('down') }, '👎');
  mark(lastVote(insight.votes));
  return el('div', { className: 'bs-row' }, buttons.up, buttons.down, msg);
}

function generatedLabel(iso) {
  const d = new Date(iso || '');
  if (isNaN(d.getTime())) return '';
  return `Written ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
}

function render(insight) {
  notes.clear();
  outcomes = (insight && typeof insight.outcomes === 'object' && insight.outcomes) || {};
  const main = $('.ti-main');
  // The "nothing to report" headline is written without the model, so there is nothing to rate.
  const hasItems = SECTIONS.some(([key]) => Array.isArray(insight[key]) && insight[key].length);
  const parts = [el('div', { className: 'bs-card' },
    el('p', { className: 'bs-headline' }, insight.headline || 'Nothing to add for today.'),
    el('div', { className: 'bs-meta' }, [insight.date, generatedLabel(insight.generated)]
      .filter(Boolean).join(' · ')),
    hasItems ? voteRow(insight) : null)];
  for (const [key, label] of SECTIONS) {
    const rows = Array.isArray(insight[key]) ? insight[key] : [];
    if (!rows.length) continue;
    parts.push(el('div', { className: 'bs-section' }, label),
               el('div', { className: 'bs-card' }, el('ul', { className: 'bs-items' },
                 ...rows.map(itemRow))));
  }
  const dismissed = Array.isArray(insight.dismissed) ? insight.dismissed.filter(r => r && r.ref) : [];
  if (dismissed.length) parts.push(dismissedBlock(dismissed));
  main.replaceChildren(...parts);
}

async function load() {
  try {
    render(await api('/api/insight/today'));
  } catch (e) {
    if (e.status === 404) status('No insight for today yet. It is written every 15 minutes; Refresh writes it now.');
    else status(`Couldn't load today's insight: ${e.message}`, true);
  }
}

const POLL_MS = 4000;
const POLL_LIMIT_MS = 15 * 60 * 1000;

function note(text, error = false) {
  const n = $('.ti-note');
  n.textContent = text || '';
  n.className = error ? 'bs-status error' : 'bs-status';
  n.hidden = !text;
}

// Today's insight, or the 404 body ({error, refreshing, refresh_error}) when there is none.
async function fetchToday() {
  try {
    return await api('/api/insight/today');
  } catch (e) {
    if (e.status === 404) return { missing: true, ...(e.data || {}) };
    throw e;
  }
}

// Refresh runs in the background (a model call outlasts a request); poll until the
// insight's `generated` changes or the server says it stopped.
async function refresh() {
  const btn = $('.ti-refresh');
  btn.disabled = true;
  btn.textContent = 'Writing…';
  note('Writing a new insight. The local model can take a few minutes.');
  try {
    const started = await postJSON('/api/insight/refresh');
    const before = started.generated || null;
    const end = Date.now() + POLL_LIMIT_MS;
    while (Date.now() < end) {
      await new Promise(resolve => setTimeout(resolve, POLL_MS));
      const now = await fetchToday();
      if (!now.missing && now.generated && now.generated !== before) {
        note('');
        render(now);
        return;
      }
      if (!now.refreshing) {
        note(now.refresh_error || 'Nothing new was written.', !!now.refresh_error);
        return;
      }
    }
    note('Still writing; press Refresh again later to see it.');
  } catch (e) {
    note(`Couldn't write the insight: ${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Refresh';
  }
}

/** Builds the view into `el` and loads it. Returns { refresh } (reloads the stored insight). */
export function mountToday(target) {
  if (root) throw new Error('the Today view is already mounted');
  root = target;
  root.classList.add('fm-root');
  const button = el('button', { type: 'button', className: 'bs-btn ti-refresh',
                                title: 'Write a new insight now' }, 'Refresh');
  root.replaceChildren(
    el('div', { className: 'bs-row', style: 'justify-content:flex-end;margin:0 0 8px' }, button),
    el('div', { className: 'bs-status ti-note', hidden: true }),
    el('div', { className: 'ti-main' }, el('div', { className: 'bs-status' }, 'Loading…')),
    el('div', { className: 'ti-weights' }),
    el('div', { className: 'ti-trends' }));
  button.addEventListener('click', refresh);
  mountWeights($('.ti-weights'));
  mountTrends($('.ti-trends'));
  load();
  return { refresh: load };
}
