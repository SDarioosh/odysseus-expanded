// Fork: the News view — the stored morning news brief (fork/news/brief.py) with its
// category editor, delivery time and Refresh. Mounted by the News window in the main app
// (static/js/forkModuleWindows.js) and by the standalone page (static/news.html). Data:
// fork/routes/news_routes.py. Contract: docs/fork/news-brief.md.
//
// Each category can have RSS/Atom feeds (one URL per line); the server checks them
// (http(s), at most MAX_FEEDS) and reads them only through its public-address guard.
//
// Every text from the brief (titles, summaries, sources) comes from web pages or the model
// and is set as text, never as HTML; links are kept only when they are http(s).

import { el, api } from './forkDom.js';

let root = null; // one mount per document
const $ = (sel) => root.querySelector(sel);

const POLL_MS = 4000;
const POLL_LIMIT_MS = 15 * 60 * 1000;
const MAX_CATEGORIES = 20;
const MAX_FEEDS = 10;

/** The feed URLs typed in a category's box: one per line (any whitespace splits). */
export function feedList(text) {
  return String(text || '').split(/\s+/).map(s => s.trim()).filter(Boolean);
}

function sendJSON(path, method, body) {
  return api(path, {
    method, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

function status(text, error = false) {
  $('.nb-main').replaceChildren(el('div', { className: error ? 'bs-status error' : 'bs-status' }, text));
}

function note(text, error = false) {
  const n = $('.nb-note');
  n.textContent = text || '';
  n.className = error ? 'bs-status error nb-note' : 'bs-status nb-note';
  n.hidden = !text;
}

/** An article link: only http(s) URLs; anything else is no link. */
export function safeUrl(href) {
  const text = String(href || '').trim();
  return /^https?:\/\/[^\s/\\]/i.test(text) ? text : null;
}

function outLink(href, text, className) {
  const url = safeUrl(href);
  if (!url) return el('span', { className }, text);
  return el('a', { className, href: url, target: '_blank', rel: 'noopener noreferrer' }, text);
}

function timeLabel(iso) {
  const d = new Date(iso || '');
  if (isNaN(d.getTime())) return '';
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return sameDay ? time : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
}

function storyRow(story) {
  const also = Array.isArray(story.also) ? story.also.filter(a => a && a.source) : [];
  const meta = [story.source, timeLabel(story.published)].filter(Boolean).join(' · ');
  const alsoLine = also.length ? el('div', { className: 'bs-meta nb-also' }, 'Also: ',
    ...also.flatMap((a, i) => [i ? ', ' : null, outLink(a.url, a.source, 'nb-src')])) : null;
  return el('li', { className: 'nb-story' },
    outLink(story.url, story.title || story.source || 'Untitled', 'bs-title'),
    story.summary ? el('div', { className: 'nb-summary' }, story.summary) : null,
    meta ? el('div', { className: 'bs-meta' }, meta) : null,
    alsoLine);
}

function categoryBlock(cat) {
  const stories = Array.isArray(cat.stories) ? cat.stories : [];
  return [
    el('div', { className: 'bs-section' }, cat.name || ''),
    el('div', { className: 'bs-card' },
      cat.overview ? el('p', { className: 'nb-overview' }, cat.overview) : null,
      cat.summarised === false
        ? el('div', { className: 'bs-meta nb-flag' }, 'Not summarised: headlines and snippets only.') : null,
      stories.length ? el('ul', { className: 'bs-items' }, ...stories.map(storyRow))
        : el('div', { className: 'bs-empty' }, 'No stories.')),
  ];
}

function render(brief) {
  const cats = Array.isArray(brief.categories) ? brief.categories : [];
  const errors = Array.isArray(brief.errors) ? brief.errors.filter(e => e && e.category) : [];
  const written = timeLabel(brief.generated);
  const parts = [el('div', { className: 'bs-meta nb-date' },
    [brief.date, written ? `Written ${written}` : ''].filter(Boolean).join(' · '))];
  for (const cat of cats) parts.push(...categoryBlock(cat));
  if (!cats.length) parts.push(el('div', { className: 'bs-status' }, 'No stories today.'));
  if (errors.length) {
    parts.push(el('div', { className: 'bs-meta nb-errors' },
      'Problems: ' + errors.map(e => `${e.category} (${e.error || 'no results'})`).join(', ')));
  }
  $('.nb-main').replaceChildren(...parts);
}

// Today's brief, or the 404 body ({error, refreshing, refresh_error}) when there is none.
async function fetchToday() {
  try {
    return await api('/api/news/today');
  } catch (e) {
    if (e.status === 404) return { missing: true, ...(e.data || {}) };
    throw e;
  }
}

async function load() {
  try {
    const brief = await fetchToday();
    if (brief.missing) {
      status(config && !config.categories.length
        ? 'No brief yet. Add categories below; the brief is written every morning at the delivery time, and Refresh writes it now.'
        : 'No brief for today yet. It is written at the delivery time; Refresh writes it now.');
    } else {
      render(brief);
    }
  } catch (e) {
    status(`Couldn't load today's brief: ${e.message}`, true);
  }
}

// Refresh runs in the background (gathering and the model outlast a request); poll until
// the brief's `generated` changes or the server says it stopped.
async function refresh() {
  const btn = $('.nb-refresh');
  btn.disabled = true;
  btn.textContent = 'Writing…';
  note('Writing a new brief. Searching and the local model can take a few minutes.');
  try {
    const started = await sendJSON('/api/news/refresh', 'POST');
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
    note(`Couldn't write the brief: ${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Refresh';
  }
}

// ------------------------------------------------------------------ settings
let config = null; // {categories, delivery_time, default_stories, enabled, timezone}

function toTimeInput(text) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(text || ''));
  return m ? `${m[1].padStart(2, '0')}:${m[2]}` : '07:00';
}

function categoryEditor(cat, index, count) {
  const name = el('input', { className: 'bs-input nb-name', type: 'text', maxlength: '60',
                             placeholder: 'Name, e.g. Formula 1', 'aria-label': 'Category name',
                             value: cat.name || '' });
  const query = el('input', { className: 'bs-input nb-query', type: 'text', maxlength: '200',
                              placeholder: 'Search query (optional; the name is used otherwise, or feeds only)',
                              'aria-label': 'Search query', value: cat.query || '' });
  const stories = el('input', { className: 'bs-input nb-stories', type: 'number', min: '1', max: '10',
                                placeholder: String(config.default_stories || 5),
                                title: 'Stories (empty: the default)', 'aria-label': 'Stories',
                                value: cat.stories ? String(cat.stories) : '' });
  const feeds = el('textarea', { className: 'bs-input nb-feeds', rows: '2', spellcheck: 'false',
                                 placeholder: `RSS or Atom feeds (optional), one URL per line, at most ${MAX_FEEDS}`,
                                 'aria-label': 'RSS or Atom feeds' });
  feeds.value = (Array.isArray(cat.feeds) ? cat.feeds : []).join('\n');
  const on = el('input', { type: 'checkbox', 'aria-label': 'On' });
  on.checked = cat.enabled !== false;
  const sync = () => {
    cat.name = name.value;
    cat.query = query.value;
    cat.stories = stories.value ? Number(stories.value) : null;
    cat.enabled = on.checked;
    cat.feeds = feedList(feeds.value);
  };
  for (const input of [name, query, stories, on, feeds]) input.addEventListener('input', sync);
  on.addEventListener('change', sync);
  const move = (delta) => {
    sync();
    const list = config.categories;
    const [row] = list.splice(index, 1);
    list.splice(index + delta, 0, row);
    renderEditor();
  };
  const remove = () => {
    config.categories.splice(index, 1);
    renderEditor();
  };
  return el('div', { className: 'nb-cat' },
    el('label', { className: 'bs-check', title: 'Include in the brief' }, on, 'On'),
    name, query, stories,
    el('div', { className: 'nb-cat-actions' },
      el('button', { type: 'button', className: 'bs-btn bs-icon-btn', title: 'Move up',
                     'aria-label': 'Move up', disabled: index === 0, onclick: () => move(-1) }, '↑'),
      el('button', { type: 'button', className: 'bs-btn bs-icon-btn', title: 'Move down',
                     'aria-label': 'Move down', disabled: index === count - 1, onclick: () => move(1) }, '↓'),
      el('button', { type: 'button', className: 'bs-btn bs-icon-btn', title: 'Delete',
                     'aria-label': `Delete ${cat.name || 'category'}`, onclick: remove }, '✕')),
    feeds);
}

function renderEditor() {
  const box = $('.nb-cats');
  const cats = config.categories;
  box.replaceChildren(...(cats.length ? cats.map((c, i) => categoryEditor(c, i, cats.length))
    : [el('div', { className: 'bs-empty' }, 'No categories yet.')]));
  $('.nb-add').disabled = cats.length >= MAX_CATEGORIES;
}

function editorMsg(text, error = false) {
  const m = $('.nb-save-msg');
  m.textContent = text || '';
  m.classList.toggle('error', error);
}

async function save() {
  const btn = $('.nb-save');
  btn.disabled = true;
  editorMsg('');
  try {
    const body = {
      delivery_time: $('.nb-time').value || '07:00',
      categories: config.categories
        .map(c => ({ name: (c.name || '').trim(), query: (c.query || '').trim(),
                     enabled: c.enabled !== false, stories: c.stories || null,
                     feeds: Array.isArray(c.feeds) ? c.feeds : [] }))
        .filter(c => c.name || c.query || c.feeds.length),
    };
    applyConfig(await sendJSON('/api/news/config', 'PUT', body));
    editorMsg('Saved.');
  } catch (e) {
    editorMsg(`Couldn't save: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

function applyConfig(data) {
  config = {
    ...data,
    categories: (Array.isArray(data.categories) ? data.categories : [])
      .map(c => ({ ...c, feeds: Array.isArray(c.feeds) ? [...c.feeds] : [] })),
  };
  $('.nb-time').value = toTimeInput(config.delivery_time);
  $('.nb-tz').textContent = config.timezone ? `(${config.timezone})` : '';
  $('.nb-off').hidden = config.enabled !== false;
  renderEditor();
}

function settingsPanel() {
  const time = el('input', { className: 'bs-input nb-time', type: 'time', step: '60',
                             'aria-label': 'Delivery time' });
  const add = el('button', { type: 'button', className: 'bs-btn nb-add', onclick: () => {
    config.categories.push({ name: '', query: '', enabled: true, stories: null, feeds: [] });
    renderEditor();
    const names = root.querySelectorAll('.nb-name');
    if (names.length) names[names.length - 1].focus();
  } }, 'Add category');
  return el('details', { className: 'nb-settings' },
    el('summary', { className: 'bs-section' }, 'Categories and delivery time'),
    el('div', { className: 'bs-card' },
      el('div', { className: 'bs-status nb-off', hidden: true },
        'The news brief is turned off on this server (NEWS_BRIEF_ENABLED=off).'),
      el('div', { className: 'bs-meta' },
        'Only these names or queries are searched, through the hub\'s own SearXNG; ' +
        'feeds are read directly (public addresses only). A category with feeds and no ' +
        'query uses its feeds only. Stories per category: empty means the default.'),
      el('div', { className: 'nb-cats' }),
      el('div', { className: 'bs-row nb-time-row' },
        add,
        el('label', { className: 'nb-time-label' }, 'Delivery time ', time),
        el('span', { className: 'bs-meta nb-tz' })),
      el('div', { className: 'bs-row' },
        el('button', { type: 'button', className: 'bs-btn primary nb-save', onclick: save }, 'Save'),
        el('span', { className: 'bs-meta bs-msg nb-save-msg' }))));
}

async function loadConfig() {
  try {
    applyConfig(await api('/api/news/config'));
    if (!config.categories.length) $('.nb-settings').open = true;
  } catch (e) {
    $('.nb-cats').replaceChildren(el('div', { className: 'bs-status error' },
      `Couldn't load the categories: ${e.message}`));
  }
}

/** Builds the view into `target` and loads it. Returns { refresh } (reloads the stored brief). */
export function mountNews(target) {
  if (root) throw new Error('the News view is already mounted');
  root = target;
  root.classList.add('fm-root');
  const button = el('button', { type: 'button', className: 'bs-btn nb-refresh',
                                title: 'Write a new brief now' }, 'Refresh');
  root.replaceChildren(
    el('div', { className: 'bs-row', style: 'justify-content:flex-end;margin:0 0 8px' }, button),
    el('div', { className: 'bs-status nb-note', hidden: true }),
    el('div', { className: 'nb-main' }, el('div', { className: 'bs-status' }, 'Loading…')),
    settingsPanel());
  button.addEventListener('click', refresh);
  loadConfig().then(load);
  return { refresh: load };
}
