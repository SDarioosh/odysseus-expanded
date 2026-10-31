// Fork: records how the Wisdom (beliefs) and Meetings views look, as windows in the app
// (static/index.html) and as standalone pages, plus the phone pairing page and the theme
// each standalone page applies. Used by tests/fork/test_fork_views_look.py.
// Runs Chromium (playwright) on the files in static/, with the hub's API answered here
// with fictional data. For each view: every element of the view (scripts and styles left
// out) with its tag, id, classes, attributes, own text and a fixed list of computed style
// properties. Boxes are left out: text widths depend on the machine's font rendering.
// Prints one JSON object; {"__skip": why} when Chromium can't start.
import fs from 'node:fs';
import path from 'node:path';
import { launch, REPO } from './ink_view_harness.mjs';

const ORIGIN = 'http://localhost:7999';
const PAGES = {
  '/today': 'today.html', '/beliefs': 'beliefs.html', '/modules/athena/record': 'call-recorder.html',
  '/modules/athena/pair': 'phone-pairing.html', '/modules/athena': 'sound-module.html',
};
const LIGHT = { name: 'light', colors: { bg: '#f3f5f7', fg: '#26313a', panel: '#ffffff', border: '#cbd3da', red: '#b34d5c' } };

const PROPS = [
  'display', 'position', 'box-sizing', 'color', 'background-color', 'opacity', 'visibility',
  'font-family', 'font-size', 'font-weight', 'font-style', 'line-height', 'letter-spacing',
  'text-transform', 'text-decoration-line', 'text-align', 'white-space', 'overflow-wrap',
  'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
  'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
  'border-top-width', 'border-top-style', 'border-top-color', 'border-left-width',
  'border-left-style', 'border-left-color', 'border-bottom-width', 'border-bottom-color',
  'border-top-left-radius', 'min-height', 'max-width', 'flex-direction', 'flex-wrap', 'flex-grow',
  'flex-shrink', 'flex-basis', 'align-items', 'justify-content', 'row-gap', 'column-gap',
  'grid-template-columns', 'cursor', 'list-style-type', 'vertical-align', 'z-index',
  'overflow-x', 'overflow-y', 'box-shadow', 'transform', 'left', 'bottom', 'resize',
];

const ev = (conv, quote, kind) => ({ source: 'odysseus-sound', times: 2, days: 2, seen_first: '2026-10-20T10:00:00Z',
  seen_last: '2026-10-27T09:00:00Z', link: `https://athena.local/view/${conv}?sig=x#t=12`,
  payload: { conversation_id: conv, quote, kind } });
const BELIEFS = [
  { id: 1, status: 'tentative', shown_text: 'Sam prefers green tea.', text: 'Sam prefers green tea.', subject: 'Sam',
    subject_display: 'Sam', subject_id: 2, subject_kind: 'person', attribute: 'likes', value: 'green tea',
    first_seen: '2026-10-20T10:00:00Z', last_seen: '2026-10-27T09:00:00Z',
    evidence: [ev('c1', 'I only drink green tea now', 'preference')] },
  { id: 2, status: 'tentative', shown_text: 'Sam is moving to Lisbon in spring.', text: 'Sam is moving to Lisbon in spring.',
    subject: 'Sam', subject_display: 'Sam', subject_id: 2, subject_kind: 'person', attribute: 'plans', value: 'move to Lisbon',
    first_seen: '2026-10-20T10:00:00Z', last_seen: '2026-10-20T10:00:00Z', valid_until: '2027-05-01',
    evidence: [ev('c1', 'We move to Lisbon in spring', 'plan')] },
  { id: 3, status: 'tentative', shown_text: 'You are allergic to peanuts.', text: 'Allergic to peanuts.',
    owner_text: 'You are allergic to peanuts.', subject: 'owner', subject_display: 'You', subject_id: 1,
    subject_kind: 'owner', category: 'health', first_seen: '2026-10-01T08:00:00Z', last_seen: '2026-10-25T08:00:00Z',
    evidence: [{ source: 'notes', times: 1, days: 1, seen_first: '2026-10-01T08:00:00Z',
                 seen_last: '2026-10-01T08:00:00Z', link: '/notes/7', gone: true, payload: {} }] },
];
const STATS = { tentative: 3, confirmed: 1, stale: 0, rejected: 2 };
const SUBJECTS = [
  { id: 1, display: 'You', kind: 'owner', counts: { tentative: 1, confirmed: 1 }, aliases: [], sources: [] },
  { id: 2, display: 'Sam', kind: 'person', counts: { tentative: 2 }, aliases: ['Samuel'], contact_id: 'k9',
    sources: [{ source: 'odysseus-sound', name: 'Sam', person_id: 4 }] },
];
const API = {
  '/api/auth/status': { authenticated: true, username: 'alice', is_admin: true, setup_required: false },
  '/api/beliefs': { beliefs: BELIEFS, stats: STATS },
  '/api/beliefs/subjects': { subjects: SUBJECTS, stats: STATS },
  '/api/beliefs/contacts': { contacts: [{ id: 'k9', name: 'Sam Rivera' }] },
  '/api/modules/athena/record/config': { configured: true },
  '/api/modules/athena/meetings/current': { meeting: null },
  '/api/modules/athena/zoom-signin': { configured: true, signed_in: false, missing: [] },
  '/api/insight/today': { date: '2026-10-29', headline: 'A quiet day.', items: [] },
};

