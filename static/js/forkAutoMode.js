// Fork: Auto/Ask button in the chat composer, and the default in Settings → Tools.
// Loaded by one hook in static/index.html; what Auto allows is in src/auto_mode.py.
//
// * Composer button (next to web search and shell): each chat is Auto or Ask. Auto runs
//   safe tools (web reads, reading your own data, the calculator) without asking; Ask
//   keeps upstream's approval prompts. A chat that never chose uses the default.
// * The choice travels with every /api/chat_stream request as `fork_tool_mode`, added
//   to the FormData here, so upstream's send code is untouched.
// * Choices are kept per chat in this browser (localStorage), and in the server's memory
//   for the running chat.
// * Settings → Tools (admin): the default for new chats (GET/PUT /api/fork/auto-mode).

import { getCurrentSessionId } from './sessions.js';

const FIELD = 'fork_tool_mode';
const STORE_KEY = 'fork.toolModeByChat';
const BTN_ID = 'fork-auto-mode-btn';
const CARD_ID = 'fork-auto-mode-card';
const MAX_CHATS = 500;

let defaultAuto = true;       // server default, loaded below
let pendingChoice = null;     // chosen on a new chat before it has an id

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
  if (text !== undefined) node.textContent = text;
  return node;
}

// ── Per-chat choice ──────────────────────────────────────────────────────────

function loadChoices() {
  try {
    const value = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
    return value && typeof value === 'object' ? value : {};
  } catch (_) {
    return {};
  }
}

function saveChoice(sessionId, mode) {
  const choices = loadChoices();
  delete choices[sessionId];
  choices[sessionId] = mode;
  const ids = Object.keys(choices);
  ids.slice(0, Math.max(0, ids.length - MAX_CHATS)).forEach((id) => delete choices[id]);
  try { localStorage.setItem(STORE_KEY, JSON.stringify(choices)); } catch (_) { /* private mode */ }
}

function modeFor(sessionId) {
  if (!sessionId) return pendingChoice || (defaultAuto ? 'auto' : 'ask');
  const stored = loadChoices()[sessionId];
  if (stored === 'auto' || stored === 'ask') return stored;
  if (pendingChoice) {
    // The new chat just got its id: keep what was chosen before the first message.
    saveChoice(sessionId, pendingChoice);
    const mode = pendingChoice;
    pendingChoice = null;
    return mode;
  }
  return defaultAuto ? 'auto' : 'ask';
}

function currentSession() {
  try { return getCurrentSessionId() || ''; } catch (_) { return ''; }
}

// ── Send the choice with every chat request ─────────────────────────────────

