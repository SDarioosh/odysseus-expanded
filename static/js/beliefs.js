// Fork: the belief review page, "What I know about you" — the belief ledger
// (src/evidence_intake.py) by status or by subject, each belief with its sources and
// links, and the owner's buttons: confirm, edit (correct), dismiss, forget, restore. Subjects
// (the owner, people, projects, things) can be renamed, given a kind, linked to an
// Odysseus contact and merged; a subject opens to its summary and its beliefs grouped
// by attribute. Data: routes/beliefs_routes.py. Forget erases the belief for good
// (src/belief_forget.py): Athena erases the facts behind it, the toast says what
// the module did and where the words still are (a note). Dismiss only rejects it (the
// module retires the facts, transcripts keep their words; Restore undoes it). Download: GET
// /api/beliefs/export. The search
// box uses GET /api/beliefs/search (routes/belief_search_routes.py) and falls back to a
// plain text match when that route isn't installed (404).
// Athena's facts (MCP 0.11.0) carry a kind, the transcript line they were said on and
// their conversation in the evidence payload (src/module_facts.py): a card shows the kind
// and the line, and the Tentative tab groups beliefs by conversation, each group with
// "Dismiss all".

const $ = (sel) => document.querySelector(sel);

const TABS = [
  ['tentative', 'Tentative'],
  ['confirmed', 'Confirmed'],
  ['stale', 'Stale'],
  ['rejected', 'Rejected'],
];

const EMPTY = {
  tentative: 'Nothing tentative. Beliefs arrive from your recorded conversations and, when enabled, your notes.',
  confirmed: 'Nothing confirmed yet. Confirm a tentative belief here, or it confirms itself once it is heard often enough.',
  stale: 'Nothing stale. A belief goes stale when no source reports it any more or it has not been heard for a long time.',
  rejected: 'Nothing dismissed or forgotten.',
};

const CONFIRMED_BY = {
  owner: 'Confirmed by you',
  source: 'Confirmed by its source',
  rule: 'Confirmed by the rule',
};

const SOURCE_NAMES = { 'odysseus-sound': 'Conversations', notes: 'Notes', routines: 'Calendar' };
const MODULE_SOURCE = 'odysseus-sound';

// Athena's fact kinds (src/module_facts.py FACT_KINDS); "other" shows the category.
const FACT_KINDS = {
  preference: 'Preference', plan: 'Plan', decision: 'Decision',
  relationship: 'Relationship', personal: 'Personal',
};

// The ledger's attribute vocabulary (evidence_intake.ATTRIBUTES), in its order.
const ATTRIBUTES = [
  ['lives_in', 'Lives in'], ['works_at', 'Works at'], ['role', 'Role'],
  ['birthday', 'Birthday'], ['allergy', 'Allergy'], ['diet', 'Diet'], ['likes', 'Likes'],
  ['dislikes', 'Dislikes'], ['relationship', 'Relationship'], ['owns', 'Owns'],
  ['pet', 'Pet'], ['plans', 'Plans'], ['decision', 'Decision'], ['routine', 'Routine'],
  ['contact_info', 'Contact info'], ['health', 'Health'],
  ['project_status', 'Project status'], ['deadline', 'Deadline'], ['other', 'Other'],
];
const ATTRIBUTE_LABEL = Object.fromEntries(ATTRIBUTES);

const KINDS = { owner: 'You', person: 'Person', project: 'Project', thing: 'Thing' };
const KIND_FILTERS = [
  ['', 'All kinds'], ['person', 'People'], ['project', 'Projects'], ['thing', 'Things'],
  ['owner', 'You'],
];

const svg = (body) => '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ' +
  `aria-hidden="true">${body}</svg>`;

const ICONS = {
  confirm: svg('<polyline points="20 6 9 17 4 12"/>'),
  edit: svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>'),
  forget: svg('<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>' +
              '<path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>'),
  dismiss: svg('<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/>' +
               '<line x1="9" y1="9" x2="15" y2="15"/>'),
  restore: svg('<polyline points="1 4 1 10 7 10"/><path d="M3.5 15a9 9 0 1 0 2.1-9.4L1 10"/>'),
  link: svg('<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>' +
            '<polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>'),
  chain: svg('<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>' +
             '<path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'),
  unlink: svg('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>'),
  merge: svg('<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/>'),
  open: svg('<polyline points="9 18 15 12 9 6"/>'),
  back: svg('<polyline points="15 18 9 12 15 6"/>'),
  kind_owner: svg('<circle cx="12" cy="12" r="10"/><circle cx="12" cy="10" r="3"/>' +
                  '<path d="M7 20.66V19a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v1.66"/>'),
  kind_person: svg('<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>'),
  kind_project: svg('<rect x="2" y="7" width="20" height="14" rx="2"/>' +
                    '<path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>'),
  kind_thing: svg('<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>' +
                  '<polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>'),
};

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
    if (c === null || c === undefined || c === false) continue;
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

// ------------------------------------------------------------------ state
const state = {
  view: 'status', // 'status' or 'subject'
  tab: 'tentative', beliefs: [], stats: null, filter: '', attribute: '',
  kind: '', subjects: [], open: null, detail: null,
  contacts: null, contactsFailed: false,
  query: '', hits: [], searchFallback: false, noSearchRoute: false,
};
const TAB_KEY = 'fork-beliefs-tab';
const VIEW_KEY = 'fork-beliefs-view';

try {
  const saved = sessionStorage.getItem(TAB_KEY);
  if (TABS.some(([key]) => key === saved)) state.tab = saved;
  const view = sessionStorage.getItem(VIEW_KEY);
  if (view === 'status' || view === 'subject') state.view = view;
} catch (e) { /* storage blocked: start on Tentative, by status */ }

let toastTimer = null;
function toast(text, error = false) {
  const t = $('#bl-toast');
  t.textContent = text;
  t.className = error ? 'bs-toast error' : 'bs-toast';
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, error ? 8000 : 5000);
}

