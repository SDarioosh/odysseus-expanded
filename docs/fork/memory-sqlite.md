# Memory on SQLite: implementation plan

Status: plan only. No code changes yet. Owner decisions are listed at the end.

## Summary

Move Odysseus memory from the single `data/memory.json` file to a SQLite database
(WAL mode, FTS5 search, vectors in the same file) behind a drop-in replacement for
`MemoryManager`. JSON itself is not the main cost. The costs are rewriting the whole file
on every change, scanning every entry on every query, and splitting text from vectors
across two stores.

- **Fixes:** full-file rewrite on each `uses` bump or add, lost updates between
  read-modify-write callers, linear Jaccard and regex retrieval, text and Chroma vectors
  drifting apart.
- **Keeps:** the entry dict shape, so call sites barely change. JSON stays as an export
  format.
- **Effort:** about 5 small PRs (roughly 600 lines of new code plus tests). Nothing needs
  real devices; the owner verifies on the test instance at `127.0.0.1:7010` only.

## Current state

Memory is a JSON list in `data/memory.json`, owned by `MemoryManager` (`src/memory.py`),
with semantic lookup in a separate Chroma collection (`src/memory_vector.py`). Every
mutation loads everything, edits the list and writes everything.

| Bottleneck | Where | Effect |
| --- | --- | --- |
| Whole-file rewrite | `MemoryManager.save()` writes all entries with `indent=2`, then `os.replace` | Cost grows with store size. `increment_uses()` rewrites the file for a counter bump on every chat turn that injects memory |
| Read-modify-write races | `load_all_for_update()` then `save()` in `ai_interaction.py`, `memory_provider.py`, `evidence_intake.py`, `belief_forget.py`, `routes/backup_routes.py` | Two writers can overwrite each other. `MemoryStoreUnreadable` only guards a corrupt read, not a concurrent write |
| Linear retrieval | `get_relevant_memories()`, `find_duplicates()`: Jaccard plus regex keyword lists over every entry | O(n) per query and per add; weak ranking |
| Two stores | Text in JSON, vectors in Chroma (`MemoryVectorStore`) | Can drift; the code carries rebuild-if-empty logic in `app_initializer.py` and a degraded mode |
| Direct file readers | `scripts/belief_eval.py`, `belief_eval_scale.py`, `claim_ownerless.py`, `migrate_faiss_to_chroma.py`, `core/database.py` migration, owner rename | Assume the file exists and is plain JSON |

The belief ledger already shows the target pattern: `src/evidence_intake.py` opens SQLite
with `PRAGMA journal_mode = WAL` behind a `_connect()` helper and a write lock, and
`src/belief_search.py` keeps vectors in a `belief_vectors` table with a text hash and
model name for staleness.

## Scope

**In scope:** a SQLite-backed store with the same public surface as `MemoryManager`; FTS5
keyword recall; vectors in the same database; a one-time `memory.json` import with
rollback; export back to JSON; tests and a benchmark.

**Out of scope:** the belief ledger schema (`beliefs.db`), Athena and the MCP contract,
the `MemoryProvider` interface for external providers, the embedding model or lanes, and
any UI change. Removing Chroma for chat-side document RAG is a separate question; only
the memory collection moves.

| File | Change | Type |
| --- | --- | --- |
| `src/memory_store.py` | New. `SqliteMemoryStore`: schema, CRUD, FTS5, vectors, import/export | New file |
| `src/memory_rank.py` | New. Hybrid ranking (BM25 + cosine + uses/recency) | New file |
| `src/memory.py` | One hook in `MemoryManager.__init__`/`save`/`load*` that delegates to the store when enabled, marked `# Fork: ... (src/memory_store.py)` | Upstream, small hook |
| `src/app_initializer.py` | Hook where `MemoryManager` and `MemoryVectorStore` are built (lines ~77-109): pick the store, skip Chroma when the store has vectors | Upstream, small hook |
| `scripts/memory_migrate.py` | New. Import, verify, export, rollback commands | New file |
| `scripts/belief_eval.py`, `belief_eval_scale.py` | Read memory through the store instead of `memory.json` bytes; keep the "second sync writes nothing" check as a row-hash comparison | Fork scripts |
| `scripts/claim_ownerless.py`, `migrate_faiss_to_chroma.py`, owner rename in `routes/` | Route through the store | Upstream, small hooks |
| `tests/test_memory_store.py` and friends | New; existing `test_memory_*.py` must pass unchanged | Tests |
| `FORK.md`, `docs/fork/`, `specs/memory-skills.md`, `specs/persistence.md` | Record every hook; update the fail-closed durability text | Docs |

