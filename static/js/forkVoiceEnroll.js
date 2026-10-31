// Fork: "Enroll My Voice" card in Settings → AI Defaults (admin only). Loaded by one hook
// in static/index.html. Guides the owner through recording a voice profile on the watch:
// Start tells Athena to enroll the next recording (routes/voice_enroll_routes.py →
// Athena's /speakers/enroll/next), the card shows statements to read one at a time, and
// follows the recording until Athena has enrolled it. The recording makes no conversation.

import { el } from './forkDom.js';

const CARD_ID = 'fork-voice-enroll-card';
const POLL_MS = 4000;

// Read aloud at a normal pace while the watch records (~75 s with the free talk). Varied
// sounds, numbers, names, questions, a louder and a quieter line and free talk, so the
// voiceprint covers how the owner really talks, not only a "reading voice".
export const STATEMENTS = [
  { text: "Hi, this is my voice profile. I'm recording it on my watch, so Athena knows when I'm the one talking." },
  { text: 'On Tuesday I walked to the market, bought apples, fresh bread and a jar of honey, and was home again by half past ten.' },
  { text: "Could you call me back tomorrow morning? My number ends in four, seven, two, nine, and I'm free after eight thirty." },
  { text: 'The quick brown fox jumps over the lazy dog, while six bright yellow birds sing loudly in the old oak tree.' },
  { text: 'We should move the project meeting to Thursday. Sarah, Jonas and Priya can join, but Michael is still on holiday.' },
  { how: 'A little louder, as if to someone across the room:',
    text: 'Hey, can you hear me over there? Grab the blue box from the top shelf, please!' },
  { how: 'Quietly, as if to someone right next to you:',
    text: "Let's keep this between us for now. I'll tell everyone else on Friday." },
  { text: 'Honestly, I think that went really well! What do you think? Should we try it again next week, or wait a little?' },
  { text: 'Numbers and dates: the fifteenth of March, twenty twenty-six, at three fifteen in the afternoon, for ninety minutes.' },
  { how: 'Now just talk for about 30 seconds, the way you talk with a friend:',
    text: "What you did today, what you're working on, or your plans for the weekend. No need to be tidy: pauses and \"um\" are fine." },
  { text: "Thanks, that's everything. I'm finishing the recording now.",
    after: 'Now stop the recording on the watch.' },
];

const TIPS = [
  'Wear the watch the way you always do. Don\'t hold it up to your mouth.',
  'Somewhere quiet, with nobody else talking and no music.',
  'Your normal speaking voice, at your normal pace.',
  'One recording from start to finish: each enrollment replaces the one before.',
];

function note(text) {
  const n = el('div', { class: 'admin-toggle-sub' }, text);
  n.style.margin = '2px 0 6px';
  return n;
}

function button(text, primary) {
  const b = el('button', { type: 'button', class: primary ? 'admin-btn-add' : 'admin-btn' }, text);
  if (!primary) {
    b.style.cssText = 'background:none;border:1px solid var(--border);color:var(--fg);'
      + 'border-radius:6px;padding:4px 10px;cursor:pointer;';
  }
  return b;
}

/** What the card says about the module's state (exported for tests). */
export function describe(info) {
  if (!info || !info.connected) return (info && info.problem) || 'Athena is not reachable.';
  if (info.available === false) {
    return "Athena's speaker recognition is off (CONVO_DIARIZE=false), so there is no voice to enroll.";
  }
  const rec = info.recording;
  switch (info.state) {
    case 'waiting':
      if (!rec) {
        return 'Waiting for the watch recording. It shows up here once the watch uploads it '
          + '(during or after recording).';
      }
      return rec.status === 'recording'
        ? `Recording on ${rec.device}: ${Math.round(rec.seconds)} s received so far.`
        : `Recording on ${rec.device}: ${Math.round(rec.seconds)} s received. Athena enrolls it in a moment.`;
    case 'processing':
      return 'Enrolling your voice…';
    case 'done': {
      let text = `Done: ${info.name} is enrolled as the owner from ${Math.round(info.speech_seconds)} s `
        + `of speech (${info.samples} samples). New recordings use it right away.`;
      if (info.relabeled_lines) {
        text += ` ${info.relabeled_lines} earlier line${info.relabeled_lines === 1 ? '' : 's'} of unnamed `
          + 'speakers now carry your name.';
      }
      return text;
    }
    case 'failed':
      return `It didn't work: ${info.error || 'unknown error'}.`;
    case 'expired':
      return 'No recording arrived within 30 minutes. Start again when you are ready.';
    default:
      return info.owner_voice
        ? `Your voice is enrolled as ${info.owner_voice}. Enroll again if Athena often misses your lines.`
        : 'No voice is enrolled as yours yet, so Athena recognizes you less well, '
          + 'above all with several people talking. Enrolling takes about two minutes.';
  }
}

