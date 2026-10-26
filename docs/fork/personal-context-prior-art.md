# Personal context: what we take from other projects

Open-source projects that solve parts of the [personal context plan](personal-context.md),
what each one is good for, and exactly what to borrow at which stage. Read this before
starting a stage of that plan: each stage below lists the ideas to use and the traps to
avoid. The plan itself stays the source of truth for *what* we build; this file is about
*how*, based on what others have learned.

_Written 2026-10-22. Licenses and features were checked against each project's README on
that date. Check them again before adopting anything: these projects change quickly._

## Rules for borrowing

- **Ideas, not code, by default.** Read their design, write our own code in fork files
  ([AGENTS.md](../../AGENTS.md) fork rules). Copying code needs a license check first
  and a line in the PR saying what was copied and under which license.
- **Licenses.** Apache-2.0 and MIT code may go into the hub (AGPL-3.0). Nothing from the
  hub, or from an AGPL project, goes into a module. A source-available or non-commercial
  license (Screenpipe) means: run it as a separate program, never vendor it.
- **Running one of them as a service** is allowed only if it runs on the owner's network,
  with a local LLM and local embeddings, with telemetry off, and with no cloud default
  left switched on. Most of these projects default to OpenAI and some send analytics;
  turning both off is part of the PR, with a test or a check that proves it.
