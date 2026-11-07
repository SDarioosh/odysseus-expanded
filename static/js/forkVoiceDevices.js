// Fork: the "Echo devices" section of the Voice assistant card (Settings → Integrations,
// admin only; static/js/forkVoiceAssistant.js appends it). Lists the admin's Echos from the
// device registry (name, room, app version, last seen) and lets the owner set each of the
// hub-managed settings (contract C6) or leave it to the device. Routes:
// fork/voice/device_settings_routes.py (GET /api/fork/voice/devices,
// PUT /api/fork/voice/devices/{id}/settings). A saved change reaches the Echo within 30 s
// through its announcements feed. Below the devices, "Wake word clips"
// (static/js/forkWakewordClips.js).

import { api, el } from './forkDom.js';
import { buildWakewordClipsSection } from './forkWakewordClips.js';

const DEVICE_DECIDES = '';

/** The value a field's raw input stands for: undefined = the device decides (exported for tests).
 *  Throws an Error with a message when the input isn't valid for the key. */
export function parseValue(key, raw) {
  const text = String(raw ?? '').trim();
  if (text === DEVICE_DECIDES) return undefined;
  if (key.type === 'bool') {
    if (text === 'true') return true;
    if (text === 'false') return false;
  } else if (key.type === 'enum') {
    if ((key.choices || []).includes(text)) return text;
  } else if (key.type === 'int') {
    if (/^\d+$/.test(text)) {
      const n = Number(text);
      if (n >= key.min && n <= key.max) return n;
    }
    throw new Error(`${key.label}: a whole number from ${key.min} to ${key.max}`);
  }
  throw new Error(`${key.label}: not a valid choice`);
}

/** A field's raw input. A number field the browser can't read ("1e", "--3") has the
 *  value "" and validity.badInput: an error, never "the device decides" (exported for tests). */
export function rawValue(input) {
  if (input && input.validity && input.validity.badInput) {
    const label = (input.getAttribute && input.getAttribute('aria-label')) || 'A number';
    throw new Error(`${label}: not a number`);
  }
  return input ? input.value : '';
}

/** The settings object to send, from {key: raw input} (exported for tests). */
export function settingsFrom(keys, raws) {
  const out = {};
  for (const key of keys) {
    const value = parseValue(key, raws[key.key]);
    if (value !== undefined) out[key.key] = value;
  }
  return out;
}

/** "Kitchen · v1400030 · seen 5 min ago" (exported for tests). */
export function deviceLine(device, now = Date.now()) {
  const parts = [];
  if (device.area) parts.push(device.area);
  if (device.app_version_code) parts.push(`app ${device.app_version_code}`);
  if (device.last_seen) {
    const mins = Math.max(0, Math.round((now - device.last_seen) / 60000));
    parts.push(mins < 1 ? 'seen just now'
      : mins < 120 ? `seen ${mins} min ago`
        : mins < 48 * 60 ? `seen ${Math.round(mins / 60)} h ago`
          : `seen ${Math.round(mins / 1440)} days ago`);
  }
  return parts.join(' · ');
}

function field(key, current) {
  const value = current === undefined || current === null ? DEVICE_DECIDES : String(current);
  if (key.type === 'int') {
    const input = el('input', { type: 'number', class: 'settings-select', min: String(key.min),
                                max: String(key.max), step: '1', value,
                                placeholder: 'Device decides', 'aria-label': key.label });
    input.style.width = '130px';
    return input;
  }
  const options = key.type === 'bool'
    ? [['true', 'On'], ['false', 'Off']]
    : (key.choices || []).map((c) => [c, c.charAt(0).toUpperCase() + c.slice(1)]);
  const select = el('select', { class: 'settings-select', 'aria-label': key.label });
  select.style.width = '130px';
  [[DEVICE_DECIDES, 'Device decides'], ...options].forEach(([v, label]) => {
    select.append(el('option', { value: v, selected: v === value ? 'selected' : null }, label));
  });
  return select;
}

function deviceBox(device, keys) {
  const box = el('details', { style: 'margin:6px 0;border:1px solid var(--border);border-radius:6px;padding:6px 8px;' });
  const summary = el('summary', { style: 'cursor:pointer;' },
    el('span', { style: 'font-weight:600;' }, device.name || device.device_id),
    el('span', { class: 'admin-toggle-sub', style: 'margin-left:8px;' }, deviceLine(device)));
  box.append(summary);
  const inputs = {};
  keys.forEach((key) => {
    const input = field(key, (device.settings || {})[key.key]);
    inputs[key.key] = input;
    const row = el('div', { style: 'display:flex;gap:8px;align-items:center;margin:3px 0;' });
    const label = el('span', { style: 'flex:1 1 auto;font-size:12px;' }, key.label);
    if (key.hint) label.append(el('span', { class: 'admin-toggle-sub' }, ` (${key.hint})`));
    row.append(label, input);
    box.append(row);
  });
  const msg = el('div', { style: 'font-size:11px;margin-top:4px;' });
  const save = el('button', { type: 'button', class: 'admin-btn-add' }, 'Save');
  box.append(el('div', { style: 'text-align:right;padding:6px 0 2px;' }, save), msg);
  save.addEventListener('click', async () => {
    msg.textContent = '';
    msg.style.color = '';
    let settings;
    try {
      const raws = {};
      for (const [k, input] of Object.entries(inputs)) raws[k] = rawValue(input);
      settings = settingsFrom(keys, raws);
    } catch (e) {
      msg.textContent = e.message;
      msg.style.color = 'var(--red)';
      return;
    }
    save.disabled = true;
    try {
      const answer = await api(`/api/fork/voice/devices/${encodeURIComponent(device.device_id)}/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings }),
      }, { redirect401: false });
      msg.textContent = `Saved (version ${answer.version}). The Echo picks it up within a minute.`;
    } catch (e) {
      msg.textContent = e.message || 'Failed';
      msg.style.color = 'var(--red)';
    } finally {
      save.disabled = false;
    }
  });
  return box;
}

async function load(list) {
  try {
    const data = await api('/api/fork/voice/devices', {}, { redirect401: false });
    list.replaceChildren();
    const found = Array.isArray(data.devices) ? data.devices : [];
    if (!found.length) {
      list.append(el('div', { class: 'admin-toggle-sub' },
        'No Echo has said hello yet. Link one, and it appears here within an hour of starting.'));
      return;
    }
    found.forEach((d) => list.append(deviceBox(d, data.keys || [])));
  } catch (e) {
    list.replaceChildren(el('div', { class: 'admin-toggle-sub' },
      e.status === 404 ? 'This Odysseus has no device settings yet.' : `Devices: ${e.message || 'failed'}`));
  }
}

/** The section's element; loads the devices once. */
export function buildDevicesSection() {
  const box = el('div', { style: 'border-top:1px solid var(--border);margin-top:10px;padding-top:8px;' });
  box.append(el('div', { style: 'font-weight:600;margin-bottom:4px;' }, 'Echo devices'));
  const sub = el('div', { class: 'admin-toggle-sub' },
    'Set an Echo’s screen, kiosk, recording and follow-up settings from here, or leave '
    + 'each to the device ("Device decides"). The Echo fetches them at start, hourly and '
    + 'within 30 s of a change.');
  sub.style.marginBottom = '6px';
  const list = el('div', {}, el('div', { class: 'admin-toggle-sub' }, 'Loading…'));
  box.append(sub, list, buildWakewordClipsSection());
  load(list);
  return box;
}
