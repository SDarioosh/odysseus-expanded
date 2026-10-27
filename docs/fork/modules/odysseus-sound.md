# Module: Athena (formerly odysseus-sound)

- Repo: [SDarioosh/barvis-sound](https://github.com/SDarioosh/barvis-sound) (renamed from
  `barvis-ai`; older docs call it "convo-ai"). Default branch `main`.
- Agent guide: [AGENTS.md](https://github.com/SDarioosh/barvis-sound/blob/main/AGENTS.md).
  Server docs: [server/README.md](https://github.com/SDarioosh/barvis-sound/blob/main/server/README.md).
- Contract it follows: [../modules.md](../modules.md).

Code identifiers keep the old name: the Python package `convo_server`, the CLI
`convo-server`, `CONVO_*` env vars, the MCP server name `convo-ai`, and the Odysseus server
name `convo`. Don't rename them.

## What it provides

Wearables record conversations (Amazfit T-Rex 3 Pro watch app now; a XIAO ESP32-S3
pendant planned; an Omi pendant later). The Athena server on the owner's machine
detects speech (Silero VAD), transcribes (whisper.cpp), identifies speakers, splits
recordings into conversations, and summarizes them with the local LLM (title, summary,
key points, action items, people, topics, facts, daily digest). Everything is stored in
SQLite (`server/data/convo.db`) and exposed through an MCP server.

## Tools

Source: `server/convo_server/mcp_server.py` (`build_mcp`).

| Tool | Arguments | Annotations |
|---|---|---|
| `search_transcripts` | `speaker`, `query`, `since`, `until`, `limit` | read-only |
| `search_conversations` | `query`, `person`, `since`, `until`, `kind` (note/conversation/empty), `limit` | read-only |
| `get_conversation` | `conversation_id` | read-only |
| `get_transcript` | `conversation_id`, `start_seconds`, `end_seconds`, `max_lines` | read-only |
| `list_action_items` | `status` (open/done/dismissed/all), `person`, `since`, `until` | read-only |
| `list_facts` | `subject`, `query`, `limit`, `confirmed` (only facts the module has confirmed), `format` (`json` for the facts sync) | read-only |
| `get_daily_digest` | `date` | read-only |
| `list_people` | — | read-only |
| `list_unnamed_speakers` | `limit` | read-only |
| `update_action_item` | `item_id`, `status` (open/done/dismissed) | **write** |
| `name_speaker` | `conversation_id`, `speaker_label`, `name`, `remember_voice` | **write** |
| `reassign_line` | `line_id`, `name` | **write** |
| `rename_speaker_everywhere` | `old_name`, `new_name` | **write, destructive** |

- Read-only tools: `readOnlyHint: true`, `openWorldHint: false`. With `convo` in
  `ODYSSEUS_TRUSTED_READONLY_MCP`, Odysseus runs them without approval.
- Write tools: `readOnlyHint: false`, `destructiveHint: false`, `idempotentHint: true`,
  `openWorldHint: false`. `rename_speaker_everywhere` has `destructiveHint: true`
  (merging two names can't be undone). They keep Odysseus' normal approval.
- `name_speaker` renames the speaker's lines, redoes the summary and (with
  `remember_voice`) adds a voice sample; since MCP version 0.2.0 it also renames
  unnamed speakers in other conversations whose voice clearly matches, and says how many.
- `get_transcript` and `search_transcripts` lines start with `#<line id>`, the
  `line_id` for `reassign_line` (fix one line given to the wrong person).
- `get_transcript` prints `"<title>" <YYYY-MM-DD HH:MM:SS> <link>`, then one
  `#<line id> [<seconds>s] <speaker>: <text>` per line, 100 lines a page by default.
  Modules before 2026-10-20 printed `[<seconds>s <HH:MM:SS>]`, minute-precision start
  times and 200 lines; `src/sound_module_view.py` reads both. The result and the
  `INSTRUCTIONS` tell the model to answer "show me the transcript" with the link and a
  short summary instead of retyping the lines. Retyping was where a `/sound` transcript
  request spent its time (sandbox: [Transcript requests](../sandbox.md#transcript-requests-2026-10-20)).
- Voice notes (the owner recording a memo for themselves) are stored as their own kind.
  Results mark them `"<title>" (voice note)`, and `search_conversations` with
  `kind="note"` returns only voice notes (`"conversation"`: only the others; empty: both).
  The server `INSTRUCTIONS` tell the model to use it for "my voice notes / notes / memos";
  without it, "my latest voice notes" got the latest conversations.
- Written notes (MCP 0.12.0, additive): a handwritten note sent from the hub's Write view
  ([Handwritten notes](#handwritten-notes-written-notes)) is a conversation of kind `note`
  on device `handwriting`. Text results mark it, its facts and its action items
  `(written by hand: <link>)` instead of `(voice note)`, where `<link>` is the note in the
  hub (`<hub>/#open=notes&note=<id>`).
- `list_unnamed_speakers` lists voices nobody has named yet ("Speaker N") with a few
  quotes, links and the closest known voice; for "who is this?" questions and cards.
- Dates are `YYYY-MM-DD` in `CONVO_TIMEZONE`.
- Links in results have the form `<CONVO_PUBLIC_URL>/view/<conversation_id>?sig=…#t=<seconds>`:
  a signed HTML transcript page that opens in a phone browser without a token. Rotating
  `CONVO_INGEST_TOKEN` revokes all links.
- Every non-empty result asks the model to put the link next to each fact used.

## Connect it

Environment for the MCP server (read from the environment or `server/.env`):

| Variable | Meaning |
|---|---|
| `CONVO_INGEST_TOKEN` | required; also the key that signs transcript links |
| `CONVO_DATA_DIR` | data directory holding `convo.db` |
| `CONVO_TIMEZONE` | timezone for dates (default `UTC`) |
| `CONVO_PUBLIC_URL` | base URL of links; use the address the phone reaches (Tailscale IP) |
| `CONVO_MCP_TOKEN` | HTTP only; ≥ 16 chars, separate from the ingest token |
| `CONVO_MCP_HOST`, `CONVO_MCP_PORT` | HTTP only; default `127.0.0.1`, `8001` |

**Streamable HTTP** (use this; it is what the sandbox stack and the owner's Docker setup
use):

```bash
cd server
CONVO_MCP_TOKEN=... CONVO_MCP_HOST=172.17.0.1 .venv/bin/convo-server mcp --http   # docker bridge IP
```

On the owner's server `odysseus-deploy deploy` runs this as a background service
(Athena's `server/scripts/deploy.sh`, settings in `server/.env`;
[../deploy.md](../deploy.md#one-command-setup-once)).

Then add an HTTP server named `convo` with URL
`http://host.docker.internal:8001/mcp?access_token=<CONVO_MCP_TOKEN>` (Odysseus in Docker)
or `http://127.0.0.1:8001/mcp?access_token=<CONVO_MCP_TOKEN>` (Odysseus native, e.g. the
sandbox).

**stdio** (`convo-server mcp`, args `["mcp"]`, with the `CONVO_*` environment) works for a
native Odysseus since stdio connections got their own owner task (before, every call after
the adding request failed; [../sandbox.md](../sandbox.md#findings-from-the-first-sandbox-run),
finding 2). HTTP stays the documented setup: Odysseus in Docker can't spawn `convo-server`.

Then, per [../modules.md](../modules.md#registering-a-module-in-odysseus):

1. `ODYSSEUS_TRUSTED_READONLY_MCP=convo`, restart Odysseus (production: already in
   `docker-compose.override.yml`, see [../deploy.md](../deploy.md#features-and-modules)).
2. Paste the server's `INSTRUCTIONS` text (top of `mcp_server.py`) into a Custom preset
   system prompt.
3. `python scripts/modules_setup.py --url http://127.0.0.1:7010 --password-file <file>`
   (sandbox, or the server's test instance; on production `fork_deploy.sh` runs it).
   It creates the `/sound` and `/context` skills from that preset text.

## Slash skill: `/sound`

- Skill **`sound`**, server **`convo`**: `/sound <question>` gives that one message
  Athena's 13 tools (plus `ask_user`/`update_plan`) and nothing else. `/context
  <question>` does the same for every trusted module (today only `convo`).
- The skill's procedure is `INSTRUCTIONS` from `server/convo_server/mcp_server.py`, the
  same text as the Custom preset. Athena owns it: change it there, update the
  preset, rerun `modules_setup.py`.
- Needed because with every Odysseus tool on and "Auto", a plain recording question
  is routed to built-ins (calendar, files) and never sees a convo tool. Sandbox
  numbers: [../sandbox.md](../sandbox.md#slash-skills-2026-10-18).
- Examples: `/sound what did Alice say about the budget in September?`,
  `/context what did Maria ask me to do on Friday?` The model gets today's date on
  every turn, so relative dates work.
- Write tools (`update_action_item`, `name_speaker`) are offered on `/sound` turns and
  still ask for approval.
- Follow-ups in the same chat don't need `/sound` again: once a convo tool has run in a
  chat, every later message there is offered all 13 tools (write tools still ask). So
  "Speaker 1 and 3 were Marisol, speaker 2 was Kath" after `/sound what did we talk about
  last night?` calls `name_speaker`.
- Approving a write tool once covers more calls of that same tool in the same reply:
  one click renames all three speakers. A different write tool in that reply is still
  refused; ask for it in a new message.

## Action items in Odysseus

Athena stays the source of truth for action items. Since 2026-10-20 (owner's
decision, replacing "no copies" of 2026-10-19) Odysseus also **files** them where they
belong, so voice notes land in the hub's own calendar and to-dos. Since 2026-10-21 only
**explicit** voice-note items are filed by default; a reflective voice note ("stop
prioritizing side projects…") gives suggestions, not four to-dos.

| `MODULE_FILE_ITEMS` | Voice-note items | Conversation items |
|---|---|---|
| `explicit` (default) | filed when explicit (below); the others are suggestions, with one push per pass: "Suggested from your voice note" and their short titles | suggestions |
| `auto` | all filed (the behaviour of 2026-10-20) | suggestions |
| `off` | not filed; "new action item" pushes only (the behaviour before filing) | pushes only |

What counts as a spoken command is the module's: Athena catches to-do, list and note
commands in code even when its model lists no item, with fillers or "can you" before
them, several in one sentence, and lists said line by line ("Make a to-do list. Get
eggs. Fix the car."); a comma list for a named list ("add eggs, milk and bread to my
shopping list") is one line each. Phrasings: Athena's `server/README.md`; its
`server/eval/voice_commands.py` checks ~100 fictional voice notes against these rules.

An item is **explicit** when the module says so: `list_action_items format=json` rows
carry `explicit` (true when the item has a due date or time, a `list_name`, kind
`reminder` or `event`, or the voice note used a command word). With an older module
that lacks the field Odysseus derives it the same way, the command words being "remind
me", "todo"/"to-do", "task" and "add … to my … list". Notes (kind `note`) are always
explicit: the module makes them only on a command. Intentions and principles are no
action items at all any more: the module turns them into facts, which reach the
[belief ledger](../beliefs.md).

Where a filed item goes:

| Item kind (module) | Example | Filed as |
|---|---|---|
| `event` | "meeting with Jason on Wednesday at 12 pm" | calendar event in the default calendar: timed (60 min or the stated length, reminder 30 min before), or all-day when no time was said |
| `task` with a date | "get the proposal done on Wednesday" | to-do (checklist note, label `odysseus-sound`), due that day at `MODULE_DIGEST_HOUR`, titled with the short title, the full text as its line |
| `task` without a date or list | "add a to-do: oil the bike chain" | one line (the short title) on the to-do list **Voice notes** (see below) |
| `reminder` | "remind me at 5 to call Mom" | to-do due at that time (Odysseus fires its own reminder), titled like a dated task |
| any task or reminder with a `list_name` | "add milk to my shopping list" | one line on that to-do list (see below) |
| `note` | "create a note battery dimensions are 13mm x 14mm x 3mm" | an Odysseus note (type note, label `odysseus-sound`) titled with the module's `note_title` ("Battery dimensions"), the text, the voice note's key points as a list (MCP 0.15.0 `key_points`, when the note is the whole voice note) and the source link as content |
| `note` with a `list_name` | "add to my hardware notes: the screen is 1.4 inch" | one line `- The screen is 1.4 inch` on those notes (see below) |

- **Named to-do lists.** When the note names a list, the module returns `list_name`
  (`shopping`, without "my"/"the"/"list") and the item text without the phrase
  (`Milk`). Odysseus adds it as a line to the open checklist or to-do note with that
  title, in any case and with or without "list" ("Shopping", "shopping list"); if there
  is none, it creates a to-do list titled "Shopping" (label `odysseus-sound`). A due date
  goes into the line's text ("Eggs (due Wed 22 Oct)"); lines have no reminder of their
  own. An open line with the same text isn't added twice. The line's status follows
  both ways: ticked ↔ done, line removed → dismissed, list archived → done. Undo and
  moving take off the line only; the list stays. On `/modules/sound`, **Add to a list**
  / **Change list** picks a list (your existing lists are suggested) or clears it.
  A list whose lines are all ticked is archived 10 minutes later unless it is pinned
  (`src/checklist_auto_archive.py`), so the next "add … to my shopping list" starts a
  new one; pin a list to keep it.

- **Short titles.** Rows carry `title` (at most 60 characters, a short imperative:
  "Send battery measurements"). A to-do of its own is titled with it and holds the full
  text as its one line; pushes and "Voice notes" lines use it too. An older module
  without `title`: the start of the text, cut at a word (60 characters at most).
- **The "Voice notes" list.** In every mode, a voice-note task without a due date and
  without a `list_name` is one line on the to-do list titled "Voice notes" (label
  `odysseus-sound`, made if missing, not pinned), instead of a to-do of its own. It
  follows the named-list rules above: status per line both ways, Undo takes the line
  only, and once every line is ticked the list is archived 10 minutes later, so the next
  undated task starts a new "Voice notes" list. When its last line is taken off (Undo,
  moved elsewhere) the empty list is archived at once, unless it is pinned. Tapping **Add to to-dos** on such a
  suggestion puts it there too.
- **Notes.** Voice notes only, and only on an explicit command ("create a note",
  "make a note", "note that", "note:", "note to self", "new note:", "add to my notes:",
  "add to my … notes"); a plain statement makes no note. Named notes may also be said
  without the word "notes" right after the command ("make a note in voice recorder that
  the case needs to be 3 mm bigger" → a line in "Voice recorder notes"; "… under car,
  …", "add to my car note: …").
  A note with a `list_name` (`hardware`) is added as a line to the open note titled
  with that name, with or without "notes" ("Hardware notes", "hardware"); if there is
  none, Odysseus creates "Hardware notes". The same line isn't added twice. Once filed
  the module item is marked done (a note is no to-do; the module's text listing, digest
  and HTTP list leave notes out anyway), and status isn't synced after that: edit,
  archive or delete the note in Odysseus as you like. Undo on `/modules/sound` (shown
  while the item is still open) removes the note, or only its line. A note whose
  filing failed waits there with **Add to notes**.
- **Explicit voice-note items are filed automatically.** One push per pass ("Filed
  from your voice note": what went where, "check the time" when the module flagged it).
  Filed items get no other "new item" or "due today" push; Odysseus' own event and to-do
  reminders take over.
- **Everything else suggests.** Conversation items, and voice-note items that aren't
  explicit, wait on `/modules/sound` with **Add to calendar** / **Add to to-dos** /
  **Dismiss**; nothing is filed until tapped. Conversation items are not pushed;
  voice-note suggestions get one "Suggested from your voice note" push per pass.
- **Fix or undo** on `/modules/sound`: Move to calendar / Move to to-dos, Change time,
  Undo (removes the Odysseus entry and dismisses the item). An item is filed once: if
  you delete the event or note in Odysseus it is not filed again.
- **Status both ways**: ticking the to-do (or archiving it) marks the module item done,
  and the other way round; deleting the event dismisses the item; a past event is done.
- The first run only remembers existing items (no backlog is filed).
- **Clean-up of the to-dos filed one per item** (before 2026-10-21 every voice-note
  item became a to-do of its own, titled with the whole sentence). Undo on
  `/modules/sound` removes one. For all of them, run
  `python scripts/unfile_voice_note_todos.py --url <hub> --password-file <admin password file>`:
  it lists the to-dos it would remove, each with why it qualifies: a to-do of its own
  (label `odysseus-sound`, one line, not a list line) of a voice-note item that is not
  explicit now, with no due date and no reminder, still in Odysseus, and filed before
  explicit filing (or in `auto` mode). Dated to-dos and reminders never qualify, so a
  reminder filed today or an old item the owner since gave a date stays. To-dos filed in
  `explicit` mode or from the page (Add / Move, recorded as `filed_mode`) are left out
  unless `--all`. Add `--apply` to delete them and dismiss their open items (done items
  stay done), or `--apply --only 12,14` for some. It calls the admin route
  `POST /api/modules/sound/unfile-todos` (`{"apply": false, "ids": [...], "all": false}`),
  which runs in the hub because the hub holds the module's MCP connection. Removed items
  are never filed again.

The built-in task **Module Action Items** (`src/module_action_items.py`, every 5 min)
does all of this. It reads `list_action_items` with `format="json"` (fields in the module's
`server/README.md`; from MCP 0.12.0 each row also has `written_by_hand` and `note_link`,
true and the hub note's URL for an item from a handwritten note, else false and null) and writes through `update_action_item` (`status`, `kind`,
`due_date`, `due_time`, `list_name`). With an older module that returns only text it falls back to
pushes without filing. The module's `due_date`/`due_time` are local to its
`CONVO_TIMEZONE`; set the same timezone in Odysseus (the task logs a warning when they
differ and converts).

Settings: `MODULE_FILE_ITEMS` (`explicit` default, `auto`, `off`; table above),
`MODULE_ACTION_ITEMS_SERVER` (default `convo`), `MODULE_NOTIFY_CHANNEL` (`ntfy`,
`browser`, `off`), `MODULE_DIGEST_HOUR` (`8`, `off`). ntfy needs an enabled ntfy
integration (Settings → Integrations); the topic is the reminder topic
(`reminder_ntfy_topic`).

The text lines of `list_action_items` (what models read) are still parsed as a fallback:
`- #<id> [<status>] [<kind>: ]<text>[ (owner <name>)][ due <YYYY-MM-DD>[ <HH:MM>][ (<as said>)]| due <as said>][ [time unclear]] — from "<title>"[ (voice note)] <YYYY-MM-DD HH:MM> <link>`
(`<kind>` is `event` or `reminder`; tasks have none). Change it only together with the
parsers and their tests.

## Facts in Odysseus memory

Confirmed facts also live in Odysseus memory, so chat context and the Memory page know
them without a module call. Since Phase 1 they pass through the hub's **belief ledger**
([../beliefs.md](../beliefs.md)): the module's facts are evidence, and the ledger decides
what is confirmed. The built-in task **Module Facts** (`src/module_facts.py`, hourly,
posts nothing to chat) calls `list_facts` with `{"format": "json", "limit": 1000}` (every
current fact; 1000 is the JSON mode's cap, so no fact is cut off and mistaken for
retired), which returns `{"facts": [{"id", "subject", "about" (owner/person/other),
"fact", "times_said", "days", "first_said", "last_said", "link", "confirmed"}]}`, and
from MCP 0.11.0 `kind`, `conversation_id` and `quote` (the transcript line), which label
and group the facts on the review page ([beliefs.md](../beliefs.md#the-athena-facts-job)).
From MCP 0.12.0 each row also has `written_by_hand` and `note_link` (true and the hub
note's URL for a fact from a handwritten note, else false and null); the facts job
doesn't read them yet, and the fact's `link` is still the signed transcript link. From
MCP 0.13.0 each row also has `valid_until` (additive).

- **Evidence per fact**: source `odysseus-sound`, source id = the fact id, the fact text,
  the signed transcript link, `first_said`/`last_said` as seen dates, `times_said`/`days`
  as totals, and the row's `confirmed` (the module's own threshold) as the source's
  verdict. A module that doesn't send `confirmed` per row yet gets one more call,
  `{"confirmed": true, "format": "json", "limit": 1000}`, whose ids are the confirmed
  set.
- **Facts about one occasion end**: the row's `valid_until` (the last day a fact like
  "has a meeting with the Meshtastic team on Tuesday at 11 a.m." is worth keeping; null
  for lasting facts) travels as `Evidence.valid_until`. The belief goes `stale` the day
  after and leaves memory, even while the module still returns the fact. A module older
  than 0.13.0 sends none: nothing ends early.
- **Confirmed beliefs become memories**: confirmed by the module's flag, by a second
  source saying the same thing (`BELIEF_CONFIRM_SOURCES`), by enough sightings on enough
  days, or by the owner. One memory per belief, for the task's owner: text = the fact
  text (or the owner's correction) followed by ` (source: <link>)`; fields `source:
  "odysseus-sound"`, `belief_id`, `module_fact_id`, `link`, `subject`, `about`,
  `times_said`, `days`, `first_said`, `last_said`, `first_seen`, `last_seen` and
  `synced_at`. The memory text ends with the transcript link so chat answers can cite it
  (chat context injects only a memory's text). Copies written before the ledger are
  adopted (same memory id and use count), not duplicated.
- **Category** (from the bare fact text, not the link): about the owner → `preference`
  when it has a preference word
  (like(s), prefer(s), love(s), hate(s), enjoy(s), favorite/favourite, want(s),
  dislike), else `fact`; about a person → `contact`; anything else → `fact`.
- **Full reconcile every run**: a changed fact (text or metadata) updates its belief and
  memory in place (same memory id and use count); a fact the module no longer returns is
  marked gone, and its belief goes stale and leaves memory unless the owner confirmed
  it; beliefs not heard of for `BELIEF_STALE_DAYS` (180) go stale. Running twice
  changes nothing. Memory Tidy may merge or delete copies like any memory; the next run
  puts them back. The owner's own memories and other owners' copies are never touched.
- **Forgetting** (4.2): when the owner forgets a belief in Odysseus, the hub calls the
  module's `erase_fact {"fact_id", "reason"}` (MCP 0.10.0, write, destructive): the fact
  is retired and its words leave the module (the transcript line that said it reads
  `[forgotten]`, the notes and digests are rewritten without it, later repeats are
  erased too). A module older than 0.10.0 gets `retire_fact {"fact_id", "reason"}`
  instead (hidden, words kept) and the facts job tries `erase_fact` again every pass; a
  module with neither is tolerated (the hub keeps the belief rejected and erased).
  [beliefs.md](../beliefs.md#complete-forget-42).
- **Retention** (module setting): `CONVO_TRANSCRIPT_RETENTION_DAYS` (default 0 = keep
  forever) deletes old transcript lines and kept audio; summaries, facts and action items
  stay, `get_transcript` and the transcript page say the lines were removed.
- The result line has counts only (`facts: 12 seen, 1 new, 1 confirmed, 0 unconfirmed,
  0 gone, 0 stale; memory: 11 kept, 1 added, 0 updated, 0 removed`); a run with no change is not logged
  in Activity. An older module that answers `list_facts` with text: nothing is synced
  (logged once).
- Settings: `MODULE_FACTS_SYNC` (`on` default, `off` stops the sync; memories already
  mirrored stay), the action-item task's `MODULE_ACTION_ITEMS_SERVER` (default `convo`),
  and the ledger's `BELIEF_*` settings ([../beliefs.md](../beliefs.md#settings)).

## Conversations window

**Conversations** (sidebar, icon rail, `/open conversations`, an optional shortcut; admin
only) opens a tool window in the app; `/modules/sound` shows the same view as its own page
(`static/js/forkModuleWindows.js`, `static/js/soundModule.js`). It shows open action items live
(Done / Dismiss call `update_action_item`; Odysseus keeps no copy), the module's
conversations (filter by words, person, dates and type: all, conversations, voice notes;
voice notes are labelled), and a "who is this?" card for every unnamed speaker. **Open** reads one
conversation in place (`get_conversation`, `get_transcript` 200 lines a page); tapping a
speaker fixes one line (`reassign_line`), a whole unnamed `Speaker N` (`name_speaker`) or
a name everywhere (`rename_speaker_everywhere`, confirmed first). **Ask in chat** puts
`/sound About the conversation "<title>" (id <id>, <start>): ` into a new chat for the
owner to finish and send (the page links to `/?ask=…`). It calls
`list_action_items`, `update_action_item`, `search_conversations`, `get_conversation`,
`get_transcript`, `reassign_line`, `rename_speaker_everywhere`,
`list_unnamed_speakers` and `name_speaker` through the MCP
manager and parses their result lines (`src/sound_module_view.py`, test
`tests/test_sound_module_view.py`); change those line shapes together with the parser.
`SOUND_MODULE_SERVER` names the server (default `convo`). A signed `/view` link to a
moment (`#t=<s>`), clicked in chat or in the view (quotes, line times, "where it was
said"), opens the reader at that line; the **Transcript** buttons and a modified click
open the module's page in a new tab (Odysseus' CSP doesn't allow framing another origin).
This relies on the link format in [../modules.md](../modules.md): a `/view/<id>` path
with a `sig` parameter.
Audio isn't kept after transcription, so cards quote lines instead of playing them.

## Recording calls from the computer

`/modules/sound/record` (**Record a call** on the Conversations page, admin only) records
a Zoom, Meet or Teams call on any computer with a Chromium browser (Chrome, Brave, Edge)
and sends it to the module, which transcribes, diarizes and summarizes it like any other
conversation (device `computer`, kind `conversation`).

- **What it captures.** The browser asks what to share. A meeting in a tab: pick the tab
  and turn on *Also share tab audio* (Windows, Mac, Linux). The Zoom desktop app: pick
  its window or the entire screen and turn on *Also share system audio* (Windows; macOS
  14.2+ with Chrome 141+; Linux depends on the desktop, untested). Plus the microphone
  (a checkbox, remembered). Video is required by the browser but never recorded.
- **How it sends.** The page mixes call audio and microphone into one track and records a
  self-contained 30 s WebM/Opus file at a time (about 120 KB; the next file starts before
  the previous one closes, so no audio is lost between them). Pieces upload one by one,
  in order, through `POST /api/modules/sound/record/chunk`; Odysseus checks them and
  forwards them to the upload routes of the module's MCP HTTP server
  (`POST /ingest/chunk`, then `POST /sessions/{id}/end` on Stop; see
  [Uploads through the MCP server](#uploads-through-the-mcp-server)). Failed uploads retry
  with backoff while the tab is open; a piece the module rejects is skipped and shown.
  When the module ends a session (`CONVO_SILENCE_STOP_SECONDS` of silence or
  `CONVO_MAX_SESSION_SECONDS`), the page continues in a new session; one within
  `CONVO_CONVERSATION_GAP_SECONDS` joins the same conversation.
- **Security.** The browser never sees a module token: Odysseus uses the token of its
  `convo` MCP server. The upload routes are admin only and need an
  `X-Odysseus-Recorder: 1` header, so a plain cross-site form can't post audio. Capture
  needs a secure page: Odysseus over `https://` (a Tailscale certificate) or on
  `localhost`.
- **Settings:** none. It uses the URL and `?access_token=` of the MCP server named
  `convo` (`SOUND_MODULE_SERVER`) in Settings → MCP. Without that server, or with a
  module older than MCP 0.8.0, the page says what to fix ("update Athena").
  Optional: `SOUND_MODULE_CALL_DEVICE` (default `computer`).
- Code: `src/call_recorder.py`, routes in `routes/sound_module_routes.py`,
  `static/call-recorder.html`, `static/js/callRecorder.js`; test
  `tests/test_call_recorder.py`.
- Limits: you press Start for each call (a web page can't notice a meeting starting);
  the tab must stay open and the computer awake (the page asks for a screen wake lock);
  call audio and microphone arrive mixed, so the module's diarization tells the speakers
  apart; without headphones the microphone also hears the other people.

## Sending Athena to a Zoom meeting

The **Record a call** page (`/modules/sound/record`, admin only) starts with a
**Send Athena to a meeting** box. The owner pastes a Zoom link and presses Join; Athena's
own Zoom bot joins on the module's server as a guest called "Athena", records each
participant on their own track and leaves when the meeting ends. Nothing records on the
owner's computer, and the tab can be closed. Needs Athena with the meeting launcher and
relay routes (SDarioosh/Athena#124, #125) and meetings set up there
(`CONVO_MEETING_BOT_PATH` and the Zoom Meeting SDK credentials; see Athena's
`server/README.md`).

- **What it accepts.** Odysseus checks the input before it calls the module, with the
  module's own rules (`src/meeting_bot.py`, `parse_join`): a join link
  `https://(*.)zoom.us/j/<9-11 digits>?pwd=…` (any subdomain such as `us06web.zoom.us` or a
  company's `acme.zoom.us`; `http` or no scheme too), the web client's `/wc/join/<n>` and
  `/wc/<n>/join`, `zoommtg://` / `zoomus://…/join?confno=<n>&pwd=…`; a whole invite pasted
  as it is (its first join link wins; an invite without a link is read from its
  "Meeting ID:" and "Passcode:" lines); or a meeting ID with the passcode in the second
  field. Refused, each with its own message and without calling the module: personal
  links (`/my/<name>`), webinars (`/w/`), registration links, the host's start links
  (`/s/`, `zoommtg://…/start`), Zoom for Government (`zoomgov.com`) and any other host
  (look-alikes, user info, ports).
- **The owner joins too.** Athena joins on the owner's behalf (Zoom's on-behalf-of token,
  signed in once on the server with `convo-server zoom-login`), so the owner must be in the
  meeting: the bot waits for them (`starting`/`waiting_for_owner`), gives up if they don't
  come (`failed`/`owner_not_joined`) and leaves when they leave (`ended`/`owner_left`). The
  box says so under the link field.
- **What it shows.** The state in plain words: "Joining…", "Waiting for you to join the
  meeting — Athena joins as soon as you're in", "Waiting in the waiting room —
  the host has to admit Athena", "Recording", "Recording, but Zoom isn't sending audio",
  "Ended: the meeting is over" (and the other end reasons), "Couldn't join: wrong
  passcode" (and the other failures), "You didn't join the meeting in time, so Athena gave up", "Ended: you left the
  meeting, so Athena left too", "Athena's Zoom sign-in is missing or expired — run
  `convo-server zoom-login` on the server" (the module's 503 `zoom_signin_needed`), "Zoom
  didn't give Athena a join token; try again in a minute" (502 `zoom_unavailable`),
  "Athena is busy in another meeting" with a Leave
  button for that meeting, "Meetings aren't set up on the server", and "Update Athena to
  use meetings" for a module without the routes. On load the page shows the meeting the
  bot is in, if any.
- **No idle cost.** The page fetches the state only while it is open and visible and a
  meeting is on (every 3 s while joining, 6 s while recording, and once when the tab is
  shown again); it stops when the meeting has ended or failed, when the tab is hidden and
  when it is closed. Odysseus runs no task, thread or poll for meetings.
- **Hub routes** (`routes/sound_module_routes.py`, admin only; the writes need the
  `X-Odysseus-Recorder: 1` header like the call recorder's uploads):

  | Route | Does |
  |---|---|
  | `GET /api/modules/sound/meetings/current` | `{meeting}`: the newest active meeting among the module's last five, or null |
  | `POST /api/modules/sound/meetings` | JSON `{link, passcode?, title?}` → 202 `{meeting}`; errors `{detail, code, active?}` (`code`: `bad_link`, `busy` with `active`, `off`, `signin`, `zoom`, `update`, `setup`, `token`, `bot`, `unreachable`, `timeout`) |
  | `GET /api/modules/sound/meetings/{id}` | `{meeting}`: `id, state, reason, active, session_id, meeting_id, title, created_at, started_at, ended_at` and `text` (the state in words) |
  | `DELETE /api/modules/sound/meetings/{id}` | Leave: answers with the final state once the bot has exited (a few seconds) |

- **Module routes it calls**: the meeting routes of the module's MCP HTTP server
  ([below](#uploads-through-the-mcp-server)), with the `convo` server's URL and
  `access_token`, like the call recorder. So **the MCP token can send the bot into a
  meeting** (one at a time; the module answers `409 busy` over its limit).
- Code: `src/meeting_bot.py`, routes in `routes/sound_module_routes.py`,
  `static/call-recorder.html`, `static/js/meetingBot.js`; test `tests/test_meeting_bot.py`.

## Uploads through the MCP server

Since MCP version 0.8.0 the module's MCP HTTP server (`convo-server mcp --http`) has plain
HTTP routes next to `/mcp`, behind the same token. They are **not MCP tools**: the model
never sees them. The module forwards each one to its own ingest API with its ingest
token, so there is one ingest code path and the ingest token (which also signs
transcript links) never leaves the module.

| Route on the MCP server | Forwarded to |
|---|---|
| `POST /ingest/chunk` | `POST /v1/ingest/chunk` (same multipart fields and answer) |
| `POST /sessions/{id}/end` | `POST /v1/sessions/{id}/end` |
| `POST /ingest/text` | `POST /v1/ingest/text` (MCP 0.12.0; JSON, same fields and answer: a written note, [below](#handwritten-notes-written-notes)) |
| `DELETE /ingest/text/{source_id}` | `DELETE /v1/ingest/text/{source_id}` (MCP 0.12.0; `deleted` or `already`) |
| `GET /health`, `GET /conversations`, `GET /conversations/{id}`, `GET /conversations/{id}/transcript`, `GET /action-items`, `PATCH /action-items/{id}`, `GET /digests/{date}`, `POST /digests/{date}` | the same `/v1` routes (the phone relay's T5 sync); from MCP 0.12.0 a conversation carries `written: {source_id, link, title}` (null for recordings) |
| `GET /settings/llm`, `PUT /settings/llm` | the same `/v1` routes (Settings → AI Defaults: the summary model) |
| `POST /meetings`, `GET /meetings?limit=1..100`, `GET /meetings/{id}`, `DELETE /meetings/{id}` | `/v1/meetings…` (SDarioosh/Athena#125; JSON only, ≤ 16 KB; [Sending Athena to a Zoom meeting](#sending-athena-to-a-zoom-meeting)): join with `{url}` or `{meeting_id, passcode}` and optional `title` → 202 `{id, state, session_id}`, 400 bad link, 409 `busy` with `active`, 501 meetings off; state; leave (waits for the bot, up to 30 s) |

So the MCP token can add recordings and send Athena's Zoom bot into a meeting. Odysseus finds the module from the `convo` server's
URL (`/mcp` stripped) and its `access_token` (`src/call_recorder.py`); Odysseus' HTTP MCP
settings have no header field, so the query token is the only place it is. A module that
answers 404 on these routes is older than 0.8.0: Odysseus says "update Athena".

## Handwritten notes (written notes)

The Write view ([../handwritten-notes.md](../handwritten-notes.md), stage H4) sends a
handwritten note's reviewed text to the module as a **written note**, through the upload
link above (the `convo` server's URL and token; `src/ink_athena.py`, which calls
`src/call_recorder.py`). Needs Athena with SDarioosh/Athena#108 (MCP 0.12.0); an older
module answers 404 and the Text tab says "update Athena".

- `POST /ingest/text` JSON: `source_id` `hub-note:<note id>`; `text` every reviewed page in
  page order, a blank line between pages (text reviewed now only); `written_at` the note's
  first stroke (device clock), fixed in the sidecar when Athena first accepts the note so
  a resend of the same lines is `unchanged`; `link` `<hub>/#open=notes&note=<id>`; `title` the note's title.
  Answer `{ok, source_id, result: created|replaced|unchanged, conversation_id, lines,
  erased_lines, transcript_link}`.
- The module's limits are checked in the hub first, and the Text tab says what is over:
  `text` at most 200,000 characters and 5,000 non-empty lines (a line over 4,000
  characters counts once per 4,000), a link of at most 2,000 characters without spaces,
  quotes, `( ) < > { } | \ ^`, backtick or user info. Text with no words isn't sent: a
  note sent before is removed with `DELETE` instead.
- The hub's address for `link`: Settings → Reminders → **Public URL** (`app_public_url`),
  else `APP_PUBLIC_URL`, else the address the browser used for Send.
- Only **Send to Athena** in the Text tab sends, and only when every page with writing is
  reviewed for its strokes as they are now. A resend (after an edit is reviewed) replaces
  the note; Athena keeps the ids of facts and to-dos said again and drops the others,
  which the hourly facts job then retires in the hub.
- **Reading them back.** In MCP text a written note's title is followed by `(written by
  hand: <hub link>)` instead of `(voice note)`; `src/sound_module_view.py` and the text
  fallback of `src/module_action_items.py` read both. `/modules/sound` (and its window)
  has a tab each for **Conversations**, **Voice notes** and **Written notes**, each with
  its own search. `/api/modules/sound/conversations` takes `kind=voice` or
  `kind=written` and passes it to `search_conversations` (MCP 0.14.0, additive:
  SDarioosh/Athena, `kind` `voice` / `written` besides `note` and `conversation`). An
  older module answers "bad kind"; the hub then reads `kind="note"` (both sorts, up to 50,
  the tool's cap) and keeps one sort by the title marker, so with more than 50 notes in the
  search range the older ones of a sort are left out until the dates are narrowed. A written
  note links back to the note in the hub (**Handwritten note**).
- A note deleted in the hub is removed from the module with `DELETE /ingest/text/{source_id}`:
  at once from Write, otherwise by the sweep at the start of the hourly **Module Facts**
  run and when Write lists notes. Its strokes stay until the module answers `deleted` or
  `already`, so an unreachable module is asked again.
- In the module, the note's `/view` page says "Written by hand" and links back to the
  note in the hub; text results say `(written by hand: <link>)`.

## Pairing a phone

`/modules/sound/pair` (**Pair a phone** in Settings → Integrations, admin only) sets up
the module's Android relay with one QR code instead of two server URLs, two tokens and a
calendar id. The relay then uploads the watch's recordings and syncs the T5 through
Odysseus only.

- **The QR code** (rendered by Odysseus with the `qrcode` package; no CDN) holds
  `odysseus-pair:v1?url=<Odysseus URL>&code=<pairing code>`. The URL defaults to the
  page's address and can be edited: the phone needs the Tailscale address. The page also
  shows the link to copy, for pasting in the app.
- **No token in the QR.** The code is random (about 120 bits), single use and valid for
  10 minutes. Odysseus keeps only its SHA-256, in memory (a restart forgets codes still
  waiting). The relay redeems it at `POST /api/pair/redeem` {code} (no session; at most
  10 tries per address and 60 overall per 10 minutes; an unknown, used or expired code
  gets the same answer). Only then does Odysseus create the phone's API token, named
  "Phone relay <date time>", owned by the admin who made the code, and answers it once
  with the URL and scopes. The page polls and shows "Paired".
- **Scopes** (only what the relay calls): `sound:ingest` (uploads), `sound:read` (the
  T5's conversations, transcripts, action items, digest), `sound:write` (the T5 marks
  action items done, asks for a digest), `calendar:read`, `calendar:write` (the T5 adds
  and deletes events), `todos:read`, `todos:write` (the T5 ticks checklist items),
  `insight:read` (the Today insight). Each phone's token is in Settings → API tokens;
  revoke it there to unpair the phone.
- **Token routes for the relay** (`routes/sound_relay_routes.py`; a 401/403 means the
  phone's token, a 502 means Odysseus or the module):

| Route | Scope | Goes to |
|---|---|---|
| `POST /api/codex/sound/chunk` | `sound:ingest` | `/ingest/chunk` (checked first: id rule for `session_id` and `device`, `kind` conversation or note, codec `ogg` or `opus`, ≤ 5 MB, timestamp not over a day ahead or 60 days old) |
| `POST /api/codex/sound/sessions/{id}/end` | `sound:ingest` | `/sessions/{id}/end` |
| `GET /api/codex/sound/health` | `sound:ingest` or `sound:read` | `/health` (the relay's Test connection) |
| `GET /api/codex/sound/conversations`, `…/conversations/{id}`, `…/conversations/{id}/transcript`, `…/action-items`, `…/digests/{date}` | `sound:read` | the module routes of the same names |
| `PATCH /api/codex/sound/action-items/{id}`, `POST /api/codex/sound/digests/{date}` | `sound:write` | the same |
| `GET /api/codex/calendars` | `calendar:read` | the token owner's calendars (`name`, `href`, `source`), for the relay's calendar list |

- The direct mode (module URL plus ingest token) stays in the relay as a fallback.
- Code: `src/phone_pairing.py`, `routes/sound_relay_routes.py`,
  `static/phone-pairing.html`, `static/js/phonePairing.js`; test
  `tests/test_sound_relay.py`. Relay side: Athena's `android-relay` README.

## Summary model in Settings

Settings → AI Defaults → **Background & Summaries** (admin only) picks, apart from the chat
model:

- **Background tasks (Odysseus)**: upstream's `task_endpoint_id` / `task_model`, which
  upstream has no field for. The Today insight, memory, notes and scheduled-task jobs and
  chat summaries use it first; blank falls back to the Utility Model, then the chat model
  (`src/task_endpoint.py`). Helpers that call the Utility Model directly (compaction,
  titles, calendar and notes helpers) keep using the Utility Model.
- **Conversation summaries (Athena)**: the model the module's own LLM server uses
  for summaries, through `GET`/`PUT /api/fork/summary-model` → the module's
  `/settings/llm`. The list is what that LLM server's `GET /models` returns; the default
  is the module's `CONVO_LLM_MODEL`. Only the model name is sent: the module's LLM server
  (`CONVO_LLM_BASE_URL`) stays what its `.env` says. The choice is stored in the module
  and used from the next summary on, also after a restart. A module without the route
  shows "update Athena".

- Code: `routes/summary_model_routes.py`, `static/js/forkBackgroundModels.js`; test
  `tests/test_summary_model_routes.py`. Module side: `server/convo_server/llm_settings.py`.

## Today insight

The hub's daily Today insight reads `get_daily_digest` (today) and `list_action_items`
(`status="open"`, `format="json"`) when the module is connected, and works without them
otherwise. Its open loops read each row's `with` (the people in the item's conversation,
module MCP ≥ 0.9.0): an open item of the owner's with someone in `with`, not filed yet,
is a candidate (`loop:<id>`); an older module without `with` gives no loops. Details:
[../today-insight.md](../today-insight.md#plans-and-open-loops).

## Eval and current score

- Script: `server/eval/bakeoff.py` in Athena. Seed fictional data first with
  `server/scripts/seed_demo.py` using the same `CONVO_DATA_DIR`.
- 21 questions; a question passes when the frontend called a convo tool, cited a link to
  the right conversation, and the answer has the expected fact.
- Odysseus run: `python eval/bakeoff.py --frontend odysseus` from `server/`, with
  `ODYSSEUS_URL` (default `http://127.0.0.1:7010`), `ODYSSEUS_USER`, and
  `ODYSSEUS_PASSWORD`, `ODYSSEUS_PASSWORD_FILE` or `ODYSSEUS_API_TOKEN`; optional
  `ODYSSEUS_PRESET` (default `custom`) and `ODYSSEUS_APPROVAL`.
- Question 21 ("What was my latest voice note about?") needs the two demo voice notes:
  rerun `seed_demo.py` on an older demo store (it adds only what is missing). Scores
  below are out of 20 (questions 1–20, unchanged); compare new runs on `--only 1 … 20`
  plus 21 separately.
- Voice notes, sandbox 2026-10-20 (Qwen3-4B, `--trim`), question 21: before the `kind`
  filter 0/1 plain and 0/1 `/sound` (the model said it has no access to voice notes);
  after it `/sound` 1/1 (answered with the latest note, not the newer conversation) and
  plain 0/1 (the model wrote `search_conversations {"kind": "note"}` as text instead of
  a tool call). `/sound` on 1, 4, 16 with it: 2/3; 16 also fails with the old
  instructions (2/2), so it is model noise, not this change.
- Results: `server/eval/results/odysseus.jsonl` (gitignored).
- Sandbox (Qwen3-4B on CPU, questions 1, 4, 16; not comparable with the table below):
  raw 1/3, Odysseus over HTTP 1/3. With every Odysseus tool on and "Auto" (questions 1,
  4, 16, 21): plain 0/4, `/sound` 4/4, `/context` 1/4, `--trim` 3/4
  ([../sandbox.md](../sandbox.md#slash-skills-2026-10-18)). Slash-prefixed runs:
  `scripts/module_slash_eval.py` in Odysseus. Steps: [../sandbox.md](../sandbox.md#run-the-eval).

| Frontend (same model, same MCP server) | Score |
|---|---|
| upstream Odysseus | 3/20 |
| our fork, PR SDarioosh/Barvis#1 | 9/20 (right fact 18/20, link cited 9/20) |
| tuned Hermes Agent | 15/20 |

**Target: ≥ 15/20.** Remaining failures: answers with the right fact but no transcript
link, and 2 replies that were just "Done." because the model returned empty text after
its tool calls. Full analysis: Athena
[docs/frontend-decision.md](https://github.com/SDarioosh/barvis-sound/blob/main/docs/frontend-decision.md).

## Known gaps

- A to-do filed from a written note (or a voice note) stays in Odysseus when its item
  disappears from the module (a resend that drops it, or a deleted note):
  `src/module_action_items.py` only follows status changes of items it still reads.

- Plain questions (no `/sound` or `/context`) don't reach the convo tools when every
  Odysseus tool is on ([../roadmap.md](../roadmap.md)).
- A malformed date argument (e.g. `'2026-09-30”,'` from the 4B sandbox model) makes
  `search_conversations` raise instead of returning a readable error, so Odysseus only
  sees `exit_code=1` and the model reports a vague failure. An Athena fix, listed
  under "Next" in SDarioosh/Barvis#9.
