// Fork: runs the DOM and fetch helpers each fork page script uses (`el`, `$`, `api`)
// over a table of inputs and prints what they did, as one JSON line:
// {"<file>": {"el": {"<case>": outcome}, "api": {...}, "$": {...}}}; static/js/forkDom.js
// itself (once it exists) under "forkDom.js", with its `request` too. The `el` and `$`
// cases need a real DOM (Chromium); without one only `api` (node) runs, and "__skip" says why.
// Run by tests/fork/test_fork_dom.py; by hand: node tests/helpers/fork_dom_cases.mjs
//
// The helper a file uses is found the way the file defines it: a top-level
// `function el(` / `async function api(` / `const $ = …;` in the file, evaluated next to
// the names the file imports from ./forkDom.js (static/js/forkDom.js, the shared helpers).
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { launch, REPO } from './ink_view_harness.mjs';

const JS = path.join(REPO, 'static', 'js');

export const FILES = {
  el: ['beliefs.js', 'forkAutoMode.js', 'forkBackgroundModels.js', 'forkInk.js',
       'forkPhonePairing.js', 'forkVoiceEnroll.js', 'forkWhisperVocabulary.js', 'soundModule.js',
       'today.js', 'todayTrends.js', 'todayWeights.js'],
  api: ['beliefs.js', 'forkInk.js', 'phonePairing.js', 'soundModule.js', 'today.js',
        'todayWeights.js'],
  $: ['beliefs.js', 'callRecorder.js', 'meetingBot.js'],
};

const esc = (s) => s.replace(/[$]/g, '\\$');
const SHARED = 'forkDom.js';
const HAS_SHARED = fs.existsSync(path.join(JS, SHARED));

/** Module source that puts the helper `name` of `file` on globalThis.__h. */
function helperModule(file, name, domUrl) {
  if (file === SHARED) {
    return `import * as __dom from '${domUrl}';\nglobalThis.__h = __dom[${JSON.stringify(name)}];`;
  }
  const src = fs.readFileSync(path.join(JS, file), 'utf8');
  const lines = [];
  const imp = src.match(/^import \{([^}]*)\} from '\.\/forkDom\.js';$/m);
  if (imp) {
    lines.push(`import * as __dom from '${domUrl}';`);
    for (const spec of imp[1].split(',').map((s) => s.trim()).filter(Boolean)) {
      const [exported, local = exported] = spec.split(/\s+as\s+/);
      lines.push(`const ${local} = __dom.${exported};`);
    }
  }
  const fn = src.match(new RegExp(`^(?:async )?function ${esc(name)}\\(.*?^}$`, 'ms'));
  const arrow = src.match(new RegExp(`^const ${esc(name)} = .*;$`, 'm'));
  const local = fn || arrow;
  if (local) lines.push(local[0]);
  else if (!imp || !new RegExp(`(^|[\\s,{])${esc(name)}([\\s,}]|$)`).test(imp[1])) {
    throw new Error(`${file}: no ${name} defined or imported`);
  }
  lines.push(`globalThis.__h = ${name};`);
  return lines.join('\n');
}

// ------------------------------------------------------------------ el and $ in Chromium
// Each case gets the helper as `el` and a few ready-made values; it returns the node.
const EL_CASES = {
  no_attrs: 'el("div")',
  string_attrs: 'el("div", { id: "x", title: "T", "aria-label": "A", "data-k": "1", role: "button" })',
  className_key: 'el("div", { className: "a b" })',
  class_key: 'el("div", { class: "a b" })',
  style_string: 'el("div", { style: "margin:0 0 4px;color:red" })',
  html_key: 'el("span", { html: "<b>x</b>" })',
  text_key: 'el("span", { text: "hi" })',
  on_handler: 'el("button", { onclick: () => { clicks++; } })',
  attr_true: 'el("div", { hidden: true })',
  attr_false: 'el("div", { hidden: false })',
  attr_null: 'el("div", { title: null })',
  attr_undefined: 'el("div", { title: undefined })',
  attr_empty_string: 'el("div", { hidden: "" })',
  attr_number: 'el("textarea", { rows: 3 })',
  option_value: 'el("option", { value: "v1" }, "Label")',
  child_string: 'el("p", {}, "hello")',
  child_empty_string: 'el("p", {}, "")',
  child_zero: 'el("p", {}, 0)',
  child_null: 'el("p", {}, null)',
  child_undefined: 'el("p", {}, undefined)',
  child_false: 'el("p", {}, false)',
  child_true: 'el("p", {}, true)',
  child_node: 'el("p", {}, kid())',
  children_mixed: 'el("p", {}, "a", kid(), "b")',
  child_array: 'el("p", {}, ["a", "b"])',
  attrs_omitted_with_child: 'el("p", undefined, "x")',
};

const DOLLAR_CASES = {
  by_id: '$("#a")',
  by_class: '$(".b")',
  missing: '$(".nothing")',
};

// What a node looks like, with invisible differences left out: attributes sorted, the
// style attribute as the browser reads it (cssText), adjacent text joined, empty text
// dropped (an empty text node changes neither innerHTML, :empty nor layout).
const SERIALIZE = `
window.__ser = function ser(n) {
  if (n === null || n === undefined) return n;
  if (!(n instanceof Node)) return { notANode: String(n) };
  const attrs = [...n.attributes].map((a) => [a.name, a.name === 'style' ? n.style.cssText : a.value])
    .sort((x, y) => (x[0] < y[0] ? -1 : 1));
  const kids = [];
  for (const c of n.childNodes) {
    if (c.nodeType === 3) {
      if (!c.data) continue;
      if (typeof kids[kids.length - 1] === 'string') kids[kids.length - 1] += c.data;
      else kids.push(c.data);
    } else kids.push(ser(c));
  }
  return { tag: n.tagName.toLowerCase(), attrs, kids };
};`;

