// Fork: Athena view — conversations list, action items, "who is this?" cards
// and a transcript reader with click-to-fix speakers. Mounted by the Conversations
// window in the main app (static/js/forkModuleWindows.js) and by the standalone page
// (static/sound-module.html). Data comes from routes/sound_module_routes.py, which
// calls the module's MCP tools. Links to the module's signed /view pages open this
// reader at that moment (#t=); the "Transcript" buttons still open the module's page.

// One mount per document: the window in the main app, or the standalone page.
let ctx = null;
const $ = (sel) => ctx.root.querySelector(sel);
const getY = () => (ctx.scroller ? ctx.scroller.scrollTop : window.scrollY);
const setY = (y) => {
  if (ctx.scroller) ctx.scroller.scrollTop = y;
  else window.scrollTo(0, y);
};

// A module transcript link: <CONVO_PUBLIC_URL>/view/<id>?sig=…#t=<seconds>
// (docs/fork/modules/odysseus-sound.md). Returns {id, t} or null.
const VIEW_PATH = /\/view\/([A-Za-z0-9_.:-]{1,200})$/;
export function parseModuleLink(href) {
  let url;
  try {
    url = new URL(href || '', window.location.href);
  } catch (e) {
    return null;
  }
  if (!/^https?:$/.test(url.protocol) || !url.searchParams.get('sig')) return null;
  const m = VIEW_PATH.exec(url.pathname);
  if (!m) return null;
  const t = /(?:^#|&)t=(\d+(?:\.\d+)?)/.exec(url.hash || '');
  return { id: m[1], t: t ? Number(t[1]) : null };
}

const OPEN_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5' +
  'a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" ' +
  'x2="21" y2="3"/></svg>';

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null) continue;
    if (k === 'className') node.className = v;
    else if (k === 'html') node.innerHTML = v; // only for our own static SVG strings
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c === null || c === undefined) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function safeLink(url) {
  return /^https?:\/\//.test(url || '') ? url : null;
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
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function status(target, text, error = false) {
  target.replaceChildren(el('div', { className: error ? 'bs-status error' : 'bs-status' }, text));
}

// ------------------------------------------------------------------ who is this?
function whoCard(s, onDone) {
  const input = el('input', { className: 'bs-input', placeholder: 'Name', 'aria-label': 'Name',
                              maxlength: '100' });
  const msg = el('div', { className: 'bs-meta' });
  const save = async (name) => {
    name = (name || '').trim();
    if (!name) {
      input.focus();
      return;
    }
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const r = await api('/api/modules/sound/name-speaker', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: s.conversation_id, speaker_label: s.label, name }),
      });
      msg.textContent = r.message;
      if (r.ok) setTimeout(onDone, 1500);
      else buttons.forEach((b) => { b.disabled = false; });
    } catch (e) {
      msg.textContent = `Could not save: ${e.message}`;
      buttons.forEach((b) => { b.disabled = false; });
    }
  };
  const buttons = [];
  if (s.guess) {
    buttons.push(el('button', { type: 'button', className: 'bs-btn primary',
                                onclick: () => save(s.guess) }, `It's ${s.guess}`));
  }
  buttons.push(el('button', { type: 'button', className: 'bs-btn', onclick: () => save(input.value) },
    'Save name'));
  buttons.push(el('button', { type: 'button', className: 'bs-btn',
                              onclick: () => card.remove() }, 'Skip'));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') save(input.value);
  });
  const quotes = s.quotes.map((q) => el('a', {
    className: 'bs-quote', href: safeLink(q.link), target: '_blank', rel: 'noopener',
    title: 'Open the transcript at this line',
  }, `“${q.text}”`));
  const card = el('div', { className: 'bs-card' },
    el('h2', {}, `${s.label}`),
    el('div', { className: 'bs-meta' },
      `${s.lines} lines in “${s.conversation_title}”, ${s.when}`
      + (s.guess ? ` · sounds like ${s.guess} (${s.guess_score.toFixed(2)})` : '')),
    ...quotes,
    el('div', { className: 'bs-row' }, input, ...buttons),
    msg);
  return card;
}

async function loadWho() {
  const box = $('#bs-who');
  try {
    const { speakers } = await api('/api/modules/sound/unnamed?limit=10');
    if (!speakers.length) {
      status(box, 'Everyone is named.');
      return;
    }
    box.replaceChildren(...speakers.map((s) => whoCard(s, () => { loadWho(); loadList(); })));
  } catch (e) {
    status(box, e.message, true);
  }
}

// ----------------------------------------------------------------- action items
// Live from Athena, the source of truth. Explicit voice-note items are filed into
// the Odysseus calendar / to-dos / notes by src/module_action_items.py; other voice-note
// items and conversation items are suggestions until the owner taps Add (MODULE_FILE_ITEMS
// decides which voice-note items count as explicit). Every change answers with the item as it is
// now, and the card is drawn again from that answer.
// Monochrome icons in the sidebar's style (calendar, checked box, bell, page).
const _ICON = (body) => '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  body + '</svg>';
const KIND_ICON = {
  event: _ICON('<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" ' +
    'x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>'),
  task: _ICON('<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 12l3 3 5-6"/>'),
  reminder: _ICON('<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/>' +
    '<path d="M13.73 21a2 2 0 0 1-3.46 0"/>'),
  note: _ICON('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>' +
    '<path d="M14 2v6h6"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/>'),
};
const KIND_NAME = { event: 'Event', task: 'To-do', reminder: 'Reminder', note: 'Note' };
const WHERE = { calendar: 'In calendar', todo: 'In to-dos', note: 'In notes' };
const whereLabel = (filed) => (filed.notes ? `In ${filed.notes}`
  : (filed.list ? `On ${filed.list} list` : (WHERE[filed.target] || 'Filed')));

