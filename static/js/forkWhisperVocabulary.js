// Fork: "Transcription Vocabulary" card in Settings → AI Defaults (admin only). Loaded by
// one hook in static/index.html. Comma-separated words (names, jargon) Athena's whisper
// is nudged toward, through /api/fork/whisper-vocabulary
// (routes/whisper_vocabulary_routes.py). The module stores and cleans the list.

import { el } from './forkDom.js';

const CARD_ID = 'fork-whisper-vocabulary-card';

function note(text) {
  const n = el('div', { class: 'admin-toggle-sub' }, text);
  n.style.margin = '2px 0 6px';
  return n;
}

function message(node, text, isError) {
  node.textContent = text || '';
  node.style.color = isError ? 'var(--red)' : 'var(--fg)';
  if (text && !isError) setTimeout(() => { if (node.textContent === text) node.textContent = ''; }, 2500);
}

function buildCard() {
  const card = el('div', { class: 'admin-card admin-only', id: CARD_ID });
  card.append(el('h2', {}, 'Transcription Vocabulary'),
    note('Words Athena\'s speech-to-text should spell your way: names, products, jargon. '
      + 'Separate them with commas. Keep the list short: a long one can make transcripts worse.'));
  const col = el('div', { class: 'settings-col' });
  const status = note('');
  const text = el('textarea', { class: 'settings-select', id: 'fork-whisperVocabularyText', rows: '3',
                                maxlength: '4000', placeholder: 'RokTalk, Meshtastic, Rokland' });
  text.style.width = '100%';
  text.style.resize = 'vertical';
  text.style.fontFamily = 'inherit';
  const save = el('button', { type: 'button', class: 'admin-btn-add' }, 'Save');
  const actions = el('div', { style: 'display:flex;gap:8px;align-items:center;margin-top:6px;' });
  const msg = el('div', { style: 'font-size:11px;' });
  actions.append(save, msg);
  const prompt = note('');
  col.append(status, text, actions, prompt);
  card.append(col);
  return { card, status, text, save, msg, prompt };
}

function mount() {
  if (document.getElementById(CARD_ID)) return null;
  // After the Background & Summaries card (static/js/forkBackgroundModels.js), else after
  // the Utility Model card.
  const background = document.getElementById('fork-background-models-card');
  const utility = document.getElementById('set-utilityEpSelect');
  const anchor = background || (utility && utility.closest('.admin-card'));
  if (!anchor) return null;
  const ui = buildCard();
  anchor.after(ui.card);
  return ui;
}

function wire(ui) {
  function show(info) {
    const ok = !!info.connected;
    ui.text.disabled = !ok;
    ui.save.disabled = !ok;
    if (!ok) {
      ui.status.textContent = info.problem || 'Athena is not reachable.';
      ui.prompt.textContent = '';
      return;
    }
    const count = (info.words || []).length;
    ui.status.textContent = `${count} of ${info.max_words || 50} words. Used from the next recording on.`;
    ui.text.value = info.text || '';
    ui.prompt.textContent = info.prompt
      ? `Whisper's prompt now (with known speakers' names): ${info.prompt}` : '';
  }

  async function load() {
    try {
      const res = await fetch('/api/fork/whisper-vocabulary', { credentials: 'same-origin' });
      if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status });
      show(await res.json());
    } catch (e) {
      show({ connected: false, problem: e.status === 403 ? 'Admin only.' : 'Could not ask Athena.' });
    }
  }

  async function saveWords() {
    ui.save.disabled = true;
    try {
      const res = await fetch('/api/fork/whisper-vocabulary', {
        method: 'PUT', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: ui.text.value }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`);
      show(body);
      message(ui.msg, 'Saved');
    } catch (e) {
      message(ui.msg, e.message || 'Failed to save', true);
    } finally {
      ui.save.disabled = false;
    }
  }

  ui.save.addEventListener('click', saveWords);
  return load;
}

function init() {
  const ui = mount();
  if (!ui) return;
  const load = wire(ui);
  let loaded = false;
  const refresh = () => { loaded = true; load(); };
  // Load when the AI Defaults tab opens (each time: the module can change).
  document.addEventListener('click', (ev) => {
    if (ev.target.closest && ev.target.closest('[data-settings-tab="ai"]')) refresh();
  });
  const panel = ui.card.closest('[data-settings-panel]');
  if (panel && !panel.classList.contains('hidden') && !loaded) refresh();
  if (panel && 'MutationObserver' in window) {
    new MutationObserver(() => {
      if (!panel.classList.contains('hidden') && !loaded) refresh();
    }).observe(panel, { attributes: true, attributeFilter: ['class'] });
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
