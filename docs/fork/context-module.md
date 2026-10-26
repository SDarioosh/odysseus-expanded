# Plan: move personal context into its own module (`odysseus-context`)

_Written 2026-10-23. A proposal for the owner; nothing here is decided yet. Open
questions are at the end._

## Why

The belief ledger and the Today insight are the fork's own system (personal context,
Phases 0–4: [personal-context.md](personal-context.md)). They sit in this repo as
about 7,500 lines of Python in fork files, plus about 7,000 lines of tests. Those
files reach into Odysseus internals, including several private (`_`-prefixed)
functions:

| Fork code | Odysseus internals it imports |
|---|---|
| `src/today_insight.py` | `core.database` (`Note`, `CalendarCal`, `CalendarEvent`, `CrewMember`, `ScheduledTask`), `routes.calendar_routes._expand_rrule`, `src.task_scheduler._checkin_calendar_events`, `src.task_scheduler._resolve_task_timezone`, `src.memory.MemoryManager`, `src.llm_core`, `src.task_endpoint`, `src.interactive_gate`, `src.user_time`, `src.builtin_actions.TaskNoop` |
| `src/evidence_intake.py`, `src/belief_forget.py`, `src/belief_export.py` | `src.memory.MemoryManager` (mirror, strict reads) |
| `src/belief_structure.py`, `src/notes_evidence.py` | `src.llm_core`, `src.task_endpoint`, `src.interactive_gate`, `core.database.Note`, `src.text_helpers` |
| `src/belief_search.py` | `src.embedding_lanes`, `src.private_endpoint_thinking`, `src.skill_invocation` |
| `src/insight_outcomes.py`, `src/insight_routines.py` | `core.database` (`Note`, `CalendarCal`, `CalendarEvent`) |
| `src/module_facts.py` | `src.tool_utils.get_mcp_manager`, `src.builtin_actions` |
| `routes/beliefs_routes.py`, `routes/*insight*_routes.py` | `routes.contacts.contacts_routes._fetch_contacts`, `routes.codex_routes._scope_owner`, `core.middleware`, `src.auth_helpers` |

Each upstream sync can break any of these without a merge conflict to warn us. The
code also can't run, be tested or keep its data without a full Odysseus install.

Moving it into a module, the way Athena is built, does three things:

1. **Upstream syncs stop reaching it.** The module talks to Odysseus only through
   documented APIs, so the logic no longer depends on Odysseus internals.
2. **The data has one owner.** `beliefs.db` and the insight files live with the code
   that writes them, with their own migrations, backups and export.
3. **Switching front ends gets cheaper.** If Odysseus is ever replaced, only the pages
   and a small sync job have to be rebuilt; the ledger, insight and their data stay.

