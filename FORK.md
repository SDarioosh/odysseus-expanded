# Our Odysseus fork

This repo is our fork of [Odysseus](https://github.com/odysseus-dev/odysseus), the hub of
a personal, fully local AI system: the one chat and agent interface. It keeps the
Odysseus name; separate **modules** add capabilities and plug into it over MCP. The first is
[Athena](https://github.com/SDarioosh/Athena) (formerly convo-ai and barvis-sound), which
records conversations from wearables, transcribes and summarizes them on a local
server, and exposes them through an MCP server. Everything runs locally; no audio,
transcripts or summaries leave the owner's network.

The fork was called Barvis until 2026-10-19. The repo is now `SDarioosh/odysseus-expanded`
(the old URL `SDarioosh/Barvis` redirects). The `barvis` branch and a few stored
identifiers (the action-item task's action id, its state files) keep that name; old
`BARVIS_*` settings are still read
([deploy.md](docs/fork/deploy.md#moving-from-the-barvis-name-once)).

Upstream's README still describes the app itself. This file only covers what the
fork changes. More:

- [AGENTS.md](AGENTS.md): instructions for coding agents (fork rules, tests, git rules).
- [docs/fork/architecture.md](docs/fork/architecture.md): hub, modules, devices,
  models, ports.
- [docs/fork/modules.md](docs/fork/modules.md): the module contract and how to
  register a module.
- [docs/fork/modules/athena.md](docs/fork/modules/athena.md): the first
  module.
- [docs/fork/upstream-sync.md](docs/fork/upstream-sync.md): weekly upstream merge.
- [docs/fork/sandbox.md](docs/fork/sandbox.md): running the whole system in a cloud
  sandbox, and the Server verification steps for the owner.
- [docs/fork/deploy.md](docs/fork/deploy.md): production on the server: deploy,
  update, rollback.
- [docs/fork/roadmap.md](docs/fork/roadmap.md): status and next work.
- [docs/fork/today-insight.md](docs/fork/today-insight.md): the daily Today insight (job,
  routes, page, chat).
- [docs/fork/beliefs.md](docs/fork/beliefs.md): the belief ledger between sources and
  memory (evidence, confirmation rule, decay, forgetting).
- [docs/fork/handwritten-notes.md](docs/fork/handwritten-notes.md): handwritten notes
  (Write, transcription, review, into Athena).
- [docs/fork/features/](docs/fork/features/): the other fork features, one page per area.
- [docs/fork/files.md](docs/fork/files.md): every file the fork adds (generated).

## Fork features

The patch set is kept small so upstream merges stay routine: new files where
possible, small hooks in core files, no renames or reformatting. One line per feature;
the link goes to its details (what it does, its files, whether it can go upstream).

### MCP servers

- [Trusted read-only MCP servers](docs/fork/features/mcp-servers.md#trusted-read-only-mcp-servers): tools a trusted server marks `readOnlyHint: true` run without approval on every turn.
- [Trusted reads aren't external effects](docs/fork/features/mcp-servers.md#trusted-reads-arent-external-effects): the evidence ledger counts trusted read-only calls as reads, not unverified changes.
- [Env reference entry](docs/fork/features/mcp-servers.md#env-reference-entry): the fork's `ODYSSEUS_*` variables are in the generated configuration reference.
- [HTTP MCP servers keep tool annotations](docs/fork/features/mcp-servers.md#http-mcp-servers-keep-tool-annotations): Streamable HTTP servers keep `readOnlyHint` (general bug).
- [Stdio MCP servers keep working after the request that added them](docs/fork/features/mcp-servers.md#stdio-mcp-servers-keep-working-after-the-request-that-added-them): each stdio connection lives in its own owner task (general bug).
- [Lost MCP sessions reconnect on their own](docs/fork/features/mcp-servers.md#lost-mcp-sessions-reconnect-on-their-own): a restarted HTTP server is reconnected and the call retried once; module calls get a 60 s deadline (general bug).
- [Empty replies after module tools get an answer](docs/fork/features/mcp-servers.md#empty-replies-after-module-tools-get-an-answer): an empty reply after a trusted module tool gets one tool-free answer round.

### Tool routing and grants

- [Web-intent routing](docs/fork/features/tool-routing.md#web-intent-routing): "latest" or "recommend" questions reach trusted MCP tools instead of "Web access is disabled".
- [MCP tools in skill `requires_toolsets`](docs/fork/features/tool-routing.md#mcp-tools-in-skill-requires_toolsets): a skill can require `mcp:<server>` or `mcp__<server>__<tool>`.
- [Slash-invoked skills bring their tools](docs/fork/features/tool-routing.md#slash-invoked-skills-bring-their-tools): `/<skill>` is offered exactly its declared tools.
- [Slash-invoked skills run in agent mode](docs/fork/features/tool-routing.md#slash-invoked-skills-run-in-agent-mode): a slash skill that declares tools switches its turn to agent mode.
- [Follow-ups keep a used module's tools](docs/fork/features/tool-routing.md#follow-ups-keep-a-used-modules-tools): a trusted module tool used earlier brings back that server's other tools.
- [An approved module tool covers the rest of the task](docs/fork/features/tool-routing.md#an-approved-module-tool-covers-the-rest-of-the-task): approving one trusted-module call lets the same tool run again in the resumed task.
- [Several tools in a row](docs/fork/features/tool-routing.md#several-tools-in-a-row): a turn may read every personal store it offers.
- [Store lookups leave chat mode](docs/fork/features/tool-routing.md#store-lookups-leave-chat-mode): chat-mode lookups in contacts, calendar, tasks, notes or memory run in agent mode.
- [Reads of your own stores without exact wording](docs/fork/features/tool-routing.md#reads-of-your-own-stores-without-exact-wording): every turn may use the read actions of the personal-store tools.
- [Plain module questions reach the module's tools](docs/fork/features/tool-routing.md#plain-module-questions-reach-the-modules-tools): a plain question with a module cue is offered the trusted modules' read tools.
- [Managed skills](docs/fork/features/tool-routing.md#managed-skills): skills made by `modules_setup.py` are slash-invokable without the skill audit.
- [Chat-mode questions aren't run](docs/fork/features/tool-routing.md#chat-mode-questions-arent-run): a how/why question that matches a shell pattern stays in chat mode.

### Web lookups and auto mode

- [Web lookups with the web toggle on](docs/fork/features/web-and-auto-mode.md#web-lookups-with-the-web-toggle-on): comparisons and "search the web" go to web routing; fetches of this run's search hits are allowed.
- [Auto mode: safe tools without asking](docs/fork/features/web-and-auto-mode.md#auto-mode-safe-tools-without-asking): an Auto / Ask button per chat; in Auto, reads and known-URL fetches skip the approval gate, plus `calculate`.
- [A failed approved web read doesn't end the turn](docs/fork/features/web-and-auto-mode.md#a-failed-approved-web-read-doesnt-end-the-turn): a network failure of an approved web read goes back to the model.

### Local models

- ["Thinking: Off" on a local server marked api/proxy](docs/fork/features/models.md#thinking-off-on-a-local-server-marked-apiproxy): a private host gets `enable_thinking: false` whatever its endpoint kind.
- ["Thinking: Off" works in Agent mode](docs/fork/features/models.md#thinking-off-works-in-agent-mode): the agent loop keeps the turn's `thinking_mode` (general bug).
- [Vision auto-detect finds the owner's models](docs/fork/features/models.md#vision-auto-detect-finds-the-owners-models): Auto-detect tries the owner's own vision-capable models first.

### Athena (module) in the hub

- [Voice notes into calendar and to-dos; action items on the phone](docs/fork/modules/athena.md#voice-notes-into-calendar-and-to-dos-action-items-on-the-phone): the "Module Action Items" task files voice-note items into calendar, to-dos and notes, and pushes via ntfy.
- [Athena page: conversations and "who is this?"](docs/fork/modules/athena.md#athena-page-conversations-and-who-is-this): `/modules/sound`: Records, Action items and Who is this?, with a transcript reader.
- [Share a record as a file](docs/fork/modules/athena.md#share-a-record-as-a-file): the Records reader's Share button exports a record as Markdown or HTML.
- [Record a call](docs/fork/modules/athena.md#record-a-call): `/modules/sound/record` records a call in a Chromium browser and uploads it to Athena.
- [Send Athena to a Zoom meeting](docs/fork/modules/athena.md#send-athena-to-a-zoom-meeting): the Meetings page sends Athena's Zoom bot into a meeting and signs it in to Zoom.
- [Send Athena to calendar meetings](docs/fork/modules/athena.md#send-athena-to-calendar-meetings): a Send Athena toggle on calendar events with a Zoom link.
- [Phone relay through Odysseus, paired by QR](docs/fork/modules/athena.md#phone-relay-through-odysseus-paired-by-qr): `/modules/sound/pair` pairs Athena's Android relay by QR code; relay routes under `/api/codex/sound/*`.
- [Background & summary model setting](docs/fork/modules/athena.md#background--summary-model-setting): Settings → AI Defaults: the hub's background-task model and Athena's summary model.
- [Transcription vocabulary setting](docs/fork/modules/athena.md#transcription-vocabulary-setting): Settings → AI Defaults: words Athena's whisper is nudged toward.
- [Enroll My Voice](docs/fork/modules/athena.md#enroll-my-voice): Settings → AI Defaults: enroll the next watch recording as the owner's voice.

### Modules setup and eval

- [Setup](docs/fork/modules.md#setup): `scripts/modules_setup.py` configures the hub for its modules through the admin API.
- [Slash eval](docs/fork/modules.md#slash-eval): `scripts/module_slash_eval.py` runs a module's bakeoff as slash commands.

### Athena windows in the app

- [Records (was Conversations) and Today as Odysseus windows](docs/fork/features/windows.md#records-was-conversations-and-today-as-odysseus-windows): Records and Today open as tool windows inside the app.
- [Meetings window](docs/fork/features/windows.md#meetings-window): recording a call and sending Athena to a meeting have their own window.
- [Athena group in the sidebar](docs/fork/features/windows.md#athena-group-in-the-sidebar): a collapsible Athena group above Tools in the sidebar.
- [Wisdom (beliefs) as an Odysseus window](docs/fork/features/windows.md#wisdom-beliefs-as-an-odysseus-window): the Wisdom entry opens `/beliefs` as a window.

### Belief ledger

- [Athena facts in Odysseus memory, through the belief ledger](docs/fork/beliefs.md#athena-facts-in-odysseus-memory-through-the-belief-ledger): the hourly "Module Facts" task feeds Athena's facts into the ledger and mirrors confirmed beliefs into memory.
- [The owner's notes as belief evidence (off by default)](docs/fork/beliefs.md#the-owners-notes-as-belief-evidence-off-by-default): the daily "Notes Evidence" task reads facts from the owner's notes.
- [Subjects, contacts, structure and projects in the belief ledger](docs/fork/beliefs.md#subjects-contacts-structure-and-projects-in-the-belief-ledger): people ids, contacts, the "Belief Structure" task and projects.
- [Upstream's memory tidy leaves sourced memories alone; the ledger merges its own near-duplicates (personal context 4.1)](docs/fork/beliefs.md#upstreams-memory-tidy-leaves-sourced-memories-alone-the-ledger-merges-its-own-near-duplicates-personal-context-41): Memory Tidy and the memory audit skip ledger-sourced memories.
- [A forget that is complete, and an export (personal context 4.2)](docs/fork/beliefs.md#a-forget-that-is-complete-and-an-export-personal-context-42): Forget erases for good, also in Athena (`erase_fact`); Download exports the ledger.
- [Move a belief to the person it is about](docs/fork/beliefs.md#move-a-belief-to-the-person-it-is-about): Move files a belief under another person and moves the source line in Athena.
- [One ranker over the belief ledger](docs/fork/beliefs.md#one-ranker-over-the-belief-ledger): `search_beliefs` for the Today insight, chat and the review page.
- [Belief review page](docs/fork/beliefs.md#belief-review-page): `/beliefs` lists the ledger by status and subject with the owner's actions.

### Today insight

- [Daily Today insight](docs/fork/today-insight.md#daily-today-insight): the "Today Insight" task, the `/today` page, the token route and the chat block.
- [Today insight learns from outcomes (Phase 3)](docs/fork/today-insight.md#today-insight-learns-from-outcomes-phase-3): outcome log, learned rule weights, push, routines, open loops and trends.

### Handwritten notes

- [Handwritten notes, stage H1: Write (pen capture)](docs/fork/handwritten-notes.md#handwritten-notes-stage-h1-write-pen-capture): the Write window: pen capture, palm rejection, autosave and offline sync.
- [Handwritten notes, stage H2: transcribe and review](docs/fork/handwritten-notes.md#handwritten-notes-stage-h2-transcribe-and-review): pages read by the local vision model, reviewed by hand.
- [Handwritten notes, stage H4: Send to Athena](docs/fork/handwritten-notes.md#handwritten-notes-stage-h4-send-to-athena): a reviewed note goes to Athena as a written note.
- [Handwritten notes, stage H5: measure the recognizer](docs/fork/handwritten-notes.md#handwritten-notes-stage-h5-measure-the-recognizer): `scripts/ink_eval.py` scores the recognizer.
- [Handwritten notes, stage H6: auto-send and photo import](docs/fork/handwritten-notes.md#handwritten-notes-stage-h6-auto-send-and-photo-import): `INK_AUTO_SEND` and photo pages.
- [Handwritten notes only in Write](docs/fork/handwritten-notes.md#handwritten-notes-only-in-write): the Notes window leaves handwritten notes out; links to them open Write.
- [Handwritten notes: notebooks](docs/fork/handwritten-notes.md#handwritten-notes-notebooks): named, ordered groups of handwritten notes.

### Notes, calendar and tasks

- [Calendar reminder shows the wall time](docs/fork/features/notes-and-tasks.md#calendar-reminder-shows-the-wall-time): the reminder to-do of a timed event shows the local time, not UTC (general bug).
- [Structured notes for token clients](docs/fork/features/notes-and-tasks.md#structured-notes-for-token-clients): `GET /api/codex/todos?format=json` returns notes as data.
- [Finished checklists archive themselves](docs/fork/features/notes-and-tasks.md#finished-checklists-archive-themselves): a finished, unpinned checklist is archived after `FORK_CHECKLIST_ARCHIVE_MINUTES`.
- [Built-in tasks recover from a broken sealed authority](docs/fork/features/notes-and-tasks.md#built-in-tasks-recover-from-a-broken-sealed-authority): a built-in housekeeping task with a broken sealed authority is re-sealed.

### Chat and app UI

- [Prefilled new chat from a link](docs/fork/features/chat-ui.md#prefilled-new-chat-from-a-link): `/?ask=<text>` opens a new chat with the text in the composer, never sent.
- [Open an event or note from a link](docs/fork/features/chat-ui.md#open-an-event-or-note-from-a-link): `/#event-<uid>` and `/#open=notes&note=<id>` open on a fresh page load.
- [Centered chat title in Nobody mode](docs/fork/features/chat-ui.md#centered-chat-title-in-nobody-mode): the chat title no longer shifts when the Nobody eye appears.
- [A run that ended while the page was away shows its answer](docs/fork/features/chat-ui.md#a-run-that-ended-while-the-page-was-away-shows-its-answer): the chat reloads a run that ended while the page was away.

### Logs and tests

- [Test isolation: a fresh `core.middleware` no longer leaks](docs/fork/features/logs-and-tests.md#test-isolation-a-fresh-coremiddleware-no-longer-leaks): an upstream test no longer leaves a fresh `core.middleware` in `sys.modules`.
- [No tokens in the logs](docs/fork/features/logs-and-tests.md#no-tokens-in-the-logs): log output writes `access_token` and similar query values as `***`.

### Deployment

- [Deployment](docs/fork/deploy.md#deployment): the `odysseus-fork:<commit>` image, `scripts/fork_deploy.sh` and the `host-relay` service.

## Hooks into upstream files

Every change to an upstream file, one row per hook, built from the code
(`git grep -n "Fork:"` and `tests/fork/test_fork_inventory.py`, which fails when a hunk in an
upstream file has no `Fork:` marker). Upstreamable: "yes" marks a general fix offered (or
to be offered) upstream, "with discussion" a change upstream may want in another form,
"no" a fork-only hook. When you add a hook, add its row here.

| Upstream file | Function or spot | Fork file it calls | What | Upstreamable |
|---|---|---|---|---|
| `app.py` | after the logging handlers | `fork/hub/log_redact.py` | log formatters write token query values as `***` | yes |
| `app.py` | `AUTH_EXEMPT_EXACT` | `fork/athena/relay_routes.py` | `/api/pair/redeem` needs no session (rate limited) | no |
| `app.py` | routers, after the MCP routes | `fork/routes/__init__.py` (`mount`, table `ROUTERS`) | the fork's ten routers in one call, in order: Athena page, Today, belief search (before the review page's `/api/beliefs/{id}`), belief review, phone relay and pairing, summary model, transcription vocabulary, Enroll My Voice, auto mode default, handwritten notes | no |
| `app.py` | `_startup_event` | `fork/hub/checklist_auto_archive.py` | start the checklist archiver | with discussion |
| `.github/workflows/ci.yml` | byte-compile step | none | compile `fork/` too | no |
| `frontend/src/features/notes/application/workflows.js` | `_fetchNotes` | none (a filter; Write is `static/js/forkInk.js`) | handwritten notes left out of the Notes window | no |
| `static/js/features/notes/application/workflows.js` | `_fetchNotes` (published copy of the above) | none | same filter | no |
| `routes/api_token_routes.py` | `ALLOWED_SCOPES` | `fork/routes/today_insight_routes.py` | scope `insight:read` | no |
| `routes/api_token_routes.py` | `ALLOWED_SCOPES` | `fork/athena/relay_routes.py` | scopes `sound:ingest`, `sound:read`, `sound:write` | no |
| `routes/calendar_routes.py` | `update_event`, before the success return | `fork/athena/meeting_auto_join.py` | Send Athena tasks follow the event | no |
| `routes/calendar_routes.py` | `delete_event`, one occurrence | `fork/athena/meeting_auto_join.py` | same | no |
| `routes/calendar_routes.py` | `delete_event`, whole event | `fork/athena/meeting_auto_join.py` | same | no |
| `routes/calendar_routes.py` | `quick_parse`, the model call | none | `thinking_mode="off"`, 1024 tokens, 30 s, so a thinking model returns JSON | yes |
| `routes/chat_routes.py` | chat handler, form fields | `fork/routing/auto_mode.py` | remember the chat's Auto/Ask choice | no |
| `routes/chat_routes.py` | after `_classify_tool_intent` | `fork/routing/turn_policy.py` (`scope_tool_intent`: `fork/routing/chat_mode_scope.py`) | how/why questions stay in chat mode | yes |
| `routes/chat_routes.py` | after upstream's chat→agent escalation | `fork/routing/turn_policy.py` (`escalate_chat_mode`: a slash skill, `fork/routing/skill_invocation.py`; else a module question, `fork/routing/module_turn_scope.py`; else a store lookup, `fork/routing/personal_store_escalation.py`) | slash skills with tools, module questions and store lookups run in agent mode | yes |
| `routes/chat_routes.py` | after the request authority and the exact-approval authority | `fork/routing/turn_policy.py` (`extend_authority`: `web_search` with the web toggle on, `fork/routing/web_turn_scope.py`; read actions of the personal stores, `fork/routing/personal_read_grants.py`; calculator in Auto, `fork/routing/auto_mode.py`; on an approval continuation only the approved trusted-module tool, `fork/mcp/trust.py`) | grants that need no exact wording | yes, with discussion (calculator: no) |
| `routes/chat_routes.py` | after `_successful_session_tool_names` | `fork/mcp/trust.py` | follow-ups keep a used module's tools | yes |
| `routes/chat_routes.py` | after `resolve_turn_contract` | `fork/routing/turn_policy.py` (`extend_contract`: a slash skill's tools, `fork/routing/skill_invocation.py`, or a module question's, `fork/routing/module_turn_scope.py`; then the calculator in Auto, `fork/routing/auto_mode.py`) | pin a slash skill's tools, offer module tools and the calculator | yes (calculator: no) |
| `routes/chat_routes.py` | after the final turn policy | `fork/routing/turn_policy.py` (`admit_offered_reads`: `fork/routing/offered_reads.py`) | read grants for every offered personal store | yes, with discussion |
| `routes/codex_routes.py` | `list_todos` | `fork/hub/codex_todos_json.py` | `format=json` | yes |
| `scripts/verify_frontend_runtime.py` | folders copied into the isolated runtime | none | copy `fork/` too | no |
| `scripts/verify_notes_browser_runtime.py` | folders copied into the isolated runtime | none | copy `fork/` too | no |
| `scripts/generate_env_reference.py` | `SOURCE_ROOTS` | none | walk `fork/` too, so the fork's settings stay in the reference | no |
| `scripts/generate_env_reference.py` | `VARIABLE_NOTES` (two places) | none | notes for the fork's `ODYSSEUS_*` variables | with the trusted read-only change |
| `website/configuration-reference.md` | generated | none | regenerated with those notes | with the trusted read-only change |
| `tests/test_procfs_scan_guard.py` | `APP_TREES` | none | scan `fork/` too | no |
| `services/memory/memory_extractor.py` | `audit_memories` (two places) | `fork/beliefs/consolidation.py` | sourced memories skip the audit and are put back unchanged | no |
| `services/memory/skills.py` | `SkillsManager.index_for` (two places) | `fork/routing/skill_invocation.py` | managed skills skip the audit gates | with discussion |
| `src/agent_loop.py` | `_web_search_unavailable_for_turn` | `fork/mcp/trust.py` | no "web disabled" reply while trusted MCP tools are on | yes |
| `src/agent_loop.py` | `_build_system_prompt`, active skills | `fork/routing/skill_mcp_toolsets.py` | expand `mcp:` entries | yes |
| `src/agent_loop.py` | `_build_base_prompt`, skill index | `fork/routing/skill_mcp_toolsets.py` | skills needing `mcp:<server>` stay listed | yes |
| `src/agent_loop.py` | `stream_agent_loop`, run security setup | `fork/routing/auto_mode.py` | bind the chat's Auto/Ask choice to the run | no |
| `src/agent_loop.py` | `stream_agent_loop`, skill matching | `fork/routing/skill_mcp_toolsets.py` | MCP entries in `requires_toolsets` | yes |
| `src/agent_loop.py` | `stream_agent_loop`, exact-approval continuation (two places) | `fork/routing/web_read_recovery.py` | a failed approved web read continues the turn | yes |
| `src/agent_loop.py` | `stream_agent_loop`, `_active_route_state` | none (`tests/fork/test_agent_thinking_switch.py`) | keep the turn's `thinking_mode` | yes |
| `src/agent_loop.py` | `stream_agent_loop`, `manage_skills view` | `fork/routing/skill_mcp_toolsets.py` | expand `mcp:` entries | yes |
| `src/agent_loop.py` | `stream_agent_loop`, end-of-loop empty-final recovery | `fork/mcp/trust.py` | answer an empty reply after module tools | yes |
| `src/agent_runtime/authority.py` | `create_request_authority` | `fork/mcp/trust.py` | grant trusted read-only MCP tools | yes |
| `src/agent_runtime/effect_adapters.py` | `classify` | `fork/mcp/trust.py` | a trusted read-only call is an admitted read | yes |
| `src/agent_tools/__init__.py` | `TOOL_HANDLERS` | `fork/routing/auto_mode.py` | `calculate` handler | no |
| `src/builtin_actions.py` | `action_consolidate_memory` | `fork/beliefs/consolidation.py` | Memory Tidy skips sourced memories | no |
| `src/builtin_actions.py` | action registry, end of file | `fork/tasks.py` (`register_actions`, table `TASKS`) | the fork's actions: Module Action Items, Module Facts, Today Insight, Notes Evidence, Belief Structure, `meeting_auto_join` | no |
| `src/caldav_sync.py` | `sync_caldav`, after the pull | `fork/athena/meeting_auto_join.py` | Send Athena tasks follow moved or deleted events | no |
| `src/chat_processor.py` | `ChatProcessor.build_context_preface`, memory context | `fork/insight/today.py` | Today and beliefs block on plan questions | no |
| `src/document_processor.py` | `_resolve_vl_model` | `fork/hub/vision_auto_detect.py` | auto-detect the owner's vision models | yes |
| `src/llm_core.py` | `_apply_local_qwen_thinking_mode` | `fork/hub/private_endpoint_thinking.py` | a private host counts as local | yes |
| `src/mcp_manager.py` | `connect_server`, stdio | `fork/mcp/stdio_owner.py` | own the stdio transport in a long-lived task | yes |
| `src/mcp_manager.py` | `_connect_http` | none | keep tool annotations | yes |
| `src/mcp_manager.py` | `hold_owned_connection` | `fork/mcp/stdio_owner.py` | stdio connections already have an owner task | yes |
| `src/mcp_manager.py` | `call_tool`, the call | `fork/mcp/session_recovery.py` | deadline for module calls | yes |
| `src/mcp_manager.py` | `call_tool`, the error branch | `fork/mcp/session_recovery.py` | reconnect a lost HTTP/SSE session and retry once | yes |
| `src/task_scheduler.py` | `_check_due_tasks` (two places) | `fork/athena/meeting_auto_join.py` | Send Athena joins are not deferred and skip the model slot | no |
| `src/task_scheduler.py` | `_execute_action` | `fork/hub/builtin_task_authority.py` | re-seal a built-in task's broken authority, else say why | yes |
| `src/task_scheduler.py` | end of file | `fork/tasks.py` (`register_scheduler`, table `TASKS`) | seed the fork's housekeeping tasks (`HOUSEKEEPING_DEFAULTS`); `barvis_sync_action_items`, `module_sync_facts` post nothing to chat; `today_insight`, `notes_evidence`, `belief_structure` post nothing and queue for the model (`_SILENT_ACTIONS`, `_MODEL_BACKED_ACTIONS`) | no |
| `src/tool_capabilities.py` | capability registry (`_register`) | `fork/routing/auto_mode.py` | `calculate` is a public read | no |
| `src/tool_capabilities.py` | `ToolRunSecurityContext.observe_messages` | `fork/routing/auto_mode.py` | note URLs the owner wrote | no |
| `src/tool_capabilities.py` | `ToolRunSecurityContext.decision_for` | `fork/mcp/trust.py` | trusted read-only tools pass the gate | yes |
| `src/tool_capabilities.py` | `ToolRunSecurityContext.decision_for` | `fork/routing/auto_mode.py` | auto mode decides safe reads and known-URL fetches | no |
| `src/tool_capabilities.py` | `ToolRunSecurityContext.observe_tool_result` | `fork/routing/auto_mode.py` | note URLs a tool returned | no |
| `src/tool_execution.py` | `execute_tool_block`, before the request-authority denial | `fork/routing/web_turn_scope.py` | `web_fetch` of this run's search hits | yes, with discussion |
| `src/tool_types.py` | `TOOL_TAGS` | `fork/routing/auto_mode.py` | `calculate` tag | no |
| `src/tools/calendar.py` | `_create_calendar_reminder` | none (upstream `src/user_time.py`) | the reminder shows the wall time | yes |
| `src/turn_contract.py` | `selected_tools_for_request`, model-discovery rule | `fork/routing/web_turn_scope.py` | comparisons and web lookups are web questions | yes, with discussion |
| `static/css/01-agent-chat.css` | `.chat-meta-overlay` with the Nobody eye | none | the chat title stays centered | yes |
| `static/index.html` | sidebar Tools list | `fork/athena/routes.py` | Records link | no |
| `static/index.html` | sidebar Tools list | `fork/routes/today_insight_routes.py` | Today link | no |
| `static/index.html` | sidebar Tools list | `fork/routes/beliefs_routes.py` | Wisdom link | no |
| `static/index.html` | script tags, end of body | `static/js/forkChatAsk.js` | `/?ask=` prefills a new chat | yes, with discussion |
| `static/index.html` | script tags | `static/js/forkEntityHash.js` | open `#event-…` / `#open=notes&note=…` on load | yes |
| `static/index.html` | script tags | `static/js/forkModuleWindows.js` | module windows, rail buttons, transcript links | no |
| `static/index.html` | script tags | `static/js/forkBackgroundModels.js` | Background & Summaries card | no |
| `static/index.html` | script tags | `static/js/forkWhisperVocabulary.js` | Transcription Vocabulary card | no |
| `static/index.html` | script tags | `static/js/forkVoiceEnroll.js` | Enroll My Voice card | no |
| `static/index.html` | script tags | `static/js/forkPhonePairing.js` | Pair a phone card | no |
| `static/index.html` | script tags | `static/js/forkAutoMode.js` | Auto/Ask button and its default | no |
| `static/js/admin.js` | `_TOKEN_SCOPES` | `fork/routes/today_insight_routes.py` | `insight:read` in the token scope list | no |
| `static/js/admin.js` | `_TOKEN_SCOPES` | `fork/athena/relay_routes.py` | `sound:*` in the token scope list | no |
| `static/js/calendar.js` | `_showEventForm` | `static/js/forkMeetingAutoJoin.js` | Send Athena toggle (lazy import) | no |
| `static/js/chat.js` | `_tryAutoRecover` | `static/js/forkResumeFallback.js` | reload a run that ended while away | yes |
| `static/js/keyboard-shortcuts.js` | imports | `static/js/forkModuleShortcuts.js` | import the fork's shortcut tables | no |
| `static/js/keyboard-shortcuts.js` | `_defaultKeybinds` | `static/js/forkModuleShortcuts.js` | Open Records / Today / … (unbound) | no |
| `static/js/keyboard-shortcuts.js` | `initKeyboardShortcuts`, window triggers | `static/js/forkModuleShortcuts.js` | "Toggle Window" includes the module windows | no |
| `static/js/keyboard-shortcuts.js` | `initKeyboardShortcuts`, `_toolBtns` | `static/js/forkModuleShortcuts.js` | shortcut buttons | no |
| `static/js/settings.js` | imports | `static/js/forkModuleShortcuts.js` | import the fork's shortcut tables | no |
| `static/js/settings.js` | `SHORTCUT_DEFAULTS` | `static/js/forkModuleShortcuts.js` | defaults | no |
| `static/js/settings.js` | `SHORTCUT_ICONS` | `static/js/forkModuleShortcuts.js` | icons | no |
| `static/js/settings.js` | `SHORTCUT_LABELS` | `static/js/forkModuleShortcuts.js` | labels | no |
| `static/js/settings.js` | `SHORTCUT_CATEGORIES` | `static/js/forkModuleShortcuts.js` | the module windows under Open Tools | no |
| `static/js/slashCommands.js` | imports | `static/js/forkModuleShortcuts.js` | import `FORK_OPEN_TARGETS` | no |
| `static/js/slashCommands.js` | `_cmdOpen`, targets | `static/js/forkModuleShortcuts.js` | `/open records`, `meetings`, `today`, `write`, `wisdom` and their aliases | no |
| `tests/helpers/test_settings_shell_coordinator.mjs` | `REAL_MODULES` | `static/js/forkModuleShortcuts.js` | the settings.js import loads in the test shell | no |
| `tests/test_reserved_username_admin_escalation.py` | `test_legacy_reserved_username_session_cannot_pass_admin_gate` | none | evict the fresh `core.middleware` with `monkeypatch` | yes |

## Fork files

Every file the fork adds (code, tests, scripts, docs) is listed in
[docs/fork/files.md](docs/fork/files.md). It is generated and kept complete by
`tests/fork/test_fork_inventory.py`; regenerate it with
`UPDATE_FORK_FILES=1 python -m pytest -q tests/fork/test_fork_inventory.py` after adding,
moving or deleting a fork file.

The fork's Python code lives in the `fork/` package, apart from upstream's `src/` and
`routes/`: `fork/mcp/` (MCP client fixes), `fork/routing/` (what a turn is offered and
granted; `turn_policy.py` is what the chat route's hooks call), `fork/athena/` (the Athena module in the hub, its routes included),
`fork/beliefs/`, `fork/insight/`, `fork/ink/`, `fork/hub/` (fixes to upstream's own
features), `fork/routes/` (the other route files; `fork.routes.mount` includes all the
fork's routers) and `fork/common/` (shared helpers). `fork/tasks.py` is the table of the
fork's scheduled tasks; `fork/upstream.py` is the one place that reads upstream's private
`_` names (`tests/fork/test_fork_upstream.py` fails when one is gone upstream).
The old paths (`src/mcp_trust.py`, `routes/sound_module_routes.py`, …) are shims that
alias the new modules; `docs/fork/files.md` marks each one, and
`tests/fork/test_fork_package_shims.py` checks them. New code imports `fork.*`.

The fork's own tests are in `tests/fork/` (`python -m pytest -q tests/fork`), with shared
fixtures in `tests/fork/conftest.py` and fakes in `tests/fork/fakes.py`; upstream's tests
stay in `tests/`.

## Setup

1. Run it with Docker Compose using the fork's image and override:
   [docs/fork/deploy.md](docs/fork/deploy.md). Every Odysseus feature stays
   available; modules add to it.
2. Register each module: add its MCP server, add its name to
   `ODYSSEUS_TRUSTED_READONLY_MCP`, put its instructions in a Custom preset, and run
   `scripts/modules_setup.py` (it also creates the module's slash skill, e.g. `/athena`,
   and `/context`). Steps:
   [docs/fork/modules.md](docs/fork/modules.md#registering-a-module-in-odysseus);
   Athena specifics:
   [docs/fork/modules/athena.md](docs/fork/modules/athena.md#connect-it).
3. Update with `scripts/fork_deploy.sh deploy`; it updates and restarts the modules
   listed in `FORK_MODULES` first and re-runs `scripts/modules_setup.py`.

## Branches and upstream sync

`dev` mirrors upstream `dev` and is never committed to; `barvis` is upstream plus the
patches above and is the default branch; PRs go into `barvis`. The weekly merge,
conflict hot spots and post-merge checks are in
[docs/fork/upstream-sync.md](docs/fork/upstream-sync.md).

## License

AGPL-3.0, same as upstream. If others use a deployed instance of this fork over a network they
must be offered its source; this public repository does that. Modules such as
Athena are separate programs reached over MCP and are not covered by this
license.