function hubLink(url) {
  // filed entries open in Odysseus itself: only same-origin paths
  return /^\/(?!\/)/.test(url || '') ? url : null;
}

function dueLabel(it) {
  if (it.due_date) {
    const [y, m, d] = it.due_date.split('-').map(Number);
    const day = new Date(y, m - 1, d);
    const text = isNaN(day.getTime()) ? it.due_date
      : day.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
    return it.due_time ? `${text} · ${it.due_time}` : text;
  }
  return it.due ? `due ${it.due}` : null;
}

function postJSON(path, body) {
  return api(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

// Inline date + time editor for "Change time"; resolves the move body or null on cancel.
function timeEditor(it, onSave, onCancel) {
  const date = el('input', { className: 'bs-input', type: 'date', 'aria-label': 'Date',
                             value: it.due_date || '' });
  const time = el('input', { className: 'bs-input', type: 'time', 'aria-label': 'Time',
                             value: it.due_time || '' });
  const save = el('button', { type: 'button', className: 'bs-btn primary', onclick: () => {
    const body = { kind: it.kind || 'task' };
    if (date.value) body.due_date = date.value;
    else if (it.due_date) body.due_date = 'none';
    if (time.value) body.due_time = time.value.slice(0, 5);
    else if (it.due_time) body.due_time = 'none';
    onSave(body);
  } }, 'Save');
  const cancel = el('button', { type: 'button', className: 'bs-btn', onclick: onCancel }, 'Cancel');
  return el('div', { className: 'bs-edit bs-row bs-time-edit' },
    el('label', { className: 'bs-field' }, el('span', { className: 'bs-meta' }, 'Date'), date),
    el('label', { className: 'bs-field' }, el('span', { className: 'bs-meta' }, 'Time'), time),
    save, cancel,
    it.time_unclear ? el('div', { className: 'bs-meta bs-wide' },
      'The time was unclear in the recording; saving a time clears the warning.') : null);
}

// Inline to-do list picker for "Add to a list" / "Change list"; an empty name takes the
// item off its list (a to-do of its own). Suggests the owner's existing lists.
let listEditorSeq = 0;
function listEditor(it, onSave, onCancel) {
  const id = `bs-lists-${++listEditorSeq}`;
  const options = el('datalist', { id });
  const current = (it.filed && it.filed.list) || it.list_name || '';
  const input = el('input', { className: 'bs-input', type: 'text', list: id, maxlength: '40',
                              'aria-label': 'To-do list', placeholder: 'e.g. Shopping',
                              value: current });
  api('/api/modules/sound/todo-lists').then((r) => {
    (r.lists || []).forEach((name) => options.append(el('option', { value: name })));
  }).catch(() => {});
  const save = el('button', { type: 'button', className: 'bs-btn primary', onclick: () => {
    const name = input.value.trim();
    if (!name && !current) return onCancel();
    const kind = it.kind === 'event' ? 'task' : (it.kind || 'task');
    onSave({ kind, list_name: name || 'none' });
  } }, 'Save');
  const cancel = el('button', { type: 'button', className: 'bs-btn', onclick: onCancel }, 'Cancel');
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save.click(); });
  return el('div', { className: 'bs-edit bs-row bs-list-edit' },
    el('label', { className: 'bs-field' }, el('span', { className: 'bs-meta' }, 'List'), input),
    options, save, cancel,
    el('div', { className: 'bs-meta bs-wide' },
      current ? 'Clear the name to make it a to-do of its own.'
        : 'Adds it as a line on that to-do list; a new list is made if there is none.'));
}