What it doesn't do: it doesn't remove the upstream hooks. Most of them stay (see "What
stays in the hub"); they get simpler, not fewer. And it adds a third service to deploy
and run.

## What moves and what stays

### Moves to `odysseus-context`

| Today (hub) | In the module |
|---|---|
| `src/evidence_intake.py` (ledger, subjects, confirm/decay, erase, move, export rows) | `context_server/ledger.py` |
| `src/belief_consolidation.py`, `src/belief_structure.py`, `src/belief_search.py`, `src/belief_move.py`, the ledger part of `src/belief_forget.py` and `src/belief_export.py` | same names under `context_server/` |
| `src/module_facts.py` (fact intake from Athena) | `context_server/sources/sound.py` |
| `src/notes_evidence.py` | `context_server/sources/notes.py` |
| `src/today_insight.py`, `src/insight_outcomes.py`, `src/insight_weights.py`, `src/insight_routines.py`, `src/insight_inputs.py`, `src/insight_trends.py`, `src/insight_push.py` | `context_server/insight/` |
| `scripts/belief_eval.py`, `scripts/belief_eval_scale.py`, `scripts/insight_eval.py` | `eval/` |
| Their tests (`tests/test_evidence_intake.py`, `test_subjects.py`, `test_belief_*.py`, `test_insight_*.py`, `test_today_insight.py`, `test_module_facts.py`, `test_notes_evidence.py`, `test_ledger_perf.py`) | `tests/` |
| Data: `DATA_DIR/beliefs.db`, `DATA_DIR/today_insight/<owner>/`, `DATA_DIR/module_facts_<owner>.json`, `notes_evidence_<owner>.json`, `belief_structure_<owner>.json` | the module's data directory, moved once by a migration script |

### Stays in the hub

| Hub piece | What it becomes |
|---|---|
| `/today` and `/beliefs` pages (`static/today.html`, `static/beliefs.html`, their JS) | Unchanged pages. The modules contract puts views of module data in the hub. |
| `routes/today_insight_routes.py`, `routes/beliefs_routes.py`, `routes/belief_search_routes.py`, `routes/insight_*_routes.py` | Thin proxies: same URLs and JSON, each handler calls one module tool. The pages and the phone/T5 routes (`/api/codex/insight/today`, `/api/codex/insight/weights`) don't change. |
| Memory mirror (confirmed beliefs as Odysseus memories with `belief_id` and link) | A new small hub job, **Context Mirror** (`src/context_mirror.py`), hourly: reads `list_beliefs(status="confirmed", format="json")` from the module and reconciles memory, as the Phase 0 facts job did. It has to stay in the hub: the memory store is Odysseus's, and the token API (`POST /api/codex/memory`) can't carry provenance fields. |
| Upstream Memory Tidy and audit hooks (`src/builtin_actions.py`, `services/memory/memory_extractor.py`) | Unchanged: mirrored memories still carry `belief_id`. |
| Chat context hook (`src/chat_processor.py`, "saved insight: today and beliefs") | Same one line, now calling `src/context_module.py`, which asks the module for the stored insight and the top beliefs (short timeout, cached per turn; nothing added if the module is down). |
| `src/module_action_items.py` (filing voice notes into calendar and to-dos) | Stays. Filing into the hub's own stores is hub work. |
| Contacts linking on `/beliefs` | The hub route adds the contact list to the page itself; the module stores only the linked contact id. |

## Module design

Built like Athena and following the [module contract](modules.md).

- **Repo** `SDarioosh/odysseus-context`, Python 3.11, package `context_server`, ruff and
  pytest, CI like Athena's.
- **MCP server** (stdio and Streamable HTTP, `BearerAuth`, `?access_token=` accepted),
  registered in Odysseus as server `context`.
- **Config** from env, prefix `CONTEXT_` (`CONTEXT_DATA_DIR`, `CONTEXT_TOKEN`,
  `CONTEXT_LLM_BASE_URL`, `CONTEXT_LLM_MODEL`, `CONTEXT_EMBED_BASE_URL`,
  `CONTEXT_TIMEZONE`, the existing `BELIEF_*` and `TODAY_INSIGHT_*` settings under the new
  prefix). Local endpoints only, and a non-private host is refused at start-up (the
  check `belief_search.py` already does).
- **Scheduler**: its own background loop runs the jobs that are Odysseus built-in tasks
  today: sound facts (hourly), notes evidence (daily), belief structure, Today insight
  (every 15 min), outcome finalize, weights, routines, the daily push.
- **Sources** (all over documented interfaces):
  - Athena over its MCP server: `list_facts`, `list_people`, `list_action_items`,
    `get_daily_digest`, `retire_fact`, `erase_fact`, `get_transcript`, `reassign_line`.
  - Odysseus over its token API, with a token holding `todos:read`, `calendar:read`,
    `memory:read`: `GET /api/codex/todos?format=json` (notes and checklists),
    `GET /api/codex/calendar/events` (already expands repeating events, so
    `_expand_rrule` isn't needed), `GET /api/codex/memory` (owner memories for the
    insight).
- **Tools** (first draft; annotations per [modules.md](modules.md#annotations-odysseus-trust-depends-on-them)):

| Tool | Annotation |
|---|---|
| `list_beliefs`, `get_belief`, `search_beliefs`, `list_subjects`, `get_today_insight`, `get_insight_weights`, `get_insight_trends`, `export_beliefs` | `ro` |
| `confirm_belief`, `correct_belief`, `restore_belief`, `record_insight_outcome`, `vote_insight`, `update_subject`, `link_contact`, `refresh_now` | `rw` |
| `reject_belief`, `forget_belief` (erase), `move_belief`, `merge_subjects`, `reset_insight_weights` | `rw_merge` (can't be undone) |

  Every belief in a result carries its source links, as the ledger does today.

## Steps

Module side first, then the hub, as `AGENTS.md` asks. Each step is its own PR (or a
linked pair) and keeps production working.

| # | Step | Repo | Done when |
|---|---|---|---|
| 1 | **Skeleton.** Repo, package, config, MCP server with `BearerAuth`, scheduler loop, local LLM and embeddings clients, CI, `seed_demo` with fictional notes/calendar/facts. | module | `ruff` and `pytest` green; server answers `list_beliefs` on an empty ledger. |
| 2 | **Ledger.** Copy the ledger files and their tests in; replace each Odysseus import with an adapter (memory reads → hub API client, MCP calls → module client, DB models → plain dicts from the API). Sound and notes sources run on the module's scheduler. Ledger tools. | module | All moved tests pass without Odysseus installed; `belief_eval` 18/18 and `belief_eval_scale` numbers equal to the hub's on the same seed. |
| 3 | **Insight.** Copy the insight files and tests; calendar and notes from the hub API, digest and action items from Athena. Insight tools. | module | Moved tests pass; `insight_eval` on fictional data matches the hub's output for the same inputs. |
| 4 | **Migration script.** `context-server migrate --from <odysseus DATA_DIR>` copies `beliefs.db` and the insight/state files (read-only on the source; refuses if the target exists). | module | Round-trip test on a fictional ledger; counts printed, no content. |
| 5 | **Hub cutover behind a switch.** `src/context_module.py` (client), `src/context_mirror.py` (memory mirror); proxies in the existing routes; chat hook calls the client. `FORK_CONTEXT_BACKEND=local` (default, today's code) or `module`. With `module`, the four hub built-in tasks (Module Facts, Notes Evidence, Belief Structure, Today Insight) pause and Context Mirror runs. `modules_setup.py` registers server `context`. | hub | Sandbox: both settings give the same `/today`, `/beliefs` and chat answers on the seed; focused tests pass; Server verification on the test instance. |
| 6 | **Owner runs `module` on the test instance**, then production via `scripts/fork_deploy.sh`, with `local` as the rollback for a few weeks. | owner | Owner's sign-off. |
| 7 | **Remove the in-hub copies.** Delete the moved `src/` files, their tests and the `local` branch of the switch; FORK.md rows updated. | hub | Only hooks and proxies are left; the fork's Python shrinks by ~7,500 lines (and ~7,000 lines of tests). |

Rough size: steps 1–4 are mostly moving code plus ~1,500 lines of adapters and server;
step 5 is ~800 lines in the hub. Between step 5 and step 7, a change to the ledger or
insight goes into the module only; the in-hub copy is frozen.

## Deployment and sandbox

- `scripts/fork_deploy.sh` already runs each module's deploy command first
  ([deploy.md](deploy.md#update-production)); the module ships one like Athena's
  `server/scripts/deploy.sh` (background service, survives reboot, binds `127.0.0.1`).
- [sandbox.md](sandbox.md) gains a step to start `context-server mcp --http` with the
  small CPU model and the seed data.

## Risks

- **One GPU, two callers.** In the hub, background model jobs wait for chat to go quiet
  (`src/interactive_gate.py`). The module can't see that. Options: run the insight on
  llama-server's second slot, keep the hub's gate by having the hub trigger the module's
  model jobs (`refresh_now`) from its queue, or accept the contention. Needs a measurement
  on the server before step 6.
- **Chat latency.** The chat context hook becomes a network call. It needs a short
  timeout and a cache; the insight is stored, so this is a read, not a model call.
- **Two copies during the switch.** Between steps 5 and 7, only one backend may write.
  The switch pauses the hub jobs; a test checks that `module` mode never writes
  `DATA_DIR/beliefs.db`.
- **Forget must stay complete.** Erase still spans the hub's memory (mirror), the ledger
  (module) and Athena (`erase_fact`). The mirror deletes the memory on the next
  pass, and `forget_belief`'s result tells the hub route to delete it right away, so the
  owner never sees it come back.
- **Another service to secure.** Its token, its port and its data directory need the
  same care as Athena's.

## Open questions for the owner

1. **Go or not.** Is this worth ~7 PRs now, or after the eval reaches 15/20?
2. **License.** The moved files were written for the fork (created by fork commits, not
   copied from upstream). As their author you can license the module as you like, as
   Athena is. Confirm that, and pick the module's license.
3. **One owner or several.** `beliefs.db` is keyed by owner today. Keep that (the hub
   passes the owner on each call), or make the module single-owner like Athena?
4. **GPU sharing.** Which of the three options under Risks?
5. **Name.** `odysseus-context` (MCP server `context`), or another name? Note that
   `/context` is already the slash skill that offers every trusted module's reads.
