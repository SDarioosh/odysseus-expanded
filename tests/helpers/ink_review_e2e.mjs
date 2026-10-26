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
    const liveCleared = (() => { const c = canvas(); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      for (let i = 3; i < d.length; i += 4) if (d[i]) return false; return true; })();
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
    const c = document.querySelector('.ink-base');
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

await browser.close();
console.log(JSON.stringify(results));