function itemCard(it, note = '') {
  const msg = el('div', { className: 'bs-meta bs-msg', role: 'status' }, note);
  const buttons = [];
  let busy = false;
  const setBusy = (on) => {
    busy = on;
    card.querySelectorAll('button, input').forEach((b) => { b.disabled = on; });
    card.setAttribute('aria-busy', on ? 'true' : 'false');
  };
  // One request at a time per card; the server's answer redraws it.
  const run = async (label, request) => {
    if (busy) return;
    setBusy(true);
    msg.classList.remove('error');
    msg.textContent = `${label}…`;
    try {
      const r = await request();
      if (r.ok === false) throw new Error(r.message || 'not saved');
      if (r.item && r.item.status === 'open') {
        const next = itemCard(r.item, r.message || '');
        if (!!r.item.filed === !!it.filed) card.replaceWith(next);
        else {
          card.remove();
          placeCard(next, r.item);
        }
      } else if (r.item === undefined && r.ok) {
        card.remove(); // Done / Dismiss
      } else {
        card.replaceWith(el('div', { className: 'bs-card bs-gone' },
          el('div', { className: 'bs-meta' }, `${it.text} — ${r.message || 'saved'}`)));
      }
      refreshItemGroups();
    } catch (e) {
      msg.classList.add('error');
      msg.textContent = `Could not save: ${e.message}`;
      setBusy(false);
    }
  };
  const base = `/api/modules/sound/action-items/${encodeURIComponent(it.id)}`;
  const setStatus = (statusName) => run('Saving', async () => {
    const r = await postJSON(base, { status: statusName });
    return r.ok ? { ok: true } : r;
  });
  const btn = (label, onclick, primary = false) => {
    const b = el('button', { type: 'button', className: primary ? 'bs-btn primary' : 'bs-btn',
                             onclick }, label);
    buttons.push(b);
    return b;
  };
  const filed = it.filed;
  const kind = it.kind || 'task';
  function listButton() {
    btn(filed && filed.list ? 'Change list' : 'Add to a list', () => {
      if (busy || card.querySelector('.bs-list-edit')) return;
      const editor = listEditor(it,
        (body) => run('Saving', () => postJSON(`${base}/move`, body)),
        () => editor.remove());
      actions.after(editor);
      editor.querySelector('input').focus();
    });
  }
  if (filed && filed.target === 'note') {
    btn('Undo', () => run('Undoing', () => postJSON(`${base}/undo`)));
  } else if (kind === 'note') {
    // a note whose filing failed: file it by hand
    btn('Add to notes', () => run('Adding', () => postJSON(`${base}/file`, { target: 'note' })), true);
  } else if (filed) {
    if (filed.target === 'calendar') {
      btn('Move to to-dos', () => run('Moving', () => postJSON(`${base}/move`, { kind: 'task' })));
    } else {
      btn('Move to calendar', () => run('Moving', () => postJSON(`${base}/move`, { kind: 'event' })));
    }
    btn('Change time', () => {
      if (busy || card.querySelector('.bs-time-edit')) return;
      const editor = timeEditor(it,
        (body) => run('Saving', () => postJSON(`${base}/move`, body)),
        () => editor.remove());
      actions.after(editor);
      editor.querySelector('input').focus();
    });
    if (filed.target === 'todo') listButton();
    btn('Undo', () => run('Undoing', () => postJSON(`${base}/undo`)));
  } else {
    btn('Add to calendar', () => run('Adding', () => postJSON(`${base}/file`, { target: 'calendar' })),
      kind === 'event');
    btn('Add to to-dos', () => run('Adding', () => postJSON(`${base}/file`, { target: 'todo' })),
      kind !== 'event');
    if (kind !== 'event') listButton();
  }
  btn('Done', () => setStatus('done'));
  btn('Dismiss', () => setStatus('dismissed'));

  const link = safeLink(it.link);
  const where = filed ? (hubLink(filed.open_url)
    ? el('a', { className: 'bs-chip bs-where', href: hubLink(filed.open_url),
                title: filed.target === 'calendar' ? 'Open the Odysseus calendar'
                  : (filed.target === 'note' ? 'Open the note'
                    : (filed.list ? 'Open the list' : 'Open the to-do')) },
      whereLabel(filed))
    : el('span', { className: 'bs-chip bs-where' }, whereLabel(filed)))
    : (it.list_name ? el('span', { className: 'bs-chip', title: kind === 'note'
      ? 'The notes named in the recording' : 'The list named in the recording' },
      kind === 'note' ? `for ${it.list_name} notes` : `for ${it.list_name} list`) : null);
  const due = dueLabel(it);
  const source = it.voice_note ? 'voice note' : 'conversation';
  const actions = el('div', { className: 'bs-row bs-actions' }, ...buttons);
  const card = el('div', { className: 'bs-card bs-item', 'data-item': String(it.id),
                           'data-voice-note': it.voice_note ? '1' : '0' },
    el('h2', { className: 'bs-item-title' },
      el('span', { className: 'bs-kind', title: KIND_NAME[kind] || 'To-do',
                   'aria-label': KIND_NAME[kind] || 'To-do',
                   html: KIND_ICON[kind] || KIND_ICON.task }),
      el('span', {}, kind === 'note' && it.note_title ? `${it.note_title}: ${it.text}` : it.text)),
    el('div', { className: 'bs-row bs-facts' },
      due ? el('span', { className: 'bs-due' }, due) : null,
      it.time_unclear ? el('span', { className: 'bs-chip warn', title: 'Check the time' },
        'time unclear') : null,
      where),
    el('div', { className: 'bs-meta' },
      [`${source} “${it.conversation_title}”${it.when ? `, ${it.when}` : ''}`,
       it.location ? `at ${it.location}` : null,
       it.owner ? `owner ${it.owner}` : null].filter(Boolean).join(' · '),
      link ? el('a', { className: 'bs-small', href: link, target: '_blank', rel: 'noopener',
                       title: 'Where it was said' }, el('span', { html: OPEN_ICON }), ' where it was said') : null),
    actions,
    msg);
  return card;
}

const ITEM_GROUPS = [
  { key: 'filed', test: (it) => !!it.filed, title: (items) =>
    (items.every((i) => i.voice_note) ? 'Filed from voice notes' : 'Filed in Odysseus') },
  { key: 'suggested', test: (it) => !it.filed, title: (items) =>
    (items.every((i) => !i.voice_note) ? 'Suggested from conversations' : 'Not filed yet') },
];

