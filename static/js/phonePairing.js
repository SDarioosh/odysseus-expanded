// Fork: "Pair a phone" page (src/phone_pairing.py, routes/sound_relay_routes.py).
// Makes a single-use pairing code, shows it as a QR code rendered by the server, and
// polls until the phone has redeemed it. The page never sees the phone's token.

const URL_KEY = 'odysseus-pair-url';

const ui = {
  url: document.getElementById('pp-url'),
  create: document.getElementById('pp-create'),
  notice: document.getElementById('pp-notice'),
  qr: document.getElementById('pp-qr'),
  img: document.getElementById('pp-img'),
  state: document.getElementById('pp-state'),
  expiry: document.getElementById('pp-expiry'),
  link: document.getElementById('pp-link'),
  copy: document.getElementById('pp-copy'),
};

let pollTimer = null;
let expiresAt = 0;

function notice(text) {
  ui.notice.textContent = text || '';
}

function savedUrl() {
  try { return localStorage.getItem(URL_KEY) || ''; } catch (e) { return ''; }
}

function saveUrl(value) {
  try { localStorage.setItem(URL_KEY, value); } catch (e) { /* private window */ }
}

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: 'same-origin', ...options });
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('not signed in');
  }
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty answer */ }
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

function showExpiry() {
  const left = Math.max(0, Math.round((expiresAt - Date.now()) / 1000));
  ui.expiry.textContent = left
    ? `Valid for ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')} more.`
    : 'Expired: show a new code.';
}

async function poll(id) {
  showExpiry();
  let data;
  try {
    data = await api(`/api/modules/sound/pair/${encodeURIComponent(id)}`);
  } catch (e) {
    return; // try again on the next tick
  }
  if (data.status === 'paired') {
    stopPolling();
    ui.img.style.opacity = '0.15';
    ui.state.textContent = `Paired: "${data.token_name}". You can close this page.`;
    ui.expiry.textContent = '';
  } else if (data.status === 'expired' || data.status === 'unknown') {
    stopPolling();
    ui.img.style.opacity = '0.15';
    ui.state.textContent = 'This code expired.';
    ui.expiry.textContent = 'Show a new code to pair.';
  }
}

async function create() {
  notice('');
  stopPolling();
  const url = ui.url.value.trim();
  ui.create.disabled = true;
  try {
    const data = await api('/api/modules/sound/pair', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    saveUrl(data.url);
    ui.url.value = data.url;
    ui.img.src = data.qr;
    ui.img.style.opacity = '';
    ui.link.textContent = data.link;
    ui.state.textContent = 'Waiting for the phone…';
    ui.qr.hidden = false;
    expiresAt = Date.now() + data.expires_in * 1000;
    if (data.module && !data.module.configured) {
      notice(`The phone can pair, but uploads won't work yet: ${data.module.problem}`);
    } else if (/^http:\/\//.test(data.url) && !/^http:\/\/(localhost|127\.|10\.|192\.168\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)/.test(data.url)) {
      notice('This address is plain http:// and not a LAN or Tailscale address: the token and ' +
             'audio could be read on the way. Use https:// or the Tailscale address.');
    }
    pollTimer = setInterval(() => poll(data.id), 2000);
    showExpiry();
  } catch (e) {
    notice(`Couldn't make a pairing code: ${e.message || e}`);
  } finally {
    ui.create.disabled = false;
  }
}

ui.url.value = savedUrl() || window.location.origin;
ui.create.addEventListener('click', create);
ui.url.addEventListener('keydown', (e) => { if (e.key === 'Enter') create(); });
ui.copy.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(ui.link.textContent);
    ui.copy.textContent = 'Copied';
    setTimeout(() => { ui.copy.textContent = 'Copy link'; }, 1500);
  } catch (e) {
    notice('Copying needs a secure page; select the link and copy it by hand.');
  }
});