function status(text, error = false) {
  $('#bl-main').replaceChildren(el('div', { className: error ? 'bs-status error' : 'bs-status' }, text));
}

// ------------------------------------------------------------------ formatting
function day(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  if (!m) return '';
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

// Only web links and links into this app open; anything else is shown as text.
function safeHref(link) {
  const s = String(link || '').trim();
  if (/^https?:\/\//i.test(s)) return s;
  if (s.startsWith('/') && !s.startsWith('//')) return s;
  return null;
}

function kindIcon(kind) {
  const label = KINDS[kind] || 'Subject';
  return el('span', { className: 'bs-kind', title: label, role: 'img', 'aria-label': label,
                      html: ICONS[`kind_${kind}`] || ICONS.kind_thing });
}

function kindChip(kind) {
  return el('span', { className: 'bs-chip bs-kind' },
    el('span', { html: ICONS[`kind_${kind}`] || ICONS.kind_thing }), KINDS[kind] || kind);
}

function attributeChip(b) {
  if (!b.attribute || !b.value) return null;
  return el('span', { className: 'bs-attr', title: 'What this belief says, structured' },
    el('b', {}, `${ATTRIBUTE_LABEL[b.attribute] || b.attribute}:`), b.value,
    b.valid_from ? ` (since ${day(b.valid_from)})` : null);
}

function subjectName(id) {
  const s = state.subjects.find((x) => x.id === id) ||
    (state.detail && state.detail.subject && state.detail.subject.id === id ? state.detail.subject : null);
  return s ? s.display : 'another subject';
}

function contactName(id) {
  const c = (state.contacts || []).find((x) => x.id === id);
  return c ? (c.name || c.id) : null;
}

// ------------------------------------------------------------------ header
function renderControls() {
  const s = state.stats || {};
  $('#bl-counts').textContent = TABS.map(([key, label]) => `${s[key] || 0} ${label.toLowerCase()}`)
    .join(' · ');
  for (const b of document.querySelectorAll('.bs-view')) {
    b.setAttribute('aria-pressed', String(b.dataset.view === state.view));
  }
  const searching = Boolean(state.query.trim());
  const byStatus = state.view === 'status';
  const tabs = $('#bl-tabs');
  tabs.hidden = !byStatus || searching;
  tabs.replaceChildren(...TABS.map(([key, label]) => el('button', {
    type: 'button', role: 'tab', className: 'bs-btn bs-tab', id: `bl-tab-${key}`,
    'aria-selected': String(key === state.tab),
    onclick: () => selectTab(key),
  }, label, ' ', el('span', { className: 'bs-count' }, String(s[key] || 0)))));
  const filter = $('#bl-filter');
  filter.hidden = searching || state.open !== null;
  const hint = byStatus ? 'Filter by subject' : 'Filter subjects by name';
  filter.placeholder = hint;
  filter.setAttribute('aria-label', hint);
  $('#bl-attribute').hidden = !byStatus || searching;
  $('#bl-kind').hidden = byStatus || searching || state.open !== null;
}

function selectTab(key) {
  if (key === state.tab) return;
  state.tab = key;
  try { sessionStorage.setItem(TAB_KEY, key); } catch (e) {}
  load();
}

function selectView(view) {
  if (view === state.view && state.open === null) return;
  state.view = view;
  state.open = null;
  state.detail = null;
  try { sessionStorage.setItem(VIEW_KEY, view); } catch (e) {}
  load();
}

// ------------------------------------------------------------------ belief cards
function statusChip(b) {
  if (b.status === 'confirmed') {
    return el('span', { className: 'bs-chip confirmed' },
      CONFIRMED_BY[b.confirmed_by] || 'Confirmed');
  }
  if (b.erased_at) return el('span', { className: 'bs-chip rejected' }, 'Erased');
  const label = { tentative: 'Tentative', stale: 'Stale', rejected: 'Dismissed' }[b.status];
  return el('span', { className: `bs-chip ${b.status}` }, label || b.status);
}

// What an erased belief's evidence says about its source (src/evidence_intake.py).
const ERASE_STATES = {
  done: 'erased there',
  retired: 'hidden there; its words stay until Athena is updated',
  pending: 'not erased there yet (retried hourly)',
  source: 'still written there: edit it yourself',
};

// The first payload value ``key`` among a belief's evidence (live evidence first).
function evidenceValue(b, key) {
  const evidence = Array.isArray(b.evidence) ? b.evidence : [];
  const live = evidence.filter((e) => !e.gone).concat(evidence.filter((e) => e.gone));
  for (const e of live) {
    const v = (e.payload || {})[key];
    if (v) return v;
  }
  return null;
}

// The kind label Athena gave the fact ("other" reads "fact"), else the memory category.
function kindLabel(b) {
  const kind = evidenceValue(b, 'kind');
  if (kind) return FACT_KINDS[kind] || 'fact';
  return b.category || null;
}

// The kind label, unless the attribute chip already says it ("Decision: …" + "Decision").
function cardKindLabel(b) {
  const label = kindLabel(b);
  if (!label || !b.attribute || !b.value) return label;
  const attr = (ATTRIBUTE_LABEL[b.attribute] || b.attribute).toLowerCase().replace(/s$/, '');
  return attr === label.toLowerCase().replace(/s$/, '') ? null : label;
}

// Beliefs a "Dismiss all" is working on: their buttons stay off, also after a re-render.
const busy = new Set();

function sourceRow(e) {
  const name = SOURCE_NAMES[e.source] || e.source;
  const said = `${e.times || 1}× · ${plural(e.days || 1, 'day')}`;
  const when = [day(e.seen_first), day(e.seen_last)].filter(Boolean);
  const span = when.length === 2 && when[0] !== when[1] ? `${when[0]} – ${when[1]}` : (when[0] || '');
  const href = safeHref(e.link);
  return el('li', {},
    el('span', { className: 'bs-src', title: e.source }, name),
    el('span', {}, said),
    span ? el('span', {}, span) : null,
    e.gone ? el('span', { className: 'bs-gone' }, 'no longer reported') : null,
    ERASE_STATES[(e.payload || {}).erase]
      ? el('span', { className: 'bs-gone' }, ERASE_STATES[e.payload.erase]) : null,
    href ? el('a', { href, target: '_blank', rel: 'noopener noreferrer',
                     title: 'Open where it came from (new tab)' },
              el('span', { html: ICONS.link }), 'Open') : null,
    (e.payload || {}).quote
      ? el('q', { className: 'bs-said', title: 'What was said' }, e.payload.quote) : null);
}

function button(icon, label, onclick, title) {
  return el('button', { type: 'button', className: 'bs-btn', title: title || label, onclick },
    el('span', { html: ICONS[icon] }), label);
}

// Disables the card's controls while saving, then reloads the current view.
async function act(card, path, body, done) {
  const live = [...card.querySelectorAll('button, select, input')].filter((b) => !b.disabled);
  for (const b of live) b.disabled = true;
  try {
    const result = await postJSON(path, body);
    if (done) done(result);
    await load(true);
  } catch (e) {
    for (const b of live) b.disabled = false;
    toast(`Couldn't save: ${e.message}`, true);
  }
}

function forgetMessage(r) {
  const p = r.propagated || {};
  const e = r.erased || {};
  const erased = p.erased || 0;
  const retired = p.retired || 0;
  const missed = (p.failed || 0) + (p.unsupported || 0);
  const parts = ['Forgotten and erased here.'];
  let warn = false;
  if (erased) parts.push(`Athena erased ${plural(erased, 'fact')} from its transcripts.`);
  if (retired) {
    parts.push(`Athena is older: ${plural(retired, 'fact')} hidden there, the words ` +
               'stay in its transcripts until it is updated.');
    warn = true;
  }
  if (missed) {
    parts.push(`Athena could not be reached for ${plural(missed, 'fact')}; ` +
               'the hourly facts job tries again.');
    warn = true;
  }
  const remaining = (e.remaining || []).length;
  if (remaining) {
    parts.push(`It is still written in ${plural(remaining, 'place')} you keep yourself ` +
               '(see the card); edit it there.');
    warn = true;
  }
  return [parts.join(' '), warn];
}

const FORGET_WARNING = 'Forget this for good? It leaves memory and is erased everywhere ' +
  'Odysseus and Athena keep it: the transcript lines that said it read ' +
  '"[forgotten]", summaries are rewritten without it. This cannot be undone.';

function forget(b, card) {
  if (!window.confirm(FORGET_WARNING)) return;
  act(card, `/api/beliefs/${b.id}/forget`, {}, (r) => {
    const [text, warn] = forgetMessage(r);
    toast(text, warn);
  });
}

function dismissMessage(r) {
  const p = r.propagated || {};
  const retired = p.retired || 0;
  const missed = (p.failed || 0) + (p.unsupported || 0);
  const parts = ['Dismissed: out of memory, transcripts untouched. Restore it from the Rejected tab.'];
  if (retired) parts.push(`Athena won't offer ${retired === 1 ? 'it' : 'them'} again.`);
  if (missed) {
    parts.push(`Athena could not be reached for ${plural(missed, 'fact')}; ` +
               'if it comes back it stays dismissed.');
  }
  return [parts.join(' '), missed > 0];
}

function dismiss(b, card) {
  act(card, `/api/beliefs/${b.id}/dismiss`, {}, (r) => {
    const [text, warn] = dismissMessage(r);
    toast(text, warn);
  });
}

function editMode(b, card, textNode) {
  const input = el('input', { type: 'text', className: 'bs-filter', value: b.shown_text,
                              'aria-label': 'Corrected text', maxlength: '500' });
  const save = () => {
    const text = input.value.trim();
    if (!text) {
      toast('A correction needs text.', true);
      return;
    }
    act(card, `/api/beliefs/${b.id}/correct`, { text }, () => toast('Saved; memory shows your text.'));
  };
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') save();
    if (ev.key === 'Escape') render();
  });
  const form = el('div', { className: 'bs-edit' }, input,
    el('button', { type: 'button', className: 'bs-btn', onclick: save }, 'Save'),
    el('button', { type: 'button', className: 'bs-btn', onclick: () => render() }, 'Cancel'));
  textNode.replaceWith(form);
  card.querySelector('.bs-actions').hidden = true;
  input.focus();
  input.select();
}

