// Fork: "Send Athena" toggle in the calendar's event form (src/meeting_auto_join.py).
// Shown only for an event with a Zoom link, off by default. When on, the hub schedules
// one task that sends Athena's Zoom bot in one minute before the start. Called from
// static/js/calendar.js (_showEventForm) for a saved event; renders nothing for
// non-admins, events without a Zoom link, or when the route is missing.

const ROUTE = '/api/modules/athena/auto-join';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

function when(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric',
                                       hour: '2-digit', minute: '2-digit' });
}

// Off and not joinable: nothing to turn on. On stays changeable even when the link stopped
// working (a webinar now, an all-day event), so the owner can still turn it off.
function locked(info) {
  return !info.eligible && !info.on;
}

export function statusText(info) {
  if (!info) return '';
  if (!info.eligible) return info.reason || '';
  if (!info.on) return 'Athena joins and records this Zoom meeting for you. You join too.';
  if (info.busy) return `Athena is busy in an earlier meeting at ${when(info.busy)}; it won't join this one.`;
  if (info.due) return `Athena joins at ${when(info.due)}, one minute early — join the meeting too: it waits for you.`;
  return 'On. Nothing coming up to join.';
}

async function load(uid) {
  const res = await fetch(`${ROUTE}?uid=${encodeURIComponent(uid)}`, { credentials: 'same-origin' });
  if (!res.ok) return null;
  return res.json();
}

async function save(uid, on) {
  const res = await fetch(ROUTE, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Odysseus-Recorder': '1' },
    body: JSON.stringify({ uid, on }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

export async function attach(form, uid) {
  if (!form || !uid || form.querySelector('.fork-send-athena')) return;
  let info;
  try { info = await load(uid); } catch (_) { return; }
  if (!info || !info.zoom || !form.isConnected) return;
  const row = document.createElement('div');
  row.className = 'cal-form-row fork-send-athena';
  row.style.cssText = 'align-items:center;gap:8px;flex-wrap:wrap;';
  row.innerHTML = `
    <label style="font-size:12px;display:flex;align-items:center;gap:6px;cursor:pointer;">
      <input type="checkbox" class="fork-send-athena-toggle" ${info.on ? 'checked' : ''} ${locked(info) ? 'disabled' : ''} />
      <span>Send Athena</span>
    </label>
    <span class="fork-send-athena-text" style="font-size:11px;opacity:0.6;flex:1;min-width:12em;">${esc(statusText(info))}</span>`;
  const actions = form.querySelector('.cal-form-actions');
  if (actions) actions.parentNode.insertBefore(row, actions); else form.appendChild(row);
  const box = row.querySelector('.fork-send-athena-toggle');
  const text = row.querySelector('.fork-send-athena-text');
  box.addEventListener('change', async () => {
    box.disabled = true;
    try {
      info = await save(uid, box.checked);
      text.textContent = statusText(info);
    } catch (err) {
      box.checked = !box.checked;
      text.textContent = String(err.message || err);
    } finally {
      box.disabled = locked(info);
    }
  });
}

export default { attach, statusText };
