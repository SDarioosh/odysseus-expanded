// Fork: the sidebar's collapsible "Athena" group (called by static/js/forkModuleWindows.js).
// Moves the Athena entries (Conversations, Meetings, Today, Write, Beliefs) out of Tools
// into a group of their own, just above Tools, with a header that looks like the Tools
// header. The entries keep their ids and click handlers, so windows, rail buttons,
// shortcuts and `/open` work as before. The open/closed choice is kept per browser.
//
// The group is not a `.section`: upstream's section code (static/js/section-management.js,
// static/js/init.js) binds its own toggle to every `.section` header when the app starts,
// which would race this one. Nothing else in upstream looks at this element.

const GROUP_ID = 'fork-athena-section';
const ITEMS_ID = 'fork-athena-items';
const PREF = 'fork-athena-collapsed';

// Waveform: Athena is what was said.
const ICON = '<svg class="section-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" '
  + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
  + 'aria-hidden="true"><path d="M3 12h1M7 8v8M11 4v16M15 7v10M19 10v4M22 12h-1"/></svg>';
const CHEVRON = '<svg class="section-collapse-chevron" width="10" height="10" viewBox="0 0 24 24" '
  + 'fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" '
  + 'stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>';

function readCollapsed() {
  try {
    return localStorage.getItem(PREF) === '1';
  } catch (e) {
    return false;
  }
}

function saveCollapsed(on) {
  try {
    localStorage.setItem(PREF, on ? '1' : '0');
  } catch (e) { /* private window: not remembered */ }
}

/**
 * Builds the group before `beforeId` (the Tools section) and moves the entries with
 * `itemIds`, in that order, into it. Ids that aren't on the page are skipped; with none
 * found, nothing is built. Returns the group element, or null.
 */
export function buildAthenaMenu(itemIds, beforeId = 'tools-section') {
  if (document.getElementById(GROUP_ID)) return document.getElementById(GROUP_ID);
  const anchor = document.getElementById(beforeId);
  const items = itemIds.map((id) => document.getElementById(id)).filter(Boolean);
  if (!anchor || !items.length) return null;

  const group = document.createElement('div');
  group.id = GROUP_ID;
  group.className = 'fork-athena-section';
  const header = document.createElement('div');
  header.className = 'section-header-flex';
  header.setAttribute('role', 'button');
  header.tabIndex = 0;
  header.setAttribute('aria-controls', ITEMS_ID);
  header.title = 'Show or hide the Athena entries';
  header.innerHTML = `<span class="section-title">${ICON}Athena</span>`
    + `<span class="section-collapse-btn" aria-hidden="true">${CHEVRON}</span>`; // our own SVG
  const list = document.createElement('div');
  list.id = ITEMS_ID;
  list.className = 'fork-athena-items';
  list.append(...items);
  group.append(header, list);
  anchor.before(group);

  const set = (collapsed) => {
    group.classList.toggle('collapsed', collapsed);
    list.hidden = collapsed;
    header.setAttribute('aria-expanded', String(!collapsed));
  };
  set(readCollapsed());
  const toggle = () => {
    const collapsed = !group.classList.contains('collapsed');
    set(collapsed);
    saveCollapsed(collapsed);
  };
  header.addEventListener('click', toggle);
  header.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault(); // Space would scroll the sidebar
    toggle();
  });
  return group;
}