async function route(r) {
  const u = new URL(r.request().url());
  if (u.origin !== ORIGIN) return r.fulfill({ status: 404, body: '' });
  const html = (file) => r.fulfill({ contentType: 'text/html',
    body: fs.readFileSync(path.join(REPO, 'static', file), 'utf8').replaceAll('{{CSP_NONCE}}', 'n'),
    headers: { 'Content-Security-Policy': "default-src 'self'; script-src 'self' 'nonce-n'; " +
      "style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data: blob:; connect-src 'self'" } });
  if (u.pathname === '/') return html('index.html');
  if (PAGES[u.pathname]) return html(PAGES[u.pathname]);
  if (u.pathname.startsWith('/static/')) {
    const f = path.join(REPO, u.pathname);
    if (!f.startsWith(path.join(REPO, 'static')) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) {
      return r.fulfill({ status: 404, body: '' });
    }
    const type = { '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
      '.json': 'application/json', '.woff2': 'font/woff2' }[path.extname(f)] || 'application/octet-stream';
    return r.fulfill({ contentType: type, body: fs.readFileSync(f) });
  }
  if (API[u.pathname]) return r.fulfill({ contentType: 'application/json', body: JSON.stringify(API[u.pathname]) });
  return r.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"stub"}' });
}

// In the page: the elements under `sel` (root included) in document order.
function capture(page, sel) {
  return page.evaluate(([s, props]) => {
    const sig = (n) => n.tagName.toLowerCase() + (n.id ? `#${n.id}` : '') +
      (n.classList.length ? `.${[...n.classList].sort().join('.')}` : '');
    const out = [];
    const walk = (n, p) => {
      const cs = getComputedStyle(n);
      out.push({
        path: p,
        attrs: [...n.attributes].filter((a) => a.name !== 'class' && a.name !== 'id')
          .map((a) => `${a.name}=${a.value}`).sort(),
        text: [...n.childNodes].filter((c) => c.nodeType === 3).map((c) => c.data).join('').replace(/\s+/g, ' ').trim(),
        style: props.map((q) => cs.getPropertyValue(q)).join('|'),
      });
      const seen = {};
      for (const c of n.children) {
        if (c.tagName === 'SCRIPT' || c.tagName === 'STYLE') continue;
        const k = sig(c);
        seen[k] = (seen[k] || 0) + 1;
        walk(c, `${p} > ${k}[${seen[k]}]`);
      }
    };
    const root = document.querySelector(s);
    if (root) walk(root, root.tagName.toLowerCase()); // the root's own classes may move
    return out;
  }, [sel, PROPS]);
}

async function settle(page, ms) {
  await page.clock.runFor(ms);
  await page.waitForTimeout(400);
}

