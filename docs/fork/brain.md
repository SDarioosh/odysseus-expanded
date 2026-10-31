# The brain: a small agent loop for Odysseus chat

Status: design only (2026-10-31). No code yet. Owner decisions so far are under
[Decisions](#decisions); open questions are at the end.

## Summary

Odysseus chat gets a new agent loop of the fork's own, `fork/brain/`, that answers every
chat turn when the `FORK_BRAIN` setting is on. It is a general personal assistant:
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
   (`src/prompt_security.py`).
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
  MCP rule), web search and fetch, and the read actions of the workspace tools.
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
| `FORK_BRAIN` | `off` | The brain answers every chat turn of every chat (one global switch, no per-chat choice) |
| `FORK_BRAIN_MAX_ROUNDS` | `8` | Tool rounds per turn |
| `FORK_BRAIN_TOOL_GROUPS` | `web,athena,workspace,beliefs,chats,email` | Groups in the tool set |

Merging the brain changes nothing at runtime until the owner turns `FORK_BRAIN` on, on
the test instance first.

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
- **Turns left to upstream:** research, compare, plan, incognito, attachments, image
  chats, workspace or path turns, native and TUI clients, and approval replies that aren't
  the brain's.
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
5. **After the owner has used it for a while:** default on, remove the patches listed under
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
- a non-local endpoint is refused;
- the query guard removes fictional contact names, emails, phones and event titles;
- follow-ups see the earlier turn's tool results; deleting a chat deletes them;
- context over budget drops old tool results first.

## Server verification (owner)

Targets only the isolated test instance on `127.0.0.1:7010`. It never touches production
(Docker, port 7000).

1. Restart the test instance on the PR's branch with
   `~/.hermes/cache/scratch/barvis-run.sh`, with `FORK_BRAIN=on`.
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
5. Restart without `FORK_BRAIN` and check chat works as before.

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
- **2026-10-31:** one global `FORK_BRAIN` switch, no per-chat choice. It stays off by
  default in code until the owner has verified the brain on the test instance (phase 5
  flips it).
- **2026-10-31:** deep research only through `/research`.
- **2026-10-31:** a pending approval waits 10 minutes.

## Open questions for the owner

None open. Earlier questions are answered under [Decisions](#decisions).
