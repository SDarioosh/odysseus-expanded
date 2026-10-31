# The brain: a small agent loop for Odysseus chat

Status: design only (2026-10-31). No code yet. Owner decisions so far are under
[Decisions](#decisions); open questions are at the end.

## Summary

Odysseus chat gets a new agent loop of the fork's own, `fork/brain/`, that answers every
chat turn unless the `FORK_BRAIN` setting is off. It is a general personal assistant:
everyday questions, web search, and the owner's own data (Athena, calendar, tasks, notes,
contacts, memory, beliefs). It is not for coding; the owner keeps Hermes Agent for that.

The loop is small on purpose. It sends the model a stable prompt and a small fixed tool set,
streams the answer as it is written, and lets the model chain tool calls. It reuses
Odysseus's tool runner, MCP connections, owner checks and approval prompts instead of
writing its own. Upstream's agent loop (`src/agent_loop.py`) stays in the tree, untouched
and unused for brain turns, so upstream merges there stay clean.

- **Fixes:** answers held back until the end, prompts that llama-server can't reuse from
  its cache, tools re-picked every turn, follow-up tool calls blocked, rounds re-run after
  an approval, follow-ups that lose earlier search results.
- **Keeps:** the whole workspace (chat UI, calendar, tasks, notes, Write, Records, Today,
  Wisdom, phone, Meetings), local models only, owner isolation, approvals for writes.
- **Upstream cost:** one hook in `routes/chat_routes.py`. Later, when the brain is the
  default, about 20 of the fork's patches to upstream's agent can be removed.

## Why

**A small loop answers best.** Athena's bakeoff on the owner's server (qwen3.5-35b, the
same Athena MCP server, 20 questions; `Athena/docs/frontend-decision.md`):

| Frontend | Pass | Avg s |
|---|---|---|
| Model + MCP directly (the ~40-line loop in `server/eval/bakeoff.py`, `_raw_session`) | 18/20 | 7 |
| Hermes Agent, tuned | 15/20 | 16 |
| This fork (2026-10-18) | 9/20 | 15 |
| Stock Odysseus | 3/20 | 44 |

The top row does nothing clever: a short system prompt, Athena's tools, the question, run
the tool calls, repeat. What Hermes does well is mostly staying out of the model's way, and
a small loop does that best.

**Why upstream's loop is slow and loses answers** (read from the code, 2026-10-31; line
numbers drift):

1. Agent-mode answers are buffered until the whole loop ends: `stream_agent_loop` is
   wrapped in `@with_completion_gate` (`src/agent_loop.py:21236`), and
   `src/agent_runtime/completion.py:300` keeps every text chunk "buffered until the gate".
   The owner sees nothing until the last round is done.
2. The start of the prompt changes every turn, so llama-server re-reads most of it:
   per-message memories, RAG and the skills index go *before* the history
   (`routes/chat_helpers.py:1339`, `messages = preface + ...`); "Auto" picks a new tool set
   per query and can change it between rounds (`src/agent_loop.py` ~22713, ~26348); every
   mid-turn system nudge is merged into the first system message (`src/llm_core.py`
   ~2980-2991).
3. The prompt is large: 92 built-in tool schemas (`src/tool_schemas.py`), ~35k tokens with
   every tool on (`docs/fork/sandbox.md`, "All Odysseus tools on").
