// Fork: "Phone relay" card in Settings → Integrations (admin only), with the
// **Pair a phone** button that opens /modules/sound/pair (src/phone_pairing.py,
// routes/sound_relay_routes.py). Loaded by one hook in static/index.html. The phone's
// token then shows in the integrations list above the card, where it is revoked.

import { el } from './forkDom.js';

const CARD_ID = 'fork-phone-pairing-card';

const PHONE_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
  + 'style="vertical-align:-2px;margin-right:5px;opacity:0.6" aria-hidden="true">'
  + '<rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>';

function buildCard() {
  const card = el('div', { class: 'admin-card admin-only', id: CARD_ID });
  const h2 = el('h2');
  h2.innerHTML = PHONE_ICON;
  h2.append('Phone relay');
  const sub = el('div', { class: 'admin-toggle-sub' },
    'Set up the Athena relay app on a phone with one QR code: it then uploads the watch\'s '
    + 'recordings and syncs the T5 through Odysseus. Each paired phone is listed above as '
    + '"Phone relay <date>"; delete it there to unpair that phone.');
  sub.style.marginBottom = '8px';
  const actions = el('div', { style: 'text-align:right;padding:8px 0;' });
  const link = el('a', { class: 'admin-btn-add', href: '/modules/sound/pair',
                         style: 'display:inline-flex;align-items:center;gap:5px;text-decoration:none;' },
    'Pair a phone');
  actions.append(link);
  card.append(h2, sub, actions);
  return card;
}

function init() {
  if (document.getElementById(CARD_ID)) return;
  const panel = document.querySelector('[data-settings-panel="integrations"]');
  if (!panel) return;
  panel.append(buildCard());
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