// What the module did with the line behind a moved belief (src/belief_move.py).
function moveMessage(r, display) {
  const l = r.lines;
  const parts = [`Moved to ${display}; confirmed by you.`];
  if (!l) return [parts.join(' '), false];
  if (l.reassigned) {
    parts.push(`Athena gave ${plural(l.reassigned, 'line')} to ${display} and ` +
               'is redoing that conversation\'s summary.');
  }
  if (l.kept) parts.push(`${plural(l.kept, 'line')} left as is (someone else said it).`);
  const missed = (l.failed || 0) + (l.unsupported || 0) + (l.not_found || 0);
  if (missed) {
    parts.push(`${plural(missed, 'line')} could not be fixed in Athena; ` +
               'open the transcript and ask in chat to reassign it.');
  }
  return [parts.join(' '), missed > 0];
}

async function movePanel(b, card) {
  const panel = togglePanel(card, 'move');
  if (!panel) return;
  panel.append(el('div', { className: 'bs-status' }, 'Loading subjects…'));
  let all;
  try {
    const data = await api('/api/beliefs/subjects');
    all = Array.isArray(data.subjects) ? data.subjects : [];
  } catch (e) {
    panel.replaceChildren(el('div', { className: 'bs-status error' },
      `Couldn't load your subjects: ${e.message}`), closeButton(panel));
    return;
  }
  const others = all.filter((o) => o.id !== b.subject_id && (o.kind === 'person' || o.kind === 'owner'));
  const select = el('select', { className: 'bs-select', 'aria-label': 'Move to' },
    el('option', { value: '' }, others.length ? 'Move to…' : 'No other people yet'),
    ...others.map((o) => el('option', { value: String(o.id) }, o.display)));
  const input = el('input', { type: 'text', className: 'bs-filter', placeholder: 'or a new name',
                              'aria-label': 'New person', maxlength: '120' });
  const fromSound = (b.evidence || []).some((e) => e.source === 'odysseus-sound' && e.link);
  const fix = el('input', { type: 'checkbox', checked: true, id: `fix-${b.id}` });
  const go = () => {
    const name = input.value.trim();
    const target = others.find((o) => String(o.id) === select.value);
    if (!name && !target) {
      toast('Pick a person or type a name.', true);
      return;
    }
    const body = name ? { name } : { subject_id: target.id };
    body.fix_line = fromSound && fix.checked;
    act(card, `/api/beliefs/${b.id}/move`, body, (r) => {
      const [text, warn] = moveMessage(r, r.subject_display || name || target.display);
      toast(text, warn);
    });
  };
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') go();
    if (ev.key === 'Escape') panel.remove();
  });
  panel.replaceChildren(
    el('p', { className: 'bs-meta' }, 'Who is this really about? The belief moves there.'),
    el('div', { className: 'bs-row' }, select, input, button('kind_person', 'Move', go), closeButton(panel)),
    fromSound ? el('label', { className: 'bs-meta', for: `fix-${b.id}` }, fix,
      ' They said it: give the line to them in the transcript too') : null);
  select.focus();
}