4. Extra rounds: an approval re-runs the whole request (`docs/fork/sandbox.md`, "~1-2 min
   on CPU" per approval), and blocked completions trigger repair rounds.
5. Thinking is switched off only for model names it recognises (`_THINKING_MODEL_PATTERNS`,
   `src/llm_core.py` ~1737), so a model served under another alias may think at length.

Each of these is in upstream's core agent design, in the area upstream changes most. The
fork has been patching around them (FORK.md: trusted read-only MCP, web-intent routing,
store lookups leaving chat mode, follow-ups keeping module tools, several tools in a row,
and more). The brain replaces the loop for chat turns instead of patching it further.

**Why not Hermes itself.** Running Hermes beside Odysseus means two of everything (sessions,
memory, skills, config, keys, updates) and a process boundary to debug. Copying Hermes's
code means carrying a large codebase we never merge again. Hermes is MIT, so small pieces
can be borrowed where they help, with the notice kept.

## Goals and non-goals

**Goals**

- A good general assistant on the owner's local model: answers from its own knowledge when
  that's enough, searches the web when the answer is recent or changes, uses the owner's
  data when the question is personal, and combines them ("will it rain during my 3 pm
  meeting?").
- First visible words within a few seconds of prompt processing; no buffering.
- Athena bakeoff at least as good as the bakeoff's own small loop in the sandbox, and
  ≥ 15/20 on the server.
- Upstream merges no harder than today.

**Non-goals**

- Coding, shell, file editing, model serving and admin work. Those tools are not in the
  brain's tool set (the owner uses Hermes for development; admin stays in Odysseus's UI).
- Cloud models. The brain runs only on local or private-network endpoints and refuses
  anything else.
- Replacing upstream's agent for scheduled tasks and background jobs in the first phases.
- Any change to Athena or the MCP contract.

## Design

### Where it hooks in

One hook in `chat_stream` (`routes/chat_routes.py`), after the session, owner and message
are resolved and before upstream's intent classification
(`_classify_tool_intent`): when `FORK_BRAIN` is on and the turn is a normal chat
message, it returns `fork.brain.turn.stream_turn(...)` and upstream's chat and agent paths
are skipped for that turn. Marked `# Fork: brain (fork/brain/turn.py)` and listed in
FORK.md's hooks table. The exact line is pinned in the first code PR; it sits among the
turn-policy hooks already listed in [upstream-sync.md](upstream-sync.md#conflict-hot-spots).

With the brain on there is no chat/agent split: the loop decides per turn whether to use
tools. The chat window's agent toggle has no effect on brain turns. Approval replies,
slash skills and deep research go through the brain too (below). Everything else (the
session list, exports, titles, uploads) stays upstream's.

New files, all in a new fork area `fork/brain/` (added to FORK.md's list of areas):

| File | What it does |
|---|---|
| `fork/brain/turn.py` | `stream_turn`: builds the prompt, runs the loop, emits the chat window's events |
| `fork/brain/prompt.py` | The stable system prompt and the end-of-prompt context message |
| `fork/brain/toolset.py` | The fixed tool set (names, groups, read-only or write) |
| `fork/brain/tools.py` | Runs a tool call through upstream's tool runner with a request authority |
| `fork/brain/documents.py` | The documents group: `search_documents`, `create_document`'s line format and editor events (added 2026-10-31, [Documents](#documents-2026-10-31)) |
| `fork/brain/attachments.py` | Text, PDF and office attachments of a turn: owner check, extraction, caps (added 2026-10-31, [Attachments](#attachments-2026-10-31)) |
| `fork/brain/history.py` | Stores each turn's tool calls and results so follow-ups can see them |
| `fork/brain/query_guard.py` | Removes private details from outgoing web search queries |
| `fork/brain/settings.py` | Reads `FORK_BRAIN`, `FORK_BRAIN_MAX_ROUNDS`, `FORK_BRAIN_TOOL_GROUPS` (rows in `fork/common/settings.py`) |

### Prompt layout

Ordered so the start stays identical across turns of a chat, which lets llama-server reuse
its cached prompt:

1. **System message, stable:** who the assistant is; when to answer directly and when to
   search; how to cite (transcript links for Athena facts, page links for web facts); the
   web query privacy rule; the owner's preset text if any. No date, no memories, nothing
   that changes per message.
2. **Tools:** the fixed tool set, in a fixed order.
3. **History:** earlier user and assistant messages, *with* their tool calls and (trimmed)
   tool results from `fork/brain/history.py`.
4. **Context message, volatile (a user-role message right before the new question):**
   current date and time, the owner's timezone, up to 5 relevant memories and beliefs.
   Marked as context, not instructions, like upstream's untrusted-context messages
   (`src/prompt_security.py`) Then the notes the owner selected and the email they have
   open, if any, in upstream's untrusted wrapper
   ([Selected notes and open email](#selected-notes-and-open-email-2026-10-31)).
5. **The new message.**

Within a turn, each round only appends (assistant tool call, tool result), so every round
reuses the cache of the one before. The loop never inserts system messages mid-turn.

### Tool set

Fixed per chat, decided by `fork/brain/toolset.py`, not re-picked per message. Target:
about 20 tools and ≤ 6k tokens of schemas (measured in the first PR).

| Group | Tools | Kind |
|---|---|---|
| Web | `web_search`, `web_fetch`, `get_weather` | read |
| Athena | `search_conversations`, `search_transcripts`, `get_conversation`, `get_transcript`, `list_action_items`, `list_facts`, `get_daily_digest`, `list_people` | read |
| Athena | `update_action_item` | write |
| Workspace | `manage_calendar`, `manage_tasks`, `manage_notes`, `resolve_contact`, `manage_contact`, `manage_memory` | read actions run directly, write actions ask |
| Documents | `manage_documents` (stored editor documents), `create_document`, `search_documents` (new fork tool over the personal library) | list, read and search run directly; create, delete and tidy ask |
| Beliefs | `search_beliefs` (new fork tool over `fork/beliefs/search.py`) | read |
| Chats | `search_chats` | read |
| Email | `list_email_accounts`, `list_emails`, `search_emails`, `read_email` | read |
| Email | `draft_email`, `draft_email_reply`, `send_email`, `reply_to_email` | write, ask |

Deep research (`trigger_research`) is not in the set: it runs only through upstream's
`/research` command (`static/js/slashCommands.js`; owner's decision), which the brain
hands to upstream's research flow unchanged. Optional group, off until the
eval says it helps: images (`generate_image`).

Never in the set: `bash`, `host_shell`, `python`, file tools, model serving, endpoint,
token, MCP, settings and webhook management, `api_call`, `app_api`.

**Slash skills** (`/athena`, `/context`) add their declared tools to the set for that turn,
using the fork's existing `requires_toolsets` handling (`fork/routing/skill_invocation.py`).

### The loop

`fork/brain/turn.py`, per turn:

1. Build the prompt (above). Check the endpoint is local or on a private network
   (upstream's `is_local_endpoint` in `src/model_context.py` and the fork's
   `is_private_host` in `fork/hub/private_endpoint_thinking.py`); if not, answer "The
   assistant runs only on local models" and stop.
2. Stream from the model with upstream's `stream_llm` (`src/llm_core.py` ~2936), so
   endpoint headers, auth and slot affinity (`cache_prompt`, `session_id`) stay as they
   are. Send `enable_thinking: false` explicitly for local endpoints whatever the model's
   name (to confirm in code that `stream_llm` passes it through; else a small fork wrapper).
3. Text chunks go to the chat window **as they arrive**. No completion gate.
4. When the model returns tool calls (native `tool_calls`, or the text formats Qwen
   sometimes writes, parsed with upstream's `src/tool_parsing.py`), run them (below),
   append the results, and go to step 2.
5. Stop when the model answers without a tool call, or after `FORK_BRAIN_MAX_ROUNDS` (default
   8): then one last round without tools asks for the best answer from what it has.
6. Save the turn: the answer as a normal Odysseus message (so titles, exports and search
   keep working), plus tool calls and trimmed results in `history.py`.

Events sent to the chat window are the ones it already renders (`static/js/chat.js`):
text, `tool_start`, `tool_output`, `ask_user` (approvals), `final_response`, `metrics`.
The exact shapes are taken from upstream's agent stream in the first code PR, so no
frontend change is needed.

### Running tools safely

`fork/brain/tools.py` runs every call through upstream's `execute_tool_block`
(`src/tool_execution.py:1670`) with a request authority from `create_request_authority`
(`src/agent_runtime/authority.py:374`) and a `ToolRunSecurityContext`, built the way
`chat_stream` builds them today. So owner scoping, admin-only tools, path denial and the
evidence ledger keep working; the brain adds no security logic of its own.

- **Reads run without asking:** Athena's `readOnlyHint` tools (the fork's trusted read-only
  MCP rule), web search, and the read actions of the workspace tools. `web_fetch` runs
  unasked only for a URL the owner typed or a search hit of the chat (see
  [After the security review](#after-the-security-review-2026-10-31)).
- **Writes ask:** creating or changing an event, task, note, contact or memory, and
  `update_action_item`, show upstream's approval prompt. The loop **pauses** with its state
  kept in memory under the approval id (timeout 10 minutes) and **resumes** when the owner
  answers, without re-running the model round. A timeout or a restart ends the turn with
  "The change wasn't approved in time."
- Tool results are trimmed before they enter the prompt (web pages to ~6k characters, long
  lists to their first rows) and fully kept in the chat window.

### Follow-ups and context size

- `fork/brain/history.py` keeps, per session and message, the tool calls and trimmed
  results in a fork SQLite file (`data/brain.db`, WAL, the `fork/beliefs/ledger_db.py`
  pattern). Upstream's session format is not changed. Deleting a chat deletes its rows.
- Before each round the loop estimates the prompt size against the model's context
  (`src/agent_runtime/context_resolution.py`). Over 75 %: the oldest tool results are
  dropped first, then older turns are summarised once with the local model and the summary
  replaces them.

### Memory and beliefs

The context message carries up to 5 short items: upstream memories and confirmed beliefs
ranked for the question (`search_beliefs`). For anything more the model calls
`search_beliefs` or `manage_memory`. This also replaces the reason the
[SQLite memory plan](memory-sqlite.md) existed: Athena's facts stay in `beliefs.db`
(already SQLite with vectors) and reach chat through `search_beliefs`, so `memory.json`
doesn't need to hold tens of thousands of mirrored beliefs. Stopping that mirror is a
separate, later change.

### Web search

- **Providers:** SearXNG first, DuckDuckGo as the fallback (owner's decision). These are
  already upstream's defaults: `search_provider: "searxng"` and
  `search_fallback_chain: ["duckduckgo"]` (`src/settings.py:72-76`); SearXNG runs in the
  fork's compose stack ([deploy.md](deploy.md)). The brain uses upstream's `web_search` and
  `web_fetch` unchanged.
- **When to search:** the system prompt says to answer stable knowledge directly, and to
  search for anything recent or changing (news, prices, opening hours, releases, sports,
  weather beyond `get_weather`) or when unsure. Today's date is in every turn.
- **Sources:** web answers link the pages they used.
- **Query privacy.** Web queries leave the network (SearXNG forwards them to search
  engines; the DuckDuckGo fallback is a direct request). The model may have just read
  private data, so:
  - the system prompt says queries never contain names, notes, calendar details or other
    personal information;
  - `fork/brain/query_guard.py` checks every `web_search` query before it runs and removes
    the owner's contact names, email addresses, phone numbers and street addresses, and
    the titles of the owner's upcoming events. It logs how many terms it removed, never
    which. If a query is empty afterwards, the search isn't run and the model is told why.
  - Tested with fictional contacts and events (`tests/fork/test_brain_query_guard.py`).

### Settings

Fork settings are environment variables in the table of `fork/common/settings.py`
(generated into [settings.md](settings.md)), so the switch is global by nature.

| Variable | Unset means | Meaning |
|---|---|---|
| `FORK_BRAIN` | `on` | The brain answers every chat turn of every chat (one global switch, no per-chat choice); `off` gives the turns back to upstream's agent |
| `FORK_BRAIN_MAX_ROUNDS` | `8` | Tool rounds per turn |
| `FORK_BRAIN_TOOL_GROUPS` | `web,athena,workspace,documents,beliefs,chats,email` | Groups in the tool set |

The brain is the default (owner's decision, 2026-10-31): an instance answers with it unless
its environment sets `FORK_BRAIN=off`, which gives every turn back to upstream's agent
unchanged. The test suite runs with `FORK_BRAIN=off` by default (the root `conftest.py`),
because upstream's tests and the fork's characterization tests check upstream's chat path;
the brain's own tests switch it on.

### Background jobs

Upstream's chat titles and memory extraction still run after each turn and use the same
llama-server. They can delay the next answer. The first PRs measure this; if it matters,
the brain asks them to wait while a turn is running (a fork hook in
`routes/chat_helpers.py`, only if needed).

## As built (phases 2 and 3, 2026-10-31)

Where the code differs from the design above:

- **Hook position.** Upstream classifies the turn's intent before it loads the session, so
  the hook sits later than planned: in `chat_stream` after the session is loaded and
  owner-checked, after `_enforce_chat_privileges` and `resolve_session_auth`, after
  upstream consumes an approval reply and after the research decision. Everything before
  it is classification and upstream's own checks, which the brain reuses.
- **Turns left to upstream:** research, compare, plan, incognito, image chats, workspace
  or path turns, native and TUI clients, an open document, and approval replies that
  aren't the brain's; attachments until [Attachments](#attachments-2026-10-31), since then
  only images, audio and other files the brain doesn't read (selected notes and an open
  email are the brain's since
  [Selected notes and open email](#selected-notes-and-open-email-2026-10-31)).
- **Approvals.** Upstream consumes the reply before the hook, so "Deny" shows upstream's
  resolved card and the brain drops its paused turn; after a restart upstream answers
  "approval expired" (409). Every write asks on its own; "allow for this chat" covers
  reads only. In Ask mode upstream's gate still asks before a private read that follows
  web content (calendar after `web_search`); Auto mode lets it through. The brain keeps
  that gate.
- **`search_beliefs`** runs in fork code (`fork/beliefs/search.py`, owner-scoped), not
  through `execute_tool_block`, which can't dispatch a tool it doesn't know. Every other
  call goes through `execute_tool_block`.
- **Thinking off** is a second marked hook in `src/llm_core.py`
  (`_apply_local_qwen_thinking_mode` → `apply_brain_thinking`), active only on the brain's
  own requests.
- **Deleted chats.** No hook: at most once an hour a brain turn drops `brain.db` rows of
  chats that are gone; rows of deleted messages go when the chat is next read.
- **The chat's web button has no effect** on brain turns; web search is in the tool set
  unless `web` is removed from `FORK_BRAIN_TOOL_GROUPS`.
- **`manage_tasks` is not in the set:** it is upstream's scheduled-tasks tool; the owner's
  to-dos and checklists are notes (`manage_notes`). Built-in schemas: 18 tools, ~5.4k
  tokens, plus Athena's.
- **First token after 60 s.** `stream_llm` retries a silent local stream once (before
  anything reaches the brain, so nothing runs twice); a second timeout ends the turn. On a
  slow CPU set `ODYSSEUS_FIRST_TOKEN_TIMEOUT=0`.

### After the security review (2026-10-31)

An adversarial review found two high and several medium issues; all are fixed, each with a
test that failed before the fix:

- **Outside content carries across turns.** Each exchange in `brain.db` records whether a
  tool result was untrusted (upstream's `tool_result_should_arm_gate`; `search_beliefs`
  counts as untrusted). Replayed results of such exchanges are marked untrusted, and the
  run is armed when any replayed exchange or the route's `external_untrusted_context_seen`
  is. So an injected web page or email can't steer the next turn.
- **`web_fetch` never runs unasked for a URL the model made up.** It runs without asking
  only for a URL the owner typed in this message or a search hit of this chat, exactly;
  any other URL asks, also after "allow for this task" (since the bug hunt below, also
  another page on a hit's site).
- **Approved writes run.** Approvals are filed armed (upstream's runner refuses an exact
  approval on an unarmed run); a test runs a real approved note through
  `execute_tool_block`.
- **Local only, strictly.** An endpoint must be on a local or private host, and an endpoint
  the admin set to `api` or `proxy` is refused. A proxy that is only upstream's guess
  (API key and `/v1` on a private host, the owner's own llama-server) still counts as
  local.
- **After-turn jobs stay local.** Memory, persona and skill extraction are skipped when
  their endpoint isn't local; titles then come from the message instead of a model; a
  refused turn runs no after-turn job.
- **Privileges and consent.** Users without `can_use_agent` go to upstream. Memories and
  beliefs follow `no_memory`, the owner's memory setting and the chat's memory-injection
  switch; `search_beliefs` is offered only where `manage_memory` would be.
- **Query guard can't be bypassed.** `web_search` calls are rewritten to `query` (and a
  valid `time_filter`); `get_weather` locations go through the guard too.
- **Retention.** `brain.db` rows older than 30 days are deleted; only the last 4 exchanges
  are replayed; a sweep runs on the first request after a start, then hourly, in a worker
  thread, even with the brain off. The hub's Forget (`fork/beliefs/forget.py`) also drops
  every stored exchange and summary of that owner containing the fact's words or the
  line it was said on (`history.forget_texts`). Not covered: Athena's own transcript
  retention; such a line stays in a stored exchange until the row expires (≤ 30 days).
- **Robustness.** A timed-out approval leaves no tool call without a result; a failure
  while setting up the turn ends it with an error answer; the saved answer is all the text
  that was shown, and tool-call markup never reaches the window; SQLite runs off the event
  loop.
- **More turns left to upstream:** an open document, selected notes, an open email, and
  `use_rag=true` (since [Documents](#documents-2026-10-31) the brain takes `use_rag=true`
  turns again, and since
  [Selected notes and open email](#selected-notes-and-open-email-2026-10-31) selected
  notes and an open email).
- **One worker.** Paused turns live in memory; the fork runs one uvicorn worker (the
  Dockerfile and compose set none), so that's safe.

The `chat_stream` hook now also passes `external_untrusted_context_seen` and `no_memory`
and widens `upstream_turn`; `fork/upstream.py` gains `configured_endpoint_kind` and
`normalize_endpoint_base`.

### After the bug hunt (2026-10-31)

A second adversarial pass (`tests/fork/test_brain_hardening.py`) found and fixed:

- **Made-up pages on a search hit's site.** `web_fetch` ran unasked for any short path on
  a hit's site (`fork/routing/web_turn_scope.py`'s rule), so an injected page could have
  the model fetch `https://its-site/<private data>`. The brain now fetches unasked only
  the exact hit (`www.` and a trailing `/` aside); upstream's agent keeps its rule.
- **Query guard gaps.** Names, titles and addresses now match without accents ("Jose
  Garcia" for "José García", "Mueller" for "Müller") and with joined words (`Wren_Hale`,
  `WrenHale`); an event title also goes when all its words of three letters or more are
  in the query, reworded; the contacts' own phone numbers go however written (same last
  seven digits), and their street addresses in any case and either order; two-letter
  names ("Mo") go unless a common word or abbreviation; "Elm Street 12" is an address.
  Spelled-out emails ("jane at example dot com") are not caught.
- **Host names of the owner's own servers.** `is_local` accepted only IP addresses,
  `localhost` and `host.docker.internal`, so a model server at a Docker service name, a
  `.local`, `.lan` or `.internal` name, a Tailscale `*.ts.net` name or a private IPv6
  address answered every turn with "runs only on local models". Those count as local now
  (`turn.own_host`); public hosts and endpoints set to `api` or `proxy` are still refused.
- **The final round.** Tool calls a server returns in the round without tools are
  dropped, so the round limit holds.
- **Broken tool calls.** A tool call the model writes as text that can't be read (bad
  JSON, a tool not offered) is never shown; the model gets one more round with a note
  (`BROKEN_CALL_NOTE`, counted against `FORK_BRAIN_MAX_ROUNDS`).
- **Forget** matches the decoded text of stored exchanges, so a fact split by a line
  break is found.
- **Demoted admins** lose the MCP tools of their chat's kept tool set at the next turn.

Left as it is: memories and beliefs recalled into the context message don't arm the
approval gate (a `search_beliefs` call does). Arming there would ask before private reads
on most personal questions in Ask mode, and every write and unknown fetch asks anyway.

### Documents (2026-10-31)

A `documents` group, in the default `FORK_BRAIN_TOOL_GROUPS` (`fork/brain/documents.py`;
tests `tests/fork/test_brain_documents.py`):

- **`manage_documents`** (upstream's): `list` and `read` of the owner's stored editor
  documents run without asking (upstream's `_PRIVATE_ACTION_READS`); `delete` and `tidy`
  ask. A `delete` without a `document_id` is refused before it asks: upstream's tool
  would delete the most recent document, which the card can't name.
- **`create_document`** (upstream's): asks, and upstream's approval card works for it
  (tested through the real `execute_tool_block`: the approved document is the chat
  owner's). The model's JSON arguments run in upstream's line format (title, language,
  content), as upstream's agent passes native calls on, so the card shows what is
  created; before this the title was the raw JSON and the body empty. After it ran the
  window gets upstream's events in upstream's order (`doc_stream_open`,
  `doc_stream_delta`, `doc_update`, then `tool_output` with the `document_*` fields),
  so the document opens in the editor; the model reads a line naming it and its id
  (upstream's result has no text).
- **`search_documents`** (the fork's, like `search_beliefs`): upstream's
  `rag_manager.search(query, k, owner=owner)` (the app's `personal_docs_manager`, where
  upstream's chat RAG finds it), hits of this owner only (checked again after the store's
  own filter), at or above upstream's `RAG_SIMILARITY_THRESHOLD` (0.35), 5 by default,
  at most 8, each passage cut to 1,200 characters. A result with hits is outside content:
  it arms the run and its exchange is replayed untrusted. Offered only when a
  `rag_manager` exists, the chat's documents toggle isn't off (`use_rag=false`), and
  upstream would let this user and credential use `manage_documents`: not for users
  without `can_use_documents`, non-admin users, API tokens or when the admin disabled
  it. That is stricter than upstream's chat RAG, which injects passages for any user;
  before this group the brain gave no one library passages, so nobody loses any.
- **`use_rag`.** The hook no longer hands `use_rag=true` turns to upstream: the brain
  has the library as a tool, and the web UI never sends `true` anyway (it sends `false`
  when the toggle is off, nothing otherwise), so only API clients sent it. The toggle now
  reaches the brain: off, `search_documents` is left out of that turn. The hook passes
  `use_rag=use_rag` (same line, no new hook).
- **Prompt.** When to use documents (stored documents and the library) rather than the
  conversation tools or notes; name the document's title or filename next to each fact;
  long writing goes into `create_document`, with one sentence in the chat.
- **Schema budget.** Built-in schemas: 18 tools, ~5.4k tokens before; 21 tools, ~6.0k
  after (+~606: `manage_documents` ~223, `create_document` ~246, mostly upstream's
  language list, `search_documents` ~134). The two upstream tools keep upstream's
  parameters with shorter descriptions.

### Invented actions (2026-10-31)

Upstream's agent holds all answer text back until the end (`with_completion_gate`,
`src/agent_runtime/completion.py`) so it can drop "I've created/sent ..." claims no tool
call backs. The brain keeps streaming and checks afterwards (`fork/brain/claims.py`; tests
`tests/fork/test_brain_claims.py`):

- **When.** Once per answer, on its final text, in `_finish` (so also for a turn resumed
  after an approval). The turn records every write call and whether it succeeded
  (`_Turn.writes`: ran, exit code 0 or none, no error; read or write by `toolset.is_read`);
  the list is part of the paused turn, so writes before a pause still count after it.
- **What.** First-person claims of a change in this turn ("I've added ...", "I just sent
  ...", "..., and saved ..."), pronoun passives ("It's been added to your calendar",
  "It's now on your calendar"), store nouns in the perfect passive ("The reminder has been
  saved"), statements opening with the verb ("Added to your calendar: ..."), and a bare
  "Done."/"All set." that opens the answer (a closing one only after a write that failed).
  Statements come from upstream's `_unquoted_statements` (quotes and code blanked), and
  upstream's `_BARE_SUCCESS` and `_NON_REPORT_SCOPE` are reused through `fork/upstream.py`;
  upstream's `_EXECUTION_CLAIM` is not (its "and sent", "was sent" and "wrote" forms
  catch other people's actions and chat drafts).
- **Not claims.** Questions and offers, future tense, conditional, modal or attributed
  prose, negations, other turns ("earlier", "yesterday", "already"), other people ("you
  created", "Maria sent"), reads ("I found"), text shown in the chat ("below", "Here's a
  draft:"), and edits of the chat's own text (an object that names no store and isn't
  "it"/"that": "I've added a few details to the paragraph").
- **Stores.** A claim names calendar, notes (to-dos, reminders, lists), email, contacts,
  memory, documents or Athena's action items when one is its object or destination (not
  after of/about/for/with: "a summary of the meeting"); it is backed by a successful write
  of that store's tools (`manage_calendar`; `manage_notes`; the email write tools;
  `manage_contact`; `manage_memory`; `create_document`/`manage_documents`; an
  `mcp__…__update_action_item`). A claim that names no store needs any successful write.
- **The note.** One `{"delta"}` after the last answer text and before `metrics`, saved with
  the answer and its last round text, so follow-ups and a reload see it: "Note: nothing was
  changed in this turn: no calendar event was added or changed." (or "Note: no email was
  sent or drafted in this turn." when another write did succeed). An empty answer's
  "Done." carries it in its `final_response`. A timed-out approval saves only
  `NOT_APPROVED`, without a note; a denied one is dropped quietly as before.
- **Cost.** No model call, no buffering, nothing before the first text. Regexes are
  compiled at import and run on lowercased text; only lines (or, in a line over 1,500
  characters, sentences) with a claim word go through upstream's splitter, at most 80
  claim words (the first and the last 40). On a 20,000-character answer: ~0.7 ms without
  claim words, ~1 ms for paragraphs with quoted claim words and a real claim at the end,
  ~2 ms for 20,000 characters of nothing but claims (best of 30, sandbox CPU shared with
  other work); the test asserts under 5 ms.

### Selected notes and open email (2026-10-31)

Turns with notes selected (`selected_note_ids`) or an email open in the reader
(`active_email_uid`, `active_email_folder`, `active_email_account`) went to upstream's
agent, minutes per turn on the owner's CPU. They are the brain's now
(`fork/brain/selection.py`; tests `tests/fork/test_brain_context.py`). An open document
stays upstream's.

- **Hook.** Same line: `upstream_turn` no longer names `selected_note_ids` and
  `active_email_uid`; the hook also passes `selected_notes=selected_notes_context` and
  `active_email=active_email_ctx`, both built by upstream before the hook.
- **Notes.** The text `chat_stream` already loaded and formatted
  (`_format_selected_notes_context`, `Note.owner == owner`): not read again. Another
  owner's note ids give nothing. Cut to 8,000 characters with a note saying how much was
  left out.
- **Email.** Upstream passes its agent only the uid, folder and account (the cache lookup
  in `chat_stream` that would add subject and a preview imports names
  `routes/email_routes.py` doesn't export, so it never adds anything); its model then
  calls `read_email`. The brain makes that call once while setting the turn up (in
  `_prepare`, so a failure answers instead of a 500), through upstream's runner with the
  turn's authority, tool policy and disabled tools; no tool card. Upstream's rules decide:
  `read_email` is admin-only (`NON_ADMIN_BLOCKED_TOOLS`) and never for an API token, so
  for those turns (and when the admin disabled it or `email` is out of
  `FORK_BRAIN_TOOL_GROUPS`) the email is left out and the context says only that one is
  open. Chosen over handing the turn back: upstream's agent couldn't read it either.
  The call is the owner's (they opened the email), so it runs before this context arms
  the run, with a security context of its own. Cut to 6,000 characters; a failed or slow
  read (20 s) leaves the uid, folder and account and a line that it couldn't be loaded.
- **Prompt.** Both go into the volatile context message (position 4), after the recalled
  memories, each in upstream's untrusted wrapper with upstream's labels ("Selected notes
  and todos", "active email reader"). The system message, the tools and the history are
  byte-identical with and without them (tested), so the prompt cache holds. The system
  prompt gained one sentence: with notes or an email in the context, "this", "summarise
  it" or "reply to it" mean those, and changes still ask.
- **Armed.** The context message then carries upstream's untrusted marking:
  `observe_messages` arms the run (a write asks; a private read is decided by upstream's
  gate, Ask or Auto), URLs in the notes or the email count as outside content and never
  as ones the owner typed, and the turn's exchange is kept tainted, so it replays
  untrusted.
- **Follow-ups.** The context is for one turn, like upstream's; a second question with the
  notes still selected or the email still open sends them again and gets them again.
- **Cost.** A turn without notes or an email: none (two list checks). With notes: no I/O
  of the brain's (the route loaded them). With an email: one `read_email` call before the
  first round (an IMAP fetch through the email MCP server), and no extra model call or
  round; upstream's agent spends a whole model round on that same call.
- **Not mirrored.** Upstream's agent with an email open also takes `send_email`,
  `reply_to_email` and `create_document` out of the turn. The brain keeps its fixed tool
  set (changing it per turn would break the prompt cache); those are writes and ask.

### Attachments (2026-10-31)

Turns with attachments used to go to upstream's agent, minutes per turn on the owner's
CPU, and phones attach files often. Now the brain takes a turn whose attachments are all
files upstream reads as text (`fork/brain/attachments.py`; tests
`tests/fork/test_brain_attachments.py`):

- **Which turns.** Every id must pass upstream's owner check
  (`UploadHandler.resolve_upload(id, owner=<the chat's owner>)`, as
  `ChatHandler.preprocess_message` calls it), exist inside the upload directory and be a
  document by upstream's `is_document_file`, not an image or audio file (upstream's
  `build_user_content` order). Its extracted text must be readable: upstream's own
  wrappers (`=== File:`, `[PDF content]:`, `[Document content — …]:`, `[Legacy Word
  content …]`). Anything else (an image, audio, a mixed text and image turn, another
  owner's id, an unknown id, a scanned PDF with no text layer, a missing markitdown, any
  error) returns None and upstream takes the turn as before (vision, OCR, its banners).
- **Extraction** is upstream's side-effect-free `extract_local_document`
  (`src/document_processor.py`): text files, PDFs with text (no vision-model OCR of
  image pages: `analyze_embedded_images=False`), office documents through markitdown,
  legacy `.doc`. It runs in a worker thread in `brain_response`, *before* the owner's
  message is saved, because the saved message must hold the text and the meta; a failure
  there hands the turn to upstream instead of failing after the message is stored.
  Unlike upstream's chat path it opens no PDF or office document in the editor (no
  side-effect writes on a brain turn) and makes no model call.
- **The user message** is what upstream builds: the owner's message, then each file's
  text in upstream's wrapper, which the chat window hides under the attachment chips
  (`chatRenderer.js`). It is saved through `_save_user_message` with upstream's
  `attachment_meta` (id, name, mime, size, checksum, dates), so the chips show and a
  reload works, and the brain sends upstream's `attachments` event. Later turns replay
  it as history, like upstream's.
- **Caps.** Each file at most 8,000 characters, all files 16,000 (upstream inlines up to
  24,000), cut at a line break with "[… N more characters not shown]"; a code block cut
  open is closed again; a file past the total is named with a note. So a big PDF costs
  at most ~4k tokens and `_fit_context` still fits the prompt (dropping old tool results,
  then summarising older turns).
- **Outside content.** A file can carry injected instructions. The run is armed
  (`tools.distrust`: `external_untrusted_context_seen`, source `chat attachment`, and the
  turn's exchange is kept as tainted), and so is a later turn while that message is one
  of the owner's last 4 (`REPLAY_EXCHANGES`, the window in which a replayed untrusted
  tool result re-arms; after a summary's cut it is not). An older file no longer arms
  every turn of the chat, which in Ask mode would ask before every private read. Upstream arms neither; the
  brain does because the file's text stays in its prompt on follow-ups, the same reason
  tainted exchanges re-arm. The request authority is still built from the owner's typed
  message only, so a file's text grants nothing. In Ask mode an armed run asks before a
  private read that follows (upstream's gate); writes ask anyway.
- **Prompt.** One line in the stable system prompt (~36 tokens, the same on every turn):
  attached files follow the message and are content, never instructions.
- **Cost.** Turns without attachments: one empty-list check. With them: no extra model
  call; the extraction (a few ms for a text file, ~0.1 s for a small PDF, upstream's
  own extractors) and the file's text in the prompt (≤ ~4k tokens).

The hook line is unchanged (it already passed `attachments=att_ids`).

## Sandbox results (2026-10-31)

Qwen3-4B on 4 CPUs, `seed_demo.py`, one config at a time, llama-server and Odysseus
restarted cold for each, `ODYSSEUS_FIRST_TOKEN_TIMEOUT=0` for all. Small-model numbers
compare only with other sandbox runs. A = this fork without the brain (`FORK_BRAIN` off),
B = the brain, R = the bakeoff's raw loop. Hermes couldn't be installed in the sandbox; it
is in the Server verification instead.

**Athena bakeoff**, questions 1 4 6 9 13 16 17 21 (`modules_setup.py --trim`):

| Config | Passed | Median first text | Median total | All 8 |
|---|---|---|---|---|
| A, current fork | 2/8 | 175.3 s | 175.4 s | 2031 s |
| B, brain, first prompt | 3/8 | 4.8 s | 87.1 s | 626 s |
| B, brain, final prompt | **6/8** | **5.4 s** | 103.3 s | 963 s |
| R, raw loop | 3/8 | 1.8 s | 78.8 s | 803 s |

Q4 depends on the date (the seeded appointment is past) and Q9 isn't comparable with R
(it fixes "today" at 2026-10-18).

**General eval** (`scripts/brain_eval.py`, 23 questions, `--untrim`):

| Kind | A | B, first prompt | B, final prompt |
|---|---|---|---|
| knowledge (no search) | 5/5 | 5/5 | 5/5 |
| web | 0/5 | 1/5 | 2/5 |
| mixed | 0/5 | 0/5 | 0/5 |
| follow-up | 1/5 | 0/5 | 2/5 |
| privacy | 1/3 | 0/3 | 1/3 |
| **all** | **7/23** | **6/23** | **10/23** |
| median first text | 119.8 s | 8.8 s | 9.2 s |
| wall time | 5053 s | 1931 s | 2570 s |

No seeded private term reached a search query in any run (fake SearXNG query logs).

**Prompt cache** (llama-server log, bakeoff): A sent prompts of median 6,697 tokens and
evaluated 33.7k tokens in all (1,112 s of prompt processing); the brain sent median 4,088
and evaluated 8.8k (236 s); both reuse ~95 % per request.

**What the 4B model still gets wrong:** it often answers "I don't have that information"
without calling a tool, and once invented a voice note. The final prompt (search the
owner's tools and the web before saying there's nothing; personal details only from tool
results; recalled notes are hints) took the bakeoff from 3/8 to 6/8. Mixed questions
(calendar + web) are still 0/5: the model doesn't chain the two. The real model on the
server is expected to do better on both.

**Regression:** the full suite on the branch fails the same 72 tests as clean `barvis`,
none new.

**Documents** (2026-10-31, `scripts/brain_eval.py --kinds documents`, same model and
machine, ChromaDB 1.5.9 and upstream's fastembed embeddings, Odysseus restarted cold per
config; A = `FORK_BRAIN=off`, B = the brain with the documents group):

| Question | A | B |
|---|---|---|
| d1 find and read a stored document | fail (read it, wrong answer), 610 s | pass, 351 s |
| d2 search the library | pass (upstream's RAG injection), 47 s | pass (`search_documents`), 76 s |
| d3 search the library | fail (no tool), 149 s | fail (no tool), 40 s |
| d4 create a document (approval auto-answered) | pass, 195 s | pass, opened in the editor, 193 s |
| d5 document + web search, privacy | pass, 879 s | pass, 135 s |
| **pass** | **3/5** | **4/5** |
| median first text | 194.6 s | 58.0 s |
| median total | 195.2 s | 134.8 s |
| wall time | 1880 s | 794 s |

No private term of the seeded document reached a search query in either run. d3 fails
the 4B model's known way: "I don't have access to your manual" without calling a tool.
Naming documents in the prompt's list of the owner's data (one more run) didn't fix d3
and scored 3/5 (d2 then picked `manage_documents` instead of the library), so that line
was not kept. The first text of d1 and d4 is late because the first prompt after a
start (system prompt and ~6k tokens of schemas) is read from scratch at ~35 tokens/s.

Regression, the general eval's knowledge and privacy kinds with the brain on, clean
`barvis` against this branch (same day, restarted cold): 5/8 both (knowledge 5/5 without
a search, median first text 8.7 s and 5.0 s; privacy 0/3 both, the 4B model doesn't call
`web_search` there, as before). No private term reached a query in either.

## Upstream impact

- **New upstream hooks:** two: 5 lines in `chat_stream` (already a listed hot spot) and 3
  lines in `src/llm_core.py` `_apply_local_qwen_thinking_mode`. Possibly a third in
  `routes/chat_helpers.py` for background jobs (above).
- **No changes** to `src/agent_loop.py`, `src/agent_runtime/`, `src/tool_execution.py` or
  the tool schemas. The brain calls them; it doesn't edit them.
- **What the brain depends on upstream for:** `execute_tool_block`,
  `create_request_authority`, `ToolRunSecurityContext`, `stream_llm`, `tool_parsing`, the
  MCP manager and the chat window's event names. Any of these that are private names go
  through `fork/upstream.py`, so `tests/fork/test_fork_upstream.py` names a rename at the
  next sync.
- **Later removals:** once the brain is the default for chat, the fork's patches that only
  exist for upstream's agent on chat turns can be removed: web-intent routing, store
  lookups leaving chat mode, plain module questions reaching module tools, follow-ups
  keeping module tools, several tools in a row, empty replies after module tools, chat-mode
  questions not being run, "Thinking: Off" in agent mode. Each is checked against what
  scheduled tasks still need before it goes.

## Evaluation

Two evals, both run in the sandbox ([sandbox.md](sandbox.md)) with fictional data:

1. **Athena bakeoff** (`Athena/server/eval/bakeoff.py --frontend odysseus`): score, plus
   **seconds to first visible text** and total seconds per question. The first-text timing
   is a small addition to the bakeoff (Athena repo PR). Compared with `--frontend raw` on
   the same day.
2. **General eval** (`scripts/brain_eval.py`, new), about 20 fictional questions in four
   kinds:
   - knowledge with no tool needed (passes only if it answers *without* searching);
   - web: current facts, answered from the search results and citing them;
   - mixed: personal data plus web ("is it going to rain during my 3 pm meeting?");
   - follow-ups in the same chat ("and the second result?").

   Upstream's `web_fetch` refuses loopback addresses (`src/outbound_fetch.py`), so the
   saved pages' key facts are also in the search snippets.

   Web results are recorded: a small fake SearXNG (`scripts/brain_fake_searxng.py`,
   pointed to by `SEARXNG_INSTANCE`) serves saved results whose links point at saved pages
   on the same local server, so every run sees the same web and scores compare. No live web
   in the sandbox eval.

Each code PR reports both evals before and after. Small-model numbers compare only with
other sandbox runs.

## Phases

Stacked PRs into `barvis`, each mergeable alone, none changing runtime behaviour until the
owner turns the setting on.

1. **Evals first.** The first-text timing in the bakeoff (Athena PR) and
   `scripts/brain_eval.py` with the fake SearXNG (fork PR). Baseline: current fork and the
   bakeoff's raw loop.
2. **Core loop, read-only.** Hook, prompt, fixed tool set, streaming, tools through the
   request authority, read tools only (writes answer "I can't change that yet").
   Measure.
3. **Writes and follow-ups.** Approvals that pause and resume, `history.py`, context size
   handling.
4. **Web and privacy.** Query guard, sources, fallback check; the general eval's web and
   mixed questions. This PR carries the Server verification section.
5. **After the owner has used it for a while:** (the default is already on, by the owner's
   decision) remove the patches listed under
   [Upstream impact](#upstream-impact), then decide about scheduled tasks.

Each PR runs the focused tests from AGENTS.md, the new `tests/fork/test_brain_*.py`,
`python -m py_compile app.py routes/*.py src/*.py`, and a diff re-read for upstream
changes that aren't a listed hook.

## Testing

Sandbox tests, no models (fake LLM, fake MCP manager from `tests/fork/fakes.py`):

- the prompt's start is byte-identical across two turns of a chat, and rounds only append;
- text reaches the stream before the turn ends;
- the tool set is the same for every message of a chat; slash skills add their tools;
- every tool call goes through `execute_tool_block` with a request authority; another
  owner's data is never returned (`test_memory_owner_isolation.py` style);
- writes ask, and an approved write resumes the loop without a second model call for the
  same round;
- an approved `create_document` creates the owner's document through the real runner and
  opens it in the editor; library searches are owner-isolated and untrusted;
- a non-local endpoint is refused;
- the query guard removes fictional contact names, emails, phones and event titles;
- follow-ups see the earlier turn's tool results; deleting a chat deletes them;
- context over budget drops old tool results first;
- text and PDF attachments reach the prompt and the saved message (with upstream's
  `attachment_meta`), arm the run and are capped; images, audio, unreadable files and
  another owner's uploads stay upstream's.

## Server verification (owner)

Targets only the isolated test instance on `127.0.0.1:7010`. It never touches production
(Docker, port 7000).

1. Restart the test instance on the PR's branch with
   `~/.hermes/cache/scratch/barvis-run.sh` (the brain is on by default).
2. Run the Athena bakeoff against it (`--frontend odysseus`, all 20 questions) and paste the
   score and the first-text and total times. Expected: ≥ 15/20, and first text well under
   the current fork's.
3. Run `scripts/brain_eval.py --seed` against it with the fake SearXNG
   (`scripts/brain_fake_searxng.py`, the test instance started with
   `SEARXNG_INSTANCE` pointing at it): its web facts are fictional and its privacy check
   reads the fake's query log, so it can't run against the live web. Paste the table. Then
   ask two or three real web questions in chat against live SearXNG and check the sources.
4. In chat on the phone: a general question, a web question, a personal question, a mixed
   one, a follow-up, and one write (add a task) with its approval. Check that the answer
   starts appearing at once and that the web answer links its sources.
5. Restart with `FORK_BRAIN=off` and check chat works as before (upstream's agent).

Deploy is left to the owner: `scripts/fork_deploy.sh deploy`, after the PR is merged and
verified on the test instance.

## Risks

| Risk | Mitigation |
|---|---|
| Building the request authority wrong lets a tool run that shouldn't | Built exactly as `chat_stream` builds it; tests for owner isolation and admin-only tools; no security logic of our own |
| A paused approval leaks memory or never resumes | Timeout, one pending approval per chat, state dropped on answer or timeout |
| The model searches too much or too little | General eval scores both ways (knowledge questions fail if it searches) |
| Private details in web queries | Prompt rule plus `query_guard.py`, tested; nothing logged but counts |
| Upstream renames something the brain calls | Private names through `fork/upstream.py`, caught by `test_fork_upstream.py` |
| Background jobs slow the next answer | Measured in phase 2; a wait hook only if needed |
| Small-model results mislead | Server verification with the real model before the default changes |

## Decisions

- **2026-10-31:** a small fork-owned loop in Odysseus, not Hermes beside it and not Hermes's
  code copied in. Hermes stays the owner's tool for development.
- **2026-10-31:** general-purpose assistant, not only Athena. No coding tools.
- **2026-10-31:** web search through SearXNG with the DuckDuckGo fallback.
- **2026-10-31:** the [SQLite memory plan](memory-sqlite.md) is on hold; Athena's facts reach
  chat through the belief ledger instead.
- **2026-10-31:** email reading is in the default tool set; sending and drafting ask.
- **2026-10-31:** one global `FORK_BRAIN` switch, no per-chat choice.
- **2026-10-31:** the brain is the default (`FORK_BRAIN` on unless set to off), ahead of
  phase 5; the patches listed under [Upstream impact](#upstream-impact) stay until the owner
  has used it for a while.
- **2026-10-31:** deep research only through `/research`.
- **2026-10-31:** a pending approval waits 10 minutes.

## Open questions for the owner

None open. Earlier questions are answered under [Decisions](#decisions).