Call sites that need no change if the compatibility layer holds: `ai_interaction.py`,
`memory_provider.py`, `evidence_intake.py`, `belief_forget.py`, `belief_export.py`,
`builtin_actions.py`, `chat_handler.py`, `routes/backup_routes.py`, `today_insight.py`,
`agent_runtime/owned_resources.py`.

## Target design

One file, `data/memory.db`, separate from `beliefs.db` so the ledger and memory can be
backed up, migrated and rolled back independently. Open it the way
`evidence_intake._connect()` does: WAL, `timeout=30`, `BEGIN IMMEDIATE` for writes, one
process-wide write lock.

```sql
CREATE TABLE memories (
  id        TEXT PRIMARY KEY,
  owner     TEXT,
  text      TEXT NOT NULL,
  norm_hash TEXT NOT NULL,          -- sha1 of lowercased, whitespace-collapsed text
  category  TEXT NOT NULL DEFAULT 'fact',
  source    TEXT NOT NULL DEFAULT 'user',
  ts        INTEGER NOT NULL,
  uses      INTEGER NOT NULL DEFAULT 0,
  extra     TEXT                    -- JSON for any other keys (pinned, session_id, ...)
);
CREATE INDEX memories_owner_ts ON memories(owner, ts DESC);
CREATE UNIQUE INDEX memories_dedup ON memories(owner, norm_hash);

CREATE VIRTUAL TABLE memories_fts USING fts5(
  text, content='memories', content_rowid='rowid', tokenize='porter unicode61');
-- AFTER INSERT/UPDATE/DELETE triggers keep memories_fts in step

CREATE TABLE memory_vectors (
  memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
  model     TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  vector    BLOB NOT NULL            -- float32, little endian
);
```

**Unknown keys survive.** `extra` holds every key the table has no column for, so an
entry loaded and saved comes back identical. This is what lets existing callers keep
working without knowing about the schema.

**Retrieval.** Query FTS5 for the top 50 by BM25, embed the query once, cosine-score the
vector rows for the same owner with numpy, and fuse the two ranked lists with reciprocal
rank fusion. Add a small bonus for `uses` and recency. This replaces the Jaccard and
regex keyword lists in `get_relevant_memories`. Pinned entries bypass ranking exactly as
today.

**Vectors.** Reuse the embedding lanes and the `text_hash` plus `model` staleness check
from `belief_search.py`. A row whose hash or model differs is re-embedded in a bounded
batch, so a model change never blocks startup. At personal scale (under about 50k
entries) brute-force cosine over a float32 matrix is a few milliseconds; `sqlite-vec` is
a later option, not part of this plan.

**Writes.** `add`, `delete` and `increment_uses` become single-row statements
(`UPDATE ... SET uses = uses + 1`). Duplicate adds hit the unique index and return the
existing row; near-duplicates are caught with a nearest-neighbour check on the new vector
instead of a scan.

**Owner scoping.** Every query takes `owner` in its `WHERE`, so the cross-tenant tests
(`test_memory_owner_isolation.py`, `test_memory_extractor_vector_cross_tenant.py`) keep
their meaning.

## Compatibility layer

The hard part is not SQL; it is that callers treat memory as a list. Six call sites do
`load_all_for_update()`, mutate the list, then `save(entries)`. The store must accept
that pattern without rewriting the table each time.

1. **`save(entries)` becomes a diff.** Inside one transaction, compare the incoming list
   with current rows by `id` (and `norm_hash` for content). Insert new ids, update only
   rows whose fields changed, delete ids that are gone. An unchanged list writes nothing,
   which also keeps the "second sync writes nothing" belief checks true.
2. **`load_all_for_update()` takes the write lock.** Hold the process-wide lock until
   `save()` or a timeout, so two read-modify-write callers serialize instead of
   overwriting each other. Return the rows as plain dicts with `extra` merged back in.
