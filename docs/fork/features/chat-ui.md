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

**An agent answer reads as one flow.** When the model writes some text, calls a tool and then answers, the chat shows that text, the tool step, and a new bubble below it. But the final answer the server sends and saves is the text of every round joined, and the page drew it into the last bubble, so the text before the tool call showed a second time under the tool step (for example the list of moon phases, then the search, then the same list again with today's phase). Now the last bubble leaves out what the earlier rounds already showed, live and after a reload. A final answer that doesn't start with the earlier rounds' text (a summary written from scratch, or a turn the server replaces whole) is shown as it is. The saved message is unchanged.

Files: `static/js/forkRoundFlow.js` (new); hooks in `static/js/chat.js` (import, round tracking, `agent_step`, `final_response`, `_replaceLiveTurnWithSavedAssistantMessage`) and `static/js/chatRenderer.js` (import, `addMessage`); test `tests/fork/test_fork_round_flow.py` (new)

Upstreamable: yes
