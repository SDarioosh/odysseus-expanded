// Fork: shows a standalone fork page inside an Odysseus window (static/js/forkModuleWindows.js).
// Used by the Meetings window (static/js/forkMeetings.js) and the Beliefs window
// (static/js/forkBeliefsWindow.js). The page's markup is fetched and moved into the window,
// then the page's own scripts are loaded, so the page and the window can't drift apart.
// The scripts look their elements up by id when they load, so they load after the markup.
// Pages can't simply be framed: Odysseus sends `frame-ancestors 'none'` (core/middleware.py).

/**
 * Splits a selector list at its top-level commas (not those inside :is(...) and the like).
 */
function splitSelectors(text) {
  const out = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '(' || ch === '[') depth++;
    else if (ch === ')' || ch === ']') depth--;
    else if (ch === ',' && depth === 0) {
      out.push(text.slice(start, i).trim());
      start = i + 1;
    }
  }
  out.push(text.slice(start).trim());
  return out.filter(Boolean);
}

/** CSS text of `rules` with every selector put under `scope`; html and body rules dropped. */
function scopeRules(rules, scope) {
  const out = [];
  for (const rule of rules) {
    if (rule.selectorText !== undefined) { // a style rule
      const selectors = splitSelectors(rule.selectorText)
        .filter((s) => !/^(html|body)\b/.test(s))
        .map((s) => `${scope} ${s}`);
      if (selectors.length) out.push(`${selectors.join(', ')} { ${rule.style.cssText} }`);
    } else if (rule.media && rule.cssRules) { // @media
      const inner = scopeRules(rule.cssRules, scope);
      if (inner) out.push(`@media ${rule.media.mediaText} { ${inner} }`);
    } else {
      out.push(rule.cssText); // @font-face, @keyframes: global by nature
    }
  }
  return out.join('\n');
}

/** The page's inline <style> blocks, scoped to `scope`, as one CSS text. */
export function scopedPageStyles(doc, scope) {
  const out = [];
  for (const style of doc.querySelectorAll('head style')) {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(style.textContent || '');
    out.push(scopeRules(sheet.cssRules, scope));
  }
  return out.join('\n');
}

/**
 * Moves the page at `page` into `root`.
 * - className: added to `root` (and the scope of the page's styles when scopeStyles is set).
 * - pick(doc): the nodes to show, from the fetched document (scripts are dropped).
 * - scripts: module paths relative to this file, imported in order after the markup.
 * - scopeStyles: also add the page's inline styles, scoped under `.${className}`.
 */
export async function mountPageView(root, { page, className, pick, scripts = [],
                                            scopeStyles = false }) {
  const res = await fetch(page, { credentials: 'same-origin' });
  if (res.status === 401) {
    window.location.href = '/login';
    return null;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
  const nodes = pick(doc);
  if (!nodes || !nodes.length) throw new Error(`${page} has no content`);
  root.classList.add(...className.split(' '));
  const parts = [];
  if (scopeStyles) {
    const style = document.createElement('style');
    style.textContent = scopedPageStyles(doc, `.${className.split(' ')[0]}`);
    parts.push(style);
  }
  for (const node of nodes) {
    node.querySelectorAll?.('script').forEach((s) => s.remove());
    if (node.nodeName !== 'SCRIPT') parts.push(document.importNode(node, true));
  }
  root.replaceChildren(...parts);
  for (const path of scripts) await import(path);
  return root;
}
