// Fork: the "Wake word clips" block of the Echo devices section (Settings → Integrations →
// Voice assistant, admin only; static/js/forkVoiceDevices.js appends it). Real recordings
// of the owner saying the wake word, sent by the Echos, to retrain the wake word model:
// counts per kind, the newest clips (play, delete) and a zip download. Routes:
// fork/voice/wakeword_routes.py (GET /api/fork/voice/wakeword/clips,
// GET …/clips/{id}/audio, DELETE …/clips/{id}, GET …/export.zip).

import { api, el } from './forkDom.js';

const BASE = '/api/fork/voice/wakeword';
const PAGE = 50;
const KINDS = [['guided', 'Guided'], ['missed', 'Missed'], ['heard', 'Heard']];

/** "Guided 12 · Missed 3 · Heard 3" (exported for tests). */
export function countsLine(counts) {
  return KINDS.map(([k, label]) => `${label} ${Number((counts || {})[k] || 0)}`).join(' · ');
}

/** "missed · Kitchen Echo · “Quietly” · score 0.42/0.60 · 1.2 s · 5 Nov 14:03" (exported for tests). */
export function clipLine(clip, { formatTime } = {}) {
  const parts = [clip.kind];
  parts.push(clip.device_name || clip.device_id || 'unknown device');
  if (clip.prompt) parts.push(`“${clip.prompt}”`);
  if (typeof clip.score === 'number') {
    parts.push(typeof clip.threshold === 'number'
      ? `score ${clip.score.toFixed(2)}/${clip.threshold.toFixed(2)}`
      : `score ${clip.score.toFixed(2)}`);
  }
  if (clip.duration_ms) parts.push(`${(clip.duration_ms / 1000).toFixed(1)} s`);
  if (clip.recorded_at) {
    const fmt = formatTime || ((ms) => new Date(ms).toLocaleString([], {
      day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }));
    parts.push(fmt(clip.recorded_at));
  }
  return parts.join(' · ');
}

/** The admin's query string for a wake word ('' = all) (exported for tests). */
export function wordQuery(word) {
  return word ? `?wake_word=${encodeURIComponent(word)}` : '';
}

function clipRow(clip, onDeleted) {
  const row = el('div', { style: 'display:flex;gap:8px;align-items:center;margin:3px 0;flex-wrap:wrap;' });
  const label = el('span', { style: 'flex:1 1 220px;font-size:12px;' }, clipLine(clip));
  const player = el('audio', { controls: true, preload: 'none',
                               src: `${BASE}/clips/${encodeURIComponent(clip.id)}/audio` });
  player.style.height = '28px';
  player.style.maxWidth = '220px';
  const del = el('button', { type: 'button', class: 'admin-btn-add', title: 'Delete this clip' }, 'Delete');
  del.addEventListener('click', async () => {
    del.disabled = true;
    try {
      await api(`${BASE}/clips/${encodeURIComponent(clip.id)}`, { method: 'DELETE' },
                { redirect401: false });
      row.remove();
      onDeleted(clip);
    } catch (e) {
      del.disabled = false;
      del.textContent = e.message || 'Failed';
    }
  });
  row.append(label, player, del);
  return row;
}

async function load(state) {
  const { body } = state;
  try {
    const data = await api(`${BASE}/clips${wordQuery(state.word)}`, {}, { redirect401: false });
    const words = Array.isArray(data.wake_words) ? data.wake_words : [];
    if (!state.word && words.length) {
      // the first look: the main wake word, shown alone
      state.word = words.includes('hey_athena') ? 'hey_athena' : words[0];
      return load(state);
    }
    render(state, data, words);
  } catch (e) {
    body.replaceChildren(el('div', { class: 'admin-toggle-sub' },
      e.status === 404 ? 'This Odysseus has no wake word clips yet.'
        : `Wake word clips: ${e.message || 'failed'}`));
  }
  return undefined;
}

function render(state, data, words) {
  const { body } = state;
  body.replaceChildren();
  const clips = Array.isArray(data.clips) ? data.clips : [];
  if (!words.length) {
    body.append(el('div', { class: 'admin-toggle-sub' }, 'No clips yet.'));
    return;
  }
  const head = el('div', { style: 'display:flex;gap:8px;align-items:center;margin:4px 0;flex-wrap:wrap;' });
  const select = el('select', { class: 'settings-select', 'aria-label': 'Wake word' });
  select.style.width = '160px';
  words.forEach((w) => select.append(el('option', { value: w, selected: w === state.word ? 'selected' : null }, w)));
  select.addEventListener('change', () => { state.word = select.value; state.shown = PAGE; load(state); });
  const counts = el('span', { class: 'admin-toggle-sub' }, countsLine(data.counts));
  const download = el('a', { href: `${BASE}/export.zip${wordQuery(state.word)}`, class: 'admin-btn-add',
                             download: '', style: 'text-decoration:none;' }, 'Download for training (.zip)');
  head.append(select, counts, el('span', { style: 'flex:1 1 auto;' }), download);
  body.append(head);
  const list = el('div', {});
  const tally = { ...(data.counts || {}) };
  const onDeleted = (clip) => {
    tally[clip.kind] = Math.max(0, (tally[clip.kind] || 0) - 1);
    counts.textContent = countsLine(tally);
  };
  clips.slice(0, state.shown).forEach((c) => list.append(clipRow(c, onDeleted)));
  body.append(list);
  if (clips.length > state.shown) {
    const more = el('button', { type: 'button', class: 'admin-btn-add' },
      `Show more (${clips.length - state.shown} more)`);
    more.addEventListener('click', () => {
      clips.slice(state.shown, state.shown + PAGE).forEach((c) => list.append(clipRow(c, onDeleted)));
      state.shown += PAGE;
      if (clips.length > state.shown) more.textContent = `Show more (${clips.length - state.shown} more)`;
      else more.remove();
    });
    body.append(el('div', { style: 'text-align:right;padding:4px 0;' }, more));
  }
}

/** The block's element; loads the clips once. */
export function buildWakewordClipsSection() {
  const box = el('div', { style: 'border-top:1px solid var(--border);margin-top:10px;padding-top:8px;' });
  box.append(el('div', { style: 'font-weight:600;margin-bottom:4px;' }, 'Wake word clips'));
  const sub = el('div', { class: 'admin-toggle-sub' },
    'Real recordings of you saying the wake word, sent by the Echos (Menu → Odysseus → '
    + 'Wake word training on the Echo). Download them to retrain the model; they never '
    + 'leave this hub otherwise.');
  sub.style.marginBottom = '6px';
  const body = el('div', {}, el('div', { class: 'admin-toggle-sub' }, 'Loading…'));
  box.append(sub, body);
  load({ body, word: '', shown: PAGE });
  return box;
}
