# athena-messages: text messages as a module (plan)

A planned module that makes the owner's text messages (iMessage first) searchable from
Odysseus and feeds their facts, promises and people into the belief ledger. Status:
**planned, not started**. Its place in the wider plan:
[context-growth.md](context-growth.md#step-2-athena-messages).

It is a separate module in its own repo (`SDarioosh/athena-messages`), built per
[modules.md](modules.md) ("Checklist: adding a new module"). Odysseus reaches it only
through its MCP server; it never imports hub code.

## Why messages

- Plans, promises and relationships are written in messages more than anywhere else.
- Text is exact: every line has a sender and a time, with no transcription or speaker
  errors (Athena's two main sources of wrong answers).
- The people are the same people Athena hears, so one person gets one subject in the
  belief ledger ([beliefs.md](beliefs.md#subjects-and-contacts-21)).

## Where the messages come from

The owner uses an Android phone with [OpenBubbles](https://openbubbles.app) for iMessage.
OpenBubbles talks to Apple directly from the phone; the Mac only gave it hardware info
once ([OpenBubbles FAQ](https://openbubbles.app/docs/faq.html)). OpenBubbles has no API
for reading history, so it is **not** the source.

| Path | How | Verdict |
|---|---|---|
| **A. The Mac's Messages app** | Sign the Mac's Messages into the same Apple ID, turn on Messages in iCloud, and the Mac holds the whole history in `~/Library/Messages/chat.db` (SQLite). A collector on the Mac reads it | **Recommended** if step 0 passes |
| B. BlueBubbles server on the Mac | Its local REST API and webhooks. Still reads the Mac's Messages, so it needs step 0 too, and adds a program to run | Only if A's database parsing proves too brittle |
| C. A rustpush receiver on the server | Another iMessage device, registered with Apple from the server (the library under OpenBubbles) | Not now: unofficial client on the owner's Apple ID, license to check, more to keep running |
| D. Android SMS/RCS | The Athena phone relay (`android-relay/`) reads the phone's SMS store | Later, separate source; RCS from Google Messages is probably not readable (check) |

**Step 0 (owner, before any code):** on the Mac, open Messages, sign in with the same
Apple ID, enable Messages in iCloud, and under Settings → iMessage → "You can be reached
at" tick the phone number if it is listed. Then send a test message to the phone number
and to the Apple ID email from another phone: both must show up on the Mac. If only the
email works, path A covers only part of the chats, and the owner decides between A
(partial), C, or waiting.

## Design

```
Mac (owner's laptop)                       Ubuntu server
┌───────────────────────────┐              ┌────────────────────────────────────┐
│ Messages.app → chat.db    │              │ athena-messages                    │
│ collector (launchd agent) │──Tailscale──▶│  ingest API → SQLite (own store)   │
│  reads a snapshot, sends  │   bearer     │  worker: threads, summaries, facts │
│  new messages since last  │   token      │  MCP server (HTTP, Tailscale)      │──▶ Odysseus
└───────────────────────────┘              └────────────────────────────────────┘
```

The Mac sleeps and travels; the server is always on and has the local models. So the
Mac runs only a small **collector**, and storage, the local LLM and the MCP server live on
the server, the same split Athena uses for its devices.

### Collector (on the Mac)

- A Python program run by a `launchd` user agent every 5 minutes and on wake. It needs
  **Full Disk Access** for the binary that runs it (System Settings → Privacy & Security).
- Never opens `chat.db` for writing. Each run copies it with SQLite's backup API (which
  also picks up the `-wal` file) into a temp file, reads the copy, deletes it.
- Reads `message`, `handle`, `chat`, `chat_message_join`, `chat_handle_join`. Keeps a
  cursor (the highest `ROWID` sent, plus `date_edited`/`date_retracted` changes for edits
  and unsends) in its own small state file.
- Message text: newer macOS often leaves `message.text` empty and keeps the text in
  `attributedBody` (an archived `NSAttributedString`). The collector decodes it; this is
  the part most likely to break on a macOS update, so it has its own tests with sample
  blobs and logs a count of messages it couldn't decode. Reference for the format:
  [imessage-exporter](https://github.com/ReagentX/imessage-exporter) (GPL-3.0: read it,
  don't copy its code).
- Dates: `message.date` is nanoseconds since 2001-01-01 UTC; sent as UTC ISO-8601.
- Tapbacks (`associated_message_type` 2000–3005) are sent as reactions on the message
  they point at, not as messages. Attachments: only the file name and type in v1, never the
  file.
- Names: phone numbers and emails are matched to the Mac's Contacts (read the same way
  from `~/Library/Application Support/AddressBook/`), and the name is sent with the handle.
- **Filters on the Mac, before anything leaves it:** excluded chats (a list the owner
  edits), verification codes and one-time passwords (pattern list: "code is 123456",
  "verification code", short-code senders), and messages older than a start date the
  owner picks for the first import.
- Posts batches to the server's `POST /v1/ingest/messages` with the ingest token. Retries
  are idempotent on the message `guid`. Only after the server answers `ok` does the cursor
  move.

### Module (on the server)

- Storage: its own SQLite (`messages`, `chats`, `people`, `threads`, `facts`,
  `action_items`, FTS5 index). Schema changes through numbered migrations, like Athena.
- **Threads:** messages of one chat are grouped into threads, split at gaps of more than
  6 hours (setting `MESSAGES_THREAD_GAP_HOURS`). A thread is the unit that gets a
  summary, like an Athena conversation.
- **Summaries, facts, promises:** the worker sends a closed thread (no new message for the
  gap) to the local llama-server with a JSON schema: title, short summary, durable facts
  (with the message they came from), and promises in both directions ("I'll send the
  photos Friday" by the owner; "Sam will book the table" by someone else). Same fact
  fields as Athena's `list_facts` JSON (`id`, `text`, `about`, `subject`, `person_id`,
  `kind`, `first_said`, `last_said`, `times_said`, `days`, `confirmed`, `valid_until`,
  `quote`), so the hub's facts job can read both modules with the same code.
- Confirmation: a fact is `confirmed` with the same rule as Athena (≥ 3 distinct threads
  on ≥ 2 distinct days), configurable.
- People: one row per person, from contact names and handles; a person can have several
  handles (phone, email). Stable ids that survive renames, as in Athena.
- Links: `<public url>/view/<chat id>?sig=…#m=<message guid>` opens a page that shows the
  thread around that message. Signed like Athena's transcript links.

### MCP tools (all read-only in v1)

| Tool | What it does |
|---|---|
| `search_messages` | shared-search arguments (`query`, `since`, `until`, `person`, `limit`; [modules.md](modules.md#shared-search-planned)), plus `chat`; dated hits with sender, chat and link |
| `get_thread` | the messages around one message or one thread, paged |
| `list_chats` | chats with their people and last activity, for "what did I talk about with…" |
| `list_facts` | facts, `format="json"` for the hub's job, `confirmed=true` filter |
| `list_action_items` | promises, both directions, with who owes what and when; `format="json"` |
| `list_people` | people with handles and counts; `format="json"` |

All `readOnlyHint: true`, `openWorldHint: false`, with a test per tool. Tool names follow
the contract: none of them clash with Odysseus built-ins, and the descriptions say "the
owner's text messages (iMessage)" so the model doesn't confuse them with Odysseus chat
search. **No sending in v1.** A later `send_message` tool would be `readOnlyHint: false`
and always asks for approval.

## Hub side (this repo)

Small, and mostly configuration:

- Register the server (`messages`) in the sandbox, then on the test instance; add it to
  `ODYSSEUS_TRUSTED_READONLY_MCP`; slash skill `/messages` with
  `scripts/modules_setup.py --module-skill messages=messages`; preset text owned by the
  module doc.
- A module doc `docs/fork/modules/athena-messages.md` and a row in the
  [modules.md](modules.md) table.
- Belief ledger: the facts job gets a second source, `athena-messages`, with its own
  setting (`MODULE_FACTS_MESSAGES`, off by default until tested, per
  [beliefs.md](beliefs.md#sources)). It reuses the Athena job's code path: `list_people`
  then `list_facts` then `reconcile_source`. This answers the open Phase 1 question
  "which source comes second" ([personal-context.md](personal-context.md#open-questions-for-the-owner)).
- Promises the owner made are offered as to-do suggestions on the Today insight, like
  Athena's conversation items; promises others made show as "waiting on".
- The module's cue words ("text", "message", "iMessage", a known person) join the
  module-cue routing that sends plain questions to agent mode
  ([tool-routing.md](features/tool-routing.md)).

## Privacy rules (on top of AGENTS.md)

- Messages leave the Mac only to the owner's server, over Tailscale, with a token.
- Other people's words are in every chat. Excluded chats are filtered on the Mac, never
  sent. The owner can exclude a chat later, which deletes it on the server too.
- One-time codes and verification messages never leave the Mac.
- Retention: a setting for message text (default: keep, like Athena's transcripts);
  summaries and facts stay. Forget on a fact in the hub erases its message text, as
  `erase_fact` does in Athena.
- Tests and the eval use fictional chats only (a `seed_demo.py` of its own, written in
  Athena's style).

## Eval

`eval/bakeoff.py --frontend odysseus` with 10 questions over fictional chats: a plan
("when is dinner with Mia?"), a promise ("what did I say I'd send Leo?"), a person fact,
a "nothing found", and two that need Athena too (said in a conversation, confirmed by
text). Target 8/10 on the server model before the facts source is turned on.

## Phases

| # | What | Done when |
|---|---|---|
| M0 | Step 0 above (owner) | Test messages to number and email both reach the Mac |
| M1 | Collector: snapshot, decode, filters, cursor, tests with sample blobs | Fictional `chat.db` in, the right JSON batches out |
| M2 | Module: ingest, storage, threads, search, `get_thread`, view page, MCP server | Eval questions that need no summaries pass in the sandbox |
| M3 | Summaries, facts, promises, people | Full eval in the sandbox; owner's server run ≥ 8/10 |
| M4 | Hub: registration, `/messages`, facts source, Today suggestions | Server verification on the test instance |
| M5 | Later: Android SMS (path D), WhatsApp or Signal as more sources of the same module | Owner decides |

## Open questions for the owner

- Step 0: do messages to the phone number reach the Mac?
- How far back to import (all of the history, or from a date)?
- Which chats to exclude from the start (family, work, groups)?
- Group chats: include them, or one-to-one chats only at first?
