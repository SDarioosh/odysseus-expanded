// Fork: "Voice assistant" card in Settings → Integrations (admin only). Loaded by one hook
// in static/index.html. Makes an API token with only the `voice` scope for a Home Assistant
// conversation agent (fork/voice/routes.py, POST /api/fork/voice/token) and shows what to
// enter in Home Assistant. The token is shown once; it is listed with the other tokens
// above the card as "Voice assistant <date>", where it is revoked.
// "Set up Home Assistant" (fork/voice/ha_routes.py) does the Home Assistant side in one
// go: the owner signs in to HA once, Odysseus sets up the agent and the Assist pipeline,
// then signs out of HA again. The card shows the last run's steps.
// "Echo devices" (static/js/forkVoiceDevices.js) sets each Echo's hub-managed settings.

import { api, el } from './forkDom.js';
import { buildDevicesSection } from './forkVoiceDevices.js';

const CARD_ID = 'fork-voice-assistant-card';

/** What Home Assistant needs, as label/value rows (exported for tests). */
export function setupRows(origin, answer) {
  return [
    ['Base URL', `${origin}/api/chat/voice/v1`],
    ['API key', answer.token],
    ['Model', answer.model || 'odysseus'],
  ];
}

/** One line per step of a setup report (exported for tests). */
export function reportLines(report) {
  const marks = { ok: '\u2713', changed: '\u2713', error: '\u2717', skipped: '\u2013' };
  return (report && Array.isArray(report.steps) ? report.steps : []).map((s) =>
    `${marks[s.status] || '?'} ${s.step}: ${s.status}${s.detail ? ` \u2014 ${s.detail}` : ''}`);
}

function labelled(label, input) {
  const row = el('div', { style: 'display:flex;gap:8px;align-items:center;margin:4px 0;' });
  input.style.flex = '1 1 auto';
  row.append(el('span', { style: 'min-width:150px;font-size:12px;' }, label), input);
  return row;
}

function buildHaSection() {
  const box = el('div', { style: 'border-top:1px solid var(--border);margin-top:10px;padding-top:8px;' });
  box.append(el('div', { style: 'font-weight:600;margin-bottom:4px;' }, 'Set up Home Assistant'));
  const sub = el('div', { class: 'admin-toggle-sub' },
    'Sign in to Home Assistant once (an administrator) and Odysseus sets it up: a voice '
    + 'token for Home Assistant, the Extended OpenAI Conversation agent (model odysseus, no '
    + 'tools) and an "Odysseus" assistant with local Whisper and Piper, made the preferred '
    + 'one. Install the Whisper and Piper add-ons and Extended OpenAI Conversation (HACS) in '
    + 'Home Assistant first. Odysseus signs out of Home Assistant when it is done.');
  sub.style.marginBottom = '6px';
  const haUrl = el('input', { type: 'url', class: 'settings-select', placeholder: 'http://homeassistant.local:8123',
                              'aria-label': 'Home Assistant address' });
  const odyUrl = el('input', { type: 'url', class: 'settings-select', value: window.location.origin,
                               'aria-label': 'Odysseus address as Home Assistant reaches it' });
  const signinUrl = el('input', { type: 'url', class: 'settings-select',
                                  placeholder: 'Empty: the Home Assistant address above',
                                  'aria-label': 'Home Assistant address this browser signs in at' });
  const signinHint = el('div', { class: 'admin-toggle-sub', style: 'margin:0 0 4px;' },
    'Only when this browser can\u2019t reach the address above (away from home): Home '
    + 'Assistant over Tailscale, e.g. http://<server>.<tailnet>.ts.net:8123. Odysseus itself '
    + 'still talks to Home Assistant at the address above.');
  const actions = el('div', { style: 'text-align:right;padding:8px 0;' });
  const connect = el('button', { type: 'button', class: 'admin-btn-add' }, 'Set up Home Assistant');
  actions.append(connect);
  const msg = el('div', { style: 'font-size:11px;margin-top:4px;' });
  const last = el('div', { style: 'display:none;font-size:12px;margin:6px 0;white-space:pre-wrap;' });
  box.append(sub, labelled('Home Assistant address', haUrl),
    labelled('Odysseus address (as Home Assistant reaches it)', odyUrl),
    labelled('Sign in to Home Assistant at (optional)', signinUrl), signinHint,
    actions, msg, last);
  return { box, haUrl, odyUrl, signinUrl, connect, msg, last };
}

function showReport(ha, state) {
  const lines = reportLines(state.report);
  if (state.running) lines.unshift('A setup is running\u2026');
  ha.last.textContent = lines.length ? ['Last setup:', ...lines].join('\n') : '';
  ha.last.style.display = lines.length ? '' : 'none';
}

async function loadHa(ha) {
  try {
    const state = await api('/api/fork/voice/ha', {}, { redirect401: false });
    if (state.ha_url && !ha.haUrl.value) ha.haUrl.value = state.ha_url;
    // the address HA was last set up with (e.g. the server's home-network one), not the browser's
    if (state.odysseus_url) ha.odyUrl.value = state.odysseus_url;
    if (state.signin_url && !ha.signinUrl.value) ha.signinUrl.value = state.signin_url;
    showReport(ha, state);
  } catch (e) { /* not an admin, or an older hub: the section still works */ }
}

function wireHa(ha) {
  ha.connect.addEventListener('click', async () => {
    ha.msg.textContent = '';
    ha.connect.disabled = true;
    try {
      const answer = await api('/api/fork/voice/ha/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ha_url: ha.haUrl.value.trim(), odysseus_url: ha.odyUrl.value.trim(),
                               signin_url: ha.signinUrl.value.trim() }),
      });
      if (!answer.authorize_url) throw new Error('No sign-in address');
      ha.msg.textContent = 'Opening Home Assistant\u2026';
      ha.msg.style.color = '';
      window.location.assign(answer.authorize_url);
    } catch (e) {
      ha.msg.textContent = e.message || 'Failed';
      ha.msg.style.color = 'var(--red)';
      ha.connect.disabled = false;
    }
  });
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
  const ha = buildHaSection();
  card.append(sub, result, actions, msg, ha.box, buildDevicesSection());
  return { card, make, result, msg, ha };
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
  wireHa(ui.ha);
  loadHa(ui.ha);
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
}
