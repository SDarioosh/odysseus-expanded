# Personal context system: plan and phases

How Odysseus becomes a personal context system: it takes in as much of the owner's own
data as the owner feeds it, learns about them over time, and uses that knowledge to help
(first: a daily "Today" insight). This document is the roll-out plan. Status of what is
built lives in [roadmap.md](roadmap.md); the module contract in [modules.md](modules.md).
What to borrow from other open-source memory projects, per stage, and the planned
Screenpipe module: [personal-context-prior-art.md](personal-context-prior-art.md). Read it
before starting a stage.

_Written 2026-10-21. Decisions are marked **decided**; everything else is a proposal for
the owner._

## Shape of the system

Three layers, kept apart on purpose:

| Layer | What it is | Where |
|---|---|---|
| **Evidence** | Things a source observed, each with a link to where it came from: a fact said in a conversation, a line in an email, a note, a calendar entry | the modules (Athena today) and Odysseus' own stores |
| **Beliefs** | What the system holds to be true about the owner, with provenance (source, id, link, first and last seen, how confirmed) and a retirement path | Odysseus memory |
| **Consumers** | Things that use the beliefs: chat context, the Today insight, later notifications and other modules | Odysseus |

Rules that hold in every phase:

- Everything stays on the owner's network: no cloud speech-to-text, LLMs or embeddings
  in any data path, no telemetry ([AGENTS.md](../../AGENTS.md)).
- Modules are reached only over their MCP server or documented HTTP API
  ([modules.md](modules.md)); Odysseus never touches a module's storage.
- Every belief carries provenance. A belief without a source link is not stored.
- Confirmation before use: a single mention is evidence, not a belief. What counts as
  confirmed is a setting, and the owner's own confirmation outranks any count.
- Everything is correctable and forgettable, and forgetting propagates back to the source.
- Every phase ships with a way to measure it (tests on fictional data, an eval, or a
  Server verification the owner runs).

## Phase 0: Today insight and confirmed facts (decided, in progress)

**Goal.** Every day a short AI insight (headline, what to do first with a reason each,
what is coming up, other things worth a look), built from the calendar, to-dos and
memory, shown in the web UI, in chat and on the T5 e-paper. Confirmed conversation facts
flow into memory so the insight gets richer over time.

**Order of work.** Module side first, then the hub, then the relays and the T5, then the
eval; one feature branch and one PR per repo, linked to each other.

| Stage | Repo | Ships |
|---|---|---|
| 0.1 Confirmed facts | Athena | A fact is confirmed when it is current, said in ≥ `CONVO_FACT_CONFIRM_SOURCES` distinct conversations spanning ≥ `CONVO_FACT_CONFIRM_DAYS` days (defaults 3 and 2). `list_facts` gains `confirmed` and `format="json"`; each JSON fact has `id`, `subject`, `about` (`owner`, `person`, `other`), `fact`, `times_said`, `days`, `first_said`, `last_said`, `link`. Text output unchanged. Contract version bumped; tool tables updated here and in the module's `AGENTS.md`. |
| 0.2 Facts into memory | hub | Hourly job: pulls every confirmed fact and reconciles against its copies by module fact id (a copy carries `source: "odysseus-sound"`, the fact id, the link, `times_said`, `days`). Facts no longer returned are retired. Category from `about`, mapped onto the hub's existing memory categories. Idempotent, so it survives the upstream memory consolidation job. |
| 0.3 Insight job | hub | Every 15 min: gathers calendar (today to +7 days), to-do notes of types `checklist`, `todo`, `goal`, memory, and the module's digest and open action items over MCP. Code buckets items (overdue, due today, due soon, undated), drops shopping-type lists without a due date (titles configurable; default Shopping, Groceries, Grocery), caps candidates, and hashes the inputs. The LLM runs only when the hash changed or the day rolled over, and only picks and writes the "why". Refs not in the input are dropped. Stored per owner and day as `{"date", "generated", "headline", "focus", "coming_up", "also"}`. Built: [today-insight.md](today-insight.md). |
| 0.4 Surfaces | hub | Today card in the web UI (tickable to-dos, events linking to the calendar, 👍/👎 stored); chat answers "what should I do first today?" from the stored insight through the memory-context hook, no new tool; `GET /api/codex/insight/today` with scope `insight:read` (404 when there is none). Built: [today-insight.md](today-insight.md). |
| 0.5 Relays | Athena | PC and Android relays fetch the insight with the Odysseus token, put it in the T5 feed as `insight`, drop refs not in the feed's `todos` or `calendar`; 404, 403 or no hub leaves `insight` out. The `type` filter stays: the hub already reports `todo` and `goal` notes as `checklist` (SDarioosh/Barvis#36); a test documents it. |
| 0.6 T5 firmware | Athena | `feed.c` parses `insight` (unknown keys ignored); `draw_today` shows INSIGHT, DO FIRST, COMING UP, ALSO WORTH A LOOK with the reasons, tickable to-dos, upcoming timed events in order, "Digest ready on Brief ›". Without an insight, `today_plan.c` orders the same sections by rule (overdue → due today by time → next days → pinned → undated; shopping lists only when dated). Host tests render the screens from fictional fixtures. |
| 0.7 Measure | both | Bakeoff rerun with `--frontend odysseus` (contract change). Server verification: confirmed-fact counts at the default thresholds (counts only), insight time on `qwen3.5-35b`, the insight on the owner's data on the test instance, phone and T5 steps. |