// The subject's name on a card; it opens the subject, like a section head does.
function subjectLabel(b) {
  const name = b.subject_display || b.subject || 'No subject';
  if (b.subject_id === null || b.subject_id === undefined) return el('b', { className: 'bs-who' }, name);
  return el('button', { type: 'button', className: 'bs-who', title: 'Open this subject',
                        onclick: () => openSubject(b.subject_id) }, name);
}

function actions(b, card, textNode) {
  const row = actionButtons(b, card, textNode);
  if (busy.has(b.id)) for (const x of row.querySelectorAll('button')) x.disabled = true;
  return row;
}

function actionButtons(b, card, textNode) {
  const row = el('div', { className: 'bs-row bs-actions' });
  const confirm = (label) => button('confirm', label,
    () => act(card, `/api/beliefs/${b.id}/confirm`, {}, () => toast('Confirmed; it is in memory now.')));
  const edit = () => button('edit', 'Edit', () => editMode(b, card, textNode));
  const forgetBtn = () => button('forget', 'Forget', () => forget(b, card),
    'Erase it everywhere, including the transcript lines that said it (cannot be undone)');
  const dismissBtn = () => button('dismiss', 'Dismiss', () => dismiss(b, card),
    'Not worth remembering: out of memory, transcripts untouched (can be restored)');
  const moveBtn = () => button('kind_person', 'Move', () => movePanel(b, card),
    'It is about someone else (for example a line heard as the wrong voice)');
  if (b.status === 'tentative') row.append(confirm('Confirm'), edit(), moveBtn(), dismissBtn(), forgetBtn());
  else if (b.status === 'confirmed') row.append(edit(), moveBtn(), dismissBtn(), forgetBtn());
  else if (b.status === 'stale') {
    row.append(button('confirm', 'Confirm',
      () => act(card, `/api/beliefs/${b.id}/confirm`, {}, () => toast('Restored and confirmed.')),
      'Restore and keep it for good'), dismissBtn(), forgetBtn());
  } else if (b.status === 'rejected' && !b.erased_at) {
    // dismissed (or forgotten before erasing existed): it can be restored, or erased now
    row.append(button('restore', 'Restore',
      () => act(card, `/api/beliefs/${b.id}/restore`, {}, () => toast('Restored as tentative.')),
      'Back to tentative; it is confirmed again once it is heard often enough'),
    button('forget', 'Erase', () => forget(b, card),
      'Erase its words everywhere (cannot be undone)'));
  }
  return row;
}

function beliefCard(b, why, showSubject = false) {
  const card = el('div', { className: 'bs-card', id: `belief-${b.id}` });
  const textNode = el('p', { className: 'bs-text' }, b.erased_at ? 'Erased' : b.shown_text,
    b.owner_text ? el('span', { className: 'bs-tag', title: `Originally: ${b.text}` }, 'edited') : null);
  const facts = el('div', { className: 'bs-facts' },
    b.subject_kind ? kindIcon(b.subject_kind) : null,
    showSubject ? subjectLabel(b) : null,
    statusChip(b),
    attributeChip(b),
    cardKindLabel(b) ? el('span', {}, cardKindLabel(b)) : null,
    b.first_seen ? el('span', {}, `first seen ${day(b.first_seen)}`) : null,
    b.status === 'stale'
      ? (b.last_seen ? el('span', {}, `not heard since ${day(b.last_seen)}`) : null)
      : (b.last_seen ? el('span', {}, `last seen ${day(b.last_seen)}`) : null),
    b.valid_until ? el('span', {}, `until ${day(b.valid_until)}`) : null,
    b.status === 'rejected' && b.note ? el('span', {}, `reason: ${b.note}`) : null);
  const evidence = Array.isArray(b.evidence) ? b.evidence : [];
  const sources = evidence.length
    ? el('ul', { className: 'bs-sources', 'aria-label': 'Sources' }, ...evidence.map(sourceRow))
    : null;
  card.append(textNode);
  if (why) card.append(el('p', { className: 'bs-why' }, why));
  card.append(facts);
  if (sources) card.append(sources);
  card.append(actions(b, card, textNode));
  return card;
}

// ------------------------------------------------------------------ by status
function sectionHead(name, kind, subjectId) {
  const label = subjectId !== null && subjectId !== undefined
    ? el('button', { type: 'button', title: 'Open this subject',
                     onclick: () => openSubject(subjectId) }, name)
    : name;
  return el('div', { className: 'bs-section' }, kind ? kindIcon(kind) : null, kind ? ' ' : null, label);
}

