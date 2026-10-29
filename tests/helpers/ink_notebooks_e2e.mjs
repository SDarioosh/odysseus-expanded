// Fork: notebooks in the Write view (static/js/forkInk.js), in a real Chromium against
// the real ink routes (tests/fork/test_ink_notebooks.py starts them on a temporary database
// and passes the base URL). Prints one JSON line {step: {ok, detail}}; "SKIP <reason>"
// when no browser can be started.
import { createRequire } from 'node:module';
import fs from 'node:fs';

const require = createRequire(import.meta.url);
const base = process.argv[2];
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) { console.log('SKIP no playwright'); process.exit(0); }
let browser;
const fallback = process.env.PLAYWRIGHT_CHROMIUM || '/opt/pw-browsers/chromium';
try { browser = await chromium.launch(); } catch (e) {
  try {
    if (!fs.existsSync(fallback)) throw e;
    browser = await chromium.launch({ executablePath: fallback });
  } catch (e2) { console.log('SKIP ' + String(e2.message).split('\n')[0]); process.exit(0); }
}

const results = {};
const check = (name, ok, detail = '') => { results[name] = { ok: !!ok, detail: ok ? '' : String(detail) }; };
const page = await browser.newPage({ viewport: { width: 1000, height: 800 } });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e.message)));
await page.addInitScript(() => {
  localStorage.setItem('odysseus-auth-user', 'alice');
  // The hub's dialogs: the next answers, in order.
  window.answers = [];
  window.uiModule = {
    styledPrompt: async () => window.answers.shift() ?? null,
    styledConfirm: async () => true,
  };
});
await page.goto(base + '/');
await page.waitForFunction(() => window.ready === true);

const text = (sel) => page.locator(sel).allInnerTexts();
const hub = () => page.evaluate(async () => (await fetch('/api/ink/notes')).json());
const draw = async () => {
  await page.waitForFunction(() => document.querySelector('.ink-live').width > 0);
  await page.evaluate(() => {
    const c = document.querySelector('.ink-live');
    const r = c.getBoundingClientRect();
    const fire = (type, x, y) => c.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true,
      pointerType: 'mouse', pointerId: 1, isPrimary: true, buttons: type === 'pointerup' ? 0 : 1,
      clientX: r.left + x, clientY: r.top + y }));
    fire('pointerdown', 200, 200);
    for (let i = 1; i < 12; i += 1) fire('pointermove', 200 + i * 15, 200 + i * 3);
    fire('pointerup', 380, 236);
  });
};

try {
  await page.waitForSelector('.ink-new-book');
  const heads = await text('.ink-section');
  check('list_has_quick_notes_section', heads.includes('Quick notes'), heads);

  await page.evaluate(() => { window.answers.push('Garden journal'); });
  await page.click('.ink-new-book');
  await page.waitForSelector('.ink-book-name');
  await page.waitForFunction(() => document.querySelector('.ink-book-name').textContent === 'Garden journal');
  check('new_notebook_opens_it', true);

  await page.click('.ink-new-page');
  await page.waitForSelector('.ink-editor:not([hidden])');
  await draw();
  const label1 = await page.locator('.ink-book-label').innerText();
  check('editor_says_notebook_and_page', label1 === 'Garden journal · page 1 of 1', label1);

  // The Page button in a notebook makes its next page.
  await page.click('.ink-bar button[aria-label="Add a page"]');
  await page.waitForFunction(() => (document.querySelector('.ink-book-label')?.textContent || '').endsWith('page 2 of 2'));
  await draw();
  const prevEnabled = await page.locator('.ink-book-nav button').first().isEnabled();
  check('previous_page_enabled_on_page_2', prevEnabled);
  await page.locator('.ink-book-nav button').first().click();
  await page.waitForFunction(() => (document.querySelector('.ink-book-label')?.textContent || '').endsWith('page 1 of 2'));
  const strokes = await page.evaluate(() => window.view.strokeCount());
  check('flipped_back_to_page_1_with_its_ink', strokes === 1, strokes);

  // Back: to the notebook, its pages in order.
  await page.click('.ink-bar .ink-group button[title^="Back to"]');
  await page.waitForSelector('.ink-book-name');
  await page.waitForFunction(() => document.querySelectorAll('.ink-card[data-note]').length === 2);
  const cards = await text('.ink-card[data-note] h2');
  check('notebook_lists_pages_in_order', cards[0].startsWith('Page 1 · ') && cards[1].startsWith('Page 2 · '), cards);
  let h = await hub();
  check('hub_has_the_notebook_with_two_pages', h.notebooks.length === 1 && h.notebooks[0].pages.length === 2
    && h.notes.every((n) => n.notebook === h.notebooks[0].id), JSON.stringify(h));

  // The list: the notebook card, no quick notes yet; then a quick note.
  await page.locator('.ink-list-head button[aria-label="All notes"]').click();
  await page.waitForSelector('.ink-book-card');
  const bookMeta = await page.locator('.ink-book-card .bs-meta').innerText();
  check('notebook_card_counts_pages', bookMeta.startsWith('2 pages'), bookMeta);
  await page.click('.ink-new-note');
  await page.waitForSelector('.ink-editor:not([hidden])');
  const navHidden = await page.locator('.ink-book-nav').isHidden();
  check('quick_note_has_no_notebook_buttons', navHidden);
  await draw();
  await page.click('.ink-bar button[aria-label="All notes"]');
  await page.waitForSelector('.ink-book-card');
  await page.waitForFunction(() => document.querySelectorAll('.ink-notes:not(.ink-books) .ink-card[data-note]').length === 1);
  check('quick_note_listed_outside_notebooks', true);

  // Move the quick note into the notebook.
  const bookId = (await hub()).notebooks[0].id;
  await page.selectOption('.ink-notes:not(.ink-books) .ink-move', bookId);
  await page.waitForFunction(() => /^3 pages/.test(document.querySelector('.ink-book-card .bs-meta')?.textContent || ''));
  h = await hub();
  check('moved_into_the_notebook', h.notebooks[0].pages.length === 3, JSON.stringify(h.notebooks));

  // Rename, then delete the notebook: its pages are kept as quick notes.
  await page.click('.ink-book-card');
  await page.waitForSelector('.ink-rename-book');
  await page.evaluate(() => { window.answers.push('Garden 2026'); });
  await page.click('.ink-rename-book');
  await page.waitForFunction(() => document.querySelector('.ink-book-name')?.textContent === 'Garden 2026');
  await page.click('.ink-delete-book');
  await page.waitForFunction(() => !document.querySelector('.ink-book-card')
    && document.querySelectorAll('.ink-notes .ink-card[data-note]').length === 3);
  h = await hub();
  check('deleted_notebook_keeps_pages', h.notebooks.length === 0 && h.notes.length === 3
    && h.notes.every((n) => n.notebook === null), JSON.stringify(h));
} catch (e) {
  check('no_exception', false, e.stack || e);
}
check('no_page_errors', !errors.length, errors);
await browser.close();
console.log(JSON.stringify(results));
