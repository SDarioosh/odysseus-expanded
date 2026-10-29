// Fork: the small DOM and fetch helpers the fork's pages and windows share (Records,
// Today, Wisdom, Write, Meetings, the phone pairing page and the Settings cards). Data
// only at import: nothing here touches the page until a helper is called, so node tests
// can import the scripts that use it. Pinned by tests/fork/test_fork_dom.py.

/**
 * A new element.
 * - attrs: `className` or `class` sets the class, `html` the inner HTML (only for our own
 *   static SVG strings), `text` the text, `on<event>` adds a listener; any other key is an
 *   attribute. A value of undefined, null or false leaves the key out; true sets an empty
 *   attribute (`hidden: true`).
 * - children: nodes, or text for anything else; undefined, null and false are left out.
 */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'className' || k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v; // only for our own static SVG strings
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : String(c));
  }
  return node;
}

/** The first element matching `sel` in `root` (the page by default), or null. */
export const $ = (sel, root = document) => root.querySelector(sel);

/**
 * fetch() for the hub's JSON routes, with the session cookie. Answers the parsed JSON
 * ({} when the answer isn't JSON). Throws on an HTTP error: the message is the answer's
 * `detail` or `error`, else `HTTP <status>`, with `status` and `data` (the parsed answer)
 * on the error. A 401 goes to the login page (and throws) unless `redirect401` is false;
 * a view inside the app that must not leave it passes false.
 */
export async function api(path, options = {}, { redirect401 = true } = {}) {
  const res = await fetch(path, { credentials: 'same-origin', ...options });
  if (res.status === 401 && redirect401) {
    window.location.href = '/login';
    throw new Error('not logged in');
  }
  let data = {};
  try {
    data = await res.json();
  } catch (e) { /* not JSON */ }
  if (!res.ok) {
    const err = new Error(data.detail || data.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

/**
 * fetch() that never throws on an HTTP error: answers { status, ok, body } (body null
 * when the answer isn't JSON). A request with a body and no headers is sent as JSON.
 * Only a network error throws. Used where the caller acts on the status (Write).
 */
export async function request(path, options = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
    ...options,
  });
  let body = null;
  try { body = await res.json(); } catch (_) { /* empty */ }
  return { status: res.status, ok: res.ok, body };
}
