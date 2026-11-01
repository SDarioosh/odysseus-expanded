// Fork: an agent answer reads as one flow: text, tool step, the rest of the answer.
//
// The chat draws each agent round in its own bubble: the text before a tool call,
// the tool step, then a new bubble for what came after. But the final answer the
// server sends (and saves) is the text of all rounds joined, and the page drew it
// into the last bubble, so the first round's text showed a second time below the
// tool step.
//
// withoutShownRounds() removes the earlier rounds' text from the start of the final
// answer, so the last bubble shows only the new part. It changes nothing when the
// final answer doesn't start with them (a summary written from scratch).
// Hooks: static/js/chat.js (final_response, saved answer) and
// static/js/chatRenderer.js (history reload).

const THINK_BLOCK = /<\s*(think|thinking|thought)\b[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi;

function visibleText(text) {
  return String(text || '').replace(THINK_BLOCK, '').trim();
}

// Index in `text` right after `prefix`, comparing with all whitespace ignored,
// or -1 when `text` doesn't start with `prefix`.
function endOfPrefix(text, prefix) {
  let i = 0;
  let j = 0;
  while (j < prefix.length) {
    while (j < prefix.length && /\s/.test(prefix[j])) j++;
    while (i < text.length && /\s/.test(text[i])) i++;
    if (j >= prefix.length) break;
    if (i >= text.length || text[i] !== prefix[j]) return -1;
    i++;
    j++;
  }
  return i;
}

export function withoutShownRounds(finalText, earlierRounds) {
  const original = String(finalText || '');
  let rest = original;
  let removed = false;
  for (const round of earlierRounds || []) {
    const shown = visibleText(round);
    if (!shown) continue;
    const end = endOfPrefix(rest, shown);
    if (end < 0) continue; // the server dropped this round's text; keep going
    rest = rest.slice(end);
    removed = true;
  }
  rest = rest.replace(/^\s+/, '');
  return removed && rest ? rest : original;
}
