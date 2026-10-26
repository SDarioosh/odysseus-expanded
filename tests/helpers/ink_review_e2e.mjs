// Fork: browser checks for the Write view (static/js/forkInk.js), one per finding of the
// PR 88 QC review that needs a real browser. Each check asserts the correct behaviour.
// Prints one JSON line {name: {ok, detail}}; "SKIP <reason>" without a browser.
// Run by tests/test_ink_view_review.py.
import { launch, openPage } from './ink_view_harness.mjs';

const { browser, skip } = await launch();
if (skip) { console.log('SKIP ' + skip); process.exit(0); }

const results = {};
async function check(name, fn) {
  try {
    const detail = await fn();
    results[name] = { ok: detail.ok, detail };
  } catch (e) {
    results[name] = { ok: false, detail: String(e && e.stack || e).split('\n').slice(0, 3).join(' | ') };
  }
}
const line = (y, x0 = 150) => [[x0, y], [x0 + 50, y + 5], [x0 + 110, y], [x0 + 170, y + 5], [x0 + 230, y]];
const newNote = (page) => page.evaluate(async () => { await view.newNote(); await settle(100); });

// 1. Notes on this device belong to the user who wrote them.
await check('f01_device_notes_per_user', async () => {
  const ctx = await browser.newContext({ viewport: { width: 1000, height: 800 } });
  const alice = await openPage(browser, { user: 'alice', context: ctx });
  await alice.evaluate(() => { hub.online = false; });
  await newNote(alice);
  await alice.evaluate((pts) => { stroke('pen', 2, pts); }, line(200));
  await alice.evaluate(async () => { document.querySelector('.ink-title').value = 'Alice Secret';
    document.querySelector('.ink-title').dispatchEvent(new Event('input')); await view.flush(); await settle(100); });
  await alice.close();
  const bob = await openPage(browser, { user: 'bob', context: ctx });
  await bob.evaluate(() => { hub.user = 'bob'; });
  await bob.evaluate(() => view.refresh());
  await bob.waitForTimeout(400);
  const listed = await bob.evaluate(() => [...document.querySelectorAll('.ink-card h2')].map((h) => h.textContent));
  const uploads = await bob.evaluate(() => Object.values(hub.notes).filter((n) => n.title === 'Alice Secret').length);
  await ctx.close();
  return { ok: !listed.includes('Alice Secret') && uploads === 0, listed, uploads };
});

// 1c. A 404 from the hub isn't retried forever.
await check('f01c_404_not_retried', async () => {
  const page = await openPage(browser, { user: 'carol' });
  await newNote(page);
  const id = await page.evaluate(() => view._state.note.id);
  await page.evaluate((nid) => { hub.answer['PUT /api/ink/notes/' + nid] = { status: 404, body: { detail: 'Note not found' } }; }, id);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(async () => { await view.flush(); await settle(200); });
  const before = await page.evaluate(() => hub.calls.filter((c) => c.startsWith('PUT')).length);
  await page.evaluate(async () => { window.dispatchEvent(new Event('online')); await settle(300);
    window.dispatchEvent(new Event('online')); await settle(300); });
  const after = await page.evaluate(() => hub.calls.filter((c) => c.startsWith('PUT')).length);
  const status = await page.evaluate(() => document.querySelector('.ink-status').textContent);
  await page.close();
  return { ok: after === before, before, after, status };
});

// 4. The view can't build a note the hub refuses.
await check('f04_page_limit', async () => {
  const page = await openPage(browser, { user: 'dan' });
  await newNote(page);
  const got = await page.evaluate(() => {
    const btn = document.querySelector('[aria-label="Add a page"]');
    for (let i = 0; i < 70; i++) btn.click();
    return { pages: view._state.note.pages.length, disabled: btn.disabled };
  });
  await page.close();
  return { ok: got.pages === 60 && got.disabled, ...got };
});
await check('f04_long_stroke_split', async () => {
  const page = await openPage(browser, { user: 'dan2' });
  await newNote(page);
  const got = await page.evaluate(() => {
    fire('pointerdown', 'pen', 3, 100, 100, { buttons: 1 });
    for (let i = 0; i < 6500; i++) fire('pointermove', 'pen', 3, 100 + (i % 600), 100 + Math.floor(i / 600) * 3 + (i % 2), { buttons: 1 });
    fire('pointerup', 'pen', 3, 100, 140, { buttons: 0 });
    const strokes = view._state.note.pages[0].strokes;
    return { strokes: strokes.length, max: Math.max(...strokes.map((s) => s.points.length)) };
  });
  await page.close();
  return { ok: got.max <= 5000 && got.strokes >= 2, ...got };
});

