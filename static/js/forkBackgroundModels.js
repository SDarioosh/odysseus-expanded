// Fork: "Background & Summaries" card in Settings → AI Defaults (admin only). Loaded by one
// hook in static/index.html. Two choices in one place, apart from the chat model:
//
// * Background tasks (hub): the upstream `task_endpoint_id` / `task_model` settings, which
//   upstream has no field for. The Today insight, memory, notes and scheduled-task jobs
//   and chat summaries use it; blank falls back to the Utility Model, then the chat model
//   (src/task_endpoint.py).
// * Conversation summaries (Athena): the model the module's own LLM server uses,
//   through /api/fork/summary-model (routes/summary_model_routes.py). Only the model name
//   is sent; the module's LLM server stays what its .env says.

import { postSettings } from './settings/api.js';

const CARD_ID = 'fork-background-models-card';

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
  if (text !== undefined) node.textContent = text;
  return node;
}

function option(value, label, selected) {
  const o = el('option', { value }, label);
  if (selected) o.selected = true;
  return o;
}

function row(label, control) {
  const r = el('div', { class: 'settings-row' });
  r.append(el('label', { class: 'settings-label' }, label), control);
  return r;
}

function note(text) {
  const n = el('div', { class: 'admin-toggle-sub' }, text);
  n.style.margin = '2px 0 6px';
  return n;
}

function message(node, text, isError) {
  node.textContent = text || '';
  node.style.color = isError ? 'var(--red)' : 'var(--fg)';
  if (text && !isError) setTimeout(() => { if (node.textContent === text) node.textContent = ''; }, 2000);
}

async function getJSON(url) {
  const res = await fetch(url, { credentials: 'same-origin' });
  if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status });
  return res.json();
}

function endpointModels(ep) {
  if (!ep) return [];
  return Array.from(new Set([...(ep.models || []), ...(ep.pinned_models || [])].filter(Boolean).map(String)));
}

function buildCard() {
  const card = el('div', { class: 'admin-card admin-only', id: CARD_ID });
  const h2 = el('h2', {}, 'Background & Summaries');
  card.append(h2, note('Use a bigger model for work that runs in the background, separate from your chat model.'));

  const col = el('div', { class: 'settings-col' });
  const hubHead = el('div', { class: 'admin-toggle-label' }, 'Background tasks (Odysseus)');
  const hubNote = note('');
  const epSel = el('select', { class: 'settings-select', id: 'fork-taskEpSelect' });
  const modelSel = el('select', { class: 'settings-select', id: 'fork-taskModelSelect' });
  const hubMsg = el('div', { style: 'font-size:11px;' });

  const modHead = el('div', { class: 'admin-toggle-label' }, 'Conversation summaries (Athena)');
  modHead.style.marginTop = '12px';
  const modNote = note('');
  const modSel = el('select', { class: 'settings-select', id: 'fork-summaryModelSelect' });
  const modInput = el('input', { type: 'text', class: 'settings-select', id: 'fork-summaryModelInput',
                                 placeholder: 'Model name' });
  const modSave = el('button', { type: 'button', class: 'admin-btn-add' }, 'Save');
  const modInputRow = el('div', { style: 'display:flex;gap:6px;flex:1;' });
  modInputRow.append(modInput, modSave);
  const modSelRow = row('Model', modSel);
  const modTextRow = row('Model', modInputRow);
  const modMsg = el('div', { style: 'font-size:11px;' });

  col.append(hubHead, hubNote, row('Endpoint', epSel), row('Model', modelSel), hubMsg,
             modHead, modNote, modSelRow, modTextRow, modMsg);
  card.append(col);
  return { card, epSel, modelSel, hubNote, hubMsg, modNote, modSel, modInput, modSave,
           modSelRow, modTextRow, modMsg };
}

function mount() {
  if (document.getElementById(CARD_ID)) return null;
  const utility = document.getElementById('set-utilityEpSelect');
  const anchor = utility && utility.closest('.admin-card');
  if (!anchor) return null;
  const ui = buildCard();
  anchor.after(ui.card);
  return ui;
}