function renderBeliefs() {
  const main = $('#bl-main');
  const needle = state.filter.trim().toLowerCase();
  const rows = needle
    ? state.beliefs.filter((b) => [b.subject_display, b.subject]
      .some((n) => (n || '').toLowerCase().includes(needle)))
    : state.beliefs;
  if (!rows.length) {
    let text = EMPTY[state.tab];
    if (needle && state.beliefs.length) text = `No ${state.tab} beliefs about “${state.filter.trim()}”.`;
    else if (state.attribute) {
      text = `No ${state.tab} beliefs with “${ATTRIBUTE_LABEL[state.attribute]}” yet. ` +
             'Confirmed beliefs get an attribute within the hour.';
    }
    main.replaceChildren(el('div', { className: 'bs-empty' }, text));
    return;
  }
  const parts = [];
  let rest = rows;
  if (state.tab === 'tentative') {
    // new beliefs come in by conversation: review (and dismiss) them that way
    const convs = new Map();
    rest = [];
    for (const b of rows) {
      const conv = conversationOf(b);
      if (!conv) { rest.push(b); continue; }
      if (!convs.has(conv)) convs.set(conv, []);
      convs.get(conv).push(b);
    }
    // a conversation's day is when its facts were first said (a fact said again later
    // doesn't move it); same day: the newest beliefs (highest id) first
    const convDay = (items) => items.map((b) => b.first_seen || b.last_seen || '').sort().pop() || '';
    const newest = (items) => Math.max(...items.map((b) => Number(b.id) || 0));
    const ordered = [...convs.values()].sort((a, b) =>
      convDay(b).localeCompare(convDay(a)) || newest(b) - newest(a));
    for (const items of ordered) {
      parts.push(conversationHead(items, convDay(items)),
        ...items.map((b) => beliefCard(b, null, true)));
    }
  }
  const groups = new Map();
  for (const b of rest) {
    const key = b.subject_id !== null && b.subject_id !== undefined
      ? `id:${b.subject_id}` : `name:${b.subject || ''}`;
    if (!groups.has(key)) {
      groups.set(key, { name: b.subject_display || b.subject || 'No subject',
                        kind: b.subject_kind, id: b.subject_id, items: [] });
    }
    groups.get(key).items.push(b);
  }
  for (const g of groups.values()) {
    parts.push(sectionHead(g.name, g.kind, g.id), ...g.items.map((b) => beliefCard(b)));
  }
  main.replaceChildren(...parts);
}

// The Athena conversation a belief was heard in (live evidence), or null.
function conversationOf(b) {
  const evidence = Array.isArray(b.evidence) ? b.evidence : [];
  const e = evidence.find((x) => x.source === MODULE_SOURCE && !x.gone &&
                                 (x.payload || {}).conversation_id);
  return e ? e.payload.conversation_id : null;
}

function conversationHead(items, lastDay) {
  const label = lastDay ? `Conversation · ${day(lastDay)}` : 'Conversation';
  const head = el('div', { className: 'bs-conv' },
    el('span', { className: 'bs-conv-name' }, label, ' · ', plural(items.length, 'belief')));
  if (items.length > 1) {
    const btn = button('dismiss', `Dismiss all ${items.length}`, () => dismissAll(items, btn),
      'None of these is worth remembering: out of memory, transcripts untouched (can be restored)');
    if (items.some((b) => busy.has(b.id))) btn.disabled = true;
    head.append(btn);
  }
  return head;
}

// Dismiss every belief of one conversation, one after another (each tells Athena).
async function dismissAll(items, btn) {
  const n = items.length;
  if (!window.confirm(`Dismiss all ${n} beliefs from this conversation? ` +
                      'You can restore them from the Rejected tab.')) return;
  // every button of these cards stays off until all are done (re-renders keep it so)
  for (const b of items) busy.add(b.id);
  btn.disabled = true;
  for (const b of items) {
    const card = document.getElementById(`belief-${b.id}`);
    if (card) for (const x of card.querySelectorAll('button')) x.disabled = true;
  }
  let done = 0;
  let failed = 0;
  let missed = 0;
  for (const b of items) {
    try {
      const r = await postJSON(`/api/beliefs/${b.id}/dismiss`, {});
      const p = (r && r.propagated) || {};
      missed += (p.failed || 0) + (p.unsupported || 0);
      done += 1;
    } catch (e) {
      failed += 1;
    }
  }
  for (const b of items) busy.delete(b.id);
  const text = done ? [`Dismissed ${plural(done, 'belief')}; restore from the Rejected tab.`] : [];
  if (failed) text.push(`${plural(failed, 'belief')} could not be dismissed; try again.`);
  if (missed) text.push(`Athena could not be reached for ${plural(missed, 'fact')}.`);
  toast(text.join(' '), failed > 0 || missed > 0);
  await load(true);
}

async function loadBeliefs(quiet) {
  if (!quiet) status('Loading…');
  try {
    let path = `/api/beliefs?status=${encodeURIComponent(state.tab)}`;
    if (state.attribute) path += `&attribute=${encodeURIComponent(state.attribute)}`;
    const data = await api(path);
    state.beliefs = Array.isArray(data.beliefs) ? data.beliefs : [];
    state.stats = data.stats || null;
    render();
  } catch (e) {
    renderControls();
    status(`Couldn't load your beliefs: ${e.message}`, true);
  }
}

// ------------------------------------------------------------------ subjects
function countsText(counts) {
  const c = counts || {};
  const parts = TABS.filter(([key]) => c[key])
    .map(([key, label]) => `${c[key]} ${key === 'rejected' ? 'forgotten' : label.toLowerCase()}`);
  return parts.length ? parts.join(' · ') : 'no beliefs';
}

async function contactsList(force = false) {
  if (state.contacts && !force) return state.contacts;
  const data = await api('/api/beliefs/contacts');
  state.contacts = Array.isArray(data.contacts) ? data.contacts : [];
  state.contactsFailed = false;
  return state.contacts;
}

// Contact names for the subject cards; a contact book that can't be read only costs
// the names.
async function ensureContacts(needed) {
  if (!needed || state.contacts || state.contactsFailed) return;
  try {
    await contactsList();
  } catch (e) {
    state.contactsFailed = true;
  }
}