const realFetch = window.fetch.bind(window);
window.fetch = function forkAutoModeFetch(input, init) {
  try {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const body = init && init.body;
    if (/\/api\/chat_stream(?:[?#]|$)/.test(url) && body instanceof FormData && !body.has(FIELD)) {
      body.append(FIELD, modeFor(String(body.get('session') || '')));
    }
  } catch (_) { /* never block a send */ }
  return realFetch(input, init);
};

// ── Composer button ─────────────────────────────────────────────────────────

const ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>';

function render(btn) {
  const auto = modeFor(currentSession()) === 'auto';
  btn.classList.toggle('active', auto);
  btn.setAttribute('aria-pressed', auto ? 'true' : 'false');
  btn.querySelector('span').textContent = auto ? 'Auto' : 'Ask';
  btn.title = auto
    ? 'Auto: safe tools run without asking (web pages, reading your own data, calculator). '
      + 'Sending, changing or deleting still asks. Click to switch this chat to Ask.'
    : 'Ask: every tool after a web page waits for your approval. Click to switch this chat to Auto.';
}

function mountButton() {
  if (document.getElementById(BTN_ID)) return null;
  const anchor = document.getElementById('bash-toggle-btn') || document.getElementById('web-toggle-btn');
  if (!anchor) return null;
  const btn = el('button', { type: 'button', class: 'input-icon-btn', id: BTN_ID,
                             'aria-label': 'Tool approvals: Auto or Ask' });
  btn.style.gap = '4px';
  btn.style.width = 'auto';
  btn.innerHTML = ICON;
  btn.append(el('span', { style: 'font-size:11px;' }, 'Auto'));
  anchor.after(btn);
  btn.addEventListener('click', () => {
    const next = modeFor(currentSession()) === 'auto' ? 'ask' : 'auto';
    const sid = currentSession();
    if (sid) saveChoice(sid, next);
    else pendingChoice = next;
    render(btn);
  });
  window.addEventListener('odysseus:session-changed', (ev) => {
    const sid = ev && ev.detail && ev.detail.sessionId;
    if (sid && pendingChoice) modeFor(sid);   // new chat got its id: keep the choice
    else if (!sid) pendingChoice = null;       // a fresh chat starts on the default
    render(btn);
  });
  render(btn);
  return btn;
}

// ── Settings → Tools: the default ───────────────────────────────────────────

function buildCard() {
  const card = el('div', { class: 'admin-card admin-only', id: CARD_ID, style: 'margin-bottom:12px;' });
  const head = el('div', { style: 'display:flex;align-items:center;gap:8px;' });
  const h2 = el('h2', { style: 'margin:0;' }, 'Auto mode for new chats');
  const sw = el('label', { class: 'admin-switch', title: 'New chats start in Auto' });
  const input = el('input', { type: 'checkbox', id: 'fork-autoModeToggle' });
  sw.append(input, el('span', { class: 'admin-slider' }));
  head.append(h2, el('span', { style: 'flex:1' }), sw);
  const sub = el('div', { class: 'admin-toggle-sub', style: 'margin:6px 0 4px;' },
    'Each chat has an Auto / Ask button next to web search in the message box. This sets '
    + 'where new chats start. Auto runs safe tools without asking, even after the chat has '
    + 'read a web page: web search, reading a page you linked or one a search or page linked '
    + 'to, reading your own notes, calendar, tasks and documents, and a calculator.');
  const still = el('div', { class: 'admin-toggle-sub', style: 'margin:0 0 4px;' },
    'Auto still asks before: sending email, creating or changing notes, tasks or events, '
    + 'deleting, terminal and code, file writes, settings, and opening a web address the AI made up.');
  const msg = el('div', { style: 'font-size:11px;' });
  card.append(head, sub, still, msg);
  return { card, input, msg };
}

function show(ui, text, isError) {
  ui.msg.textContent = text || '';
  ui.msg.style.color = isError ? 'var(--red)' : 'var(--fg)';
  if (text && !isError) setTimeout(() => { if (ui.msg.textContent === text) ui.msg.textContent = ''; }, 2000);
}

function mountCard() {
  if (document.getElementById(CARD_ID)) return;
  const anchorInput = document.getElementById('set-agentMaxTools');
  const anchor = anchorInput && anchorInput.closest('.admin-card');
  if (!anchor) return;
  const ui = buildCard();
  anchor.after(ui.card);
  ui.input.checked = defaultAuto;
  ui.input.addEventListener('change', async () => {
    const want = ui.input.checked;
    ui.input.disabled = true;
    try {
      const res = await realFetch('/api/fork/auto-mode', {
        method: 'PUT', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: want }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      defaultAuto = body.enabled === true;
      ui.input.checked = defaultAuto;
      show(ui, defaultAuto ? 'New chats start in Auto' : 'New chats start in Ask');
      const btn = document.getElementById(BTN_ID);
      if (btn) render(btn);
    } catch (e) {
      ui.input.checked = !want;
      show(ui, e.message || 'Failed to save', true);
    } finally {
      ui.input.disabled = false;
    }
  });
}

// ── Start ───────────────────────────────────────────────────────────────────

async function loadDefault() {
  try {
    const res = await realFetch('/api/fork/auto-mode', { credentials: 'same-origin' });
    if (res.ok) defaultAuto = (await res.json()).enabled !== false;
  } catch (_) { /* keep Auto */ }
  const btn = document.getElementById(BTN_ID);
  if (btn) render(btn);
  const toggle = document.getElementById('fork-autoModeToggle');
  if (toggle) toggle.checked = defaultAuto;
}

function init() {
  mountButton();
  mountCard();
  loadDefault();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
