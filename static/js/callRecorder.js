// Fork: "Record a call" page (static/call-recorder.html). Captures a meeting's audio
// (a tab, a window or the screen, shared with audio, plus the microphone), mixes it,
// and records one self-contained 30-second WebM/Opus file at a time. Each piece goes to
// routes/sound_module_routes.py, which forwards it to Athena's ingest API
// (src/call_recorder.py). Pieces upload one by one, in order, and retry until they are
// sent; the module ends a session after long silence or its maximum length, and the page
// then continues in a new session.

const PIECE_MS = 30000;
const BITRATE = 32000; // Opus voice; ~120 KB per 30 s piece
const RETRY_MAX_MS = 60000;
const MIC_PREF = 'odysseus-call-recorder-mic';

const $ = (sel) => document.querySelector(sel);
const ui = {
  notice: $('#rc-notice'), start: $('#rc-start'), stop: $('#rc-stop'), mic: $('#rc-mic'),
  dot: $('#rc-dot'), time: $('#rc-time'), state: $('#rc-state'), sent: $('#rc-sent'),
  meterCall: $('#rc-meter-call'), meterMic: $('#rc-meter-mic'),
};

let rec = null; // the recording in progress (see start())
let ready = false; // set up on the server and supported by this browser
const queue = []; // pieces waiting to upload: {blob, codec, startedAt, sessionId, sequence}
let pumping = false;
let sentCount = 0;
let lastError = '';

// ------------------------------------------------------------------ helpers
function notice(text) {
  ui.notice.textContent = text || '';
}

function pickFormat() {
  if (typeof MediaRecorder === 'undefined') return null;
  const options = [
    { mime: 'audio/webm;codecs=opus', codec: 'webm' },
    { mime: 'audio/ogg;codecs=opus', codec: 'ogg' },
    { mime: 'audio/webm', codec: 'webm' },
  ];
  return options.find((o) => MediaRecorder.isTypeSupported(o.mime)) || null;
}

function newSessionId() {
  const t = new Date().toISOString().replace(/[-:]/g, '').replace(/\..*$/, '');
  const r = Math.random().toString(16).slice(2, 8);
  return `call-${t}-${r}`; // matches the module's id rule [A-Za-z0-9._:-]{1,128}
}

function clock(ms) {
  const s = Math.floor(ms / 1000);
  const two = (n) => String(n).padStart(2, '0');
  return `${Math.floor(s / 3600)}:${two(Math.floor(s / 60) % 60)}:${two(s % 60)}`;
}

function readPref() {
  try {
    return localStorage.getItem(MIC_PREF) !== 'off';
  } catch (e) {
    return true;
  }
}

function savePref(on) {
  try {
    localStorage.setItem(MIC_PREF, on ? 'on' : 'off');
  } catch (e) { /* private window: not remembered */ }
}

function render() {
  const recording = !!rec && !rec.stopping;
  ui.start.disabled = !!rec || !ready;
  ui.stop.disabled = !recording;
  ui.mic.disabled = !!rec;
  ui.dot.classList.toggle('on', recording);
  const waiting = queue.length + (pumping ? 1 : 0);
  ui.sent.textContent = `${sentCount} piece${sentCount === 1 ? '' : 's'}` +
    (waiting ? `, ${waiting} waiting to upload` : '');
  if (recording) ui.state.textContent = lastError ? `Recording. ${lastError}` : 'Recording.';
  else if (rec) ui.state.textContent = 'Finishing: sending the last pieces…';
  else if (!lastError) ui.state.textContent = sentCount ? 'Done. The conversation appears in ' +
    'Conversations once Athena has processed it.' : 'Not recording.';
}

// ------------------------------------------------------------------ uploads
async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST', credentials: 'same-origin', body,
    headers: { 'X-Odysseus-Recorder': '1' },
  });
  let data = {};
  try {
    data = await res.json();
  } catch (e) {}
  return { status: res.status, data };
}

function retryable(status) {
  // 401: logged out (log in again in another tab; the pieces wait here).
  // 502/503/504: Athena or Odysseus not reachable or not set up yet.
  return status === 0 || status === 401 || status === 408 || status === 429 || status >= 500;
}

