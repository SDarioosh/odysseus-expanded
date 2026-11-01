# Chat and app UI

Small changes to the chat page: links that open a chat, an event or a note, the chat title, and resumed runs.

Each section below was a row of the "Changes from upstream" table in
[FORK.md](../../../FORK.md) until 2026-10-29, moved here word for word; FORK.md now lists
the features and the hooks into upstream files.

## Prefilled new chat from a link

**Prefilled new chat from a link.** `/?ask=<text>` opens a new chat with the text in the composer (one line, at most 2000 characters), focused, and removes `ask` from the URL. It never sends: a link can't start a turn or a tool call. Used by the Athena page's Ask in chat.

Files: `static/js/forkChatAsk.js` (new), hook in `static/index.html` (one script tag)

Upstreamable: yes, with discussion

## Open an event or note from a link

**Open an event or note from a link.** `/#event-<uid>` and `/#open=notes&note=<id>` open the calendar on that event or the note on a fresh page load (upstream opens them only when clicked inside a chat). After start-up, clicks a hidden link with the same hash so upstream's own handler opens it; only these two views. Used by the Athena page's "In calendar" / "In to-dos".

Files: `static/js/forkEntityHash.js` (new), hook in `static/index.html` (one script tag)

Upstreamable: yes

## Centered chat title in Nobody mode

**Centered chat title in Nobody mode.** The chat title at the top no longer shifts sideways when the Nobody (incognito) eye appears; the title area reserves the same space on both sides.

Files: `static/css/01-agent-chat.css` (one rule)

Upstreamable: yes

## A run that ended while the page was away shows its answer

**A run that ended while the page was away shows its answer.** Runs keep going on the server when the phone sleeps; when the page came back after the run had ended, re-attaching got 404 and the chat showed "Connection lost. The existing run could not be resumed." over a saved answer. Now, once `/api/chat/stream_status` confirms nothing is streaming, the chat reloads from the server, and says so when the run saved no answer. Offline or still streaming keeps upstream's message.

Files: `static/js/forkResumeFallback.js` (new); hook in `static/js/chat.js` (`_tryAutoRecover`); test `tests/fork/test_fork_resume_fallback.py` (new)

Upstreamable: yes

## An agent answer reads as one flow

**An agent answer reads as one flow.** When the model writes some text, calls a tool and then answers, the chat shows that text, the tool step, then the answer below it, and the same for two or more tool steps with text before each. Three things broke that flow (for example: the list of moon phases, the search, then the same list again with today's phase):

- The final answer the server sends and saves is the text of every round joined, and the page drew it into the last bubble, so earlier rounds showed a second time. The last bubble now leaves out what the earlier rounds already showed, live and after a reload (`withoutShownRounds`). A final answer that doesn't start with the earlier rounds' text (a summary written from scratch, or a turn the server replaces whole) is shown as it is.
- Upstream's completion gate (`src/agent_runtime/completion.py`) can hold every text delta until the run ends and release them all, each tagged with its round. The page then put the first round's text below the tool step. A held-back delta of an earlier round now goes back to its own bubble (`createRoundFlow`).
- The agent loop sometimes reports a round's text as empty (text ending in "Let me check ..." counts as a preamble) or puts the whole joined answer in the last round, while the page still shows each round's text. The chat route now saves the text each round showed in `round_texts` (`fork/hub/round_flow.py`), so a reload shows the same flow. The saved message itself (`content`) is unchanged.

Checked end to end in the sandbox (the app, a scripted model, a browser) for: text, one tool, answer; two and three tool steps with text before each; no text before the tool; a short "I'll search the web." line; two tools in one step.

Files: `static/js/forkRoundFlow.js` (new), `fork/hub/round_flow.py` (new); hooks in `static/js/chat.js` (import, stream setup, text deltas, `agent_step`, `teacher_takeover`, `final_response`, `_replaceLiveTurnWithSavedAssistantMessage`), `static/js/chatRenderer.js` (import, `addMessage`) and `routes/chat_routes.py` (`_AgentRenderState.consume`, `metadata`); tests `tests/fork/test_fork_round_flow.py`, `tests/fork/test_round_flow_saved.py` (new)

Upstreamable: yes