3. **`MemoryStoreUnreadable` stays.** Raise it when `memory.db` cannot be opened or fails
   `PRAGMA quick_check`, so every caller that fails closed today still does.
   `load_all()` stays lenient.
4. **Add direct methods for hot paths:** `add_entry` (insert), `increment_uses(ids)`
   (single `UPDATE`), `delete(ids)`, `search(query, owner, limit)`. Migrate
   `ai_interaction.py` and the chat processor to them in a later phase; until then the
   list API is enough.
5. **Vectors follow rows.** `MemoryVectorStore.add/remove` calls in `ai_interaction.py`
   become writes to `memory_vectors`. When the new store is active, `app_initializer.py`
   skips building the Chroma memory collection; the `healthy` flag and degraded mode keep
   their meaning (healthy = embeddings available).

**Switch.** A setting `memory_backend` (`json` default, `sqlite`) selects the store, read
in the one hook in `MemoryManager.__init__`. Defaults stay `json` until the owner has
verified the migration, so merging the code changes nothing at runtime.

**Fork bookkeeping.** Each hook gets a `# Fork: ... (src/memory_store.py)` comment and a
row in the "Hooks into upstream files" table in `FORK.md`. No reformatting of `memory.py`.

## Migration and rollback

`scripts/memory_migrate.py` has four commands, all safe to rerun.

| Command | What it does |
| --- | --- |
| `import` | Copy `memory.json` to `memory.json.bak-<timestamp>`, read it with the same strict parser as `_read_entries()`, insert every entry in one transaction, embed missing vectors in batches. Refuses if the JSON is unreadable |
| `verify` | Compare entry counts, ids, per-owner counts and a hash of `(id, text, owner, category)` between the JSON and the database. Exit non-zero on any mismatch |
| `export` | Write the database back to `memory.json` in the current format |
| `rollback` | Set `memory_backend=json` and restore the newest `.bak` if `memory.json` is missing or older than the database's last write |

**Order of events on the test instance.** Take a backup, run `import`, run `verify`, set
`memory_backend=sqlite`, restart. If anything looks wrong, `export` then set the backend
back to `json`; no data is lost in either direction.

**Dual-write window (optional).** For one release, the store can mirror each committed
change into `memory.json` through the existing atomic writer. It costs the full-file
rewrite we want to remove, so it is off by default and exists only if the owner wants a
hot fallback.

**Other writers of `memory.json`.** The legacy `memory.txt` conversion and the
`core/database.py` migration still write JSON. With the SQLite backend they must feed the
importer instead: run the import step once after them, then ignore the file.

**Chroma.** Leave the existing `odysseus_memories` collection in place after migration.
It is dead weight but harmless, and deleting it is irreversible, so removal is a separate
owner decision after a stable period.

## Implementation phases

Five PRs into `barvis`, each Conventional Commits style, each mergeable alone and none
changing runtime behaviour until phase 4 adds an opt-in setting.

1. **Benchmark first (`test(memory): ...`).** Add `scripts/memory_bench.py`: generate N
   fictional memories (1k, 10k, 50k), time `add`, `increment_uses`,
   `get_relevant_memories` and cold load on the current JSON store. Record the baseline in
   the PR. This is the number every later phase has to beat.
2. **Store core (`feat(memory): sqlite store`).** `src/memory_store.py` with schema, WAL
   connection, CRUD, the `save()` diff, `load_all_for_update()` locking, `extra` round
   trip, `MemoryStoreUnreadable`, plus `tests/test_memory_store.py`. Not wired in. Gate:
   the existing `test_memory_*.py` suite, run against both backends through a fixture.
3. **Search and vectors (`feat(memory): fts and vectors`).** FTS5 triggers,
   `memory_vectors`, `src/memory_rank.py` hybrid ranking, bounded re-embedding. Add
   recall-quality tests on fictional data: queries that the Jaccard code misses and BM25
   plus vectors find, and the reverse, so we know nothing regresses.
4. **Wire-in and migration (`feat(memory): opt-in sqlite backend`).** The
   `memory_backend` setting, the hooks in `memory.py` and `app_initializer.py`,
   `scripts/memory_migrate.py`, updates to the belief eval scripts, `claim_ownerless.py`,
   owner rename and backup import, `FORK.md` rows. Default stays `json`. This PR carries
   the Server verification section.
