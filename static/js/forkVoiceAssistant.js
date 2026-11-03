// Fork: "Voice assistant" card in Settings → Integrations (admin only). Loaded by one hook
// in static/index.html. Makes an API token with only the `voice` scope for a Home Assistant
// conversation agent (fork/voice/routes.py, POST /api/fork/voice/token) and shows what to
// enter in Home Assistant. The token is shown once; it is listed with the other tokens
// above the card as "Voice assistant <date>", where it is revoked.

import { el } from './forkDom.js';

const CARD_ID = 'fork-voice-assistant-card';

/** What Home Assistant needs, as label/value rows (exported for tests). */
export function setupRows(origin, answer) {
  return [
    ['Base URL', `${origin}/v1`],
    ['API key', answer.token],
    ['Model', answer.model || 'odysseus'],
  ];
}

function buildCard() {
  const card = el('div', { class: 'admin-card admin-only', id: CARD_ID });
  card.append(el('h2', {}, 'Voice assistant'));
  const sub = el('div', { class: 'admin-toggle-sub' },
    'Ask Odysseus and Athena by voice through Home Assistant: make a voice token and enter '
    + 'it in an OpenAI-compatible conversation agent there. Voice answers can only look '
    + 'things up (no changes, no email). Each voice conversation is saved as a chat in the '
    + '"Voice" folder. The token is listed above as "Voice assistant <date>"; delete it '
    + 'there to disconnect Home Assistant.');
  sub.style.marginBottom = '8px';
  const result = el('div', { style: 'display:none;font-size:12px;margin:6px 0;' });
  const msg = el('div', { style: 'font-size:11px;margin-top:4px;' });
  const actions = el('div', { style: 'text-align:right;padding:8px 0;' });
  const make = el('button', { type: 'button', class: 'admin-btn-add' }, 'Make a voice token');
  actions.append(make);
  card.append(sub, result, actions, msg);
  return { card, make, result, msg };
}

function showResult(ui, answer) {
  ui.result.replaceChildren();
  ui.result.append(el('div', { style: 'margin-bottom:4px;font-weight:600;' },
    'Copy the API key now: it is shown only once.'));
  setupRows(window.location.origin, answer).forEach(([label, value]) => {
    const row = el('div', { style: 'display:flex;gap:8px;align-items:center;margin:2px 0;' });
    const input = el('input', { type: 'text', class: 'settings-select', readonly: 'readonly',
                                value, 'aria-label': label });
    input.style.flex = '1 1 auto';
    input.addEventListener('focus', () => input.select());
    row.append(el('span', { style: 'min-width:70px;' }, label), input);
    ui.result.append(row);
  });
  ui.result.style.display = '';
}

function wire(ui) {
  ui.make.addEventListener('click', async () => {
    ui.msg.textContent = '';
    ui.make.disabled = true;
    try {
      const res = await fetch('/api/fork/voice/token', { method: 'POST', credentials: 'same-origin' });
      const answer = await res.json().catch(() => ({}));
      if (!res.ok || !answer.token) {
        throw new Error(typeof answer.detail === 'string' ? answer.detail : `HTTP ${res.status}`);
      }
      showResult(ui, answer);
    } catch (e) {
      ui.msg.textContent = e.message || 'Failed';
      ui.msg.style.color = 'var(--red)';
    } finally {
      ui.make.disabled = false;
    }
  });
}

function init() {
  if (document.getElementById(CARD_ID)) return;
  const panel = document.querySelector('[data-settings-panel="integrations"]');
  if (!panel) return;
  const ui = buildCard();
  panel.append(ui.card);
  wire(ui);
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
}