// 5 and 6. Back to the list after writing: the card shows the saved state and image.
await check('f05_f06_list_after_back', async () => {
  const page = await openPage(browser, { user: 'erin' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(async () => { await view.flush(); await settle(200); });
  const firstUrl = await page.evaluate(() => Object.values(hub.notes)[0].image_url);
  await page.evaluate(() => { hub.online = false; });
  await page.evaluate((pts) => stroke('pen', 2, pts), line(300));
  await page.evaluate(async () => { await view.flush(); await settle(100); });
  const user = 'erin';
  const local = await page.evaluate((u) => idbNotes('fork-ink:' + u), user);
  const keptImage = local.length === 1 && local[0].image_url === firstUrl;
  await page.evaluate(() => { hub.online = true; hub.delay.PUT = 300; });
  await page.evaluate((pts) => stroke('pen', 2, pts), line(400));
  await page.click('[aria-label="All notes"]');
  await page.waitForSelector('.ink-card');
  await page.waitForTimeout(900);
  const card = await page.evaluate(() => ({ meta: document.querySelector('.ink-card .bs-meta').textContent,
    img: document.querySelector('.ink-card img') && document.querySelector('.ink-card img').getAttribute('src') }));
  const latest = await page.evaluate(() => Object.values(hub.notes)[0].image_url);
  await page.close();
  return { ok: keptImage && !card.meta.includes('waiting to sync') && card.img === latest, keptImage, firstUrl, card, latest };
});

// 11. Undo while the pen is down does nothing.
await check('f11_undo_while_drawing', async () => {
  const page = await openPage(browser, { user: 'fay' });
  await newNote(page);
  const got = await page.evaluate(async () => {
    document.querySelector('[aria-label="Add a page"]').click();
    await settle(50);
    const c = canvas();
    const r = c.getBoundingClientRect();
    fire('pointerdown', 'mouse', 1, 300, r.height - 100, { buttons: 1 });
    document.querySelector('.ink-root').dispatchEvent(new KeyboardEvent('keydown', { key: 'z', ctrlKey: true, bubbles: true }));
    document.querySelector('[aria-label="Undo"]').click();
    fire('pointermove', 'mouse', 1, 360, r.height - 90, { buttons: 1 });
    fire('pointerup', 'mouse', 1, 360, r.height - 90, { buttons: 0 });
    await settle(50);
    return { pages: view._state.note.pages.length, strokes: view.strokeCount(), errors: window.errors.slice() };
  });
  await page.close();
  return { ok: got.pages === 2 && got.strokes === 1 && !got.errors.length, ...got };
});

// 11c. An autosave in the middle of an erase doesn't save half of it.
await check('f11c_no_save_mid_erase', async () => {
  const page = await openPage(browser, { user: 'fay2' });
  await newNote(page);
  await page.evaluate((pts) => { stroke('pen', 2, pts); }, line(200));
  await page.evaluate((pts) => { stroke('pen', 2, pts); }, line(260));
  await page.evaluate(async () => { await view.flush(); await settle(200); });
  const got = await page.evaluate(async () => {
    fire('pointerdown', 'pen', 2, 210, 150, { buttons: 32 });
    fire('pointermove', 'pen', 2, 210, 205, { buttons: 32 });
    view._state.dirty = true;                 // as if a stroke were drawn just before
    await view._persist();                    // the autosave timer fires mid-erase
    await settle(200);
    const savedMid = Object.values(hub.notes)[0].pages[0].strokes.length;
    fire('pointermove', 'pen', 2, 210, 265, { buttons: 32 });
    fire('pointerup', 'pen', 2, 210, 265, { buttons: 0 });
    return { savedMid, after: view.strokeCount() };
  });
  await page.close();
  return { ok: got.savedMid === 2 && got.after === 0, ...got };
});

// 12. A slow load of one note doesn't replace the note being written.
await check('f12_open_race', async () => {
  const page = await openPage(browser, { user: 'gus' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(async () => { await view.flush(); await settle(200); });
  const oldId = await page.evaluate(() => view._state.note.id);
  await page.click('[aria-label="All notes"]');
  await page.waitForSelector('.ink-card');
  await page.evaluate(() => { hub.delay.GET = 600; });
  await page.evaluate(() => { document.querySelector('.ink-card').click(); });
  await page.waitForTimeout(100);
  await page.evaluate(() => document.querySelector('.ink-list-head .bs-btn.primary').click());
  await page.waitForTimeout(1200);
  const now = await page.evaluate(() => view._state.note && view._state.note.id);
  await page.close();
  return { ok: now && now !== oldId, oldId, now };
});

// 13. A 409 while the pen is down: the stroke is dropped cleanly and nothing is frozen.
await check('f13_conflict_mid_stroke', async () => {
  const page = await openPage(browser, { user: 'hal' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(async () => { await view.flush(); await settle(200); });
  const got = await page.evaluate(async () => {
    const id = view._state.note.id;
    const theirs = { ...hub.notes[id], edited_at: Date.now() + 100000, title: 'From the desktop' };
    hub.notes[id] = theirs;
    // A save leaves before the pen goes down; its 409 arrives mid-stroke.
    hub.delay.PUT = 300;
    view._state.dirty = true;
    await view._persist();
    fire('pointerdown', 'pen', 2, 150, 400, { buttons: 1 });
    fire('pointermove', 'pen', 2, 200, 420, { buttons: 1 });
    await settle(600);
    // Nothing of the dropped stroke is left: the visible canvas is just the pages.
    const liveCleared = (() => {
      const a = canvas().getContext('2d').getImageData(0, 0, canvas().width, canvas().height).data;
      const b = view._base.getContext('2d').getImageData(0, 0, view._base.width, view._base.height).data;
      for (let i = 0; i < a.length; i++) if (Math.abs(a[i] - b[i]) > 2) return false;
      return true;
    })();
    fire('pointermove', 'pen', 2, 260, 420, { buttons: 1 });
    fire('pointerup', 'pen', 2, 260, 420, { buttons: 0 });
    await settle(50);
    // the next stroke draws normally
    stroke('pen', 2, [[150, 500], [250, 510], [350, 500]]);
    return { title: view._state.note.title, liveCleared, strokes: view.strokeCount(), errors: window.errors.slice() };
  });
  await page.close();
  return { ok: got.title === 'From the desktop' && got.liveCleared && got.strokes === 2 && !got.errors.length, ...got };
});

// 13c. A 409 for a note deleted on this device doesn't bring it back.
await check('f13c_conflict_after_local_delete', async () => {
  const page = await openPage(browser, { user: 'ian' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(async () => { await view.flush(); await settle(200); });
  const got = await page.evaluate(async () => {
    const id = view._state.note.id;
    hub.notes[id] = { ...hub.notes[id], edited_at: Date.now() + 100000 };
    hub.delay.PUT = 300;
    stroke('pen', 2, [[150, 300], [250, 310]]);
    const p = view._persist();
    await settle(50);
    await view._deleteLocal(id);
    await p; await settle(500);
    return (await idbNotes('fork-ink:ian')).map((n) => n.id).includes(id);
  });
  await page.close();
  return { ok: got === false, resurrected: got };
});

// 14. Ink stays on the page; the eraser follows the pen onto the next page.
await check('f14_margins_and_pages', async () => {
  const page = await openPage(browser, { user: 'jo' });
  await newNote(page);
  const got = await page.evaluate(async () => {
    const r = canvas().getBoundingClientRect();
    const k = r.width / (1000 + 48);
    // a stroke in the left margin, outside the page
    stroke('mouse', 1, [[k * 4, 200], [k * 10, 260]]);
    const pts = view._state.note.pages[0].strokes[0].points;
    const inside = pts.every(([x, y]) => x >= 0 && x <= 1000 && y >= 0 && y <= 1414);
    // page 2 with a stroke near its top; erase by dragging from page 1 down onto it
    document.querySelector('[aria-label="Add a page"]').click();
    view.fit();
    await settle(50);
    view._state.sy = 1414 - 300; view._clamp();
    const y1 = (1414 - 100 - view._state.sy) * k;           // page 1, near its bottom
    const y2 = (1414 + 40 + 120 - view._state.sy) * k;       // page 2, near its top
    stroke('mouse', 1, [[300, y2], [400, y2 + 5], [500, y2]]);
    const before = view._state.note.pages[1].strokes.length;
    stroke('pen', 2, [[400, y1], [400, (y1 + y2) / 2], [400, y2], [400, y2 + 5]], { buttons: 32 });
    return { inside, before, after: view._state.note.pages[1].strokes.length };
  });
  await page.close();
  return { ok: got.inside && got.before === 1 && got.after === 0, ...got };
});

// 15. Overlapping highlighter strokes don't get darker (same as the hub's image).
await check('f15_highlighter_overlap', async () => {
  const page = await openPage(browser, { user: 'kim' });
  await newNote(page);
  const got = await page.evaluate(async () => {
    document.querySelector('[data-pen="highlighter"]').click();
    stroke('pen', 2, [[150, 300], [450, 300]]);
    stroke('pen', 2, [[300, 300], [600, 300]]);
    await settle(100);
    const c = canvas();
    const ctx = c.getContext('2d');
    const dpr = c.width / c.getBoundingClientRect().width;
    const px = (x, y) => [...ctx.getImageData(Math.round(x * dpr), Math.round(y * dpr), 1, 1).data];
    return { single: px(200, 300), double: px(380, 300) };
  });
  await page.close();
  const same = got.single.every((v, i) => Math.abs(v - got.double[i]) <= 2);
  return { ok: same, ...got };
});

// 19. Enter on a card's Delete button only asks to delete.
await check('f19_enter_on_delete', async () => {
  const page = await openPage(browser, { user: 'lee' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.click('[aria-label="All notes"]');
  await page.waitForSelector('.ink-card');
  page.on('dialog', (d) => d.dismiss());
  await page.focus('.ink-card button');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(400);
  const screen = await page.evaluate(() => view._state.screen);
  await page.focus('.ink-card');
  await page.keyboard.press(' ');
  await page.waitForTimeout(400);
  const screen2 = await page.evaluate(() => view._state.screen);
  await page.close();
  return { ok: screen === 'list' && screen2 === 'editor', screen, screen2 };
});

// 22. Reopening Write shows an edit made elsewhere; no listener while closed.
await check('f22_refresh_editor', async () => {
  const page = await openPage(browser, { user: 'max' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(async () => { await view.flush(); await settle(200); });
  const got = await page.evaluate(async () => {
    view.suspend();
    const id = view._state.note.id;
    hub.notes[id] = { ...hub.notes[id], edited_at: Date.now() + 1000, title: 'Edited on the desktop',
      pages: [{ ...hub.notes[id].pages[0], strokes: [] }] };
    const timersWhileHidden = view._timers();
    await view.refresh();
    await settle(200);
    return { title: view._state.note.title, strokes: view.strokeCount(), timersWhileHidden, timersAfter: view._timers() };
  });
  await page.close();
  return { ok: got.title === 'Edited on the desktop' && got.strokes === 0 && got.timersWhileHidden === 0
    && got.timersAfter === 2, ...got };
});

// 13d. Last edit wins: written here while a save was out, after the other device's
// edit: this device's version is kept and sent again, not replaced.
await check('f13d_newer_local_edit_wins', async () => {
  const page = await openPage(browser, { user: 'ivy' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(async () => { await view.flush(); await settle(200); });
  const got = await page.evaluate(async () => {
    const id = view._state.note.id;
    hub.notes[id] = { ...hub.notes[id], edited_at: Date.now() + 50, title: 'Desktop' };
    hub.delay.PUT = 400;
    view._state.dirty = true;
    view._persist();                                  // goes out older than the desktop's
    await settle(150);
    stroke('pen', 2, [[150, 300], [250, 310], [350, 300]]);
    await view._persist();                            // newer than the desktop's, stored
    await settle(1500);
    return { title: view._state.note.title, strokes: view.strokeCount(),
      hubStrokes: hub.notes[id].pages[0].strokes.length, puts: hub.calls.filter((c) => c.startsWith('PUT')).length };
  });
  await page.close();
  return { ok: got.title !== 'Desktop' && got.strokes === 2 && got.hubStrokes === 2 && got.puts < 8, ...got };
});

// The black page while writing (Android): the visible canvas is opaque and shows the
// page under the stroke being drawn, so a hardware overlay has nothing to show black.
await check('opaque_canvas_while_writing', async () => {
  const page = await openPage(browser, { user: 'nia' });
  await newNote(page);
  const got = await page.evaluate(async () => {
    await settle(50);
    fire('pointerdown', 'pen', 2, 200, 300, { buttons: 1 });
    fire('pointermove', 'pen', 2, 300, 320, { buttons: 1 });
    const c = canvas();
    const dpr = c.width / c.getBoundingClientRect().width;
    const px = (x, y) => [...c.getContext('2d').getImageData(Math.round(x * dpr), Math.round(y * dpr), 1, 1).data];
    const attrs = c.getContext('2d').getContextAttributes ? c.getContext('2d').getContextAttributes() : {};
    const out = { paper: px(600, 500), stroke: px(250, 310), alpha: attrs.alpha, desync: attrs.desynchronized,
      canvases: document.querySelectorAll('.ink-root .ink-stage canvas').length, // the Text tab's page image is not in the stage
      noSwipe: document.querySelector('.ink-root').hasAttribute('data-no-swipe-dismiss') };
    fire('pointerup', 'pen', 2, 300, 320, { buttons: 0 });
    return out;
  });
  await page.close();
  const white = got.paper.slice(0, 3).every((v) => v > 240) && got.paper[3] === 255;
  return { ok: white && got.stroke[0] < 120 && got.alpha === false && got.canvases === 1 && got.noSwipe, ...got };
});

// Drawn in pieces (pen) and in boxes (eraser, highlighter), the screen must match a
// full redraw of the note: no leftovers of the open end, the prediction or the ring.
await check('incremental_matches_full_redraw', async () => {
  const page = await openPage(browser, { user: 'ola' });
  await newNote(page);
  const got = await page.evaluate(async () => {
    const c = canvas();
    const zig = (x0, y0, n, dx, dy) => Array.from({ length: n }, (_, i) => [x0 + i * dx, y0 + (i % 2 ? dy : 0)]);
    for (let line = 0; line < 6; line++) stroke('pen', 2, zig(80, 120 + line * 40, 40, 12, 14));
    document.querySelector('[data-pen="highlighter"]').click();
    stroke('pen', 2, [[70, 160], [300, 165], [520, 160]]);
    document.querySelector('[data-pen="red"]').click();
    stroke('pen', 2, zig(100, 420, 60, 8, 30));
    stroke('pen', 2, [[150, 100], [155, 150], [160, 175]], { buttons: 32 });   // barrel: erase across two lines
    stroke('pen', 2, [[400, 110], [380, 260]], { buttons: 32 });
    await settle(100);
    const read = () => c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    const shown = read();
    view.fit();                       // a full redraw of the same note and view
    await settle(100);
    const full = read();
    let diff = 0;
    for (let i = 0; i < shown.length; i += 4) {
      if (Math.abs(shown[i] - full[i]) + Math.abs(shown[i + 1] - full[i + 1]) + Math.abs(shown[i + 2] - full[i + 2]) > 12) diff++;
    }
    return { diff, strokes: view.strokeCount(), errors: window.errors.slice() };
  });
  await page.close();
  return { ok: got.diff < 30 && got.strokes >= 3 && !got.errors.length, ...got };
});

// Predicted points are drawn ahead of the pen and taken back on the next update: after
// the stroke, nothing of them is left (they were never part of the stroke).
await check('prediction_leaves_nothing', async () => {
  const page = await openPage(browser, { user: 'pia' });
  await newNote(page);
  const got = await page.evaluate(async () => {
    const c = canvas();
    const r = c.getBoundingClientRect();
    const ev = (type, x, y, buttons, ahead) => {
      const e = new PointerEvent(type, { bubbles: true, cancelable: true, pointerType: 'pen', pointerId: 4,
        clientX: r.left + x, clientY: r.top + y, buttons, button: type === 'pointermove' ? -1 : 0 });
      // Fake predictions far ahead and off to the side: a leftover would be obvious.
      if (ahead) Object.defineProperty(e, 'getPredictedEvents', { value: () => [
        { clientX: r.left + x + 40, clientY: r.top + y + 60, timeStamp: e.timeStamp },
        { clientX: r.left + x + 80, clientY: r.top + y + 120, timeStamp: e.timeStamp }] });
      c.dispatchEvent(e);
    };
    ev('pointerdown', 100, 200, 1, false);
    let sawAhead = false;
    for (let i = 1; i <= 30; i++) {
      ev('pointermove', 100 + i * 10, 200 + (i % 2) * 6, 1, true);
      if (i === 15) {
        // mid-stroke: the prediction is on screen 80 px ahead, below the pen
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height);
        const dpr = c.width / r.width;
        const at = (x, y) => d.data[(Math.round(y * dpr) * c.width + Math.round(x * dpr)) * 4];
        sawAhead = at(100 + 15 * 10 + 40, 200 + 6 + 60) < 120 || at(100 + 15 * 10 + 20, 206 + 30) < 120;
      }
    }
    ev('pointerup', 400, 200, 0, false);
    await settle(100);
    const read = () => c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    const shown = read();
    view.fit();
    await settle(100);
    const full = read();
    let diff = 0;
    for (let i = 0; i < shown.length; i += 4) {
      if (Math.abs(shown[i] - full[i]) + Math.abs(shown[i + 1] - full[i + 1]) + Math.abs(shown[i + 2] - full[i + 2]) > 12) diff++;
    }
    return { sawAhead, diff, points: view._state.note.pages[0].strokes[0].points.length };
  });
  await page.close();
  return { ok: got.sawAhead && got.diff < 30 && got.points === 31, ...got };
});

// ------------------------------------------------ H2: the Text tab (transcription, review)
const textTab = (page) => page.evaluate(async () => { document.querySelectorAll('.ink-tab')[1].click(); await settle(50); });
const inkTab = (page) => page.evaluate(async () => { document.querySelectorAll('.ink-tab')[0].click(); await settle(50); });
const textState = (page) => page.evaluate(() => ({
  view: view._state.view, page: view._state.textPage, text: document.querySelector('.ink-tarea').value,
  state: document.querySelector('.ink-tstate').textContent, marks: document.querySelectorAll('.ink-thl mark').length,
  readOnly: document.querySelector('.ink-tarea').readOnly }));
const typeText = (page, text) => page.evaluate((t) => {
  const a = document.querySelector('.ink-tarea'); a.value = t; a.dispatchEvent(new Event('input')); }, text);
const waitText = (page, pred, ms = 3000) => page.waitForFunction(pred, null, { timeout: ms });

// Writing then opening the Text tab reads the page; the machine text shows with [?]
// marked, and Save review stores the checked text in the note.
await check('h2_01_transcribe_and_review', async () => {
  const page = await openPage(browser, { user: 'nia' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await textTab(page);
  await waitText(page, () => document.querySelector('.ink-tarea').value.includes('Line one'));
  const machine = await textState(page);
  const calls = await page.evaluate(() => hub.transcribeCalls);
  // never reviewed: no Restore button on screen
  const restoreShown = await page.evaluate(() => getComputedStyle(document.querySelector('.ink-trow .bs-btn[title^="Put back"]')).display !== 'none');
  await typeText(page, 'Line one of page 1\nthe blue word');
  await page.evaluate(async () => { document.querySelector('.ink-trow .bs-btn.primary').click(); await settle(700); });
  const after = await textState(page);
  const content = await page.evaluate(() => Object.values(hub.content)[0]);
  const drafts = await page.evaluate(() => new Promise((resolve) => {
    const req = indexedDB.open('fork-ink:nia');
    req.onsuccess = () => { const g = req.result.transaction('drafts').objectStore('drafts').getAll(); g.onsuccess = () => resolve(g.result.length); };
  }));
  await page.close();
  return { ok: !restoreShown && machine.marks === 1 && /unreadable/.test(machine.state) && calls.length === 1 && calls[0].only_changed
    && content === 'Line one of page 1\nthe blue word' && /Reviewed/.test(after.state) && drafts === 0,
  machine, calls, after, content, drafts, restoreShown };
});

// Leaving a page whose strokes changed asks the hub to read it; scrolling without
// changes doesn't.
await check('h2_02_auto_transcribe_on_leaving_a_page', async () => {
  const page = await openPage(browser, { user: 'oli' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  const got = await page.evaluate(async () => {
    document.querySelector('[aria-label="Add a page"]').click();     // scrolls to page 2
    await settle(600);
    const first = (hub.transcribeCalls || []).slice();
    view._state.sy = -40; view._clamp(); window.dispatchEvent(new Event('resize'));
    view.fit();
    await settle(400);
    view._state.sy = 1414; view._clamp(); view.fit();                // no change: no call
    await settle(400);
    return { first, all: hub.transcribeCalls || [] };
  });
  await page.close();
  const p1 = got.first[0];
  return { ok: got.first.length === 1 && p1.pages.length === 1 && p1.only_changed && got.all.length === 1, ...got };
});

// Ctrl+Z while typing in the Text tab doesn't undo strokes.
await check('h2_03_undo_key_in_text_tab', async () => {
  const page = await openPage(browser, { user: 'pat' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await textTab(page);
  await page.focus('.ink-tarea');
  await page.keyboard.press('Control+z');
  await page.keyboard.press('Control+y');
  const strokes = await page.evaluate(() => view.strokeCount());
  await page.close();
  return { ok: strokes === 1, strokes };
});

// No tab change while the pen is down.
await check('h2_04_no_tab_switch_mid_stroke', async () => {
  const page = await openPage(browser, { user: 'quin' });
  await newNote(page);
  const got = await page.evaluate(async () => {
    fire('pointerdown', 'pen', 2, 150, 300, { buttons: 1 });
    fire('pointermove', 'pen', 2, 200, 310, { buttons: 1 });
    const disabled = document.querySelectorAll('.ink-tab')[1].disabled;
    document.querySelectorAll('.ink-tab')[1].click();
    const viewMid = view._state.view;
    fire('pointermove', 'pen', 2, 260, 300, { buttons: 1 });
    fire('pointerup', 'pen', 2, 260, 300, { buttons: 0 });
    await settle(50);
    return { disabled, viewMid, strokes: view.strokeCount(), points: view._state.note.pages[0].strokes[0].points.length };
  });
  await page.close();
  return { ok: got.disabled && got.viewMid === 'ink' && got.strokes === 1 && got.points === 3, ...got };
});

// A slow transcript answer for one note never shows in another.
await check('h2_05_slow_answer_for_another_note_is_dropped', async () => {
  const page = await openPage(browser, { user: 'rae' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await textTab(page);
  await waitText(page, () => document.querySelector('.ink-tarea').value.includes('Line one'));
  const first = await page.evaluate(() => view._state.note.id);
  await page.click('[aria-label="All notes"]');
  await page.waitForSelector('.ink-card');
  await page.evaluate(() => { hub.read = () => 'Second note text'; });
  await page.evaluate(async () => { await view.newNote(); await settle(100); });
  await page.evaluate((pts) => stroke('pen', 2, pts), line(300));
  await page.evaluate(async () => { await view.flush(); await settle(200); });
  const second = await page.evaluate(() => view._state.note.id);
  const got = await page.evaluate(async ({ a }) => {
    hub.delay['GET /api/ink/notes/' + a + '/transcripts'] = 500;
    // open the first note's Text tab, then move to the second note at once
    view.openNote(a, { textPage: 0 });
    await settle(30);
    document.querySelector('[aria-label="All notes"]').click();
    await settle(50);
    return null;
  }, { a: first });
  await page.waitForSelector('.ink-card');
  await page.evaluate(async (b) => { await view.openNote(b, { textPage: 0 }); await settle(50); }, second);
  await waitText(page, () => document.querySelector('.ink-tarea').value.includes('Second note'));
  await page.waitForTimeout(700);
  const now = await textState(page);
  const id = await page.evaluate(() => view._state.note.id);
  await page.close();
  return { ok: id === second && now.text.includes('Second note') && !now.text.includes('Line one'), now, got };
});

// Edits not saved yet survive a reload, and only for the user who typed them.
await check('h2_06_drafts_per_user', async () => {
  const ctx = await browser.newContext({ viewport: { width: 1000, height: 800 } });
  const page = await openPage(browser, { user: 'sam', context: ctx });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await textTab(page);
  await waitText(page, () => document.querySelector('.ink-tarea').value.includes('Line one'));
  await typeText(page, 'Private words of sam');
  await page.waitForTimeout(600);
  const id = await page.evaluate(() => view._state.note.id);
  const hubState = await page.evaluate(() => JSON.stringify({ notes: hub.notes, tr: hub.tr }));
  await page.close();
  const again = await openPage(browser, { user: 'sam', context: ctx });
  await again.evaluate((st) => { const s = JSON.parse(st); hub.notes = s.notes; hub.tr = s.tr; }, hubState);
  await again.evaluate(async (nid) => { await view.openNote(nid, { textPage: 0 }); await settle(300); }, id);
  const restored = await textState(again);
  await again.close();
  const other = await openPage(browser, { user: 'tia', context: ctx });
  const leaked = await other.evaluate(() => new Promise((resolve) => {
    const req = indexedDB.open('fork-ink:tia');
    req.onsuccess = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('drafts')) { resolve(0); return; }
      const g = db.transaction('drafts').objectStore('drafts').getAll();
      g.onsuccess = () => resolve(g.result.length);
    };
  }));
  await ctx.close();
  return { ok: restored.text === 'Private words of sam' && /not saved/.test(restored.state) && leaked === 0, restored, leaked };
});

// The list shows pages that need review; a click opens that page's text.
await check('h2_07_needs_review_list', async () => {
  const page = await openPage(browser, { user: 'uma' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(async () => {
    document.querySelector('[aria-label="Add a page"]').click(); await settle(50);
  });
  await page.evaluate((pts) => stroke('pen', 2, pts), line(300));
  await page.evaluate(async () => {
    await view.flush(); await settle(200);
    const n = Object.values(hub.notes)[0];
    await fetch('/api/ink/notes/' + n.note_id + '/transcribe', { method: 'POST', body: '{}' });
    await settle(400);
  });
  await page.click('[aria-label="All notes"]');
  await page.waitForSelector('.ink-review-item');
  const items = await page.evaluate(() => [...document.querySelectorAll('.ink-review-item')].map((b) => b.textContent));
  await page.evaluate(() => document.querySelectorAll('.ink-review-item')[1].click());
  await page.waitForTimeout(400);
  const st = await textState(page);
  await page.close();
  return { ok: items.length === 2 && /page 2/.test(items[1]) && st.view === 'text' && st.page === 1
    && st.text.includes('page 2'), items, st };
});

// No vision model: the reason shows, nothing changes.
await check('h2_08_no_model_message', async () => {
  const page = await openPage(browser, { user: 'val' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(() => { hub.noModel = true; });
  await textTab(page);
  await page.waitForTimeout(300);
  await page.evaluate(async () => { document.querySelector('.ink-trow .bs-btn:not(.primary):not([hidden])').click(); await settle(300); });
  const buttons = await page.evaluate(() => [...document.querySelectorAll('.ink-trow .bs-btn')].map((b) => b.textContent));
  await page.evaluate(async () => {
    [...document.querySelectorAll('.ink-trow .bs-btn')].find((b) => b.textContent === 'Transcribe').click(); await settle(300); });
  const st = await textState(page);
  await page.close();
  return { ok: /Settings, Vision/.test(st.state) && st.text === '', st, buttons };
});

// While Write is closed nothing polls.
await check('h2_09_no_polling_while_closed', async () => {
  const page = await openPage(browser, { user: 'wes' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  const got = await page.evaluate(async () => {
    hub.readMs = 60000;                       // stays pending
    document.querySelectorAll('.ink-tab')[1].click();
    await settle(400);
    view.suspend();
    const polls = () => hub.calls.filter((c) => c.endsWith('/transcripts')).length;
    const before = polls();
    await settle(6000);
    return { during: polls() - before, polledBefore: before };
  });
  await page.close();
  return { ok: got.during === 0 && got.polledBefore >= 1, ...got };
});

// Transcribing a reviewed page asks first; its reviewed text can be put back.
await check('h2_10_reviewed_text_is_kept_aside', async () => {
  const page = await openPage(browser, { user: 'xan' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await textTab(page);
  await waitText(page, () => document.querySelector('.ink-tarea').value.includes('Line one'));
  await typeText(page, 'My checked words');
  await page.evaluate(async () => { document.querySelector('.ink-trow .bs-btn.primary').click(); await settle(200); });
  let asked = 0;
  page.on('dialog', (d) => { asked += 1; d.accept(); });
  await page.evaluate(async () => {
    [...document.querySelectorAll('.ink-trow .bs-btn')].find((b) => /Transcribe/.test(b.textContent)).click(); await settle(100); });
  await waitText(page, () => document.querySelector('.ink-tarea').value.includes('Line one'));
  const restoreVisible = await page.evaluate(() => getComputedStyle(document.querySelector('.ink-trow .bs-btn[title^="Put back"]')).display !== 'none');
  await page.evaluate(async () => { document.querySelector('.ink-trow .bs-btn[title^="Put back"]').click(); await settle(50); });
  const st = await textState(page);
  await page.close();
  return { ok: asked === 1 && restoreVisible && st.text === 'My checked words' && /not saved/.test(st.state), asked, restoreVisible, st };
});

// Another device's newer version arriving while in the Text tab keeps the tab and page.
await check('h2_11_conflict_keeps_the_text_tab', async () => {
  const page = await openPage(browser, { user: 'yul' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(async () => { await view.flush(); await settle(200); });
  await textTab(page);
  const got = await page.evaluate(async () => {
    const id = view._state.note.id;
    hub.notes[id] = { ...hub.notes[id], edited_at: Date.now() + 100000, title: 'From the desktop' };
    view._state.dirty = true;
    await view._persist();
    await settle(400);
    return { view: view._state.view, title: view._state.note.title, errors: window.errors.slice() };
  });
  await page.close();
  return { ok: got.view === 'text' && got.title === 'From the desktop' && !got.errors.length, ...got };
});

// Edits on two pages in quick succession are both kept on the device.
await check('h2_12_drafts_of_two_pages', async () => {
  const page = await openPage(browser, { user: 'zed' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(async () => { document.querySelector('[aria-label="Add a page"]').click(); await settle(50); });
  await page.evaluate((pts) => stroke('pen', 2, pts), line(300));
  await textTab(page);
  await page.waitForTimeout(300);
  await page.evaluate(() => { view._state.textPage = 0; });
  await page.evaluate(() => document.querySelector('[aria-label="Previous page"]').click());
  await page.evaluate(async () => {
    const a = document.querySelector('.ink-tarea');
    a.value = 'first page words'; a.dispatchEvent(new Event('input'));
    document.querySelector('[aria-label="Next page"]').click();
    a.value = 'second page words'; a.dispatchEvent(new Event('input'));
    await settle(800);
  });
  const saved = await page.evaluate(() => new Promise((resolve) => {
    const req = indexedDB.open('fork-ink:zed');
    req.onsuccess = () => { const g = req.result.transaction('drafts').objectStore('drafts').getAll(); g.onsuccess = () => resolve(g.result.map((d) => d.text).sort()); };
  }));
  await page.close();
  return { ok: saved.length === 2 && saved[0] === 'first page words', saved };
});

// ------------------------------------------- QC bug hunt on H2 (2026-10-26), PR 92
// docs/fork/qc-ink-h2-2026-10-26.md
const idbDrafts = (page, user) => page.evaluate((u) => new Promise((resolve) => {
  const req = indexedDB.open('fork-ink:' + u);
  req.onsuccess = () => {
    const db = req.result;
    if (!db.objectStoreNames.contains('drafts')) { resolve([]); return; }
    const g = db.transaction('drafts').objectStore('drafts').getAll();
    g.onsuccess = () => resolve(g.result);
  };
}), user);

// O-5. An older transcripts answer arriving after the transcribe answer doesn't stop
// the Text tab from following the run.
await check('qc_o5_old_transcripts_answer_is_dropped', async () => {
  const page = await openPage(browser, { user: 'qa1' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(async () => { await view.flush(); await settle(300); });
  await page.evaluate(() => {
    // the hub answers the first GET at once (nothing read yet) but it arrives late
    const hubFetch = window.fetch;
    let first = true;
    window.fetch = async (url, opts = {}) => {
      const res = hubFetch(url, opts);
      if (first && (opts.method || 'GET') === 'GET' && String(url).endsWith('/transcripts')) {
        first = false;
        const r = await res;
        await settle(400);
        return r;
      }
      return res;
    };
  });
  await textTab(page);
  let ok = true;
  try { await waitText(page, () => document.querySelector('.ink-tarea').value.includes('Line one'), 4500); } catch (_) { ok = false; }
  const st = await textState(page);
  await page.close();
  return { ok, st };
});

// O-8 (1). Typing a page's text back to what the hub has keeps no draft, so a later
// transcription shows.
await check('qc_o8_no_op_draft_is_dropped', async () => {
  const page = await openPage(browser, { user: 'qa2' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await textTab(page);
  await waitText(page, () => document.querySelector('.ink-tarea').value.includes('Line one'));
  const original = (await textState(page)).text;
  await typeText(page, original + ' x');
  await page.waitForTimeout(600);
  const kept = (await idbDrafts(page, 'qa2')).length;
  await typeText(page, original);
  await page.waitForTimeout(600);
  const left = (await idbDrafts(page, 'qa2')).length;
  await page.evaluate(() => { hub.read = () => 'New words after more ink'; });
  await inkTab(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(300));
  await textTab(page);
  let shown = true;
  try { await waitText(page, () => document.querySelector('.ink-tarea').value.includes('New words'), 3000); } catch (_) { shown = false; }
  const st = await textState(page);
  await page.close();
  return { ok: kept === 1 && left === 0 && shown && !/not saved/.test(st.state), kept, left, shown, st };
});

// O-8 (3). A note with edits not synced yet keeps its "to review" pages in the list.
await check('qc_o8_list_keeps_review_of_unsynced_note', async () => {
  const page = await openPage(browser, { user: 'qa3' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await page.evaluate(async () => {
    await view.flush(); await settle(300);
    const n = Object.values(hub.notes)[0];
    await fetch('/api/ink/notes/' + n.note_id + '/transcribe', { method: 'POST', body: '{}' });
    await settle(400);
    hub.answer['PUT /api/ink/notes/' + n.note_id] = { status: 500, body: {} };  // stays unsynced
  });
  await page.evaluate((pts) => stroke('pen', 2, pts), line(300));
  await page.click('[aria-label="All notes"]');
  await page.waitForSelector('.ink-card');
  await page.waitForTimeout(300);
  const got = await page.evaluate(() => ({ items: document.querySelectorAll('.ink-review-item').length,
    meta: document.querySelector('.ink-card .bs-meta').textContent }));
  await page.close();
  return { ok: got.items === 1 && /1 to review/.test(got.meta) && /waiting to sync/.test(got.meta), ...got };
});

// O-8 (4). Closing the Write window reads the pages changed, like Back does.
await check('qc_o8_closing_write_reads_changed_pages', async () => {
  const page = await openPage(browser, { user: 'qa4' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  const calls = await page.evaluate(async () => {
    view.suspend();
    await settle(600);
    const out = (hub.transcribeCalls || []).slice();
    view.resume();
    return out;
  });
  await page.close();
  return { ok: calls.length === 1 && calls[0].only_changed && calls[0].pages.length === 1, calls };
});

// O-8 (5). Deleting a note drops its drafts on the device.
await check('qc_o8_delete_drops_drafts', async () => {
  const page = await openPage(browser, { user: 'qa5' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await textTab(page);
  await waitText(page, () => document.querySelector('.ink-tarea').value.includes('Line one'));
  await typeText(page, 'a draft never saved');
  await page.waitForTimeout(600);
  const before = (await idbDrafts(page, 'qa5')).length;
  await page.click('[aria-label="All notes"]');
  await page.waitForSelector('.ink-card');
  page.on('dialog', (d) => d.accept());
  await page.evaluate(() => document.querySelector('.ink-card .bs-icon-btn').click());
  await page.waitForTimeout(600);
  const after = (await idbDrafts(page, 'qa5')).length;
  const notes = await page.evaluate(() => Object.keys(hub.notes).length);
  await page.close();
  return { ok: before === 1 && after === 0 && notes === 0, before, after, notes };
});

// O-9 (2). The page image in the Text tab follows the window's size (rotation).
await check('qc_o9_text_image_follows_resize', async () => {
  const page = await openPage(browser, { user: 'qa6' });
  await newNote(page);
  await page.evaluate((pts) => stroke('pen', 2, pts), line(200));
  await textTab(page);
  await page.waitForTimeout(300);
  const width = () => page.evaluate(() => parseFloat(document.querySelector('.ink-timage').style.width));
  const before = await width();
  await page.evaluate(() => { document.getElementById('host').style.width = '520px'; });
  await page.waitForTimeout(400);
  const after = await width();
  const fits = await page.evaluate(() => document.querySelector('.ink-tfigure').clientWidth);
  await page.close();
  return { ok: after < before && after <= fits, before, after, fits };
});

// While the pen is down, the line on screen is whole: drawn in pieces it must match the
// same stroke drawn in one go. A real pen sends ~240 samples a second, so the steps
// are tiny; the bug was a dotted line until the pen lifted.
await check('live_stroke_is_whole_while_writing', async () => {
  const page = await openPage(browser, { user: 'quin' });
  await newNote(page);
  const got = await page.evaluate(async () => {
    const c = canvas();
    const r = c.getBoundingClientRect();
    const ev = (type, x, y, buttons, ahead) => {
      const e = new PointerEvent(type, { bubbles: true, cancelable: true, pointerType: 'pen', pointerId: 6,
        clientX: r.left + x, clientY: r.top + y, buttons, button: type === 'pointermove' ? -1 : 0 });
      if (ahead) Object.defineProperty(e, 'getPredictedEvents', { value: () => [
        { clientX: r.left + x + 3, clientY: r.top + y + 2, timeStamp: e.timeStamp },
        { clientX: r.left + x + 6, clientY: r.top + y + 4, timeStamp: e.timeStamp }] });
      c.dispatchEvent(e);
    };
    // A curvy line in 0.7 px steps, with predictions, like a fast real pen.
    ev('pointerdown', 120, 200, 1, false);
    let x = 120; let y = 200;
    for (let i = 1; i <= 500; i++) {
      x = 120 + i * 0.7;
      y = 200 + Math.sin(i / 25) * 60;
      ev('pointermove', x, y, 1, i < 500);
    }
    const read = () => c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    const shown = read();
    view.fit();                          // the same live stroke, drawn in one go
    await settle(100);
    const full = read();
    let diff = 0;
    for (let i = 0; i < shown.length; i += 4) {
      if (Math.abs(shown[i] - full[i]) + Math.abs(shown[i + 1] - full[i + 1]) + Math.abs(shown[i + 2] - full[i + 2]) > 60) diff++;
    }
    ev('pointerup', x, y, 0, false);
    return { diff };
  });
  await page.close();
  return { ok: got.diff < 20, ...got };
});

await browser.close();
console.log(JSON.stringify(results));
