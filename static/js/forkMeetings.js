// Fork: the Meetings view: "Send Athena to a meeting" (static/js/meetingBot.js) and "Record
// it on this computer" (static/js/callRecorder.js). Shown by the standalone Meetings page
// (static/call-recorder.html, served at /modules/sound/record) and by the Meetings window
// of the main app (static/js/forkModuleWindows.js). Their look is in
// static/css/fork-modules.css: the page's under .fm-meetings-page, the window's under
// .fm-meetings.
//
// Closing the window only hides it (forkModuleWindows.js keeps the view), so a recording
// or a meeting goes on while the owner uses the chat.

// The cards, the same on the page and in the window. The scripts find their elements by
// id when they load, so they load after the cards are in the page.
const MARKUP = `
    <div class="bs-card" id="mb-card">
      <h2>Send Athena to a meeting</h2>
      <p class="bs-text">Paste a Zoom link, or the whole invite. Athena joins on the server as a
        guest called “Athena”, records each person under their Zoom name and leaves when the
        meeting ends. Nothing on this computer records, and you can close this page.</p>
      <div class="mb-form">
        <textarea class="bs-input" id="mb-link" rows="1" spellcheck="false" autocomplete="off"
          aria-label="Zoom link, invite or meeting ID"
          placeholder="https://zoom.us/j/… or meeting ID"></textarea>
        <input class="bs-input" id="mb-pass" type="text" spellcheck="false" autocomplete="off"
          aria-label="Passcode" placeholder="Passcode (with an ID)">
      </div>
      <div class="bs-meta mb-hint">Join the meeting yourself too — Athena records while you're in it.</div>
      <div class="mb-signin" id="mb-signin" hidden>
        <div class="mb-signin-line">
          <span id="mb-signin-text"></span>
          <button type="button" class="bs-btn" id="mb-signin-btn" hidden>Sign in to Zoom</button>
        </div>
        <div id="mb-signin-paste" hidden>
          <p class="bs-text">In the new tab, sign in to Zoom and click Allow. Zoom then opens a
            page that can't load — that's expected. Copy that page's address from the address
            bar and paste it here.</p>
          <div class="bs-meta mb-hint" id="mb-signin-open" hidden>No tab opened?
            <a id="mb-signin-link" href="#" target="_blank" rel="noopener noreferrer">Open Zoom's sign-in page</a>.</div>
          <div class="mb-paste">
            <input class="bs-input" id="mb-signin-input" type="text" spellcheck="false"
              autocomplete="off" aria-label="Address of the page Zoom opened"
              placeholder="Paste the address here">
            <button type="button" class="bs-btn primary" id="mb-signin-finish">Finish</button>
          </div>
        </div>
        <div class="bs-meta mb-hint" id="mb-signin-msg" role="status" aria-live="polite"></div>
      </div>
      <div class="bs-row">
        <button type="button" class="bs-btn primary" id="mb-join" disabled>Join</button>
        <button type="button" class="bs-btn" id="mb-leave" hidden>Leave</button>
      </div>
      <div class="mb-state" id="mb-state" role="status" aria-live="polite"></div>
    </div>

    <div class="bs-card">
      <h2>Or record it on this computer</h2>
      <p class="bs-text">Records a meeting's audio on this computer and sends it to
        Athena, which transcribes and summarizes it like any other conversation.
        Keep Odysseus open while you record.</p>
      <p class="bs-text" data-in-app hidden>You can close this window and keep using the
        chat: the recording goes on until you press Stop.</p>
      <ul class="bs-list">
        <li><b>Meeting in a browser tab</b> (Zoom web, Google Meet, Teams web): choose that tab and turn on
          <i>Also share tab audio</i>.</li>
        <li><b>Zoom or another app</b>: choose its window or the entire screen and turn on
          <i>Also share system audio</i> (Windows; macOS 14.2 or newer with Chrome 141 or newer;
          Linux depends on the desktop, try it once).</li>
        <li>The video is not recorded or sent, only the sound.</li>
      </ul>
    </div>

    <div id="rc-notice" class="bs-notice" role="alert"></div>

    <div class="bs-card">
      <label class="bs-check"><input type="checkbox" id="rc-mic" checked> Include my microphone</label>
      <div class="bs-meta">Use headphones, or the microphone also picks up the other people.</div>
      <div class="bs-row">
        <button type="button" class="bs-btn primary" id="rc-start" disabled>Start recording</button>
        <button type="button" class="bs-btn" id="rc-stop" disabled>Stop</button>
        <a class="bs-btn" href="/modules/sound" data-conversations>Records</a>
      </div>
      <div class="rc-grid">
        <div><span class="rc-dot" id="rc-dot"></span><span class="rc-time" id="rc-time">0:00:00</span></div>
        <div class="bs-meta" id="rc-state">Not recording.</div>
        <div>Call audio</div>
        <div class="rc-meter off" id="rc-meter-call"><div></div></div>
        <div>Microphone</div>
        <div class="rc-meter off" id="rc-meter-mic"><div></div></div>
        <div>Sent</div>
        <div class="bs-meta" id="rc-sent">0 pieces</div>
      </div>
    </div>
`;
const SCRIPTS = ['./callRecorder.js', './meetingBot.js'];

let mounted = false;

/**
 * Builds the Meetings view into `root` and loads its scripts.
 * - inApp: true in the app's window: `root` is the window body and shows only the cards
 *   (the window has its own title bar), with the lines meant for the window. On the page,
 *   `root` is the page's .bs-wrap and the cards go after its top row.
 * - openConversations(): opens the Records window (the cards link to /modules/sound).
 * Returns { refresh }.
 */
export async function mountMeetings(root, { inApp = false, openConversations = null } = {}) {
  if (mounted) throw new Error('the Meetings view is already mounted');
  mounted = true;
  const template = document.createElement('template');
  template.innerHTML = MARKUP; // our own static markup
  if (inApp) {
    template.content.querySelectorAll('[data-in-app]').forEach((n) => { n.hidden = false; });
    root.classList.add('fm-root', 'fm-meetings');
    root.replaceChildren(template.content);
  } else {
    root.append(template.content);
  }
  try {
    for (const path of SCRIPTS) await import(path);
  } catch (e) {
    mounted = false;
    throw e;
  }
  if (openConversations) {
    root.addEventListener('click', (e) => {
      const a = e.target.closest && e.target.closest('a[data-conversations]');
      if (!a || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      openConversations();
    });
  }
  return { refresh() {} };
}
