# Cleanup plan: hub side

As of 2026-10-28. Status: plan, nothing started.

The full plan, for both repos, is in Athena:
[docs/cleanup-plan.md](https://github.com/SDarioosh/Athena/blob/main/docs/cleanup-plan.md).
It has the ground rules, the testing levels, the overseer's job, the prompt and PR
templates, the shared interfaces and the lane table. Read it first; this page only lists
the hub lanes and the rules that are specific to this repo.

## Hub lanes

| Phase | Lane | What | Depends on |
|---|---|---|---|
| 0 | 0C | Inventory tests: every added hunk in an upstream file has a `Fork:` marker; every fork file is in one generated list (`docs/fork/files.md`); Athena contract fixtures parsed by `module_facts`, `module_action_items`, `sound_module_view` | Athena 0A (fixtures) |
| 0 | 0D | Upstream sync per [upstream-sync.md](upstream-sync.md), `dev` mirror fast-forwarded | — |
| 1 | 1D | Fork settings reach production (`docker/fork.override.yml`), unique temp names for atomic writes, the three unmarked hooks marked | Gate 0 |
| 1 | 1F | FORK.md split into a hook index and a feature list; AGENTS.md and FORK.md link the generated file list; roadmap trimmed to status and links | 0C |
| 2 | 2E | Fork `src/` and `routes/` files move into a `fork/` package; hooks change only their import line | Gate 1, 0D |
| 2 | 2F | Fork tests into `tests/fork/` with shared fakes in `conftest.py`; `pytest tests/fork` in CI | 2E |
| 3 | 3F | One Athena client (`fork/athena/client.py`) for MCP and HTTP calls, one error type, one server setting | 3G settings |
| 3 | 3G | `fork/common/`: typed settings table, per-owner state files, local-model JSON calls, one boolean parser | Gate 2 |
| 3 | 3H | Task table, `fork.routes.mount(app)`, `fork/upstream.py` as the only user of upstream private names | Gate 2 |
| 3 | 3I | `fork/routing/turn_policy.py`: 8 hunks in `routes/chat_routes.py` become 3 (evals before and after) | 3H |
| 3 | 3J | `static/js/forkDom.js` (`el`, `$`, `api`), all sidebar entries from `forkModuleWindows.js` | Gate 2 |
| 3 | 3K | Beliefs and Meetings use the `mountX` pattern; `forkPageView.js` goes | 3J |
| 3 | 3L | `scripts/eval_common.py` for the four evals | Gate 2 |
| 4 | 4C | Split `evidence_intake.py`, `module_action_items.py`, `today_insight.py`; AGENTS.md lists the package; CI reports upstream drift | Gate 3 |

## Rules specific to the hub

- **Fork rules first** ([AGENTS.md](../../AGENTS.md#fork-rules-merge-friendly)): each lane
  adds new files and changes upstream files only with small, marked hooks, recorded in
  FORK.md. The aim of 2E, 3H and 3I is *fewer* changed upstream lines; the overseer checks
  `git diff --stat <upstream base>` for upstream files before and after each lane and
  rejects a lane that grows it without a reason.
- **Stored names stay**: action ids (`barvis_sync_action_items`, `module_sync_facts`, …),
  state files (`barvis_action_items_*.json`, `beliefs.db`, `today_insight/`), URLs
  (`/modules/sound/*`, `/api/*`), token scopes (`sound:*`), the server name `convo`.
- **Tests**: the focused trio, all fork tests (`pytest tests/fork` after 2F), `py_compile`,
  `node --check` on changed JS. Judge failures on a clean `barvis` worktree; known
  upstream failures are not ours.
- **Production is never touched** by a lane or its verification; it gets each phase through
  `scripts/fork_deploy.sh deploy`, run by the owner after merge and a test-instance check.

## Baseline (2026-10-28, sandbox)

- Fork tests (73 files added since the last upstream base) plus the focused trio: 1,931
  passed in ~5.3 min (2026-10-29).
- 66 commits behind upstream `dev` (last base 2026-10-22), and 21 of the upstream files the
  fork hooks changed upstream since; lane 0D catches up and starts first.