function groupBox(key, items) {
  const g = ITEM_GROUPS.find((x) => x.key === key);
  return el('div', { className: 'bs-item-group', 'data-group': key },
    el('div', { className: 'bs-sub' }, g.title(items)), ...items.map((it) => itemCard(it)));
}

// A card whose item was just filed (or unfiled) goes to the top of its group.
function placeCard(card, it) {
  const box = $('#bs-items');
  const key = ITEM_GROUPS.find((g) => g.test(it)).key;
  let group = box.querySelector(`.bs-item-group[data-group="${key}"]`);
  if (!group) {
    group = groupBox(key, []);
    group.firstChild.textContent = ITEM_GROUPS.find((g) => g.key === key).title([it]);
    if (key === 'filed') box.prepend(group);
    else box.append(group);
  }
  group.firstChild.after(card);
}

// Keep each group's heading in step after a card changed group or went away.
function refreshItemGroups() {
  const box = $('#bs-items');
  box.querySelectorAll('.bs-item-group').forEach((g) => {
    const cards = [...g.querySelectorAll('.bs-item')];
    if (!cards.length && !g.querySelector('.bs-gone')) {
      g.remove();
      return;
    }
    const spec = ITEM_GROUPS.find((x) => x.key === g.dataset.group);
    if (spec && cards.length) {
      g.firstChild.textContent = spec.title(cards.map((c) => ({ voice_note: c.dataset.voiceNote === '1' })));
    }
  });
  if (!box.querySelector('.bs-item-group')) status(box, 'No open action items.');
}

async function loadItems() {
  const box = $('#bs-items');
  try {
    const { action_items: items } = await api('/api/modules/sound/action-items?status=open');
    if (!items.length) {
      status(box, 'No open action items.');
      return;
    }
    const groups = ITEM_GROUPS.map((g) => ({ key: g.key, list: items.filter(g.test) }))
      .filter(({ list }) => list.length)
      .map(({ key, list }) => groupBox(key, list));
    box.replaceChildren(...groups);
  } catch (e) {
    status(box, e.message, true);
  }
}

// ---------------------------------------------------------------- conversations
// Text for a new chat's composer (static/js/forkChatAsk.js); it never sends by itself.
// The window hands it over directly; the standalone page links to the main page's ?ask=.
function askText(title, id, start) {
  return `/sound About the conversation "${title}" (id ${id}, ${start}): `;
}