function buildCard() {
  const card = el('div', { class: 'admin-card admin-only', id: CARD_ID });
  card.append(el('h2', {}, 'Enroll My Voice'),
    note('Teach Athena your voice with one recording on the watch you wear every day: '
      + 'press Start, record while you read the statements below, then stop. '
      + 'This recording becomes your voice profile, not a conversation.'));
  const col = el('div', { class: 'settings-col' });
  const status = note('');

  // Before starting: the name and the Start button.
  const setup = el('div');
  const nameRow = el('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;' });
  const name = el('input', { type: 'text', class: 'settings-select', id: 'fork-voiceEnrollName',
                             maxlength: '100', placeholder: 'Your name', 'aria-label': 'Your name' });
  name.style.flex = '1 1 160px';
  const start = button('Start', true);
  nameRow.append(name, start);
  const tips = el('ul', { style: 'margin:8px 0 0 18px;padding:0;font-size:12px;' });
  TIPS.forEach((t) => tips.append(el('li', {}, t)));
  setup.append(nameRow, tips);

  // While waiting: the steps and one statement at a time.
  const guide = el('div', { style: 'display:none;' });
  const steps = el('ol', { style: 'margin:4px 0 8px 18px;padding:0;font-size:12px;' });
  ['Start recording on the watch.', 'Read each statement aloud, then press Next.',
   'Stop the recording on the watch after the last one.']
    .forEach((t) => steps.append(el('li', {}, t)));
  const box = el('div', { style: 'border:1px solid var(--border);border-radius:8px;padding:12px;' });
  const counter = el('div', { class: 'admin-toggle-sub' });
  const how = el('div', { style: 'font-size:12px;font-style:italic;margin-top:4px;' });
  const statement = el('div', { style: 'font-size:18px;line-height:1.45;margin:6px 0;' });
  const after = el('div', { style: 'font-size:13px;font-weight:600;margin-top:4px;' });
  const nav = el('div', { style: 'display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;' });
  const back = button('Back');
  const next = button('Next', true);
  const cancel = button('Cancel');
  cancel.style.marginLeft = 'auto';
  nav.append(back, next, cancel);
  box.append(counter, how, statement, after, nav);
  guide.append(steps, box);

  // Afterwards: start over.
  const again = button('Enroll again');
  again.style.display = 'none';
  again.style.marginTop = '6px';

  const msg = el('div', { style: 'font-size:11px;margin-top:4px;' });
  col.append(status, setup, guide, again, msg);
  card.append(col);
  return { card, status, setup, name, start, guide, counter, how, statement, after, back,
           next, cancel, again, msg };
}

function mount() {
  if (document.getElementById(CARD_ID)) return null;
  // After the Transcription Vocabulary card (static/js/forkWhisperVocabulary.js), else
  // after the Utility Model card.
  const vocabulary = document.getElementById('fork-whisper-vocabulary-card');
  const utility = document.getElementById('set-utilityEpSelect');
  const anchor = vocabulary || (utility && utility.closest('.admin-card'));
  if (!anchor) return null;
  const ui = buildCard();
  anchor.after(ui.card);
  return ui;
}

function wire(ui) {
  let index = 0;
  let timer = null;
  let nameTouched = false;
  let lastState = null;

  function showStatement() {
    const s = STATEMENTS[index];
    ui.counter.textContent = `Statement ${index + 1} of ${STATEMENTS.length}`;
    ui.how.textContent = s.how || '';
    ui.statement.textContent = s.text;
    ui.after.textContent = s.after || '';
    ui.back.disabled = index === 0;
    ui.next.disabled = index === STATEMENTS.length - 1;
  }

  function poll(on) {
    if (on && !timer) timer = setInterval(load, POLL_MS);
    if (!on && timer) { clearInterval(timer); timer = null; }
  }

  function show(info) {
    const ok = !!info.connected && info.available !== false;
    const state = ok ? info.state : 'off';
    ui.status.textContent = describe(info);
    ui.status.style.color = state === 'failed' ? 'var(--red)' : '';
    const busy = state === 'waiting' || state === 'processing';
    ui.setup.style.display = ok && !busy && state !== 'done' && state !== 'failed' ? '' : 'none';
    ui.guide.style.display = state === 'waiting' ? '' : 'none';
    ui.again.style.display = state === 'done' || state === 'failed' ? '' : 'none';
    ui.start.disabled = !ok;
    if (ok && !nameTouched) ui.name.value = (state !== 'none' && info.name) || info.default_name || '';
    if (state === 'waiting' && lastState !== 'waiting') { index = 0; showStatement(); }
    lastState = state;
    poll(busy);
  }

  async function load() {
    if (timer && document.hidden) return; // the phone's screen is off: next tick
    try {
      const res = await fetch('/api/fork/voice-enroll', { credentials: 'same-origin' });
      if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status });
      show(await res.json());
    } catch (e) {
      show({ connected: false, problem: e.status === 403 ? 'Admin only.' : 'Could not ask Athena.' });
    }
  }

  async function send(method, body) {
    ui.msg.textContent = '';
    try {
      const res = await fetch('/api/fork/voice-enroll', {
        method, credentials: 'same-origin',
        headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
      const answer = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof answer.detail === 'string' ? answer.detail : `HTTP ${res.status}`);
      show(answer);
    } catch (e) {
      ui.msg.textContent = e.message || 'Failed';
      ui.msg.style.color = 'var(--red)';
    }
  }

  ui.name.addEventListener('input', () => { nameTouched = true; });
  ui.start.addEventListener('click', () => send('POST', { name: ui.name.value }));
  ui.cancel.addEventListener('click', () => send('DELETE'));
  ui.again.addEventListener('click', () => {
    ui.again.style.display = 'none';
    ui.setup.style.display = '';
    ui.status.textContent = 'Press Start when you are ready to record.';
    ui.status.style.color = '';
  });
  ui.next.addEventListener('click', () => { if (index < STATEMENTS.length - 1) { index += 1; showStatement(); } });
  ui.back.addEventListener('click', () => { if (index > 0) { index -= 1; showStatement(); } });
  return load;
}

function init() {
  const ui = mount();
  if (!ui) return;
  const load = wire(ui);
  let loaded = false;
  const refresh = () => { loaded = true; load(); };
  // Load when the AI Defaults tab opens (each time: the state changes on the server).
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

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
}
