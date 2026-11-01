// Fork: an agent answer reads as one flow: text, tool step, the rest of the answer.
//
// The chat draws each agent round in its own bubble: the text before a tool call,
// the tool step, then a new bubble for what came after. Two things broke that flow:
//
// - Upstream's completion gate (src/agent_runtime/completion.py) holds every text
//   delta until the run ends, then releases them all, each tagged with its round.
//   By then the page is in the last round's bubble, so the first round's text
//   ("Let me check today's phase.") landed below the tool step. place() puts a
//   late delta back into its own round's bubble.
// - The final answer the server sends and saves is the text of all rounds joined,
//   and the page drew it into the last bubble, so earlier rounds showed twice.
//   withoutShownRounds() leaves out what the earlier rounds already showed. It
//   changes nothing when the final answer doesn't start with them (a summary
//   written from scratch).
//
// The saved message keeps each round's text too (fork/hub/round_flow.py), so a
// reload shows the same flow.
// Hooks: static/js/chat.js (stream setup, deltas, agent_step, teacher takeover,
// final_response, saved answer) and static/js/chatRenderer.js (history reload).

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

// One per streamed turn. `markdown` is static/js/markdown.js; `displayText` is
// chat.js's _streamDisplayText (hides tool markup and the like).
export function createRoundFlow({ markdown, displayText = (text) => text } = {}) {
  let rounds = []; // rounds[n - 1] = { holder, text } for each finished round n
  let current = 1;

  function render(entry) {
    const body = entry.holder.querySelector('.body');
    if (!body || !markdown) return;
    const shown = markdown.normalizeThinkingMarkup(displayText(entry.text));
    body.innerHTML = markdown.processWithThinking(markdown.squashOutsideCode(shown));
    entry.holder.style.display = shown.trim() ? '' : 'none';
  }

  return {
    // agent_step started round `nextRound`: the round before it is finished.
    endRound(holder, text, nextRound) {
      const next = Number(nextRound);
      const done = Number.isInteger(next) && next > 1 ? next - 1 : current;
      if (holder) rounds[done - 1] = { holder, text: String(text || '') };
      current = done + 1;
    },
    // A text delta of an earlier round: add it to that round's bubble. Returns
    // true when it was placed there (the caller then skips the current bubble).
    place(event) {
      const round = Number(event && event.round);
      if (!Number.isInteger(round) || round < 1 || round >= current) return false;
      const entry = rounds[round - 1];
      if (!entry || !entry.holder || !entry.holder.isConnected) return false;
      entry.text += String(event.delta || '');
      render(entry);
      return true;
    },
    // The text each finished round showed, in order.
    shown() {
      return Array.from(rounds, (entry) => (entry ? entry.text : ''));
    },
    // A teacher model takes over and starts its own rounds.
    reset() {
      rounds = [];
      current = 1;
    },
  };
}