function askButton(title, id, start) {
  const text = askText(title, id, start);
  return el('a', { className: 'bs-btn', href: `/?ask=${encodeURIComponent(text)}`,
                   title: 'Start a new chat about this conversation',
                   onclick: (e) => {
                     if (!ctx.onAsk || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
                     e.preventDefault();
                     ctx.onAsk(text);
                   } }, 'Ask in chat');
}

// The module's own transcript page, in a new tab (not caught by the reader links).
function moduleLink(url, className = 'bs-link') {
  const href = safeLink(url);
  return href ? el('a', { className, href, target: '_blank', rel: 'noopener', 'data-external': '1',
                          title: 'Open the module\'s transcript page' },
    el('span', { html: OPEN_ICON }), 'Transcript') : null;
}

function convCard(c) {
  const link = safeLink(c.link);
  return el('div', { className: 'bs-card' },
    el('h2', {}, c.title),
    el('div', { className: 'bs-meta' },
      (c.voice_note ? 'Voice note · ' : '') +
      `${c.start}–${c.end}` + (c.people.length ? ` · ${c.people.join(', ')}` : '')),
    c.summary ? el('p', { className: 'bs-summary' }, c.summary) : null,
    el('div', { className: 'bs-row' },
      el('button', { type: 'button', className: 'bs-btn primary',
                     onclick: () => openReader(c.id, c.start) }, 'Open'),
      askButton(c.title, c.id, c.start),
      moduleLink(link)));
}

async function loadList() {
  const box = $('#bs-list');
  const params = new URLSearchParams();
  for (const [k, v] of new FormData($('#bs-filter')).entries()) {
    if (String(v).trim()) params.set(k, String(v).trim());
  }
  params.set('limit', '30');
  status(box, 'Loading…');
  try {
    const { conversations } = await api(`/api/modules/sound/conversations?${params}`);
    if (!conversations.length) {
      status(box, params.get('kind') === 'note' ? 'No voice notes found.' : 'No conversations found.');
      return;
    }
    box.replaceChildren(...conversations.map(convCard));
  } catch (e) {
    status(box, e.message, true);
  }
}

// ------------------------------------------------------------------- reader
// One conversation: its detail (get_conversation) and transcript (get_transcript).
// On the standalone page it is deep-linkable as #c=<id>[&t=<seconds>], so reload and
// the browser back button work; in the window it is shown in place.
const SPEAKER_LABEL = /^Speaker \d+$/;
const MAX_SEEK_PAGES = 40; // a #t= jump loads at most this many transcript pages
const reader = {
  id: null,        // conversation shown
  start: '',       // its start ("YYYY-MM-DD HH:MM"), for "Ask in chat"
  gen: 0,          // bumps on every open/reload; late answers of older ones are dropped
  detail: null,
  lines: [],
  seen: new Set(), // line ids already shown (load more dedupes by id)
  nextStart: null,
  pages: 0,
  notice: '',
  listScroll: 0,
  fromList: false,
};

function hashRoute() {
  const m = /^#c=([^&]+)(?:&t=(\d+(?:\.\d+)?))?$/.exec(window.location.hash || '');
  if (!m) return null;
  try {
    return { id: decodeURIComponent(m[1]), t: m[2] !== undefined ? Number(m[2]) : null };
  } catch (e) {
    return { id: '', t: null };
  }
}

const VALID_ID = /^[A-Za-z0-9_.:-]{1,200}$/;

function openReader(id, start = '', t = null) {
  if (!reader.id) {
    reader.listScroll = getY();
    reader.fromList = true;
  }
  if (start) reader.start = start;
  if (ctx.embedded) {
    showReader(id, t);
    return;
  }
  const hash = `c=${encodeURIComponent(id)}${t !== null ? `&t=${t}` : ''}`;
  if (window.location.hash === `#${hash}`) showReader(id, t);
  else window.location.hash = hash; // -> route()
}

function closeReader() {
  if (ctx.embedded) {
    showHome();
    return;
  }
  if (reader.fromList) {
    window.history.back(); // -> hashchange -> route()
    return;
  }
  window.history.pushState(null, '', window.location.pathname + window.location.search);
  route();
}

// A link to a moment in the module's transcript; opens this reader there.
function linkSmall(url, label = 'transcript') {
  const href = safeLink(url);
  return href ? el('a', { className: 'bs-small', href, title: 'Show this moment in the transcript' },
    label) : null;
}

function startOf(when) {
  const m = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.exec(when || '');
  return m ? m[0] : '';
}

function namesOf(d) {
  const names = new Set([...(d ? d.speakers : []), ...(d ? d.people : [])]);
  for (const ln of reader.lines) names.add(ln.speaker);
  return [...names].filter((n) => n && n !== '—' && !SPEAKER_LABEL.test(n)).sort();
}

function refreshIcon() {
  return el('span', { html: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" '
    + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    + '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>'
    + '<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>' });
}

function renderReader() {
  const box = $('.bs-reader');
  const d = reader.detail;
  const title = d ? d.title : 'Conversation';
  const start = reader.start || (d ? startOf(d.when) : '');
  const top = el('div', { className: 'bs-reader-top' },
    el('button', { type: 'button', className: 'bs-btn', onclick: closeReader }, '← Back'),
    el('h2', {}, title),
    el('button', { type: 'button', className: 'bs-btn bs-icon-btn', title: 'Refresh',
                   'aria-label': 'Refresh', onclick: () => {
                     reader.notice = '';
                     reloadReader();
                   } }, refreshIcon()));
  const actions = el('div', { className: 'bs-row' },
    d ? askButton(d.title, d.id, start) : null,
    d ? moduleLink(d.link) : null);
  const parts = [top];
  if (reader.notice) parts.push(el('div', { className: 'bs-notice', role: 'status' }, reader.notice));
  if (d) {
    const people = d.people.filter((p) => p !== '—');
    const card = el('div', { className: 'bs-card' },
      el('div', { className: 'bs-meta' }, d.when + (people.length ? ` · ${people.join(', ')}` : '')),
      d.summary ? el('p', { className: 'bs-summary' }, d.summary) : null,
      actions);
    if (d.key_points.length) {
      card.append(el('div', { className: 'bs-sub' }, 'Key points'),
        el('ul', { className: 'bs-list' }, ...d.key_points.map((k) => el('li', {}, k))));
    }
    if (d.action_items.length) {
      card.append(el('div', { className: 'bs-sub' }, 'Action items'),
        el('ul', { className: 'bs-list' }, ...d.action_items.map((it) => el('li', {},
          `[${it.status}] ${it.text}`
          + (it.owner ? ` (owner ${it.owner})` : '') + (it.due ? ` · due ${it.due}` : ''),
          linkSmall(it.link)))));
    }
    if (d.facts.length) {
      card.append(el('div', { className: 'bs-sub' }, 'Facts'),
        el('ul', { className: 'bs-list' }, ...d.facts.map((f) => el('li', {},
          f.subject ? `${f.subject}: ${f.fact}` : f.fact, linkSmall(f.link)))));
    }
    parts.push(card);
  } else {
    parts.push(el('div', { className: 'bs-detail-status' }, el('div', { className: 'bs-status' }, 'Loading…')));
  }
  parts.push(el('div', { className: 'bs-section' }, 'Transcript'));
  parts.push(el('div', { className: 'bs-meta' }, 'Tap a speaker name to fix who said a line.'));
  const datalist = el('datalist', { id: 'bs-names' }, ...namesOf(d).map((n) => el('option', { value: n })));
  const lines = el('div', { className: 'bs-lines' });
  if (reader.lines.length) lines.append(...reader.lines.map(lineRow));
  else lines.append(el('div', { className: 'bs-status' },
    reader.pages ? 'No transcript lines.' : 'Loading…'));
  parts.push(datalist, lines);
  if (reader.nextStart !== null) {
    parts.push(el('div', { className: 'bs-row' },
      el('button', { type: 'button', className: 'bs-btn', onclick: (e) => loadMore(e.currentTarget) },
        'Load more')));
  }
  parts.push(el('div', { className: 'bs-reader-status' }));
  box.replaceChildren(...parts);
}

// Seconds into the conversation of a transcript line: its link's #t=, else its "m:ss".
function lineSeconds(ln) {
  const ref = parseModuleLink(ln.link);
  if (ref && ref.t !== null) return ref.t;
  const parts = String(ln.time || '').split(':').map(Number);
  if (!parts.length || parts.some((n) => Number.isNaN(n))) return null;
  return parts.reduce((acc, n) => acc * 60 + n, 0);
}

function lastSeconds(lines) {
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const t = lineSeconds(lines[i]);
    if (t !== null) return t;
  }
  return null;
}