function rotateSession(endedId) {
  // The module ended this session (long silence or maximum length). Continue in a new
  // one; pieces still waiting for the old session move to the new one, in order.
  if (!rec || rec.sessionId !== endedId) return;
  rec.sessionId = newSessionId();
  rec.sequence = 0;
  for (const p of queue) {
    if (p.sessionId === endedId) {
      p.sessionId = rec.sessionId;
      p.sequence = rec.sequence++;
    }
  }
}

async function pump() {
  if (pumping) return;
  pumping = true;
  let delay = 2000;
  try {
    while (queue.length) {
      const p = queue[0];
      const form = new FormData();
      form.append('audio', p.blob, `${String(p.sequence).padStart(6, '0')}.${p.codec}`);
      form.append('session_id', p.sessionId);
      form.append('sequence', String(p.sequence));
      form.append('timestamp', p.startedAt.toISOString());
      form.append('codec', p.codec);
      let res;
      try {
        res = await post('/api/modules/sound/record/chunk', form);
      } catch (e) {
        res = { status: 0, data: {} };
      }
      if (res.status === 200) {
        queue.shift();
        sentCount += 1;
        lastError = '';
        delay = 2000;
        if (rec) rec.sentIn.add(p.sessionId);
        if (res.data.stop) rotateSession(p.sessionId);
        render();
        continue;
      }
      const detail = res.data.detail || (res.status ? `HTTP ${res.status}` : 'no connection');
      if (!retryable(res.status)) {
        // Can't succeed on a retry (bad piece, too large, undecodable): skip it.
        queue.shift();
        lastError = `A piece was not accepted (${detail}).`;
        notice(lastError);
        render();
        continue;
      }
      lastError = res.status === 401
        ? 'Logged out: log in to Odysseus in another tab; the pieces wait here.'
        : `Upload failed (${detail}); retrying.`;
      render();
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 2, RETRY_MAX_MS);
    }
  } finally {
    pumping = false;
    render();
  }
  if (rec && rec.stopping && !queue.length && rec.recorder.state === 'inactive') await finish();
}

// ------------------------------------------------------------------ recording
function meter(stream, ctx, target) {
  const src = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  src.connect(analyser);
  target.classList.remove('off');
  const bar = target.firstElementChild;
  const buf = new Float32Array(analyser.fftSize);
  return { src, read() {
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (const v of buf) sum += v * v;
    const rms = Math.sqrt(sum / buf.length);
    bar.style.width = `${Math.min(100, Math.round(rms * 400))}%`;
  } };
}

function startPiece() {
  const r = new MediaRecorder(rec.dest.stream, { mimeType: rec.format.mime,
                                                 audioBitsPerSecond: BITRATE });
  const piece = { chunks: [], startedAt: new Date() };
  r.ondataavailable = (e) => { if (e.data && e.data.size) piece.chunks.push(e.data); };
  r.onstop = () => {
    if (!rec) return;
    if (piece.chunks.length) {
      queue.push({ blob: new Blob(piece.chunks, { type: rec.format.mime }),
                   codec: rec.format.codec, startedAt: piece.startedAt,
                   sessionId: rec.sessionId, sequence: rec.sequence++ });
    }
    render();
    pump(); // after Stop, the last piece's onstop is what lets the recording finish
  };
  r.start();
  rec.recorder = r;
  rec.pieceTimer = setTimeout(nextPiece, PIECE_MS);
}

function nextPiece() {
  // Start the next file before closing this one, so no audio falls between them; each
  // file has its own header, which the module needs to decode a piece on its own.
  if (!rec || rec.stopping) return;
  const old = rec.recorder;
  startPiece();
  old.stop();
}