async function run(browser) {
  const out = { props: PROPS };
  for (const theme of ['dark', 'light']) {
    for (const width of [1300, 420]) {
      const tag = `${theme} ${width}`;
      const phone = width < 600;
      const newPage = async () => {
        const ctx = await browser.newContext({ viewport: { width, height: phone ? 860 : 900 }, isMobile: phone,
          hasTouch: phone, deviceScaleFactor: 1, locale: 'en-US', timezoneId: 'UTC', reducedMotion: 'reduce' });
        await ctx.route('**/*', route);
        await ctx.addInitScript((t) => {
          localStorage.setItem('odysseus-auth-user', 'alice');
          if (t) localStorage.setItem('odysseus-theme', t);
        }, theme === 'light' ? JSON.stringify(LIGHT) : null);
        const page = await ctx.newPage();
        page.on('dialog', (d) => d.dismiss());
        await page.clock.install({ time: new Date('2026-10-29T12:00:00Z') });
        return page;
      };
      // the windows
      {
        const page = await newPage();
        const errors = [];
        page.on('pageerror', (e) => errors.push(e.message));
        await page.goto(`${ORIGIN}/`);
        await page.waitForFunction(() => document.getElementById('tool-beliefs-btn'), null, { timeout: 60000 });
        await settle(page, 3000);
        for (const [btn, modal, name] of [['tool-meetings-btn', 'fork-meetings-modal', 'meetings'],
          ['tool-beliefs-btn', 'fork-beliefs-modal', 'beliefs']]) {
          await page.evaluate((id) => document.getElementById(id).click(), btn);
          await page.waitForFunction((m) => document.querySelector(`#${m} .fm-modal-body .bs-card`), modal,
            { timeout: 30000 });
          await settle(page, 2000);
          out[`window ${name} ${tag}`] = await capture(page, `#${modal} .fm-modal-body`);
          if (name === 'beliefs') {
            await page.evaluate(() => document.querySelector('#fork-beliefs-modal .bs-view[data-view="subject"]').click());
            await page.waitForFunction(() => document.querySelector('#fork-beliefs-modal #subject-2'));
            await settle(page, 1000);
            out[`window beliefs subjects ${tag}`] = await capture(page, '#fork-beliefs-modal .fm-modal-body');
          }
          await page.evaluate((m) => document.querySelector(`#${m} .close-btn`).click(), modal);
          await settle(page, 500);
        }
        out[`window errors ${tag}`] = errors.filter((e) => /belief|meeting|record|fork/i.test(e));
        await page.context().close();
      }
      // the pages
      for (const [url, file] of Object.entries(PAGES)) {
        const page = await newPage();
        const errors = [];
        page.on('pageerror', (e) => errors.push(e.message));
        await page.goto(ORIGIN + url);
        await settle(page, 1500);
        out[`page ${file} theme ${tag}`] = await page.evaluate(() => ({
          title: document.title,
          rootStyle: document.documentElement.getAttribute('style'),
          themeColor: document.querySelector('meta[name="theme-color"]')?.content,
          body: [getComputedStyle(document.body).backgroundColor, getComputedStyle(document.body).color,
                 getComputedStyle(document.body).fontFamily],
        }));
        out[`page ${file} errors ${tag}`] = errors;
        if (file === 'beliefs.html' || file === 'call-recorder.html' || file === 'phone-pairing.html') {
          if (file === 'beliefs.html') await page.waitForFunction(() => document.querySelector('#belief-1'));
          out[`page ${file} ${tag}`] = await capture(page, 'body');
          if (file === 'beliefs.html') {
            await page.evaluate(() => document.querySelector('.bs-view[data-view="subject"]').click());
            await page.waitForFunction(() => document.querySelector('#subject-2'));
            await settle(page, 1000);
            out[`page beliefs.html subjects ${tag}`] = await capture(page, 'body');
          }
        }
        await page.context().close();
      }
    }
  }
  return out;
}

const { browser, skip } = await launch();
if (skip) {
  console.log(JSON.stringify({ __skip: skip }));
} else {
  try {
    console.log(JSON.stringify(await run(browser)));
  } finally {
    await browser.close();
  }
}