// Scrolls to the line said at `t` (the last line starting at or before it) and marks it.
function focusLine(t) {
  let index = 0;
  reader.lines.forEach((ln, i) => {
    const s = lineSeconds(ln);
    if (s !== null && s <= t + 0.5) index = i;
  });
  const row = ctx.root.querySelectorAll('.bs-lines .bs-line')[index];
  if (!row) return;
  ctx.root.querySelectorAll('.bs-line-focus').forEach((n) => n.classList.remove('bs-line-focus'));
  row.classList.add('bs-line-focus');
  row.scrollIntoView({ block: 'center' });
  setTimeout(() => row.classList.remove('bs-line-focus'), 4000);
}

function lineRow(ln) {
  const href = safeLink(ln.link);
  const spk = el('button', { type: 'button', className: 'bs-spk', 'aria-expanded': 'false',
                             title: 'Fix who said this' }, ln.speaker);
  const row = el('div', { className: 'bs-line' },
    href ? el('a', { className: 'bs-time', href, title: 'Link to this moment' }, ln.time)
      : el('span', { className: 'bs-time' }, ln.time),
    spk,
    el('span', { className: 'bs-text' }, ln.text));
  spk.addEventListener('click', () => {
    const open = row.querySelector('.bs-edit');
    ctx.root.querySelectorAll('.bs-edit').forEach((n) => n.remove());
    ctx.root.querySelectorAll('.bs-spk[aria-expanded="true"]')
      .forEach((b) => b.setAttribute('aria-expanded', 'false'));
    if (open) return; // second tap closes
    spk.setAttribute('aria-expanded', 'true');
    const ed = speakerEditor(ln, () => {
      ed.remove();
      spk.setAttribute('aria-expanded', 'false');
      spk.focus();
    });
    row.append(ed);
    ed.querySelector('input').focus();
  });
  return row;
}

function speakerEditor(ln, close) {
  const label = ln.speaker;
  const unnamed = SPEAKER_LABEL.test(label);
  const input = el('input', { className: 'bs-input', list: 'bs-names', maxlength: '100',
                              placeholder: unnamed ? 'Name' : `Name (now ${label})`,
                              'aria-label': `New name for ${label}`, autocomplete: 'off' });
  const msg = el('div', { className: 'bs-meta', role: 'status' });
  const buttons = [];
  const run = async (kind) => {
    const name = input.value.trim();
    if (!name) {
      msg.textContent = 'Type a name first.';
      input.focus();
      return;
    }
    let path;
    let body;
    if (kind === 'line') {
      path = `/api/modules/sound/lines/${encodeURIComponent(ln.id)}`;
      body = { name };
    } else if (kind === 'label') {
      path = '/api/modules/sound/name-speaker';
      body = { conversation_id: reader.id, speaker_label: label, name };
    } else {
      if (name === label) {
        msg.textContent = `That is already "${label}".`;
        return;
      }
      const sure = await ctx.confirm(
        `Rename "${label}" to "${name}" in ALL conversations?\n\n`
        + `If "${name}" already exists, the two people are merged into one.\n`
        + 'This can\'t be undone.');
      if (!sure) return;
      path = '/api/modules/sound/rename-speaker';
      body = { old_name: label, new_name: name };
    }
    buttons.forEach((b) => { b.disabled = true; });
    msg.textContent = 'Saving…';
    try {
      const r = await api(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (r.ok) {
        reader.notice = r.message;
        await reloadReader();
        return;
      }
      msg.textContent = r.message;
    } catch (e) {
      msg.textContent = `Could not save: ${e.message}`;
    }
    buttons.forEach((b) => { b.disabled = false; });
  };
  buttons.push(el('button', { type: 'button', className: 'bs-btn primary', onclick: () => run('line') },
    'Only this line'));
  if (unnamed) {
    buttons.push(el('button', { type: 'button', className: 'bs-btn', onclick: () => run('label') },
      `Every line of ${label} here`));
  } else {
    buttons.push(el('button', { type: 'button', className: 'bs-btn', onclick: () => run('everywhere'),
                                title: 'Renames this speaker in all conversations' }, 'Rename everywhere'));
  }
  buttons.push(el('button', { type: 'button', className: 'bs-btn', onclick: close }, 'Cancel'));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') run('line');
    else if (e.key === 'Escape') close();
  });
  return el('div', { className: 'bs-edit' },
    el('div', { className: 'bs-meta' }, `Who says this instead of ${label}?`),
    el('div', { className: 'bs-row' }, input, ...buttons),
    msg);
}