async function runDom(browser, results) {
  const page = await browser.newPage();
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/') {
      return route.fulfill({ contentType: 'text/html',
        body: '<!doctype html><html><body><div id="a" class="b">A</div><div class="b">B</div></body></html>' });
    }
    const file = path.join(REPO, url.pathname);
    if (url.pathname.startsWith('/static/js/') && fs.existsSync(file)) {
      return route.fulfill({ contentType: 'text/javascript', body: fs.readFileSync(file) });
    }
    return route.fulfill({ status: 404, body: '' });
  });
  await page.goto('http://hub.test/');
  await page.addScriptTag({ content: SERIALIZE });
  for (const [name, cases] of [['el', EL_CASES], ['$', DOLLAR_CASES]]) {
    for (const file of [...FILES[name], ...(HAS_SHARED ? [SHARED] : [])]) {
      await page.evaluate(() => { delete globalThis.__h; });
      await page.addScriptTag({ type: 'module', content: helperModule(file, name, '/static/js/forkDom.js') });
      await page.waitForFunction(() => typeof globalThis.__h === 'function');
      const out = await page.evaluate((cs) => {
        const res = {};
        for (const [label, code] of Object.entries(cs)) {
          let clicks = 0;
          const kid = () => { const s = document.createElement('span'); s.textContent = 'k'; return s; };
          try {
            // eslint-disable-next-line no-new-func
            const node = new Function('el', '$', 'kid', 'bump', `let clicks = 0; const r = (${code});
              if (r && r.click && ${JSON.stringify(label)} === 'on_handler') { r.click(); r.click(); }
              bump(clicks); return r;`)(globalThis.__h, globalThis.__h, kid, (n) => { clicks = n; });
            res[label] = { node: window.__ser(node), clicks };
          } catch (e) {
            res[label] = { threw: e.constructor.name };
          }
        }
        return res;
      }, cases);
      ((results[file] ||= {})[name] = out);
    }
  }
  await page.close();
}

// ------------------------------------------------------------------ api in node
// Each case: what the fake fetch answers, and the call.
const API_CASES = {
  ok_json: { answer: [200, '{"x": 1}'] },
  ok_not_json: { answer: [200, 'plain text'] },
  no_content: { answer: [204, null] },
  unauthorized_json: { answer: [401, '{"detail": "nope"}'] },
  unauthorized_text: { answer: [401, 'no'] },
  bad_request_detail: { answer: [400, '{"detail": "Bad thing."}'] },
  not_found_error_key: { answer: [404, '{"error": "SESSION_NOT_FOUND", "message": "m"}'] },
  server_error_text: { answer: [500, 'Internal Server Error'] },
  bad_gateway_both: { answer: [502, '{"detail": "d", "error": "e"}'] },
  conflict_detail_object: { answer: [409, '{"detail": {"why": "busy"}}'] },
  network_error: { answer: 'reject' },
  post_body_no_headers: { answer: [200, '{}'], options: { method: 'POST', body: '{"a":1}' } },
  post_body_headers: { answer: [200, '{}'],
    options: { method: 'POST', body: '{"a":1}', headers: { 'Content-Type': 'application/json' } } },
  credentials_override: { answer: [200, '{}'], options: { credentials: 'omit' } },
};

async function runApi(results) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fork-dom-'));
  const domUrl = pathToFileURL(path.join(JS, 'forkDom.js')).href;
  try {
    const runs = FILES.api.map((file) => [file, 'api']);
    if (HAS_SHARED) runs.push([SHARED, 'api'], [SHARED, 'request']);
    for (const [file, name] of runs) {
      const mod = path.join(dir, `${file}.${name}.mjs`);
      fs.writeFileSync(mod, helperModule(file, name, domUrl));
      delete globalThis.__h;
      await import(pathToFileURL(mod).href);
      const api = globalThis.__h;
      const out = {};
      for (const [label, c] of Object.entries(API_CASES)) {
        const calls = [];
        globalThis.window = { location: { href: 'http://hub.test/app' } };
        globalThis.fetch = async (url, init) => {
          calls.push([url, JSON.parse(JSON.stringify(init || null))]);
          if (c.answer === 'reject') throw new TypeError('Failed to fetch');
          const [status, body] = c.answer;
          return new Response(body, { status });
        };
        const r = { };
        try {
          r.result = await api('/api/x', c.options);
        } catch (e) {
          r.error = { type: e.constructor.name, message: e.message };
          if ('status' in e) r.error.status = e.status;
          if ('data' in e) r.error.data = e.data;
        }
        r.href = globalThis.window.location.href;
        r.calls = calls;
        out[label] = r;
      }
      ((results[file] ||= {})[name] = out);
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

const results = {};
await runApi(results);
const { browser, skip } = await launch();
if (skip) {
  results.__skip = skip;
} else {
  try {
    await runDom(browser, results);
  } finally {
    await browser.close();
  }
}
console.log(JSON.stringify(results));