async function start() {
  notice('');
  lastError = '';
  const format = pickFormat();
  if (!window.isSecureContext || !navigator.mediaDevices?.getDisplayMedia || !format) {
    notice('This browser can\'t record here. Use a Chromium browser (Chrome, Brave, Edge) and open ' +
           'Odysseus over https:// or on localhost.');
    return;
  }
  let display;
  try {
    display = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: 1, width: { max: 640 } }, // required by the browser; never recorded
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      systemAudio: 'include', windowAudio: 'system', selfBrowserSurface: 'exclude',
      surfaceSwitching: 'include', preferCurrentTab: false,
    });
  } catch (e) {
    notice(e && e.name === 'NotAllowedError' ? 'Sharing was cancelled.' : `Couldn't share: ${e.message || e}`);
    return;
  }
  if (!display.getAudioTracks().length) {
    display.getTracks().forEach((t) => t.stop());
    notice('No call audio was shared. Start again and turn on "Also share tab audio" (a tab) or ' +
           '"Also share system audio" (a window or the entire screen). If the option is missing, ' +
           'this browser or OS can\'t share that audio: use the meeting in a browser tab instead.');
    return;
  }
  let mic = null;
  if (ui.mic.checked) {
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (e) {
      notice('No microphone: recording the call audio only.');
    }
  }

  const ctx = new AudioContext();
  const dest = ctx.createMediaStreamDestination();
  const callAudio = new MediaStream(display.getAudioTracks());
  const meters = [meter(callAudio, ctx, ui.meterCall)];
  if (mic) meters.push(meter(mic, ctx, ui.meterMic));
  meters.forEach((m) => m.src.connect(dest)); // mixed into one track; not played back
  await ctx.resume();

  rec = { display, mic, ctx, dest, format, meters, sessionId: newSessionId(), sequence: 0,
          sentIn: new Set(), startedAt: Date.now(), stopping: false, recorder: null,
          pieceTimer: null, tick: null, frame: null, wakeLock: null };
  // The browser's own "Stop sharing" button ends the recording too.
  display.getTracks().forEach((t) => t.addEventListener('ended', () => stop()));
  try {
    rec.wakeLock = await navigator.wakeLock?.request('screen');
  } catch (e) { /* not allowed: the computer may sleep, which pauses recording */ }
  rec.tick = setInterval(() => { ui.time.textContent = clock(Date.now() - rec.startedAt); }, 1000);
  const draw = () => {
    if (!rec) return;
    rec.meters.forEach((m) => m.read());
    rec.frame = requestAnimationFrame(draw);
  };
  draw();
  startPiece();
  render();
}

function stop() {
  if (!rec || rec.stopping) return;
  rec.stopping = true;
  clearTimeout(rec.pieceTimer);
  clearInterval(rec.tick);
  cancelAnimationFrame(rec.frame);
  if (rec.recorder && rec.recorder.state !== 'inactive') rec.recorder.stop(); // queues the last piece
  rec.display.getTracks().forEach((t) => t.stop());
  if (rec.mic) rec.mic.getTracks().forEach((t) => t.stop());
  try {
    rec.wakeLock?.release();
  } catch (e) {}
  [ui.meterCall, ui.meterMic].forEach((m) => {
    m.classList.add('off');
    m.firstElementChild.style.width = '0';
  });
  render();
}

async function finish() {
  if (!rec || rec.finishing) return;
  rec.finishing = true;
  const { sessionId, sentIn, ctx } = rec;
  try {
    ctx.close();
  } catch (e) {}
  if (sentIn.has(sessionId)) {
    // Process now rather than after the module's idle timeout.
    try {
      const res = await post(`/api/modules/sound/record/${encodeURIComponent(sessionId)}/end`, null);
      if (res.status !== 200) lastError = `Sent, but couldn't end the session (${res.data.detail ||
        `HTTP ${res.status}`}); Athena ends it by itself after a few minutes.`;
    } catch (e) {
      lastError = 'Sent, but couldn\'t end the session; Athena ends it by itself after a few minutes.';
    }
  }
  rec = null;
  if (lastError) ui.state.textContent = lastError;
  render();
}

// ------------------------------------------------------------------ page
async function init() {
  ui.mic.checked = readPref();
  ui.mic.addEventListener('change', () => savePref(ui.mic.checked));
  ui.start.addEventListener('click', start);
  ui.stop.addEventListener('click', stop);
  window.addEventListener('beforeunload', (e) => {
    if (rec || queue.length) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
  try {
    const res = await fetch('/api/modules/sound/record/config', { credentials: 'same-origin' });
    if (res.status === 401) {
      window.location.href = '/login';
      return;
    }
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    if (!data.configured) {
      notice(`Call recording isn't available: ${data.problem || 'Athena is not connected.'}`);
    } else {
      ready = true;
    }
  } catch (e) {
    notice(`Couldn't check the setup: ${e.message || e}`);
  }
  if (!pickFormat() || !navigator.mediaDevices?.getDisplayMedia) {
    notice('This browser can\'t record calls. Use a Chromium browser (Chrome, Brave, Edge).');
    ready = false;
  } else if (!window.isSecureContext) {
    notice('Recording needs a secure address: open Odysseus over https:// (for example a ' +
           'Tailscale certificate) or on localhost.');
    ready = false;
  }
  render();
}

init();
