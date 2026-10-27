// Fork: the fork windows' shortcuts ("Open Conversations", "Open Meetings", "Open Today",
// "Open Write" in
// Settings → Shortcuts) and their `/open` targets. Data only (no side effects), spread
// into upstream's tables by small hooks in static/js/settings.js,
// static/js/keyboard-shortcuts.js and static/js/slashCommands.js. Unbound by default,
// like upstream's other open-tool shortcuts. The buttons they click are the sidebar
// entries whose clicks static/js/forkModuleWindows.js turns into windows (it adds the
// Meetings and Write entries itself). A new fork window needs a row here, not an upstream edit.

const MIC = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" '
  + 'height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><line x1="12" y1="17" x2="12" y2="22"/></svg>';
const VIDEO = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m16 13 5.2 3.5a.5.5 0 0 0 '
  + '.8-.4V7.9a.5.5 0 0 0-.8-.4L16 11"/><rect x="2" y="6" width="14" height="12" rx="2"/></svg>';
const SUN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/>'
  + '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4'
  + 'M17.7 6.3l1.4-1.4"/></svg>';
const PEN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/>'
  + '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';

const SHORTCUTS = {
  open_conversations: { button: 'tool-sound-module-btn', rail: 'rail-sound-module',
                        window: 'fork-sound-modal', label: 'Open Conversations', icon: MIC,
                        open: ['conversations'] },
  open_meetings: { button: 'tool-meetings-btn', rail: 'rail-meetings', window: 'fork-meetings-modal',
                   label: 'Open Meetings', icon: VIDEO, open: ['meetings', 'calls'] },
  open_today: { button: 'tool-today-insight-btn', rail: 'rail-today-insight',
                window: 'fork-today-modal', label: 'Open Today', icon: SUN, open: ['today'] },
  open_write: { button: 'tool-ink-btn', rail: 'rail-ink', window: 'fork-ink-modal',
                label: 'Open Write', icon: PEN, open: ['write', 'ink', 'handwriting'] },
};

const pick = (field) => Object.fromEntries(
  Object.entries(SHORTCUTS).map(([action, s]) => [action, s[field]]));

/** action -> '' (unbound default) */
export const FORK_SHORTCUT_DEFAULTS = Object.fromEntries(Object.keys(SHORTCUTS).map((a) => [a, '']));
/** action -> sidebar button id it clicks */
export const FORK_SHORTCUT_BUTTONS = pick('button');
/** action -> label in Settings → Shortcuts */
export const FORK_SHORTCUT_LABELS = pick('label');
/** action -> icon in Settings → Shortcuts */
export const FORK_SHORTCUT_ICONS = pick('icon');
/** action names, for the "Open Tools" group */
export const FORK_SHORTCUT_ACTIONS = Object.keys(SHORTCUTS);
/** window id -> the button that opens it, for "Toggle Window" */
export const FORK_WINDOW_TRIGGERS = Object.fromEntries(
  Object.values(SHORTCUTS).map((s) => [s.window, s.button]));
/** `/open <target>` -> [sidebar button id, rail button id] (static/js/slashCommands.js) */
export const FORK_OPEN_TARGETS = Object.fromEntries(
  Object.values(SHORTCUTS).flatMap((s) => s.open.map((name) => [name, [s.button, s.rail]])));
