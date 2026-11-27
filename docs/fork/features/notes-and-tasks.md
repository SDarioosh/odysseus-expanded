# Notes, calendar and tasks

Fixes and additions around upstream's notes, calendar reminders and built-in tasks.

Each section below was a row of the "Changes from upstream" table in
[FORK.md](../../../FORK.md) until 2026-10-29, moved here word for word; FORK.md now lists
the features and the hooks into upstream files.

## Calendar reminder shows the wall time

**Calendar reminder shows the wall time.** The reminder to-do for a timed event stored in UTC (any event created with a `timezone`) said "Meet Jason — Wed Oct 21 10:00" for a 12:00 Berlin meeting. It now shows the time in the stated zone, else the user's. General bug fix.

Files: `src/tools/calendar.py` (`_create_calendar_reminder`, a few lines), test `tests/fork/test_calendar_reminder_wall_time.py`

Upstreamable: yes

## Calendar events in the wall time

**Calendar events in the wall time.** Asked about an upcoming event, the model gave the wrong hour: `manage_calendar` `list_events` showed stored times as UTC (`2026-11-05T15:00:00Z -> …` for a 16:00 Berlin meeting), and the model read them as local. Each line now gives the user's wall time (`2026-11-05 Thu 16:00 -> 17:00`), the answer names the zone, and each event row starts with `local_start` and `local_end` (the stored `dtstart`/`dtend` are unchanged). A voice turn (`/api/chat/voice/v1/chat/completions`) has no browser to send the zone, so it used the server's (UTC in Docker); it now uses the owner's: their personal assistant's timezone, else any of their crew members' (Settings). Without one, the server's zone is still used. General bug fix; the voice part is fork-only.

Files: `fork/common/local_time.py` (new), hook in `src/tools/calendar.py` (`list_events`), `fork/voice/routes.py` (`chat_completions`); test `tests/fork/test_calendar_local_time.py` (new)

Upstreamable: yes (the `list_events` part)

## Structured notes for token clients

**Structured notes for token clients.** `GET /api/codex/todos?format=json` (scope `todos:read`) returns the token owner's notes as data: id, title, type, label, pinned, due date, updated time, checklist items with their done flags (`checklist`, `todo` and `goal` notes, in stored order, since `toggle_item` addresses items by index), and plain-note content. Same selection and order as `manage_notes` `list` (not archived, calendar-reminder notes left out, pinned first, then most recently updated). Without `format=json` the route answers the chat-model text as before. For Athena's T5 PDA, whose phone relay shows the owner's checklists and ticks items through `toggle_item`; the notes UI routes refuse API tokens on purpose.

Files: `src/codex_todos_json.py` (new), hook in `routes/codex_routes.py` (`list_todos`); test `tests/fork/test_codex_todos_json.py` (new)

Since the 2026-11-24 sync, items are the note's task lines (see [the next section](#fork-code-reads-notes-as-upstreams-markdown-documents)) and each note also has `revision`, upstream's body revision that `toggle_item` checks. Upstream's `toggle_item` now refuses a call without the current `body_revision`; Athena's T5 relays send none, so a hook in `manage_todos` (`fill_toggle_revision`) fills in the current one for a `toggle_item` with an explicit `done`: the same index-based tick as before. A call without an explicit `done` (a flip) gets upstream's 409. The relays can send the feed's `revision` later.

Upstreamable: yes

## Fork code reads notes as upstream's markdown documents

**Fork code reads notes as upstream's markdown documents.** Upstream keeps a note as one markdown document since odysseus-dev/odysseus#7323: checklist items are task lines (`- [ ] milk`) and images are `![](url)` lines in `Note.content`, parsed by `core/notes_markdown.py`; a boot migration folds the legacy `items` and `image_url` columns into it (backing up each row to `note_legacy_fields`). `fork/common/note_items.py` is the one place fork code reads or writes a note's checklist (`note_items`, `set_note_items`, `is_checklist`, `note_type`, `prose_of`); a row the migration skipped is folded on read. A checklist made in the notes window is stored as `note`, so a note counts as a checklist by its stored type (`checklist`, `todo`, `goal`) or when its content is a pure task list (upstream's `derive_note_type`); the fork's SQL filters on `note_type` moved into Python. Users: Athena's filed to-dos and named lists, voice Done on the Echos, finished-checklist archiving, the T5 todos feed, the Today insight and its weights, and notes evidence (which reads a note's prose without its task and image lines). Write's handwritten notes put their page image into `content` the way upstream's note route does.

Files: `fork/common/note_items.py` (new); callers in `fork/athena/action_items_lists.py`, `fork/voice/done.py`, `fork/voice/confirm.py`, `fork/hub/checklist_auto_archive.py`, `fork/hub/codex_todos_json.py`, `fork/insight/today_candidates.py`, `fork/insight/today_gather.py`, `fork/insight/weights.py`, `fork/beliefs/notes_evidence.py`, `fork/ink/notes.py`; test `tests/fork/test_note_items.py` (new)

Upstreamable: no (fork code)

## Finished checklists archive themselves

**Finished checklists archive themselves.** A checklist whose items were all done stayed on the notes page and on Athena's T5 PDA until archived by hand, taking room from new lists. About once a minute, an unpinned checklist (`checklist`, `todo` or `goal` note) with at least one item, every item done, and no change for `FORK_CHECKLIST_ARCHIVE_MINUTES` (default 10) is archived, so it also leaves the T5 at its next sync. Unticking an item within the wait keeps it; after that it is in the notes archive. Unarchiving counts as a change, so a list brought back is archived again after the wait unless it has an open item. Pinned lists (finished or empty), calendar-reminder notes and repeating notes are never touched. `0` or `off` turns it off.

Files: `src/checklist_auto_archive.py` (new), hook in `app.py` (startup, next to the background-job monitor); test `tests/fork/test_checklist_auto_archive.py` (new)

Upstreamable: with discussion

## Built-in tasks recover from a broken sealed authority

**Built-in tasks recover from a broken sealed authority.** Every scheduled action runs only with the request authority sealed into its row; when a built-in housekeeping task's snapshot is missing, unreadable, sealed for another owner, prompt or action, or grants nothing, each run failed with "Scheduled action has no matching server request authority" and an error notification. The server now re-seals it exactly as seeding does, only for the owner's single row of a `HOUSEKEEPING_DEFAULTS` action with an empty prompt (or the email page's `{"account_id"}`), never inside an active request authority and never past a policy denial (`disabled_tools`); it logs why. Any other denied run says why in its error.

Files: `src/builtin_task_authority.py` (new), hook in `src/task_scheduler.py` (`_execute_action`); test `tests/fork/test_builtin_task_authority.py` (new)

Upstreamable: yes