async function fetchPage(id, start) {
  return api(`/api/modules/sound/conversations/${encodeURIComponent(id)}/transcript`
    + `?start_seconds=${encodeURIComponent(start)}`);
}

function addLines(page) {
  for (const ln of page.lines) {
    if (reader.seen.has(ln.id)) continue;
    reader.seen.add(ln.id);
    reader.lines.push(ln);
  }
  reader.nextStart = page.more && page.next_start !== null && page.next_start !== undefined
    ? page.next_start : null;
  reader.pages += 1;
}

async function loadMore(btn) {
  if (reader.nextStart === null) return;
  const gen = reader.gen;
  btn.disabled = true;
  btn.textContent = 'Loading…';
  try {
    const before = reader.lines.length;
    const page = await fetchPage(reader.id, reader.nextStart);
    if (gen !== reader.gen) return;
    addLines(page);
    // no new line although the module says there are more: stop instead of looping
    if (reader.lines.length === before) reader.nextStart = null;
    const y = getY();
    renderReader();
    setY(y);
  } catch (e) {
    if (gen !== reader.gen) return;
    btn.disabled = false;
    btn.textContent = 'Load more';
    status($('.bs-reader-status'), e.message, true);
  }
}

// Loads detail and the transcript; keepPages > 1 reloads as many pages as were shown,
// and seekT loads pages until the line said at that second is in, then shows it.
async function loadReader(id, keepPages = 1, seekT = null) {
  const gen = ++reader.gen;
  const [detail, transcript] = await Promise.allSettled([
    api(`/api/modules/sound/conversations/${encodeURIComponent(id)}`),
    (async () => {
      const acc = { lines: [], seen: new Set(), next: null, pages: 0 };
      let start = 0;
      const wantMore = () => acc.pages < keepPages || (seekT !== null && acc.pages < MAX_SEEK_PAGES
        && (lastSeconds(acc.lines) === null || lastSeconds(acc.lines) < seekT));
      do {
        const page = await fetchPage(id, start);
        for (const ln of page.lines) {
          if (!acc.seen.has(ln.id)) {
            acc.seen.add(ln.id);
            acc.lines.push(ln);
          }
        }
        acc.pages += 1;
        const next = page.more && page.next_start !== null && page.next_start !== undefined
          ? page.next_start : null;
        acc.next = next === start && acc.pages > 1 ? null : next;
        start = acc.next;
      } while (acc.next !== null && wantMore());
      return acc;
    })(),
  ]);
  if (gen !== reader.gen) return;
  reader.detail = detail.status === 'fulfilled' ? detail.value.conversation : null;
  if (transcript.status === 'fulfilled') {
    reader.lines = transcript.value.lines;
    reader.seen = transcript.value.seen;
    reader.nextStart = transcript.value.next;
    reader.pages = transcript.value.pages;
  }
  const y = getY();
  renderReader();
  if (seekT !== null) focusLine(seekT);
  else if (keepPages > 1) setY(y);
  if (detail.status === 'rejected') {
    const box = $('.bs-detail-status');
    if (box) status(box, detail.reason.message, true);
  }
  if (transcript.status === 'rejected') {
    status($('.bs-lines'), transcript.reason.message, true);
  }
}

function showReader(id, t = null) {
  if (!VALID_ID.test(id || '')) {
    showHome();
    status($('#bs-list'), 'That conversation link is not valid.', true);
    return;
  }
  const changed = id !== reader.id;
  reader.id = id;
  $('.bs-home').hidden = true;
  $('.bs-reader').hidden = false;
  if (!changed) {
    // same conversation, another moment: load up to it if needed, then show it
    if (t === null) return;
    if (reader.nextStart !== null && (lastSeconds(reader.lines) ?? -1) < t) {
      loadReader(id, Math.max(1, reader.pages), t);
    } else {
      focusLine(t);
    }
    return;
  }
  reader.detail = null;
  reader.lines = [];
  reader.seen = new Set();
  reader.nextStart = null;
  reader.pages = 0;
  reader.notice = '';
  renderReader();
  setY(0);
  loadReader(id, 1, t);
}

function reloadReader() {
  return loadReader(reader.id, Math.max(1, reader.pages));
}

function showHome() {
  reader.gen += 1;
  const wasReading = !!reader.id;
  reader.id = null;
  reader.start = '';
  $('.bs-reader').hidden = true;
  $('.bs-reader').replaceChildren();
  $('.bs-home').hidden = false;
  if (wasReading) setY(reader.fromList ? reader.listScroll : 0);
  reader.fromList = false;
}

function route() {
  const r = hashRoute();
  if (r) showReader(r.id, r.t);
  else showHome();
}

// ---------------------------------------------------------------------- tabs
const TABS = ['conversations', 'items', 'who'];

function showTab(name, focus = false) {
  if (!TABS.includes(name)) name = 'conversations';
  for (const t of TABS) {
    const tab = $(`#bs-tab-${t}`);
    const on = t === name;
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
    tab.tabIndex = on ? 0 : -1;
    $(`#bs-panel-${t}`).hidden = !on;
    if (on && focus) tab.focus();
  }
  try {
    sessionStorage.setItem('bs-tab', name);
  } catch (e) {}
}