- **Our rules win over their design.** Every belief keeps its provenance (source, id,
  link), single mentions are evidence and not beliefs, and forgetting reaches the source
  ([personal-context.md](personal-context.md#shape-of-the-system)). A library that can't
  store a link per fact or delete by id is not adopted.
- **Measure before adopting.** A borrowed engine replaces our code only if it does better
  on the memory eval (stage 1.6) on fictional data in the sandbox.

## The projects at a glance

| Project | License | What it is | Use it for | Verdict |
|---|---|---|---|---|
| [Graphiti](https://github.com/getzep/graphiti) (Zep's engine) | Apache-2.0 | Temporal knowledge graph: entities, fact edges with valid/invalid times, episodes as the source of each fact. Neo4j or FalkorDB. Reference MCP server. Works with OpenAI-compatible local servers (`OpenAIGenericClient`) | 1.5 expiry, 2.1 people, 2.2 structure | **Borrow the data model now; spike as an engine in Phase 2** |
| [Hindsight](https://github.com/vectorize-io/hindsight) (Vectorize) | MIT | Agent memory with `retain`, `recall`, `reflect`. Four memory kinds (world facts, experiences, observations, mental models). Recall merges semantic, BM25, graph and time search with reciprocal rank fusion and a cross-encoder reranker. Postgres + pgvector. Local LLMs (llama.cpp, Ollama, any OpenAI-compatible). MCP per memory bank | 1.1 kinds of evidence, 2.3 retrieval, 3.x and 4.1 reflection | **Borrow retrieval and reflection; spike as an engine in Phase 2** |
| [Cognee](https://github.com/topoteretes/cognee) | Apache-2.0 (per the project's guides; check the LICENSE file) | Graph + vector memory with `remember`, `recall`, `improve`, `forget`. Custom ontologies. Ollama guide. MCP server (port 8001) | 2.2 ontology, 1.4 forget | **Reference; second choice for the Phase 2 spike** |
| [Mem0](https://github.com/mem0ai/mem0) | Apache-2.0 | Simple memory API. Its new algorithm is single-pass, add-only extraction: memories accumulate, nothing is overwritten | A lesson for 1.1 (below) | **Skip as an engine; take the lesson** |
| [Letta](https://github.com/letta-ai/letta) (formerly MemGPT) | Apache-2.0 | A whole agent runtime whose agents edit their own memory | Nothing: Odysseus is already the runtime | **Skip** |
| [LongMemEval](https://github.com/xiaowu0162/LongMemEval) | not stated in the README; check before copying data | The standard long-term memory benchmark: 500 questions over long chat histories | 1.6 and 2.5 eval design | **Borrow the question categories** |
| [Khoj](https://github.com/khoj-ai/khoj) | AGPL-3.0 | Personal AI over your notes and documents, local models | Reference for 1.2 (notes and documents as sources) | **Reference only** |
| [Screenpipe](https://github.com/screenpipe/screenpipe) | Screenpipe Commercial License (source-available; free for personal use) | Records screen (accessibility tree, OCR fallback) and audio, local Whisper, SQLite + FTS5, REST API on `localhost:3030`, own MCP server | A future module, `odysseus-screen` | **Planned module (owner wants it)**; see [below](#future-module-odysseus-screen-screenpipe) |

Honcho (Plastic Labs) is in the Hermes memory catalog our module docs mention, but wasn't
checked for this file. Look at it before Phase 3 (it models the user from conversation).

## What to use, stage by stage

Stage numbers are those of [personal-context.md](personal-context.md).

### 1.1 Evidence intake

- **Keep evidence add-only.** Mem0's current algorithm no longer updates or deletes
  memories during extraction: one LLM call adds memories, and nothing is overwritten.
  Our plan already splits evidence from beliefs; keep it that way: the intake only
  appends evidence (no LLM rewriting old entries), and confirmation, supersession and
  retirement happen at the belief level, in code we can test.
- **Two times on every piece of evidence** (Graphiti's "episodes"): when it happened or
  was said (`said_at`, from the source) and when we learned it (`seen_at`, intake time).
  A voice note from Monday uploaded on Wednesday is Monday evidence. Point-in-time
  questions ("where did I work in March?") need the first; debugging needs the second.
- **A kind on every piece of evidence** (Hindsight's memory kinds): `fact` (about the
  world or a person), `experience` (something the owner did), `preference` or `opinion`
  (can change and can be held loosely), `observation` (something the system inferred).
  Only `fact` and `preference` can become beliefs in Phase 1; observations need the
  owner's confirmation. Map these onto the hub's existing categories (`fact`,
  `preference`, `contact`; see `src/module_facts.py`) instead of adding a new store.

### 1.3 Review page

- Group by subject (Graphiti's entity view): one card per person or topic, its current
  beliefs, and under each belief the evidence with links.
- Show what was superseded ("was: works at Acme, until 2026-09") instead of hiding it.
  This is what makes a temporal model trustworthy to the owner.
- Mark inferred beliefs (Hindsight observations) apart from things the owner said.

### 1.4 Forget propagates

- Cognee and Hindsight both expose forget/delete as a first-class operation. Ours must
  delete the belief, every copy (the facts job's reconcile), and tell the source (the
  module write tool in the plan). Test it: forget, run the reconcile, the fact stays gone.

### 1.5 Expiry and decay

Take Graphiti's temporal model; this is the most useful single idea in this file.

- Each belief has `valid_from` and `valid_to` (when it was true in the world) next to
  `created_at` and `retired_at` (when we stored and retired it).
- A contradiction **closes** the old belief (`valid_to` = when the new one became true);
  it doesn't delete it. "Moved to Lisbon" closes "lives in Porto"; both stay, so "where
  did I live last year?" still works.
- Retrieval uses beliefs valid now by default; a date in the question selects beliefs
  valid then.
- Athena's supersede chain already gives the dates; map it onto these fields
  rather than inventing new ones.
- Decay is a ranking signal, not deletion: "not heard since" lowers rank and shows on
  the review page; only the owner or a contradiction retires a belief.

### 1.6 Memory eval

Model the eval on LongMemEval's five abilities. Write our own fictional cases (the
`seed_demo.py` people and places); don't depend on its dataset, which is large and made for
cloud-sized models.

| LongMemEval ability | Our case |
|---|---|
| Information extraction | a fact said once in a conversation is found (as evidence, not a belief) |
| Multi-session reasoning | a fact confirmed across 3 conversations on 2 days becomes a belief; an answer combines two sources |
| Knowledge updates | after "Alice moved to Lisbon", the current answer is Lisbon and the old one is closed, not lost |
| Temporal reasoning | "what was I working on in early September?" uses `valid_from`/`valid_to` |
| Abstention | a question about something never said gets "I don't know", not a guess |

Add our own two: **forget** (a forgotten fact never returns) and **provenance** (every
answer cites a link). Score with exact checks where possible (expected belief ids, links
present); use a local judge only for free-text answers, like `bakeoff.py`.

### 2.1 People linking

- Graphiti resolves entities on the way in (is this "Alice" the known Alice?). Do the
  same but conservatively: link on the module's stable person id and the hub contact id,
  never on a name match alone; an uncertain match becomes a "who is this?" card, as
  Athena already does for speakers.

### 2.2 Structured entries

- Use Graphiti's fact-edge shape: `subject` → `relation` → `object`, plus the original
  `fact` text, the time fields of 1.5, and the list of evidence ids. The free text stays,
  so nothing is lost if the structure is wrong.
- Keep a small ontology (Cognee's custom data models): only entity types and relations a
  consumer uses (person, place, organisation, project; works_at, lives_in, likes,
  deadline). This is the plan's "only attributes a consumer uses" rule in practice.
- Constrain extraction with `json_schema` on llama-server (we already do this for
  summaries). Graphiti warns that small models often produce JSON that doesn't match the
  schema; constrained decoding is the fix, and the sandbox's small model is the test.

### 2.3 Retrieval

Take Hindsight's recall pipeline; all parts can run locally.

1. Run several searches in parallel: semantic (local embeddings), keyword (SQLite FTS5 /
   BM25), graph (beliefs linked to the people or projects in the question) and time (the
   question's date range).
2. Merge them with reciprocal rank fusion (score = Σ 1 / (60 + rank); no tuning needed).
3. Optionally rerank the top ~30 with a small local cross-encoder; measure on the eval
   whether it is worth the CPU time.
4. Filter by validity (1.5) and demote stale beliefs.

Keep the current keyword-and-similarity lookup as the fallback, as the plan says.

### 3.x Learning from outcomes, and 4.1 consolidation

- Hindsight's `reflect` periodically turns many facts and experiences into higher-level
  observations and "mental models" ("plans the week on Sunday evenings"). Use the same
  idea for 3.4 routines, with two rules of ours: every observation keeps the ids of the
  evidence it came from (so it can be reviewed and forgotten), and observations stay
  tentative until the owner confirms them on the review page.
- This is also the shape of 4.1: consolidation that merges within a source and keeps ids
  and links, instead of upstream's consolidation that rewrites entries.

## Phase 2 spike: our code, or an engine?

Phases 0 and 1 are our own code (they're small and the provenance rules are ours). At the
start of Phase 2, run one spike before writing the structured store:

- **Candidates:** Hindsight (MIT, Postgres, local LLMs listed, retrieval already built)
  and Graphiti (Apache-2.0, best temporal model, needs Neo4j or FalkorDB). Cognee as a
  third if both fail.
- **Setup:** in the sandbox, as a separate local service, fed through the 1.1 intake from
  `seed_demo.py` data, with llama-server and a local embedding model, telemetry off.
- **Pass if:** it scores at least as well as our own store on the 1.6 eval, stores our
  provenance fields and link per fact, deletes by id (1.4), and runs on the server's CPU
  and RAM next to the existing models.
- **Then:** the hub talks to it from a fork file (`src/<feature>.py`), never through
  upstream code changes beyond a listed hook. If it fails, we build 2.2 and 2.3 ourselves
  with the designs above.

## Future module: odysseus-screen (Screenpipe)

The owner wants this. It is a new module in its own repo (`odysseus-screen`), built per
[modules.md](modules.md) ("Checklist: adding a new module"). It comes after Phase 1, so
its evidence lands on the review page instead of straight in memory.

**What Screenpipe gives us** (checked 2026-10-22):

- Continuous capture of the screen (text from the OS accessibility tree, OCR as fallback:
  Apple Vision on macOS, Tesseract on Linux) and of audio, transcribed by local Whisper.
- macOS and Windows fully; Linux x86_64 with caveats (depends on the desktop and
  permissions).
- Local SQLite with FTS5 plus media files; app, window and URL exclusions.
- REST API on `localhost:3030` (`/search?q=…&content_type=all&limit=…`, frames, audio,
  health, raw SQL) and its own MCP server (`screenpipe-mcp`).

**What it sends to the company, and how to stop it** (read from the source at
`screenpipe/screenpipe@6661b7d`, 2026-10-21; check again on every upgrade):

| Outbound traffic | Default | How to stop it |
|---|---|---|
| Usage analytics (PostHog) and crash reports (Sentry) from the engine | **on** | `--disable-telemetry` and env `SCREENPIPE_DISABLE_TELEMETRY=1` (both; the env var also covers `screenpipe-mcp` and the SDK) |
| Update check (`registry.npmjs.org`) at every engine start | **on** | env `SCREENPIPE_NO_UPDATE_CHECK=1` |
| CLI nudges and survey links | on (printed only) | env `SCREENPIPE_NO_REMINDERS=1` |
| Desktop app: PostHog all through the UI, feature flags, announcements, account and trial checks, auto-updater | **on** | **don't use the desktop app**; run the engine headless |
| Workflow-event classifier: sends recent screen text to `api.screenpipe.com` | off (needs a subscription) | leave `enableWorkflowEvents` off |
| Cloud sync (`screenpi.pe/api/sync`), team/enterprise upload | off | never sign in, never pass `--enable-sync` |
| Cloud transcription (Deepgram, Screenpipe cloud) | off; default is local Whisper or Parakeet | set the audio engine explicitly to a local one, or disable audio |
| Built-in AI agents and chat (default to Screenpipe's or a cloud API) | off unless used | don't use them; the module reads the REST API only |
| Connectors (Google Calendar, Jira, Airtable…) | off | don't connect any |
| Model downloads (Hugging Face) | on first use | allowed (model weights); download once, then block |

Then enforce it, so a new version can't quietly add a call: a per-app firewall on the Mac
(LuLu, free and open source, or Little Snitch) that allows the Screenpipe binaries only
`localhost`, after the models are downloaded. Verify with `nettop -p <pid>` or
`lsof -i -P | grep screenpipe` while it records: only `127.0.0.1:3030`. Pin the version
and upgrade on purpose, rereading this table each time.

- License: the Screenpipe Commercial License allows personal use. Run it as its own
  program; never copy its code into the module or the hub.

**Design:**

- Screenpipe runs on the machine it records (the owner's Mac first). The module is a
  small program next to it: it reads Screenpipe's REST API on localhost and serves its
  own MCP server, bound to Tailscale with a bearer token like Athena
  ([modules.md](modules.md#auth-http)).
- **Don't register `screenpipe-mcp` in Odysseus directly.** Our own MCP server is what
  gives Odysseus the correct `readOnlyHint` annotations, a source link on every hit, the
  citation rule in each result, auth, and the shared-search arguments
  ([modules.md](modules.md#shared-search-planned)).
- Read tools first (all `readOnlyHint: true`): `search_screen` (shared-search arguments
  `query`, `since`, `until`, `person`, `limit`; hits with app, window, time and link),
  `get_screen_moment` (the text around one moment), `get_activity_summary` (time per app
  or project for a day). Links open a module page that shows the frame and its text at
  that moment, signed like Athena's transcript links.
- Evidence later: a daily job turns the day into a short summary and candidate facts
  ("worked on the T5 firmware for 3 h"), submitted through the 1.1 intake as `experience`
  evidence with links. Raw screen text never goes into hub memory.
- Audio: Athena owns conversations, including Zoom and other calls on the
  computer, recorded with the hub's **Record a call** page into the module's normal ingest
  path ([modules/odysseus-sound.md](modules/odysseus-sound.md#recording-calls-from-the-computer)).
  Run Screenpipe with audio capture off, so a meeting isn't recorded and transcribed
  twice.

**Privacy rules for this module** (on top of AGENTS.md):

- Exclusions are set before the first recording: password managers, banking, private
  browser windows, and the Odysseus and Athena pages themselves (no
  self-recording).
- A retention limit for frames and media (proposal: 30 days), with text summaries kept.
- A one-click pause on the Mac, and a visible indicator while recording.
- Screen text is the most sensitive data in the system (it can contain passwords and
  other people's messages): read tools never return more than the moment asked for, and
  the eval uses fictional screenshots only.

**Open questions for the owner:**

- Which machines: the Mac only, or the Ubuntu server's desktop too?
- Retention for frames and for text summaries.
- The app and site exclusion list.

## Keep this file current

- When a stage uses an idea from here, link the PR next to that stage's heading.
- When a spike or eval disproves an idea, say so here in one line, with the score.
- Re-check licenses and telemetry before any project is run as a service.
