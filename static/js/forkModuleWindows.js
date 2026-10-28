// Fork: the Athena Records (conversations) and Meetings views and the Today view as Odysseus windows.
// Loaded by one hook in static/index.html. The sidebar entries stay plain links to the
// standalone pages (/modules/sound, /today), which still work on their own; here a
// normal click opens the view in a tool window instead, so the chat and other windows
// stay put. The windows use upstream's window machinery: dragging, resizing and snapping
// (windowDrag.js), minimize to a dock chip and stacking (modalManager.js), Esc and the
// mobile bottom sheet (the generic .modal rules). Also adds the rail buttons, and
// opens the Records reader for links to a module transcript moment
// (<CONVO_PUBLIC_URL>/view/<id>?sig=…#t=<s>) clicked in chat.
//
// Meetings (static/js/forkMeetings.js: record a call, send Athena to a meeting) has its
// sidebar entry added here, after Records; it is a link to its standalone page
// (/modules/sound/record) like the entries in index.html.
//
// Beliefs ("What I know about you", static/js/forkBeliefsWindow.js) opens as a window too;
// its sidebar entry stays the link in index.html. All of these sit in the sidebar's
// collapsible "Athena" group above Tools (static/js/forkAthenaMenu.js).
//
// Write (handwritten notes, static/js/forkInk.js) has no standalone page and no entry in
// index.html: its sidebar entry is added here, after Today. Its code and stylesheet are
// loaded in idle time after the app starts, so a service worker that controls the app
// page caches them for an offline start (static/sw.js is left alone; today its scope is
// /static/, see open question 4 in docs/fork/handwritten-notes.md). Handwritten notes are
// left out of the Notes window, so links to one open it in Write (forkInkLinks.js).

import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';
import { FORK_SHORTCUT_ICONS } from './forkModuleShortcuts.js';
import { buildAthenaMenu } from './forkAthenaMenu.js';
import { wireHandwrittenNoteLinks } from './forkInkLinks.js';

const STYLESHEET = '/static/css/fork-modules.css';
const INK_SCRIPT = './forkInk.js';
const INK_STYLESHEET = '/static/css/fork-ink.css';

const ICON_16 = (svg) => svg.replace('width="14" height="14"', 'width="16" height="16"');

const WINDOWS = {
  sound: {
    id: 'fork-sound-modal',
    title: 'Records',
    sidebarBtn: 'tool-sound-module-btn',
    railBtn: 'rail-sound-module',
    icon: FORK_SHORTCUT_ICONS.open_conversations,
    async mount(body, win) {
      const { mountSoundModule } = await import('./soundModule.js');
      return mountSoundModule(body, {
        embedded: true,
        scroller: body,
        onAsk: (text) => askInChat(text, win),
        confirm: styledConfirm,
      });
    },
  },
  meetings: {
    id: 'fork-meetings-modal',
    title: 'Meetings',
    sidebarBtn: 'tool-meetings-btn',
    railBtn: 'rail-meetings',
    href: '/modules/sound/record',
    icon: FORK_SHORTCUT_ICONS.open_meetings,
    async mount(body) {
      const { mountMeetings } = await import('./forkMeetings.js');
      return mountMeetings(body, { openConversations: () => open(WINDOWS.sound) });
    },
  },
  today: {
    id: 'fork-today-modal',
    title: 'Today',
    sidebarBtn: 'tool-today-insight-btn',
    railBtn: 'rail-today-insight',
    icon: FORK_SHORTCUT_ICONS.open_today,
    async mount(body) {
      const { mountToday } = await import('./today.js');
      return mountToday(body);
    },
  },
  beliefs: {
    id: 'fork-beliefs-modal',
    title: 'Wisdom',
    sidebarBtn: 'tool-beliefs-btn',
    railBtn: 'rail-beliefs',
    icon: FORK_SHORTCUT_ICONS.open_beliefs,
    async mount(body) {
      const { mountBeliefs } = await import('./forkBeliefsWindow.js');
      return mountBeliefs(body);
    },
  },
  ink: {
    id: 'fork-ink-modal',
    title: 'Write',
    sidebarBtn: 'tool-ink-btn',
    railBtn: 'rail-ink',
    icon: FORK_SHORTCUT_ICONS.open_write,
    async mount(body) {
      const { mountInk } = await import(INK_SCRIPT);
      return mountInk(body);
    },
  },
};

function styledConfirm(message) {
  const ui = window.uiModule;
  if (ui && typeof ui.styledConfirm === 'function') {
    return ui.styledConfirm(message, { confirmText: 'Rename everywhere', danger: true });
  }
  return Promise.resolve(window.confirm(message));
}

async function askInChat(text, win) {
  close(win); // the chat is under the window (a full sheet on the phone)
  const { askInChat: prefill } = await import('./forkChatAsk.js');
  prefill(text);
}