const TAB_ICON = {
  conversations: KIND_ICON.note.replace(/width="14" height="14"/, 'width="12" height="12"'),
  items: KIND_ICON.task.replace(/width="14" height="14"/, 'width="12" height="12"'),
  who: _ICON('<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>')
    .replace(/width="14" height="14"/, 'width="12" height="12"'),
};

function tabButton(name, label) {
  return el('button', { type: 'button', className: 'bs-tab', role: 'tab', id: `bs-tab-${name}`,
                        'data-tab': name, 'aria-controls': `bs-panel-${name}`,
                        'aria-selected': 'false', tabindex: '-1' },
    el('span', { html: TAB_ICON[name], 'aria-hidden': 'true' }), label);
}

function buildView() {
  const filter = el('form', { className: 'bs-filters', id: 'bs-filter' },
    el('input', { className: 'bs-input', name: 'query', placeholder: 'Words', 'aria-label': 'Words' }),
    el('input', { className: 'bs-input', name: 'person', placeholder: 'Person', 'aria-label': 'Person' }),
    el('input', { className: 'bs-input', name: 'since', type: 'date', 'aria-label': 'From' }),
    el('input', { className: 'bs-input', name: 'until', type: 'date', 'aria-label': 'To' }),
    el('select', { className: 'bs-input', name: 'kind', 'aria-label': 'Type' },
      el('option', { value: '' }, 'All recordings'),
      el('option', { value: 'conversation' }, 'Conversations'),
      el('option', { value: 'note' }, 'Voice notes')),
    el('button', { type: 'submit', className: 'bs-btn primary' }, 'Search'));
  const panel = (name, ...children) => el('div', { role: 'tabpanel', id: `bs-panel-${name}`,
                                                    'aria-labelledby': `bs-tab-${name}`, hidden: '' },
    ...children);
  const loading = () => el('div', { className: 'bs-status' }, 'Loading…');
  const home = el('div', { className: 'bs-home' },
    el('div', { className: 'bs-bar' },
      el('div', { className: 'bs-tabs', role: 'tablist', 'aria-label': 'Conversations sections' },
        tabButton('conversations', 'Conversations'), tabButton('items', 'Action items'),
        tabButton('who', 'Who is this?')),
      el('div', { className: 'bs-bar-end' },
        el('a', { className: 'bs-btn', href: '/modules/sound/record' }, 'Record a call'),
        el('a', { className: 'bs-btn', href: '/modules/sound/pair' }, 'Pair a phone'),
        el('button', { type: 'button', className: 'bs-btn bs-icon-btn bs-refresh', title: 'Refresh',
                       'aria-label': 'Refresh' }, refreshIcon()))),
    panel('conversations', filter, el('div', { id: 'bs-list' }, loading())),
    panel('items', el('div', { id: 'bs-items' }, loading())),
    panel('who', el('div', { id: 'bs-who' }, loading())));
  return [home, el('div', { className: 'bs-reader', hidden: '' })];
}

function refreshAll() {
  loadWho();
  loadItems();
  loadList();
}

/**
 * Builds the view into `root` and loads it.
 * - embedded: true in the main app's window; the reader is shown in place (no #c= hash).
 * - scroller: the element that scrolls (the window body); the page scrolls by default.
 * - onAsk(text): "Ask in chat" hands its text over instead of linking to /?ask=.
 * - confirm(message): Promise<boolean>; window.confirm by default.
 * Returns { refresh, openConversation(id, t), showTab }.
 */
export function mountSoundModule(root, { embedded = false, scroller = null, onAsk = null,
                                         confirm = null } = {}) {
  if (ctx) throw new Error('the Athena view is already mounted');
  ctx = {
    root, embedded, scroller, onAsk,
    confirm: confirm || ((message) => Promise.resolve(window.confirm(message))),
  };
  root.classList.add('fm-root');
  root.replaceChildren(...buildView());
  ctx.root.querySelectorAll('.bs-tab').forEach((tab) => {
    tab.addEventListener('click', () => showTab(tab.dataset.tab));
    tab.addEventListener('keydown', (e) => {
      const i = TABS.indexOf(tab.dataset.tab);
      if (e.key === 'ArrowRight') showTab(TABS[(i + 1) % TABS.length], true);
      else if (e.key === 'ArrowLeft') showTab(TABS[(i + TABS.length - 1) % TABS.length], true);
    });
  });
  let savedTab = 'conversations';
  try {
    savedTab = sessionStorage.getItem('bs-tab') || savedTab;
  } catch (e) {}
  showTab(savedTab);
  $('#bs-filter').addEventListener('submit', (e) => {
    e.preventDefault();
    loadList();
  });
  $('.bs-refresh').addEventListener('click', refreshAll);
  // Links to a moment of a module transcript (quotes, times, "where it was said") open
  // the reader there. A modified click (new tab) and the "Transcript" buttons keep the link.
  root.addEventListener('click', (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.dataset.external === '1') return;
    const ref = parseModuleLink(a.getAttribute('href'));
    if (!ref) return;
    e.preventDefault();
    openReader(ref.id, '', ref.t);
  });
  refreshAll();
  if (!embedded) {
    window.addEventListener('hashchange', route);
    route();
  }
  return {
    refresh: () => {
      if (reader.id) reloadReader();
      refreshAll();
    },
    openConversation: (id, t = null) => openReader(id, '', t),
    showTab,
  };
}
