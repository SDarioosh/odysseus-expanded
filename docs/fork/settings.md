# Fork settings

Generated from the table in `fork/common/settings.py` by
`tests/fork/test_fork_settings.py`; don't edit by hand. After changing a row:

```bash
UPDATE_FORK_SETTINGS=1 python -m pytest -q tests/fork/test_fork_settings.py
```

Environment variables the fork's code reads, read at call time. Former names are
still read when the current one is unset. An empty value means unset unless the
row says otherwise. Production passes them from `.env` through
`docker/fork.override.yml` ([deploy.md](deploy.md#fork-settings-in-env)). On/off
settings that are on by default are off for `0`, `false`, `no` or `off`; those
that are off by default are on for `1`, `true`, `yes` or `on` (any case).

| Variable | Former names | Type | Unset means | Meaning | Used in |
|---|---|---|---|---|---|
| `MODULE_ACTION_ITEMS_SERVER` | `BARVIS_ACTION_ITEMS_SERVER` | text | `convo` | The Athena MCP server (name or id) for to-dos, facts, today insight and the beliefs page; compared case-insensitively. | `fork/athena/action_items.py`, `fork/athena/client.py`, `fork/athena/facts.py`, `fork/insight/today.py`, `fork/mcp/session_recovery.py`, `fork/routes/beliefs_routes.py` |
| `MODULE_NOTIFY_CHANNEL` | `BARVIS_NOTIFY_CHANNEL` | ntfy, browser or off | `ntfy` | Where Athena's to-do pushes go; anything else means ntfy. | `fork/athena/action_items.py` |
| `MODULE_DIGEST_HOUR` | `BARVIS_DIGEST_HOUR` | hour 0-23 or off | `8` | Local hour after which the daily pushes go out, and the time of to-dos with a date only; off or none: no daily pushes. Out of range is clamped; not a number: 8. | `fork/athena/action_items.py` |
| `MODULE_FILE_ITEMS` | `BARVIS_FILE_ITEMS` | explicit, auto or off | `explicit` | Which voice-note items are filed as to-dos and events: explicit ones, all (auto or all) or none (off, no, false, 0). | `fork/athena/action_items.py` |
| `MODULE_FACTS_SYNC` | `BARVIS_FACTS_SYNC` | on/off | `on` | Copy Athena's confirmed facts into the belief ledger. | `fork/athena/facts.py` |
| `SOUND_MODULE_SERVER` | `BARVIS_SOUND_SERVER` | text | `convo` | The Athena MCP server (name or id) for uploads, the Athena page, the meeting bot and the module's other HTTP routes. | `fork/athena/call_recorder.py`, `fork/athena/client.py`, `fork/athena/routes.py` |
| `SOUND_MODULE_CALL_DEVICE` | - | device name | `computer` | The device name recordings from the hub are uploaded under; an invalid name means computer. | `fork/athena/call_recorder.py` |
| `BELIEF_CONFIRM_SOURCES` | - | integer >= 1 | `2` | Distinct sources that confirm a belief. | `fork/beliefs/ledger_model.py` |
| `BELIEF_CONFIRM_TIMES` | - | integer >= 1 | `3` | Sightings that confirm a belief (with BELIEF_CONFIRM_DAYS). | `fork/beliefs/ledger_model.py` |
| `BELIEF_CONFIRM_DAYS` | - | integer >= 1 | `2` | Distinct days those sightings must span. | `fork/beliefs/ledger_model.py` |
| `BELIEF_STALE_DAYS` | - | integer >= 1 | `180` | Days without a sighting after which a belief goes stale. | `fork/beliefs/ledger_model.py` |
| `BELIEF_CONSOLIDATE` | - | on/off | `on` | Merge beliefs that say the same thing. | `fork/beliefs/consolidation.py` |
| `BELIEF_STRUCTURE` | - | on/off | `on` | Give beliefs an attribute, value and validity with the local model. | `fork/beliefs/structure.py` |
| `BELIEF_STRUCTURE_BATCH` | - | integer 1-10 | `5` | Beliefs per model call when structuring; clamped to 1-10. | `fork/beliefs/structure.py` |
| `EVIDENCE_NOTES` | - | on/off | `off` | Read facts from the owner's notes into the belief ledger. | `fork/beliefs/notes_evidence.py` |
| `TODAY_INSIGHT_ENABLED` | - | on/off | `on` | Write the daily insight. | `fork/insight/today.py` |
| `TODAY_INSIGHT_SKIP_LISTS` | - | comma-separated list titles | `Shopping,Groceries,Grocery` | Lists left out of the insight unless the note has a due date; empty: none. An empty value is a value of its own. | `fork/insight/today.py` |
| `TODAY_INSIGHT_PUSH` | - | on/off | `off` | Push the daily insight to the phone. | `fork/insight/push.py` |
| `ODYSSEUS_PERSONAL_READ_GRANTS` | - | on/off | `1` | Grant read-only access to the owner's personal stores on every turn. | `fork/routing/personal_read_grants.py` |
| `ODYSSEUS_PERSONAL_STORE_ESCALATION` | - | on/off | `1` | Run chat-mode lookups in the owner's personal stores in agent mode. | `fork/routing/personal_store_escalation.py` |
| `ODYSSEUS_TRUSTED_READONLY_MCP` | - | comma-separated server names or ids | (empty) | MCP servers whose readOnlyHint tools run without approval. Production sets `convo` in the override. | `fork/mcp/trust.py` |
| `FORK_CHECKLIST_ARCHIVE_MINUTES` | - | minutes or off | (empty) | Minutes a finished checklist waits before it is archived (default 10); 0, off, no, false or none: never. | `fork/hub/checklist_auto_archive.py` |
| `INK_AUTO_SEND` | - | on/off | (empty) | Send pages the model read without [?] to Athena without review. | `fork/ink/transcribe.py` |
| `APP_PUBLIC_URL` | - | URL | (empty) | The hub's address for links in notes sent to Athena when Settings has no Public URL (upstream's MCP OAuth redirect reads it too). | `fork/ink/athena.py` |
