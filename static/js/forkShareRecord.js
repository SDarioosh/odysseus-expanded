// Fork: the Records reader's Share button (static/js/soundModule.js). Gets the record as a
// file from GET /api/modules/sound/conversations/{id}/export (routes/sound_module_routes.py,
// src/record_share.py; Athena makes the file) and hands it to the system share sheet where
// the browser can share files (Android), else downloads it.

const SECTIONS = [
  ['summary', 'Summary'],
  ['action_items', 'Action items'],
  ['transcript', 'Transcript'],
  ['facts', 'Facts'],
];
const FORMATS = [['md', 'Text (Markdown)'], ['html', 'Web page (HTML)']];
const DEFAULTS = { include: ['summary', 'action_items', 'transcript'], format: 'md' };
const STORE_KEY = 'fork.shareRecord';

// The reader redraws itself often; keep the panel open across redraws of the same record.
let openFor = null;

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
    if (saved && Array.isArray(saved.include) && FORMATS.some(([f]) => f === saved.format)) {
      const include = saved.include.filter((s) => SECTIONS.some(([k]) => k === s));
      if (include.length) return { include, format: saved.format };
    }
  } catch (e) {}
  return { ...DEFAULTS, include: [...DEFAULTS.include] };
}

function save(opts) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(opts));
  } catch (e) {}
}

// filename*=UTF-8''… first (Athena's names can have any script), then filename="…".
export function fileName(disposition, format) {
  let name = '';
  const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition || '');
  if (star) {
    try {
      name = decodeURIComponent(star[1].trim());
    } catch (e) {}
  }
  if (!name) {
    const plain = /filename\s*=\s*"([^"]*)"/i.exec(disposition || '');
    name = plain ? plain[1] : '';
  }
  name = name.replace(/[\u0000-\u001f\u007f/\\:*?"<>|]+/g, ' ').trim();
  return name || `Athena record.${format}`;
}

export function exportUrl(id, opts) {
  const q = new URLSearchParams({ format: opts.format, include: opts.include.join(',') });
  return `/api/modules/sound/conversations/${encodeURIComponent(id)}/export?${q}`;
}

async function getFile(id, opts) {
  const res = await fetch(exportUrl(id, opts), { credentials: 'same-origin' });
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('not logged in');
  }
  if (!res.ok) {
    let detail = '';
    try {
      detail = (await res.json()).detail;
    } catch (e) {}
    throw new Error(detail || `HTTP ${res.status}`);
  }
  const type = (res.headers.get('content-type') || 'text/plain').split(';')[0];
  const blob = await res.blob();
  return new File([blob], fileName(res.headers.get('content-disposition'), opts.format), { type });
}

function download(file) {
  const url = URL.createObjectURL(file);
  const a = document.createElement('a');
  a.href = url;
  a.download = file.name;
  a.rel = 'noopener';
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

// A file the share sheet takes, or null. Some Android targets refuse text/markdown, so
// the same bytes as text/plain are tried next.
function shareable(file) {
  if (!navigator.share || !navigator.canShare) return null;
  for (const type of [file.type, 'text/plain']) {
    const f = type === file.type ? file : new File([file], file.name, { type });
    try {
      if (navigator.canShare({ files: [f] })) return f;
    } catch (e) {}
  }
  return null;
}

const canShareFiles = () => !!(navigator.share && navigator.canShare);

// {button, panel}: the button goes in the reader's action row, the panel under it.
export function shareControls(el, id, title) {
  const opts = load();
  const panel = el('div', { className: 'bs-share', hidden: openFor === id ? null : '' });
  const button = el('button', { type: 'button', className: 'bs-btn', title: 'Send this record to someone as a file',
                                'aria-expanded': openFor === id ? 'true' : 'false',
                                onclick: () => {
                                  const open = panel.hidden;
                                  panel.hidden = !open;
                                  openFor = open ? id : null;
                                  button.setAttribute('aria-expanded', open ? 'true' : 'false');
                                } }, 'Share');

  const msg = el('div', { className: 'bs-msg', role: 'status' });
  const say = (text, error = false) => {
    msg.className = error ? 'bs-msg error' : 'bs-msg';
    msg.textContent = text;
  };
  const boxes = SECTIONS.map(([key, label]) => {
    const box = el('input', { type: 'checkbox', value: key,
                              onchange: () => {
                                opts.include = boxes.filter((b) => b.checked).map((b) => b.value);
                                save(opts);
                                refresh();
                              } });
    box.checked = opts.include.includes(key);
    return el('label', { className: 'bs-check' }, box, label);
  });
  const formatButtons = FORMATS.map(([fmt, label]) => el('button', {
    type: 'button', className: 'bs-btn', 'aria-pressed': opts.format === fmt ? 'true' : 'false',
    onclick: () => {
      opts.format = fmt;
      save(opts);
      formatButtons.forEach((b, i) => b.setAttribute('aria-pressed', FORMATS[i][0] === fmt ? 'true' : 'false'));
    } }, label));

  const run = async (how) => {
    if (!opts.include.length) return;
    actions.forEach((b) => { b.disabled = true; });
    say('Preparing the file…');
    try {
      const file = await getFile(id, { ...opts });
      const target = how === 'share' ? shareable(file) : null;
      if (target) {
        try {
          await navigator.share({ files: [target], title });
          say('');
          return;
        } catch (e) {
          if (e && e.name === 'AbortError') {
            say('');
            return;
          }
          // The share sheet can refuse after the wait (no user gesture left): download.
        }
      }
      download(file);
      say(how === 'share' ? `This browser can't share files, so ${file.name} was downloaded.`
        : `Downloaded ${file.name}.`);
    } catch (e) {
      say(`Couldn't make the file: ${e.message}`, true);
    } finally {
      refresh();
    }
  };
  const shareBtn = canShareFiles()
    ? el('button', { type: 'button', className: 'bs-btn primary', onclick: () => run('share') }, 'Share…')
    : null;
  const downloadBtn = el('button', { type: 'button', className: shareBtn ? 'bs-btn' : 'bs-btn primary',
                                     onclick: () => run('download') }, 'Download');
  const actions = [shareBtn, downloadBtn].filter(Boolean);
  function refresh() {
    const none = !opts.include.length;
    actions.forEach((b) => { b.disabled = none; });
    if (none) say('Pick at least one part to share.');
    else if (msg.textContent === 'Pick at least one part to share.') say('');
  }

  panel.append(
    el('div', { className: 'bs-sub' }, 'Share as a file'),
    el('div', { className: 'bs-row' }, ...boxes),
    el('div', { className: 'bs-row' }, ...formatButtons),
    el('div', { className: 'bs-meta' }, 'The file has no links back to Athena or Odysseus. '
      + 'Anyone you send it to can read everything in it.'),
    el('div', { className: 'bs-row' }, ...actions),
    msg);
  refresh();
  return { button, panel };
}