function loadStylesheet(href = STYLESHEET) {
  if (document.querySelector(`link[href="${href}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = href;
  document.head.appendChild(link);
}

// ------------------------------------------------------------------ the windows
function build(win) {
  const modal = document.createElement('div');
  modal.id = win.id;
  modal.className = 'modal hidden';
  const content = document.createElement('div');
  content.className = 'modal-content fm-modal-content';
  content.setAttribute('role', 'dialog');
  content.setAttribute('aria-label', win.title);
  const header = document.createElement('div');
  header.className = 'modal-header';
  const h4 = document.createElement('h4');
  h4.innerHTML = win.icon; // our own static SVG
  h4.append(win.title);
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'close-btn';
  closeBtn.setAttribute('aria-label', `Close ${win.title}`);
  closeBtn.textContent = '✖';
  closeBtn.addEventListener('click', () => close(win));
  header.append(h4, closeBtn);
  const body = document.createElement('div');
  body.className = 'fm-modal-body';
  content.append(header, body);
  modal.append(content);
  document.body.appendChild(modal);
  Modals.injectMinimizeButton(modal, win.id);
  makeWindowDraggable(modal, {
    content,
    header,
    skipSelector: 'button, input, select, label',
    enableDock: true,
    enableLeftDock: true,
  });
  win.modal = modal;
  win.body = body;
  return modal;
}

function isOpen(win) {
  return !!(win.modal && !win.modal.classList.contains('hidden'));
}

function setActive(win, on) {
  document.getElementById(win.sidebarBtn)?.classList.toggle('active', on);
}

function register(win) {
  if (Modals.isRegistered(win.id)) return;
  Modals.register(win.id, {
    sidebarBtnId: win.sidebarBtn,
    railBtnId: win.railBtn,
    label: win.title,
    icon: win.icon,
    closeFn: () => hide(win),
    restoreFn: () => setActive(win, true),
  });
}

async function open(win) {
  if (Modals.isRegistered(win.id) && Modals.isMinimized(win.id)) {
    Modals.restore(win.id);
    return win.view;
  }
  if (!win.modal) build(win);
  win.modal.classList.remove('hidden', 'modal-minimized');
  win.modal.style.display = '';
  register(win);
  setActive(win, true);
  closeMobileSidebar();
  if (!win.mounting) {
    win.mounting = win.mount(win.body, win).then((view) => {
      win.view = view;
      return view;
    }).catch((e) => {
      win.mounting = null;
      win.body.textContent = `Could not load ${win.title}: ${e.message}`;
      throw e;
    });
  } else if (win.view && (win.id === WINDOWS.today.id || win.id === WINDOWS.ink.id)) {
    win.view.refresh(); // the stored insight / the notes list may be newer
  }
  return win.mounting;
}

function hide(win) {
  if (win.modal) win.modal.classList.add('hidden');
  setActive(win, false);
  // Write: save now and stop its timers and window-wide listener while closed.
  if (win.view && typeof win.view.suspend === 'function') win.view.suspend();
}

// Close keeps the view (and its state) for the next open; it only hides the window.
function close(win) {
  if (Modals.isRegistered(win.id)) Modals.close(win.id); // drops the dock chip, calls hide()
  else hide(win);
}

function toggle(win) {
  if (isOpen(win)) close(win);
  else open(win);
}

// On the phone the sidebar is a drawer over the chat; close it like a chat pick does.
function closeMobileSidebar() {
  if (window.innerWidth > 768) return;
  document.getElementById('sidebar')?.classList.add('hidden');
  document.getElementById('sidebar-backdrop')?.classList.remove('visible');
}

// ------------------------------------------------------------- entry points
const plainClick = (e) => e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;

function wireSidebar(win) {
  const link = document.getElementById(win.sidebarBtn);
  if (!link) return;
  // A plain click (or .click() from a shortcut, the rail, or /open) opens the window;
  // a modified click still opens the standalone page in a new tab (links only).
  link.addEventListener('click', (e) => {
    if (!plainClick(e) && link.tagName === 'A') return;
    e.preventDefault();
    toggle(win);
  });
}

function addRailButton(win, after) {
  if (document.getElementById(win.railBtn)) return document.getElementById(win.railBtn);
  const anchor = document.getElementById(after);
  if (!anchor) return null;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'icon-rail-btn';
  btn.id = win.railBtn;
  btn.title = win.title;
  btn.setAttribute('aria-label', win.title);
  btn.innerHTML = ICON_16(win.icon);
  btn.addEventListener('click', () => document.getElementById(win.sidebarBtn)?.click());
  anchor.after(btn);
  return btn;
}

// The Meetings and Write sidebar entries, styled like the entries around them. With an
// href the entry is a link (a modified click opens the standalone page), else a button.
function addSidebarEntry(win, after) {
  if (document.getElementById(win.sidebarBtn)) return;
  const anchor = document.getElementById(after);
  if (!anchor) return;
  const item = document.createElement(win.href ? 'a' : 'div');
  item.className = 'list-item';
  item.id = win.sidebarBtn;
  if (win.href) {
    item.href = win.href;
    item.style.color = 'inherit';
    item.style.textDecoration = 'none';
  } else {
    item.setAttribute('role', 'button');
    item.tabIndex = 0;
  }
  item.innerHTML = win.icon.replace('<svg ', '<svg style="flex-shrink:0;opacity:0.5;" '); // our own SVG
  const label = document.createElement('span');
  label.className = 'grow';
  label.textContent = win.title;
  item.append(label);
  item.addEventListener('keydown', (e) => {
    if (win.href) return; // a link: Enter clicks it already
    if (e.target !== item || (e.key !== 'Enter' && e.key !== ' ')) return;
    e.preventDefault(); // Space would scroll the sidebar
    item.click();
  });
  anchor.after(item);
}

// Load Write's code and stylesheet once the app is idle, so they are cached (the service
// worker's runtime cache when it controls the page, else the HTTP cache) before needed.
function warmInk() {
  const go = () => {
    import(INK_SCRIPT).catch(() => {});
    loadStylesheet(INK_STYLESHEET);
  };
  if ('requestIdleCallback' in window) window.requestIdleCallback(go, { timeout: 10000 });
  else setTimeout(go, 3000);
}

// The rail button follows its sidebar entry when Appearance hides it.
function mirrorVisibility(win) {
  const link = document.getElementById(win.sidebarBtn);
  const rail = document.getElementById(win.railBtn);
  if (!link || !rail) return;
  const sync = () => { rail.style.display = link.style.display === 'none' ? 'none' : ''; };
  new MutationObserver(sync).observe(link, { attributes: true, attributeFilter: ['style'] });
  sync();
}

// Links to a module transcript moment in chat answers open the Records reader.
// The click must be recognised before the browser follows the link, so the parser is
// loaded up front; soundModule.js is small and does nothing until a view is mounted.
let linkParser = null;
async function preloadParser() {
  try {
    const mod = await import('./soundModule.js');
    linkParser = mod.parseModuleLink;
  } catch (e) { /* the chat links then simply open the module's page */ }
}

function wireTranscriptLinks() {
  document.addEventListener('click', (e) => {
    if (e.defaultPrevented || !plainClick(e) || !linkParser) return;
    const a = e.target && e.target.closest && e.target.closest('a[href]');
    if (!a || a.dataset.external === '1') return;
    const win = WINDOWS.sound;
    if (win.modal && win.modal.contains(a)) return; // the view handles its own links
    const link = document.getElementById(win.sidebarBtn);
    if (!link || link.style.display === 'none') return; // not offered to this user
    const ref = linkParser(a.getAttribute('href'));
    if (!ref) return;
    e.preventDefault();
    e.stopPropagation();
    open(win).then((view) => view && view.openConversation(ref.id, ref.t)).catch(() => {});
  }, true);
}

function init() {
  loadStylesheet();
  addSidebarEntry(WINDOWS.meetings, WINDOWS.sound.sidebarBtn);
  addSidebarEntry(WINDOWS.ink, WINDOWS.today.sidebarBtn);
  buildAthenaMenu([WINDOWS.sound.sidebarBtn, WINDOWS.meetings.sidebarBtn, WINDOWS.today.sidebarBtn,
                   WINDOWS.ink.sidebarBtn, WINDOWS.beliefs.sidebarBtn]);
  for (const win of Object.values(WINDOWS)) wireSidebar(win);
  addRailButton(WINDOWS.sound, 'rail-notes');
  addRailButton(WINDOWS.meetings, WINDOWS.sound.railBtn);
  addRailButton(WINDOWS.today, WINDOWS.meetings.railBtn);
  addRailButton(WINDOWS.ink, WINDOWS.today.railBtn);
  addRailButton(WINDOWS.beliefs, WINDOWS.ink.railBtn);
  for (const win of Object.values(WINDOWS)) mirrorVisibility(win);
  preloadParser();
  wireTranscriptLinks();
  wireHandwrittenNoteLinks({
    offered: () => {
      const entry = document.getElementById(WINDOWS.ink.sidebarBtn);
      return !!entry && entry.style.display !== 'none';
    },
    openInWrite: (id) => open(WINDOWS.ink).then((view) => view && view.openNote(id)),
  });
  if (document.readyState === 'complete') warmInk();
  else window.addEventListener('load', warmInk, { once: true });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export { open as openModuleWindow, WINDOWS as MODULE_WINDOWS };