**Open questions for the owner** (from the design review): reconcile instead of
`changed_since`; whether the server's hub predates SDarioosh/Barvis#36; whether the
existing memory categories are enough.

**Done when:** the insight shows on all three surfaces from fictional data in the
sandbox, the owner's verification is pasted into both PRs, and the bakeoff score is not
below the previous run.

## Phase 1: One intake, a review page, and forgetting

**Goal.** Make the belief store trustworthy and correctable, and let every source feed it
the same way.

| Stage | Repo | Ships |
|---|---|---|
| 1.1 Evidence intake | hub | One internal path for submitting evidence: subject, `about`, text, source, source id, link, seen-at. The facts job of 0.2 becomes its first caller. Confirmation counting moves here, so the hub can count across sources (a fact said in a conversation and written in an email confirms twice). The module keeps its own confirmation for its own tools. Built: [beliefs.md](beliefs.md) (the belief ledger in `DATA_DIR/beliefs.db`; the facts job submits every module fact as evidence and only confirmed beliefs reach memory). |
| 1.2 More sources | hub | Chat extraction, email (the sender-signature job already learns per sender), notes and documents submit through 1.1 with provenance. Each source has a switch, off by default until tested. Built: notes (off by default), [beliefs.md](beliefs.md#notes-12) (`EVIDENCE_NOTES=on`: the daily Notes Evidence task submits the facts the local model finds in changed plain notes, unconfirmed, with a link to the note). |
| 1.3 Review page | hub | "What do you know about me": tentative and confirmed beliefs grouped by subject, each with its sources and links. Buttons: confirm, correct, forget. A confirmation makes a once-said fact usable the same day. Built: [beliefs.md](beliefs.md#review-page-13) (`/beliefs`, sidebar Beliefs: tabs per status, subject filter, sources with links, Confirm / Edit / Forget / Restore, Sync now). |
| 1.4 Forget propagates | both | A new module write tool retires a fact by id (`readOnlyHint: false`, `destructiveHint: true`), so a fact the owner forgot in the hub does not return on the next reconcile. Contract change, coordinated. Built: [beliefs.md](beliefs.md#forgetting-propagates-14) (hub: Forget on the review page rejects locally, then calls the module's `retire_fact`; a module that is down or lacks the tool is reported, never fatal; "No fact #N." counts as retired). |
| 1.5 Expiry and decay | hub | Every belief carries last-seen and an optional valid-until. Retrieval demotes stale beliefs; the review page shows "not heard since". The module's supersede chain supplies the dates. Built: [beliefs.md](beliefs.md#expiry-and-decay-15) (confirmed beliefs not seen for `BELIEF_STALE_DAYS` and beliefs past `valid_until` go stale and leave memory; owner-confirmed ones never decay by age). |
| 1.6 Measure | hub | A memory eval on fictional seeded data: expected confirmed set, expected retirements after a correction, expected answers to "what do you know about X". Runs in the sandbox like the bakeoff. Built: [beliefs.md](beliefs.md#measure-16) (`scripts/belief_eval.py`: a deterministic seeded history, expected status and memory per case, a score; `--chat` asks a running Odysseus). |

**Voice-note intentions (2026-10-21).** Intentions and principles said in a voice note
("stop prioritizing side projects") are facts in the module now, so they reach the
ledger as evidence instead of becoming to-dos, and the hub files only explicit
voice-note action items (`MODULE_FILE_ITEMS=explicit`,
[odysseus-sound.md](modules/odysseus-sound.md#action-items-in-odysseus)).

**Depends on:** Phase 0 merged. **Risk:** more sources means more wrong beliefs; keep the
sources switched off until the review page exists (1.3 before 1.2 in roll-out).

## Phase 2: Connected and structured

**Goal.** Beliefs connect to the owner's people and projects, and keep working past a few
hundred entries.

| Stage | Repo | Ships |
|---|---|---|
| 2.1 People linking | both | A module voice profile links to a hub contact (the module's `list_people` exposes a stable person id; the hub stores the contact id next to it). A fact about "Alice" lands on the contact with her phone, email and calendar history. Names are no longer matched by string alone. Built (hub side): [beliefs.md](beliefs.md#subjects-and-contacts-21) (every belief has a subject: the owner, a person, a project or a thing; a module person id from `list_people format=json` or a fact's `person_id` maps to one subject; names and aliases match case-insensitively; subjects can be renamed, merged and linked to an Odysseus contact; memory entries carry `subject_id`, `subject_kind`, `contact_id`). The review page has a By subject view: kind, rename, merge and a contact picker per subject, a subject's summary and its beliefs by attribute ([Review page](beliefs.md#review-page-13)). |
| 2.2 Structured entries | hub | Beliefs get optional structure next to the text: subject, attribute, value, valid-from, valid-to. Provenance from Phase 0 makes the migration lossless. Free text stays the fallback. Built: [beliefs.md](beliefs.md#structure-22) (beliefs gain `attribute` from a fixed vocabulary, `value`, `valid_from`; the hourly Belief Structure task fills them for confirmed beliefs with the local model, 5 per call (`BELIEF_STRUCTURE_BATCH`), at most 40 per run, items checked against their belief's text and no guessed dates; a correction re-structures; existing ledgers migrate on first open). |
| 2.3 Retrieval | hub | Chat, the insight and the review page use one ranker over the structured store, with a local embedding model (the hub's vector store, the module's optional embeddings; both must be local). The flat keyword-and-similarity lookup is kept as a fallback. Built (hub side): [beliefs.md](beliefs.md#retrieval-23) (`search_beliefs` ranks by subject, attribute synonyms, local-embedding similarity and keyword overlap; vectors cached in `beliefs.db`, at most 200 embedded per call; the Today insight ranks its memory with it, chat adds a "What I know (confirmed)" block for questions about a known subject or an attribute, never for module questions, and the review page gets `GET /api/beliefs/search`). |
| 2.4 Projects | hub | Project as a first-class subject: facts, action items, events and documents that mention it are grouped. Feeds the insight's "why" ("blocks the proposal due Friday"). Built (ledger side): [beliefs.md](beliefs.md#projects-24) (subject kind `project`, set by the owner or inferred from the name or a `project_status`/`deadline` belief; `subject_summary` gives one paragraph per subject for the insight and chat). Grouping action items, events and documents, and the insight's use of it, come with 2.3. |
| 2.5 Measure | hub | The memory eval grows to a few thousand fictional entries; answers and insight ordering must not degrade. Built: [beliefs.md](beliefs.md#at-scale-25) (`scripts/belief_eval_scale.py`: a seeded fictional corpus, default 300 subjects and 3000 beliefs, `--size small|medium|large`; every belief must end in its planned status, a second memory sync must write nothing, the Belief Structure task must structure every confirmed belief as planned; times submit, owner actions, listing and the memory mirror; retrieval top-1, top-5 recall and p50/p95 with and without the embedding lane, top-5 ≥ 0.9 to pass). Insight ordering is not measured yet. |

**Depends on:** 1.1 and 1.3. **Risk:** structure invites over-modelling; only attributes
that a consumer actually uses are added.

## Phase 3: Learning from outcomes

**Goal.** The system notices what the owner does with its help and gets better at
choosing.

| Stage | Repo | Ships |
|---|---|---|
| 3.1 Outcome log | hub | For each insight: which "do first" items were done that day, which were ignored, which were re-ranked by the owner, next to the 👍/👎 votes of Phase 0. Counts and ids only. Built: [today-insight.md](today-insight.md#outcome-log) (`<day>.outcome.json` next to each insight: every ref shown, first section and position, outcome done / moved / promoted ("↑ first") / dismissed ("not today") / ignored / gone; observed from the notes and calendar state on every 15-min pass, so ticks on any surface count; the first pass after midnight finalizes earlier days; `/today` records its own ticks and marks at once). |
| 3.2 Ranking tuning | hub | Rule weights per list, note type and event type learned from the outcome log (simple, inspectable weights; no model training). The T5's offline order in `today_plan.c` follows the same published rules. Built: [today-insight.md](today-insight.md#weights) (integer weights per note type, list, event type and importance from the last 28 finalized days, 0 until a key was shown 6 times, ±3 per key and ±6 per item, applied only within the rule tiers so overdue and due-today items always lead; "How Today ranks" with Reset and "Why first?" on `/today`; `GET /api/codex/insight/weights` feeds the T5's fallback through the relays, module `docs/t5-pda-plan.md` §5). Time of day and the attributes of beliefs used are not weights yet: the log keeps `done_at`, and the model doesn't say which beliefs it used. |
| 3.3 Push | hub | One ntfy push at the digest hour with the headline and the first item, through the existing module notify channel; off with one setting. Built: [today-insight.md](today-insight.md#push) (`TODAY_INSIGHT_PUSH=on`, default off; after `MODULE_DIGEST_HOUR`, today's insight only, never twice, at most 3 tries a day). |
| 3.4 Richer insight inputs | hub | Routines (habitual times and days from the calendar and conversation history), what the owner said they wanted to do this week (voice notes), and open loops from conversations ("you told Alice you'd send the file"). Built: [today-insight.md](today-insight.md#routines) (routines from the calendar only: single events on the same weekday and time in ≥ 3 of the last 6 weeks join the prompt and are submitted once as tentative beliefs, attribute `routine`, for the owner to confirm; the owner's `plans`/`decision` beliefs of the last 7 days and open loops (module action items that are the owner's and have `with`, MCP 0.9.0) are extra candidates on the web and in chat; the T5 relays drop them). |
| 3.5 Measure | hub | Vote and outcome trends per week on the review page; the memory eval gains insight-ordering cases. Built: [today-insight.md](today-insight.md#trends-and-the-insight-eval) (weekly counts on `/today`; `scripts/insight_eval.py` on fictional days, its fixture shared with the T5's host test). |

**Depends on:** Phase 0 votes and Phase 1 review page. **Risk:** optimising for "done"
rewards easy items; keep the weights visible and the owner in control.

## Phase 4: Keep it healthy (ongoing from Phase 1)

| Stage | Repo | Ships |
|---|---|---|
| 4.1 Provenance-aware consolidation | hub | The fork's own consolidation pass that merges only within a source and keeps ids and links; the upstream consolidation job is turned off for sourced entries. Built: [beliefs.md](beliefs.md#consolidation-41) (upstream's Memory Tidy task and memory audit skip memories with a `belief_id` or a Phase 0 `module_fact_id` and write them back unchanged; each source's job merges near-duplicate beliefs of that source only: same subject and category, words that differ only in filler or plural/third-person forms, no different negation, number or structured value; owner-decided beliefs never; evidence moves with its source ids and links, the kept belief keeps its memory id; `BELIEF_CONSOLIDATE=off` skips the merge). |
| 4.2 Retention and export | both | A retention rule for transcripts and audio (the module), memory export in the hub, and a forget that is complete across hub and module. Built: [beliefs.md](beliefs.md#complete-forget-42) (Forget on `/beliefs` erases for good, with a confirmation and no restore: Athena's `erase_fact`, MCP 0.10.0, redacts the transcript line that said it in every conversation that said it, later repeats too, and rewrites the notes and digests with the local model; the ledger keeps only hashed matching keys and no words; memory copies with the same words and stored Today insights drop it; an older module gets `retire_fact` and is retried hourly; beliefs forgotten before 4.2 get an Erase button), [export](beliefs.md#export-42) (Download on `/beliefs`: ledger and memories as one JSON file, no import back), and in the module an opt-in `CONVO_TRANSCRIPT_RETENTION_DAYS` (default 0 = keep forever, the owner's decision) that deletes old transcript lines and kept audio and keeps summaries, facts and action items. |
| 4.3 Capture quality | Athena | Diarization, owner-voice enrollment and the recorder work in `PROGRESS.md`. Capture is the ceiling on everything above: a fact attributed to the wrong speaker is wrong evidence. |
| 4.4 Shared search | both | The `context` search aggregator from [modules.md](modules.md#shared-search-planned), so chat can ask every module at once and the intake of 1.1 has a uniform read side. |
| 4.5 Upstream sync | hub | Each phase keeps its code in new files with small hooks ([FORK.md](../../FORK.md)); general fixes are offered upstream per [upstream-sync.md](upstream-sync.md). |

## Order of roll-out and what gates each step

1. Phase 0, module PR then hub PR, owner's Server verification, bakeoff not below the
   previous run.
2. Phase 1 in the order 1.1, 1.3, 1.5, 1.4, then 1.2 one source at a time, 1.6 alongside.
3. Phase 2 once the review page has been used for a few weeks and the owner has corrected
   real beliefs (the corrections tell us which structure is worth having).
4. Phase 3 once there are a few weeks of votes and outcomes to learn from.
5. Phase 4 items run alongside from Phase 1 on; 4.1 before 1.2 adds a second source.

## Open questions for the owner

- Phase 0: the three questions above (reconcile, hub age on the server, categories).
- Phase 1: which source comes second after conversations (email or notes)?
- Phase 1: should the review page live on `/modules/sound` or be its own page under
  the hub (proposal: its own page, since beliefs come from every source)?
- Phase 2: is a person id in the module contract acceptable (it is a contract change)?
- Phase 3: is a daily push wanted at all, or does the T5 cover it? (2026-10-22: built, off
  by default.)