function wire(ui) {
  let endpoints = [];

  function fillModels(selected) {
    const ep = endpoints.find((e) => e.id === ui.epSel.value);
    ui.modelSel.replaceChildren(option('', ep ? 'First model of the endpoint' : 'Same as Utility Model', !selected));
    const models = endpointModels(ep);
    if (selected && !models.includes(selected)) models.unshift(selected);
    models.forEach((m) => ui.modelSel.append(option(m, m, m === selected)));
    ui.modelSel.disabled = !ep;
  }

  async function saveHub() {
    try {
      await postSettings({ task_endpoint_id: ui.epSel.value || '',
                           task_model: ui.epSel.value ? (ui.modelSel.value || '') : '' });
      message(ui.hubMsg, 'Saved');
    } catch (e) {
      message(ui.hubMsg, e.message || 'Failed to save', true);
    }
  }

  ui.epSel.addEventListener('change', () => { fillModels(''); saveHub(); });
  ui.modelSel.addEventListener('change', saveHub);

  async function loadHub() {
    try {
      const [eps, settings] = await Promise.all([getJSON('/api/model-endpoints'), getJSON('/api/auth/settings')]);
      endpoints = Array.isArray(eps) ? eps : [];
      const current = settings.task_endpoint_id || '';
      ui.epSel.replaceChildren(option('', 'Same as Utility Model', !current));
      endpoints.forEach((ep) => ui.epSel.append(
        option(ep.id, ep.name + (ep.online ? '' : ' (offline)'), ep.id === current)));
      if (current && !endpoints.some((ep) => ep.id === current)) {
        ui.epSel.append(option(current, `${current} (missing)`, true));
      }
      fillModels(settings.task_model || '');
      const fallback = settings.utility_model || settings.default_model || '';
      ui.hubNote.textContent = 'Today insight, memory and notes jobs, scheduled tasks and chat summaries. '
        + (fallback ? `Blank: the Utility Model, else the chat model (now ${fallback}).`
                    : 'Blank: the Utility Model, else the chat model.');
    } catch (e) {
      message(ui.hubMsg, e.status === 403 ? '' : 'Could not load the endpoints.', e.status !== 403);
    }
  }

  function showModule(info) {
    ui.modSel.disabled = false;
    if (!info.connected) {
      ui.modNote.textContent = info.problem || 'Athena is not reachable.';
      ui.modSelRow.style.display = 'none';
      ui.modTextRow.style.display = 'none';
      return;
    }
    ui.modNote.textContent = `Runs on the module's LLM server (${info.base_url}); change the server in its .env. `
      + 'Applies from the next summary on.';
    const def = `Default from .env (${info.default_model})`;
    if (Array.isArray(info.available)) {
      ui.modSelRow.style.display = '';
      ui.modTextRow.style.display = 'none';
      ui.modSel.replaceChildren(option('', def, !info.override));
      const names = info.available.slice();
      if (info.override && !names.includes(info.override)) names.unshift(info.override);
      names.filter((m) => m !== info.default_model)
        .forEach((m) => ui.modSel.append(option(m, m, m === info.override)));
    } else {
      // The server can't list its models: type a name (blank: the .env default).
      ui.modSelRow.style.display = 'none';
      ui.modTextRow.style.display = '';
      ui.modInput.value = info.override || '';
      ui.modInput.placeholder = def;
    }
  }

  async function saveModule(model) {
    ui.modSel.disabled = true;
    try {
      const res = await fetch('/api/fork/summary-model', {
        method: 'PUT', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: model || null }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      showModule(body);
      message(ui.modMsg, `Saved: summaries use ${body.model}`);
    } catch (e) {
      message(ui.modMsg, e.message || 'Failed to save', true);
      loadModule();
    } finally {
      ui.modSel.disabled = false;
    }
  }

  ui.modSel.addEventListener('change', () => saveModule(ui.modSel.value));
  ui.modSave.addEventListener('click', () => saveModule(ui.modInput.value.trim()));

  async function loadModule() {
    try {
      showModule(await getJSON('/api/fork/summary-model'));
    } catch (e) {
      showModule({ connected: false, problem: e.status === 403 ? 'Admin only.' : 'Could not ask Athena.' });
    }
  }

  return () => { loadHub(); loadModule(); };
}

function init() {
  const ui = mount();
  if (!ui) return;
  const load = wire(ui);
  let loaded = false;
  const refresh = () => { loaded = true; load(); };
  // Load when the AI Defaults tab opens (each time: endpoints and the module can change).
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