5. **Hot paths and cleanup (`refactor(memory): direct store calls`).** Move
   `increment_uses`, add, delete and search in `ai_interaction.py` and the chat processor
   to the direct methods; drop per-turn full-list loads. After the owner has run on
   `sqlite` for a while: flip the default and update `specs/memory-skills.md` and
   `specs/persistence.md`.

Each phase runs the focused checks from `AGENTS.md`: `python -m pytest -q` on the touched
tests, `python -m py_compile app.py routes/*.py src/*.py`, and a diff re-read for
upstream-file changes that are not a listed hook.

## Testing and Server verification

**Sandbox tests (no models needed, fakes for embeddings).**

- Run the existing memory suite against both backends:
  `test_memory_store_unreadable_no_wipe.py`, `test_memory_owner_isolation.py`,
  `test_memory_provider.py`, `test_memory_tool_contract.py`, `test_manage_memory_*.py`,
  `test_mcp_memory_owner_scope.py`, `test_builtin_memory_consolidation.py`,
  `test_belief_forget.py`.
- New: round trip of unknown keys through `extra`; `save()` diff (no-op writes nothing,
  one changed row updates one row); two threads in `load_all_for_update()` serialize;
  corrupt database raises `MemoryStoreUnreadable`; unique-index dedup; import then export
  equals the original JSON; owner isolation in FTS and vector queries; stale-vector
  re-embedding is bounded.
- Performance gate in the style of `tests/test_ledger_perf.py`: at 10k entries
  `increment_uses` and `add` stay under a fixed millisecond budget set from the phase 1
  baseline, and recall latency does not grow linearly with a 5x larger store.

**Server verification (owner).** Targets the isolated test instance on `127.0.0.1:7010`
only. It never touches production (Docker, port 7000).

1. Copy the test instance's `data/` aside, then
   `python scripts/memory_migrate.py import && python scripts/memory_migrate.py verify`.
   Expected: counts match, exit code 0.
2. Set `memory_backend=sqlite`, restart with `~/.hermes/cache/scratch/barvis-run.sh`.
3. Run `python scripts/memory_bench.py --entries 10000` and paste the before and after
   table.
4. Run `python scripts/belief_eval.py` and `belief_eval_scale.py`; expected: every check
   passes, including "second sync writes nothing".
5. In chat, add, edit and forget a memory, ask a question that should recall it, and
   check the answer cites it.
6. Rollback drill: `python scripts/memory_migrate.py export`, set `memory_backend=json`,
   restart, confirm the same memories appear.

Deploy is left to the owner: `scripts/fork_deploy.sh deploy`, after the PR is merged and
verified on the test instance.

## Risks and open questions

| Risk | Mitigation |
| --- | --- |
| `save(entries)` diff misses a field and silently drops data | `extra` round-trip tests; `verify` command; backup before import |
| Lock held by `load_all_for_update()` and never released if a caller raises | Context-manager form plus a lock timeout; audit the six call sites in phase 4 |
| Hybrid ranking recalls different memories than today and changes answers | Recall tests on fictional data; owner reviews a side-by-side of 20 queries before the default flips |
| Embedding model change leaves vectors stale | `model` + `text_hash` check and bounded background re-embed, as in `belief_search.py` |
| Eval scripts and tools that read `memory.json` directly break | Phase 4 updates the listed scripts; `export` can regenerate the file on demand |
| More upstream hooks than the fork rules like | Keep to three small hooks (`memory.py`, `app_initializer.py`, owner migration) and list each in `FORK.md` |

**Open questions for the owner**

- How many memories do you have today, and what latency do you see? The phase 1
  benchmark will answer it, but a rough number now decides whether vectors need
  `sqlite-vec` or brute force is enough.
- Should the SQLite store ever be mirrored back to `memory.json` (dual-write), or is
  export on demand enough?
- Is it acceptable to retire the Chroma memory collection after a stable period, or do
  you want it kept for other features?
- Should this go upstream? The store is general and could be offered, but the
  fork-specific belief hooks would have to stay out of it.
