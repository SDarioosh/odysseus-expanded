# Notes, calendar and tasks

Fixes and additions around upstream's notes, calendar reminders and built-in tasks.

Each section below was a row of the "Changes from upstream" table in
[FORK.md](../../../FORK.md) until 2026-10-29, moved here word for word; FORK.md now lists
the features and the hooks into upstream files.

## Calendar reminder shows the wall time

**Calendar reminder shows the wall time.** The reminder to-do for a timed event stored in UTC (any event created with a `timezone`) said "Meet Jason — Wed Oct 21 10:00" for a 12:00 Berlin meeting. It now shows the time in the stated zone, else the user's. General bug fix.

Files: `src/tools/calendar.py` (`_create_calendar_reminder`, a few lines), test `tests/fork/test_calendar_reminder_wall_time.py`

Upstreamable: yes

## Structured notes for token clients

**Structured notes for token clients.** `GET /api/codex/todos?format=json` (scope `todos:read`) returns the token owner's notes as data: id, title, type, label, pinned, due date, updated time, checklist items with their done flags (`checklist`, `todo` and `goal` notes, in stored order, since `toggle_item` addresses items by index), and plain-note content. Same selection and order as `manage_notes` `list` (not archived, calendar-reminder notes left out, pinned first, then most recently updated). Without `format=json` the route answers the chat-model text as before. For Athena's T5 PDA, whose phone relay shows the owner's checklists and ticks items through `toggle_item`; the notes UI routes refuse API tokens on purpose.

Files: `src/codex_todos_json.py` (new), hook in `routes/codex_routes.py` (`list_todos`); test `tests/fork/test_codex_todos_json.py` (new)

Upstreamable: yes

## Finished checklists archive themselves

**Finished checklists archive themselves.** A checklist whose items were all done stayed on the notes page and on Athena's T5 PDA until archived by hand, taking room from new lists. About once a minute, an unpinned checklist (`checklist`, `todo` or `goal` note) with at least one item, every item done, and no change for `FORK_CHECKLIST_ARCHIVE_MINUTES` (default 10) is archived, so it also leaves the T5 at its next sync. Unticking an item within the wait keeps it; after that it is in the notes archive. Unarchiving counts as a change, so a list brought back is archived again after the wait unless it has an open item. Pinned lists (finished or empty), calendar-reminder notes and repeating notes are never touched. `0` or `off` turns it off.

Files: `src/checklist_auto_archive.py` (new), hook in `app.py` (startup, next to the background-job monitor); test `tests/fork/test_checklist_auto_archive.py` (new)

Upstreamable: with discussion

## Built-in tasks recover from a broken sealed authority

**Built-in tasks recover from a broken sealed authority.** Every scheduled action runs only with the request authority sealed into its row; when a built-in housekeeping task's snapshot is missing, unreadable, sealed for another owner, prompt or action, or grants nothing, each run failed with "Scheduled action has no matching server request authority" and an error notification. The server now re-seals it exactly as seeding does, only for the owner's single row of a `HOUSEKEEPING_DEFAULTS` action with an empty prompt (or the email page's `{"account_id"}`), never inside an active request authority and never past a policy denial (`disabled_tools`); it logs why. Any other denied run says why in its error.

Files: `src/builtin_task_authority.py` (new), hook in `src/task_scheduler.py` (`_execute_action`); test `tests/fork/test_builtin_task_authority.py` (new)

Upstreamable: yes