// One open panel per card (link picker or merge); the same button closes it.
function togglePanel(card, name) {
  const open = card.querySelector('.bs-panel');
  if (open) {
    open.remove();
    if (open.dataset.panel === name) return null;
  }
  const panel = el('div', { className: 'bs-panel', 'data-panel': name });
  card.append(panel);
  return panel;
}

function closeButton(panel) {
  return el('button', { type: 'button', className: 'bs-btn', onclick: () => panel.remove() }, 'Cancel');
}

function renameMode(s, card, nameNode) {
  const input = el('input', { type: 'text', className: 'bs-filter', value: s.display,
                              'aria-label': 'New name', maxlength: '120' });
  const save = () => {
    const display = input.value.trim();
    if (!display) {
      toast('A subject needs a name.', true);
      return;
    }
    if (display === s.display) {
      render();
      return;
    }
    act(card, `/api/beliefs/subjects/${s.id}/rename`, { display },
      () => toast(`Renamed; “${s.display}” stays an alias.`));
  };
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') save();
    if (ev.key === 'Escape') render();
  });
  nameNode.replaceWith(el('div', { className: 'bs-edit' }, input,
    el('button', { type: 'button', className: 'bs-btn', onclick: save }, 'Save'),
    el('button', { type: 'button', className: 'bs-btn', onclick: () => render() }, 'Cancel')));
  card.querySelector('.bs-actions').hidden = true;
  const panel = card.querySelector('.bs-panel');
  if (panel) panel.remove();
  input.focus();
  input.select();
}

async function linkPanel(s, card) {
  const panel = togglePanel(card, 'link');
  if (!panel) return;
  panel.append(el('div', { className: 'bs-status' }, 'Loading your contacts…'));
  let list;
  try {
    list = await contactsList(true);
  } catch (e) {
    panel.replaceChildren(el('div', { className: 'bs-status error' },
      `Couldn't load your contacts: ${e.message}`), closeButton(panel));
    return;
  }
  if (!list.length) {
    panel.replaceChildren(el('div', { className: 'bs-empty' },
      'No contacts yet. Add the person in Contacts first.'), closeButton(panel));
    return;
  }
  const input = el('input', { type: 'search', className: 'bs-filter', placeholder: 'Search contacts',
                              'aria-label': 'Search contacts', autocomplete: 'off' });
  const ul = el('ul', { className: 'bs-pick', 'aria-label': 'Contacts' });
  const pick = (c) => act(card, `/api/beliefs/subjects/${s.id}/link`, { contact_id: c.id },
    () => toast(`Linked to ${c.name || 'the contact'}; its memory entries carry the contact now.`));
  const draw = () => {
    const needle = input.value.trim().toLowerCase();
    const rows = list.filter((c) => !needle || (c.name || '').toLowerCase().includes(needle)).slice(0, 50);
    if (!rows.length) {
      ul.replaceChildren(el('li', { className: 'bs-empty' }, 'No contact matches.'));
      return;
    }
    ul.replaceChildren(...rows.map((c) => {
      const here = c.subject_id === s.id;
      const elsewhere = c.subject_id !== null && c.subject_id !== undefined && !here;
      const note = here ? 'linked here' : (elsewhere ? `linked to ${subjectName(c.subject_id)}` : '');
      return el('li', {}, el('button', {
        type: 'button', className: 'bs-btn', disabled: here || elsewhere,
        title: elsewhere ? 'Linked to another subject: merge the two subjects instead'
                         : `Link ${s.display} to this contact`,
        onclick: () => pick(c),
      }, el('span', {}, c.name || c.id), note ? el('span', { className: 'bs-meta' }, note) : null));
    }));
  };
  input.addEventListener('input', draw);
  input.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') panel.remove(); });
  panel.replaceChildren(input, ul, el('div', { className: 'bs-row' }, closeButton(panel)));
  draw();
  input.focus();
}

async function mergePanel(s, card) {
  const panel = togglePanel(card, 'merge');
  if (!panel) return;
  panel.append(el('div', { className: 'bs-status' }, 'Loading subjects…'));
  let all;
  try {
    const data = await api('/api/beliefs/subjects');
    all = Array.isArray(data.subjects) ? data.subjects : [];
  } catch (e) {
    panel.replaceChildren(el('div', { className: 'bs-status error' },
      `Couldn't load your subjects: ${e.message}`), closeButton(panel));
    return;
  }
  const others = all.filter((o) => o.id !== s.id);
  if (!others.length) {
    panel.replaceChildren(el('div', { className: 'bs-empty' }, 'There is no other subject to merge into.'),
      closeButton(panel));
    return;
  }
  const select = el('select', { className: 'bs-select', 'aria-label': `Merge ${s.display} into` },
    el('option', { value: '' }, 'Merge into…'),
    ...others.map((o) => el('option', { value: String(o.id) }, `${o.display} (${KINDS[o.kind] || o.kind})`)));
  const go = () => {
    const target = others.find((o) => String(o.id) === select.value);
    if (!target) {
      toast('Pick the subject to merge into.', true);
      return;
    }
    const ok = window.confirm(`Merge “${s.display}” into “${target.display}”? Its beliefs, ` +
      `names and person ids move to “${target.display}”, and “${s.display}” is removed. ` +
      'This cannot be undone.');
    if (!ok) return;
    act(card, '/api/beliefs/subjects/merge', { keep_id: target.id, drop_id: s.id }, () => {
      if (state.open === s.id) state.open = target.id;
      toast(`Merged into ${target.display}.`);
    });
  };
  panel.replaceChildren(el('p', { className: 'bs-meta' },
    `Pick the subject “${s.display}” is the same as. Its beliefs and names move there.`),
  el('div', { className: 'bs-row' }, select, button('merge', 'Merge', go), closeButton(panel)));
  select.focus();
}

