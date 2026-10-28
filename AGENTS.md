# Odysseus fork: agent guide

Instructions for any coding agent (Claude Code, Google Antigravity, others) working in
this repository. Read this file completely before changing anything.

## What this repo is

- Our fork of [Odysseus](https://github.com/odysseus-dev/odysseus), a self-hosted AI
  workspace (chat, agents, MCP, documents, email, notes, tasks, calendar). License
  AGPL-3.0. It was called Barvis until 2026-10-19; the repo URL (`SDarioosh/Barvis`) and
  the `barvis` branch keep that name.
- Odysseus is the **hub** of a personal, fully local AI system: the one chat and agent
  interface the owner uses from an Android phone and a Mac over Tailscale. The fork
  keeps the Odysseus name and adds capabilities through modules.
- **Modules** are separate programs in separate repos (`odysseus-<name>`, or a name of
  their own: the first one's repo is `SDarioosh/Athena`, formerly `barvis-sound`). Each
  owns its data and exposes it through its own MCP server. Odysseus is only a client of
  them.
- First module: [Athena](https://github.com/SDarioosh/Athena) (formerly
  odysseus-sound; recorded conversations: transcripts, summaries, action items, facts).
  Details: [docs/fork/modules/odysseus-sound.md](docs/fork/modules/odysseus-sound.md).
  The rename is wording only. Code and stored values keep the old names: the MCP server
  `convo`, `/modules/sound` routes, `sound:*` token scopes, and the
  string `"odysseus-sound"` used as the belief source, the label on filed notes, the
  memory source and calendar uid prefixes. Changing them breaks existing data; don't
  "fix" them. The slash command is the exception: it is now `/athena` (was `/sound`;
  `scripts/modules_setup.py` deletes the old managed skill).
- What the fork changes today: [FORK.md](FORK.md). System picture, ports and
  services: [docs/fork/architecture.md](docs/fork/architecture.md). Status and
  next work: [docs/fork/roadmap.md](docs/fork/roadmap.md).

## Privacy rules (non-negotiable)

- Audio, transcripts, summaries, facts, embeddings and chat content stay on the owner's
  network.
- No cloud speech-to-text, cloud LLMs or cloud embedding APIs in any data path. No
  telemetry. Don't add a provider, default or fallback that sends user data off the
  network.
- Cloud is fine only for development tooling (coding agents, package and model-weight
  downloads).
- Never paste real transcripts, chat content or tokens into issues, PRs, commit messages
  or test fixtures. Use fictional data (Athena's `server/scripts/seed_demo.py`).

## Where Odysseus keeps things

Odysseus is large (~478k lines of Python) and fast-moving. Start from these files and
`grep` from there; don't read whole directories.

| Area | Where |
|---|---|
| Agent loop, turn routing, web-intent handling | `src/agent_loop.py` |
| Agent runtime: request authority, effects, evidence ledger | `src/agent_runtime/` (`authority.py`, `effect_adapters.py`, `effects.py`) |
| Tool gating, approvals, policy, admin-only tools | `src/tool_capabilities.py`, `src/tool_approvals.py`, `src/tool_policy.py`, `src/tool_security.py` |
| Tool dispatch and built-in tools | `src/tool_execution.py`, `src/agent_tools/` |
| MCP client, built-in MCP servers | `src/mcp_manager.py`, `src/mcp_oauth.py`, `src/builtin_mcp.py`, `mcp_servers/` |
| Presets (system prompts) | `src/preset_manager.py`, `routes/preset_routes.py` |
| HTTP routes | `routes/` (e.g. `routes/mcp_routes.py`, `routes/task_routes.py`, `routes/calendar_routes.py`) |
| Auth, middleware, paths and constants | `core/`, `src/constants.py` |
| Frontend | `static/` (`index.html`, `js/`, `css/`); module sources in `frontend/src/`, mapped by `frontend/modules.json` |
| Tests | `tests/` (see `tests/README.md`, `tests/KNOWN_FAILURES.md`) |
| Upstream's own rules | `CONTRIBUTING.md`, `SECURITY.md`, `THREAT_MODEL.md` |

Fork code so far: `src/mcp_trust.py`, `src/mcp_stdio_owner.py`, `src/mcp_session_recovery.py`, `src/skill_mcp_toolsets.py`,
`src/skill_invocation.py`, `src/offered_reads.py`, `src/module_action_items.py`,
`src/sound_module_view.py`, `src/web_turn_scope.py`, `src/chat_mode_scope.py`, `src/module_turn_scope.py`, `src/auto_mode.py`,
`src/web_read_recovery.py`, `src/checklist_auto_archive.py`, `src/builtin_task_authority.py`, `src/module_facts.py`, `src/evidence_intake.py`, `src/belief_consolidation.py`, `src/belief_forget.py`, `src/belief_move.py`, `src/belief_export.py`, `src/today_insight.py`, `src/vision_auto_detect.py`, `src/log_redact.py`,
`src/call_recorder.py`, `src/record_share.py`, `src/meeting_bot.py`, `src/meeting_auto_join.py`, `src/phone_pairing.py`, `routes/sound_relay_routes.py`,
`src/notes_evidence.py`, `src/belief_structure.py`, `src/belief_search.py`, `src/ink_notes.py`, `src/ink_transcribe.py`, `src/ink_athena.py`, `src/ink_notebooks.py`, `routes/ink_routes.py`,
`src/insight_outcomes.py`, `src/insight_weights.py`, `src/insight_push.py`, `src/insight_routines.py`,
`src/insight_inputs.py`, `src/insight_trends.py`, `routes/insight_outcome_routes.py`,
`routes/insight_weights_routes.py`, `routes/insight_trends_routes.py`,
`routes/sound_module_routes.py`, `routes/today_insight_routes.py`, `routes/summary_model_routes.py`, `routes/whisper_vocabulary_routes.py`, `routes/voice_enroll_routes.py`, `routes/auto_mode_routes.py`, `routes/beliefs_routes.py`, `routes/belief_search_routes.py`, `static/sound-module.html`,
`static/today.html`, `static/js/today.js`, `static/js/todayWeights.js`, `static/js/todayTrends.js`, `static/beliefs.html`, `static/js/beliefs.js`,
`static/call-recorder.html`, `static/js/callRecorder.js`, `static/js/meetingBot.js`, `static/js/forkMeetingAutoJoin.js`, `static/phone-pairing.html`, `static/js/phonePairing.js`,
`static/js/soundModule.js`, `static/js/forkShareRecord.js`, `static/js/forkMeetings.js`, `static/js/forkChatAsk.js`, `static/js/forkEntityHash.js`,
`static/js/forkModuleWindows.js`, `static/js/forkAthenaMenu.js`, `static/js/forkPageView.js`, `static/js/forkBeliefsWindow.js`, `static/js/forkModuleShortcuts.js`, `static/js/forkInk.js`, `static/js/forkInkLinks.js`, `static/css/fork-ink.css`, `static/js/forkBackgroundModels.js`, `static/js/forkWhisperVocabulary.js`, `static/js/forkVoiceEnroll.js`, `static/js/forkPhonePairing.js`, `static/js/forkAutoMode.js`, `static/js/forkResumeFallback.js`, `static/css/fork-modules.css`,
`scripts/modules_setup.py`, `scripts/belief_eval.py`, `scripts/belief_eval_scale.py`, `scripts/unfile_voice_note_todos.py`,
`scripts/module_slash_eval.py`, `scripts/insight_eval.py`, `scripts/ink_eval.py`, `scripts/fork_deploy.sh`, `docker/fork.override.yml`,
their tests (`tests/test_mcp_trust.py`,
`tests/test_mcp_http_annotations.py`, `tests/test_mcp_stdio_owner.py`, `tests/test_mcp_session_recovery.py`, `tests/test_agent_thinking_switch.py`, `tests/test_modules_setup.py`,
`tests/test_skill_mcp_toolsets.py`, `tests/test_offered_reads.py`,
`tests/test_module_action_items.py`, `tests/test_sound_module_view.py`, `tests/test_web_turn_scope.py`, `tests/test_chat_mode_scope.py`,
`tests/test_module_turn_scope.py`, `tests/test_calendar_reminder_wall_time.py`,
`tests/test_checklist_auto_archive.py`, `tests/test_builtin_task_authority.py`, `tests/test_module_facts.py`, `tests/test_evidence_intake.py`,
`tests/test_today_insight.py`, `tests/test_beliefs_routes.py`, `tests/test_notes_evidence.py`,
`tests/test_belief_eval.py`, `tests/test_subjects.py`, `tests/test_belief_structure.py`, `tests/test_belief_search.py`, `tests/test_belief_eval_scale.py`, `tests/test_ledger_perf.py`, `tests/test_fork_module_windows.py`,
`tests/test_call_recorder.py`, `tests/test_record_share.py`, `tests/test_meeting_bot.py`, `tests/test_meeting_bot_js.py`, `tests/test_meeting_auto_join.py`, `tests/test_sound_relay.py`, `tests/test_insight_outcomes.py`,
`tests/test_insight_weights.py`, `tests/test_insight_push.py`, `tests/test_insight_routines.py`,
`tests/test_insight_inputs.py`, `tests/test_insight_eval.py`, `tests/test_summary_model_routes.py`, `tests/test_whisper_vocabulary_routes.py`, `tests/test_voice_enroll_routes.py`,
`tests/test_belief_consolidation.py`, `tests/test_module_fact_kinds.py`, `tests/test_belief_forget.py`, `tests/test_belief_move.py`, `tests/test_auto_mode.py`, `tests/test_web_read_recovery.py`, `tests/test_fork_resume_fallback.py`, `tests/test_ink_notes.py`, `tests/test_ink_routes.py`, `tests/test_ink_upstream_contract.py`, `tests/test_ink_palm_rejection.py`, `tests/test_ink_perf.py`, `tests/test_ink_transcribe.py`, `tests/test_ink_athena.py`, `tests/test_ink_eval.py`, `tests/test_ink_auto_send.py`, `tests/test_ink_photos.py`, `tests/test_ink_phone_layout.py`, `tests/test_ink_notebooks.py`, `tests/test_ink_notes_hidden.py`, `tests/test_vision_auto_detect.py`, `tests/test_log_redact.py`), and the hooks listed in [FORK.md](FORK.md).

## Fork rules (merge-friendly)

Upstream changes every day, so every line we touch in an upstream file is a future
merge conflict.

- Put fork code in **new files** (`src/<feature>.py`, `scripts/<feature>.py`,
  `tests/test_<feature>.py`, `docs/fork/`).
- Change upstream files only with **small hooks**: a lazy import plus a call into the new
  file, a few lines at most. Put a comment on the hook that names the fork file it
  calls (for example `# Fork: ... (src/mcp_trust.py)`), so `git grep` finds every hook.
- No renames, reformatting, import sorting, or drive-by refactors of upstream code. Don't
  run formatters over upstream files.
- Branding goes through theme and config, never by renaming identifiers or strings in
  upstream code.
- Prefer configuration over code (see `scripts/modules_setup.py`).
- Follow upstream's code conventions in `CONTRIBUTING.md` (paths from `src/constants.py`,
  `internal_api_base()` instead of hardcoded URLs, Conventional Commits, its visual style).
- A general fix (not fork-specific) is written so it could go upstream, and is offered
  upstream. How: [docs/fork/upstream-sync.md](docs/fork/upstream-sync.md).
- Record every new hook in the "Changes from upstream" table in [FORK.md](FORK.md).

## Boundaries: hub and modules

- Odysseus reaches a module **only** through that module's MCP server (or its documented
  HTTP API).
- Odysseus never opens, reads or writes a module's database or files directly.
- A module never imports Odysseus code. The AGPL covers the fork only; modules talk to it
  over a protocol, so the AGPL doesn't extend to them. Don't copy Odysseus code into a
  module.
- The module contract (tool naming, annotations, links, auth, eval):
  [docs/fork/modules.md](docs/fork/modules.md).

## Where does this change belong?

| The change is about | Put it in |
|---|---|
| Capturing, processing or storing a module's data (audio, transcripts, summaries) | the module repo |
| A module's tool names, arguments, results, links or annotations | the module repo, coordinated per [modules.md](docs/fork/modules.md#versioning) |
| How the model is offered or gated on MCP tools, approvals, routing | this repo (hook + new file) |
| Chat UI, views of module data, notifications, tasks/calendar in the hub | this repo (calls the module's MCP tools or API) |
| Which tools are on, model tool mode | config: `scripts/modules_setup.py` |
| A preset's instructions for a module | Odysseus config (Custom preset), text owned by the module doc |
| A bug in Odysseus that isn't fork-specific | this repo, written to be offered upstream |

If a feature needs both repos, do the module side first, then the fork side, and link
the two PRs to each other.

## Where you work: the cloud sandbox

- You work in a cloud sandbox, **not on the owner's server**. Never assume access to the
  server, its services, its data or Tailscale.
- The sandbox can build and run the whole system with small CPU models and fictional
  data: llama-server, whisper-server, Athena, its MCP server, Odysseus on
  `127.0.0.1:7010`, and the eval. Steps, timings and known issues:
  [docs/fork/sandbox.md](docs/fork/sandbox.md).
- Small-model scores are only comparable with other sandbox runs. The real score
  (`qwen3.5-35b`, target ≥ 15/20) is measured by the owner on the server.
- Use only fictional data (`seed_demo.py`). Sandbox tokens and passwords are throwaway
  placeholders.

### Server verification (owner)

- Every PR that can change answers, devices or deployment has a **Server verification**
  section: the exact commands, the expected result, and any real-device steps. Format:
  [docs/fork/sandbox.md](docs/fork/sandbox.md#server-verification).
- The owner runs it and pastes the result into the PR. Continue from that result; you
  don't run anything on the server.
- On the server there are two instances of the fork ([docs/fork/deploy.md](docs/fork/deploy.md)).
  **Production** (Docker, `~/models/odysseus`, port 7000) is the owner's daily
  install, with all Odysseus features. Only the owner changes it, and only with
  `scripts/fork_deploy.sh` after the PR is merged and verified on the test instance.
  Never write verification steps that edit, restart, rebuild or reconfigure production
  in any other way; a PR may end its Server verification with "deploy:
  `scripts/fork_deploy.sh deploy`" for the owner to run. The **isolated test instance**
  (`127.0.0.1:7010`, started with `~/.hermes/cache/scratch/barvis-run.sh`, log
  `~/.hermes/cache/scratch/bakeoff/barvis.log`) is what verification steps target.
- Server clones: `~/projects/barvis` (this repo) and `~/projects/barvis-ai-wt/*`
  (Athena worktrees; the folder may be renamed).

## Tests

Upstream's full suite is slow and not clean. Run the focused tests for what you touched:

```bash
python -m pytest -q tests/test_mcp_trust.py tests/test_request_authority.py tests/test_tool_policy.py
python -m py_compile app.py routes/*.py src/*.py
node --check static/js/<file-you-changed>.js
```

Install first (`uv venv -p 3.11 venv && uv pip install -p venv/bin/python -r
requirements.txt -r requirements-dev.txt && mkdir -p data`); the three files above run
281 tests in ~30 s. Setup for a full run is in `tests/KNOWN_FAILURES.md`. For changes
that affect answers, also run the module evals in the sandbox stack
([sandbox.md](docs/fork/sandbox.md#run-the-eval)) and add a Server verification
section to the PR.

### Known upstream failures

- 23 tests already fail on clean upstream, in `tests/test_null_owner_gates.py` and
  `tests/test_runtime_evidence_contract.py`. They are not fork bugs. Don't fix them
  in fork PRs.
- To judge a failure, run the same test on a clean `barvis` checkout (a worktree):
  `git worktree add ../fork-clean origin/barvis`. A failure that also happens there is
  not yours. Report new failures only.

## Git rules

- Work on a feature branch and open a PR. Small commits, Conventional Commits style
  (`feat(mcp): ...`, `docs(fork): ...`).
- Branches here: `dev` mirrors upstream `dev` and is **never committed to**. `barvis` is
  the integration and default branch; PRs go into `barvis`. Stacked PRs say so in the
  description.
- Agents **never merge**, and never push to `main`, `barvis` or `dev` directly. No force
  pushes to branches you didn't create.
- Never commit secrets, `.env`, databases, `data/`, logs, audio, or tokens. Before
  pushing, run the checks in `SECURITY.md` ("Publishing A Fork").
- Upstream sync is its own PR: [docs/fork/upstream-sync.md](docs/fork/upstream-sync.md).

## When stuck

- After **three failed attempts at the same problem**, stop. Don't try a fourth variant.
  End your reply with `ESCALATE: <reason>` (one sentence: what fails and what you tried).
- If something in these docs is unclear or contradicts the code, say so in the PR
  description as an open question instead of guessing.

## Device code

The fork has no device code; devices belong to modules. If a change can't be verified on
real hardware, list what still needs real-device testing in the PR's Server verification
section and the module's `PROGRESS.md`.

## Tool-specific files

- `AGENTS.md` (this file) is the single source of instructions.
- `CLAUDE.md` imports it for Claude Code (`@AGENTS.md`) and adds Claude Code notes.
- Google Antigravity reads `AGENTS.md` at the workspace root directly;
  `.agents/rules/antigravity.md` adds Antigravity notes only.
- Put shared rules here, never in the tool-specific files.