function subjectActions(s, card, nameNode, detail) {
  const row = el('div', { className: 'bs-row bs-actions' });
  if (!detail) row.append(button('open', 'Open', () => openSubject(s.id), 'Summary and beliefs'));
  if (s.kind !== 'owner') {
    const select = el('select', { className: 'bs-select', 'aria-label': `Kind of ${s.display}`,
                                  title: 'What this subject is' },
      ...['person', 'project', 'thing'].map((k) => el('option', { value: k, selected: k === s.kind }, KINDS[k])));
    select.addEventListener('change', () => act(card, `/api/beliefs/subjects/${s.id}/kind`,
      { kind: select.value }, () => toast(`${s.display} is a ${KINDS[select.value].toLowerCase()} now.`)));
    row.append(select);
  }
  row.append(button('edit', 'Rename', () => renameMode(s, card, nameNode)));
  row.append(button('chain', s.contact_id ? 'Change contact' : 'Link contact', () => linkPanel(s, card),
    'Link to one of your contacts'));
  if (s.contact_id) {
    row.append(button('unlink', 'Unlink', () => act(card, `/api/beliefs/subjects/${s.id}/unlink`, {},
      () => toast('Contact link removed.')), 'Remove the contact link'));
  }
  if (s.kind !== 'owner') {
    row.append(button('merge', 'Merge into…', () => mergePanel(s, card), 'It is the same as another subject'));
  }
  return row;
}

function subjectCard(s, detail = false) {
  const card = el('div', { className: 'bs-card', id: `subject-${s.id}` });
  const nameNode = detail
    ? el('h2', { className: 'bs-subject-name' }, s.display)
    : el('button', { type: 'button', className: 'bs-subject-name', title: 'Open this subject',
                     onclick: () => openSubject(s.id) }, s.display);
  const head = el('div', { className: 'bs-subject-head' }, nameNode, kindChip(s.kind));
  const contact = s.contact_id
    ? el('span', { className: 'bs-kind', title: `Contact ${s.contact_id}` }, el('span', { html: ICONS.chain }),
      contactName(s.contact_id) || (state.contactsFailed ? 'a contact' : 'a contact not in your book'))
    : null;
  const known = (s.sources || []).map((x) => `in ${SOURCE_NAMES[x.source] || x.source} as “${x.name || x.person_id}”`);
  const facts = el('div', { className: 'bs-facts' },
    el('span', {}, countsText(s.counts)),
    (s.aliases || []).length ? el('span', {}, `also: ${s.aliases.join(', ')}`) : null,
    contact,
    known.length ? el('span', {}, known.join('; ')) : null);
  card.append(head, facts, subjectActions(s, card, nameNode, detail));
  return card;
}

function renderSubjects() {
  const main = $('#bl-main');
  const needle = state.filter.trim().toLowerCase();
  const rows = needle
    ? state.subjects.filter((s) => [s.display, ...(s.aliases || [])]
      .some((n) => (n || '').toLowerCase().includes(needle)))
    : state.subjects;
  if (!rows.length) {
    const kind = KIND_FILTERS.find(([k]) => k === state.kind);
    let text = 'No subjects yet. They appear with the first beliefs.';
    if (needle && state.subjects.length) text = `No subject called “${state.filter.trim()}”.`;
    else if (state.kind) text = `Nothing under ${kind ? kind[1] : state.kind}.`;
    main.replaceChildren(el('div', { className: 'bs-empty' }, text));
    return;
  }
  main.replaceChildren(...rows.map((s) => subjectCard(s)));
}

async function loadSubjects(quiet) {
  if (!quiet) status('Loading…');
  try {
    const q = state.kind ? `?kind=${encodeURIComponent(state.kind)}` : '';
    const data = await api(`/api/beliefs/subjects${q}`);
    state.subjects = Array.isArray(data.subjects) ? data.subjects : [];
    state.stats = data.stats || state.stats;
    await ensureContacts(state.subjects.some((s) => s.contact_id));
    render();
  } catch (e) {
    renderControls();
    status(`Couldn't load your subjects: ${e.message}`, true);
  }
}

// ------------------------------------------------------------------ one subject
function openSubject(id) {
  state.view = 'subject';
  state.open = id;
  state.detail = null;
  try { sessionStorage.setItem(VIEW_KEY, 'subject'); } catch (e) {}
  load();
  // In the Beliefs window (static/js/forkBeliefsWindow.js) the window body scrolls.
  const scroller = $('#bl-main').closest('.fm-modal-body');
  if (scroller) scroller.scrollTo(0, 0);
  else window.scrollTo(0, 0);
}

function closeSubject() {
  state.open = null;
  state.detail = null;
  load();
}

function renderSubject() {
  const d = state.detail;
  if (!d || !d.subject) {
    status('Loading…');
    return;
  }
  const back = el('div', { className: 'bs-row bs-back' }, button('back', 'All subjects', closeSubject));
  const summary = el('div', { className: 'bs-card' },
    el('div', { className: 'bs-meta' }, 'Summary from what is confirmed'),
    d.summary
      ? el('p', { className: 'bs-summary' }, d.summary)
      : el('p', { className: 'bs-empty' }, 'Nothing about this subject is confirmed yet, so there is no summary.'));
  const beliefs = Array.isArray(d.beliefs) ? d.beliefs : [];
  const parts = [back, subjectCard(d.subject, true), summary];
  if (!beliefs.length) {
    parts.push(el('div', { className: 'bs-empty' },
      'No beliefs about this subject. Forgotten ones are on the Rejected tab of “By status”.'));
  }
  const groups = new Map(ATTRIBUTES.map(([key]) => [key, []]));
  for (const b of beliefs) {
    const key = b.attribute && b.value && groups.has(b.attribute) ? b.attribute : 'other';
    groups.get(key).push(b);
  }
  for (const [key, items] of groups) {
    if (!items.length) continue;
    parts.push(el('div', { className: 'bs-section' },
      el('span', { className: 'bs-attr' }, ATTRIBUTE_LABEL[key]), ` ${items.length}`),
    ...items.map((b) => beliefCard(b)));
  }
  $('#bl-main').replaceChildren(...parts);
}

async function loadSubject(quiet) {
  if (!quiet) status('Loading…');
  try {
    const data = await api(`/api/beliefs/subjects/${encodeURIComponent(state.open)}`);
    state.detail = data;
    state.stats = data.stats || state.stats;
    await ensureContacts(Boolean(data.subject && data.subject.contact_id));
    render();
  } catch (e) {
    if (e.status === 404) {
      state.open = null;
      state.detail = null;
      toast('That subject is gone (merged or renamed elsewhere).', true);
      await loadSubjects(quiet);
      return;
    }
    renderControls();
    status(`Couldn't load this subject: ${e.message}`, true);
  }
}

// ------------------------------------------------------------------ search
let searchSeq = 0;

function normalizeHits(data) {
  const list = data.results || data.hits || data.beliefs || [];
  return (Array.isArray(list) ? list : []).map((item) => {
    const b = item && item.belief ? item.belief : item;
    return { belief: b, why: (item && item.why) || (b && b.why) || '' };
  }).filter((h) => h.belief && h.belief.id !== undefined && h.belief.id !== null);
}

// Fallback while the ranked search route isn't installed: every word must appear in the
// text, the subject or the structured value.
async function localSearch(q) {
  const data = await api('/api/beliefs?status=all');
  state.stats = data.stats || state.stats;
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
  const fields = [
    ['text', (b) => `${b.shown_text || ''} ${b.text || ''}`],
    ['subject', (b) => `${b.subject_display || ''} ${b.subject || ''}`],
    ['attribute', (b) => `${ATTRIBUTE_LABEL[b.attribute] || ''} ${b.value || ''}`],
    ['kind', (b) => kindLabel(b) || ''],
    ['quote', (b) => (Array.isArray(b.evidence) ? b.evidence : [])
      .map((e) => (e.payload || {}).quote || '').join(' ')],
  ];
  const hits = [];
  for (const b of Array.isArray(data.beliefs) ? data.beliefs : []) {
    const where = new Set();
    const all = terms.every((t) => {
      const found = fields.filter(([, get]) => get(b).toLowerCase().includes(t));
      for (const [name] of found) where.add(name);
      return found.length > 0;
    });
    if (all) hits.push({ belief: b, why: `Words found in the ${[...where].join(' and ')}` });
  }
  return hits;
}

async function runSearch(quiet) {
  const q = state.query.trim();
  const seq = ++searchSeq;
  if (!quiet) status('Searching…');
  try {
    let hits = null;
    let fallback = false;
    if (!state.noSearchRoute) {
      try {
        const data = await api(`/api/beliefs/search?q=${encodeURIComponent(q)}&status=all`);
        hits = normalizeHits(data);
        if (data.stats) state.stats = data.stats;
      } catch (e) {
        if (e.status !== 404 && e.status !== 405) throw e;
        state.noSearchRoute = true; // not installed on this server: don't ask again
      }
    }
    if (hits === null) {
      fallback = true;
      hits = await localSearch(q);
    }
    if (seq !== searchSeq) return;
    state.hits = hits;
    state.searchFallback = fallback;
    render();
  } catch (e) {
    if (seq !== searchSeq) return;
    renderControls();
    status(`Couldn't search: ${e.message}`, true);
  }
}

function clearSearch() {
  $('#bl-search').value = '';
  state.query = '';
  load();
}

function renderSearch() {
  const q = state.query.trim();
  const parts = [el('div', { className: 'bs-row' },
    el('span', { className: 'bs-meta' }, `${plural(state.hits.length, 'result')} for “${q}”`),
    button('unlink', 'Clear search', clearSearch))];
  if (state.searchFallback) {
    parts.push(el('p', { className: 'bs-meta' }, 'Plain text match: ranked search is not installed yet.'));
  }
  if (!state.hits.length) parts.push(el('div', { className: 'bs-empty' }, 'Nothing found.'));
  parts.push(...state.hits.map((h) => beliefCard(h.belief, h.why)));
  $('#bl-main').replaceChildren(...parts);
}

// ------------------------------------------------------------------ dispatch
function render() {
  renderControls();
  if (state.query.trim()) renderSearch();
  else if (state.view === 'subject') {
    if (state.open !== null) renderSubject();
    else renderSubjects();
  } else renderBeliefs();
}

async function load(quiet = false) {
  if (state.query.trim()) return runSearch(quiet);
  if (state.view === 'subject') return state.open !== null ? loadSubject(quiet) : loadSubjects(quiet);
  return loadBeliefs(quiet);
}

async function sync() {
  const btn = $('#bl-sync');
  btn.disabled = true;
  btn.textContent = 'Syncing…';
  try {
    const r = await api('/api/beliefs/run', { method: 'POST' });
    toast(r.summary || 'Synced.');
    await load(true);
  } catch (e) {
    toast(`Couldn't sync: ${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sync now';
  }
}

$('#bl-attribute').replaceChildren(el('option', { value: '' }, 'All attributes'),
  ...ATTRIBUTES.map(([key, label]) => el('option', { value: key }, label)));
$('#bl-kind').replaceChildren(...KIND_FILTERS.map(([key, label]) => el('option', { value: key }, label)));

let filterTimer = null;
$('#bl-filter').addEventListener('input', (ev) => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => {
    state.filter = ev.target.value || '';
    render();
  }, 150);
});
let searchTimer = null;
$('#bl-search').addEventListener('input', (ev) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    const q = (ev.target.value || '').trim();
    if (q === state.query.trim()) return;
    state.query = q;
    load();
  }, 350);
});
$('#bl-search').addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && state.query) clearSearch();
});
$('#bl-attribute').addEventListener('change', (ev) => {
  state.attribute = ev.target.value || '';
  load();
});
$('#bl-kind').addEventListener('change', (ev) => {
  state.kind = ev.target.value || '';
  load();
});
for (const b of document.querySelectorAll('.bs-view')) {
  b.addEventListener('click', () => selectView(b.dataset.view));
}
$('#bl-sync').addEventListener('click', sync);
renderControls();
load();
