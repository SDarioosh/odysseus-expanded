# Voice assistant (Home Assistant)

Status: built (2026-11-03), hub side only; changes after a spoken "yes", the screen's
feeds and the shared prompt cache added the same day. Needs the owner's Home Assistant and a voice
satellite (the Echo Show 5, 2nd gen, on LineageOS) to be used. Device setup is a separate
piece of work and is documented with the device.

## Summary

A wake word on a voice satellite asks Odysseus a question, and Odysseus answers out loud,
from Athena (recorded conversations), the calendar, notes, contacts, memory, beliefs,
documents and the web. Home Assistant runs the voice pipeline. Odysseus is its
conversation agent through an OpenAI-compatible endpoint:

```
satellite (wake word, mic, speaker)
   │ audio
Home Assistant Assist pipeline
   1. speech-to-text: local Wyoming faster-whisper (or the server's whisper-server)
   2. conversation agent: HA's own commands first, the rest ──► Odysseus
                                                               POST /api/chat/voice/v1/chat/completions
                                                               a brain turn: reads, and
                                                               changes only after a "yes"
   3. text-to-speech: local Piper (or Kokoro)
   │ reply audio
satellite
```

Nothing leaves the owner's network: every stage is local, and Home Assistant Cloud
(Nabu Casa) speech is not used.

- **Voice looks things up, and changes a few things after a spoken "yes".** A to-do, a
  reminder, a list item, a note or a calendar event can be added and an Athena action
  item marked done: the model's call is kept, Odysseus asks "Add Dentist on Tuesday at
  three to your calendar?", and only "yes" runs it. Everything else that writes is
  refused; email is never run.
- **The device's screen sees what a turn did:** `GET …/actions` and
  `GET …/turns/latest` (the turn ledger).
- **Done from the device's screen:** a tap ticks a to-do, finishes a reminder or marks an
  Athena action item done, without a spoken "yes" ([Done from a
  device](#done-from-a-device)).
- **Voice shares chat's prompt cache:** a voice prompt starts exactly like a chat
  prompt (same system prompt and tools), so the model server needs no second cache.
- **Each voice conversation is a chat** in the **Voice** folder, so the owner can read
  what was asked and answered, and continue it in the app.
- **Answers are spoken text:** short, no markdown, links taken out of the speech and
  returned apart for a screen.

## The endpoint

`fork/voice/routes.py`, mounted by `fork.routes.mount`. The OpenAI base URL is
`https://<odysseus>/api/chat/voice/v1`. It is under `/api/chat` on purpose: `app.py`'s
45-second request timeout (`REQUEST_HARD_TIMEOUT`) leaves that prefix alone, as it does
for chat streams, and a voice answer can take longer (minutes on the sandbox's CPU); and a
`/v1` API of upstream's own later can't clash with it. A test fails if the exemption goes.

| Route | Who | What |
|---|---|---|
| `POST /api/chat/voice/v1/chat/completions` | API token with the `voice` scope | one voice question; OpenAI's chat-completion request and response, `stream: true` supported |
| `GET /api/chat/voice/v1/models` | the same | `{"data": [{"id": "odysseus", …}]}`, for clients that list models when set up |
| `GET /api/chat/voice/v1/actions?since=<ms>&limit=<n>` | the same | the changes voice made (below) |
| `GET /api/chat/voice/v1/turns/latest?since=<ms>` | the same | the newest finished voice turn (below) |
| `GET /api/chat/voice/v1/today` | the same | the owner's day at a glance for a device screen: insight headline and focus, today's events, due to-dos, Athena's open action items ([Today for devices](#today-for-devices)) |
| `GET /api/chat/voice/v1/app/latest` | the same | the Echo app's release on offer, or 404 ([App updates](#app-updates)) |
| `GET /api/chat/voice/v1/app/apk` | the same | that release's APK ([App updates](#app-updates)) |
| `POST /api/chat/voice/v1/satellite/hello` | the same | the device's id, name, room and app version ([Rooms and devices](#rooms-and-devices)) |
| `GET /api/chat/voice/v1/announcements?device_id=…&since=<ms>` | the same | the announcements and reminders meant for that device ([feed](#the-devices-feed)) |
| `GET /api/chat/voice/v1/devices/{device_id}/settings` | the same | the settings the hub manages for that device ([Device settings](#device-settings)) |
| `POST /api/fork/voice/token` | signed-in admin (browser) | makes a token with only the `voice` scope (the Settings card) |
| `GET /api/fork/voice/ha` | the same | the saved Home Assistant address and the last setup's report |
| `POST /api/fork/voice/ha/connect` | the same, same-origin | starts "Set up Home Assistant": answers HA's login address ([Setup](#setup-owner)) |
| `GET /api/fork/voice/ha/callback` | the same | Home Assistant's redirect after the login: runs the setup, revokes the login, shows the steps |
| `GET /api/fork/voice/devices` | signed-in admin (browser) | the admin's Echos with their hub-managed settings ([Device settings](#device-settings)) |
| `PUT /api/fork/voice/devices/{device_id}/settings` | the same | sets one Echo's hub-managed settings |

Only a bearer token with the `voice` scope may call the `/api/chat/voice/v1` routes: a browser login may
not, and a token with only other scopes (`chat`, …) may not. The token is listed under
Settings → Integrations as "Voice assistant <date>"; deleting it there disconnects Home
Assistant.

**Request** (OpenAI's shape; only these fields are read):

- `messages`: the newest `user` message is the question (a string, or content parts of
  type `text`; at most 2000 characters). Earlier messages only name the conversation
  (below). `system` messages and `tools` from the client are ignored: Home Assistant
  handles its own device commands before it asks a conversation agent.
- `stream`: `true` for server-sent events.
- `user`: the client's conversation id, if it sends one; or the `X-Conversation-Id`
  header.
- `model`: echoed back; any value works.

**Response** without `stream`: a `chat.completion` with one assistant message (the
spoken text), `finish_reason: "stop"` and `usage` (the brain's token counts). With
`stream: true`: `chat.completion.chunk` events, one finished sentence at a time, a last
chunk with `finish_reason: "stop"` and `usage`, then `data: [DONE]`. Both carry an extra
object the OpenAI clients ignore:

```json
"odysseus": {"session_id": "<the chat's id>", "links": ["https://…/view/12?sig=…#t=34"],
             "turn_id": "<the ledger's turn id>"}
```

Errors are OpenAI's `{"error": {"message", "type"}}`: 400 for a request we can't answer,
403 without a voice token, 503 when the brain is off (`FORK_BRAIN`), the owner may not
use the agent, or no default chat model is set.

## How a question is answered

`fork/brain/turn.py`, `read_only_turn` with `shared_prefix`: the same loop as chat (the
brain, [brain.md](brain.md)), with the same prompt, history, tools and owner checks, and
these differences:

- **It runs as the token's owner.** Upstream keeps every run driven by an API token away
  from MCP and the personal stores (`delegated_tool_is_blocked` in
  `src/tool_capabilities.py`), and a voice question is about exactly those. A voice turn
  is therefore not a delegated run. In place of that cap, what may run is decided at
  execution (`fork/voice/policy.py`, `VoiceGate`, asked by `fork/brain/tools.py` before
  anything runs):
  1. **email is never run** (listed, refused: "Not run: email isn't available by
     voice"): the answer is read aloud, to anyone in the room;
  2. **private answers** (below): with `FORK_VOICE_PRIVATE=owner`, a voice that isn't the
     owner's gets `PRIVATE_REPLY` from the personal tools (Athena, calendar, notes,
     contacts, memory, beliefs, documents, chat search); web and weather stay;
  3. **reads run** as in chat, except a `web_fetch` of an address that is neither in the
     question nor a search hit, and a call upstream's approval gate would ask about
     (`tools.read_only_refusal`);
  4. **an allowed change is kept, not run** (next section);
  5. **every other write is refused, never asked** ("Not run: this request can only
     read"): deletes, edits, `erase_fact`, `retire_fact`, `rename_speaker_everywhere`,
     memory, contacts, documents, image generation. The turn never pauses for an
     approval card.
- **The voice instructions** (`fork/voice/prompt.py`) go in the question's message,
  before the question (after the cached prefix, next section but one): the owner's tools
  first for questions about their life (as in chat), then one to three spoken
  sentences, no markdown, numbers and times as spoken, links at the end, no closing
  question or offer, and how changes work by voice. A first version that also asked for
  "the answer first" and "I don't know when the tools return nothing" made the sandbox's
  small model skip the tools and answer "I don't know"; the instructions say tools come
  first.
- **The context message** says where the question was asked ("Asked from: Kitchen Echo,
  Kitchen") when the satellite posted its context.
- **Model:** the owner's default chat model (Settings → default model). The brain's
  local-only rule applies: an outside API answers "The assistant runs only on local
  models."
- **Memory:** no memories are extracted from a voice turn (anyone near the satellite
  can speak). Recalled context holds confirmed beliefs, as in chat, but not upstream's
  memory list (the route has no memory manager); the model can still search beliefs
  (`search_beliefs`) and read the memory store (`manage_memory`). For a voice that isn't
  the owner's (private answers on) nothing is recalled.

## Changes by voice

`fork/voice/confirm.py`. The allow-list (`WRITABLE`, one entry per tool and action, so
a new writable tool is one entry):

| Call | Kept as | Where |
|---|---|---|
| `manage_notes` `add` with a `due_date` | reminder | To-dos |
| `manage_notes` `add`, a checklist | to-do | To-dos |
| `manage_notes` `add`, a note | note | Notes |
| `manage_notes` `update` that only adds lines to one of the owner's lists (every line kept, text and tick, in order; no rename, no other field) | list item | the list's title |
| `manage_calendar` `create_event` | event | Calendar |
| Athena `update_action_item` with `status: done` and nothing else | action item, completed | Athena |

A `due_date` the owner didn't say is dropped before the call is kept
(`without_unsaid_due`): the model sometimes gives a plain to-do a due date (upstream's
tool example shows one). The date stays only when the question, or the conversation's
previous question, names a date or time ("tomorrow", "Friday", "at five", "3pm", "in 20
minutes", "the 1st"…, `WHEN`); otherwise the call is kept as a to-do (a title alone gets
`note_type: checklist`), and the confirmation question, built from the kept call, says
no date. The voice instructions also ask for a `due_date` only when one was said.

1. **Proposed:** an allowed call is not run. The model's tool result is "Not run yet:
   changes by voice need the owner's spoken yes. Ask the owner to confirm in one short
   question…". The exact tool name and arguments are kept as the voice conversation's
   proposal, in memory, for 5 minutes; several calls of one turn are one proposal, and a
   new turn's proposal replaces the old one. The spoken answer ends with the model's
   confirmation question, or, if it asked none, one made from the call ("Add Dentist to
   your calendar?"); the turn's `awaiting_confirmation` holds it.
2. **The next question of that conversation** (same `user` or conversation id, within
   the follow-up window) decides, without a model call:
   - an affirmation ("yes", "yeah", "sure", "do it", "please do", "okay", "go ahead",
     "correct", alone or with "please"/"thanks"): the kept calls run exactly as the model
     made them, each with an exact approval filed and consumed at once in upstream's
     `tool_approval_store` and run by the brain's executor (`tools.execute`, upstream's
     `execute_tool_block`): the path of a call approved on a chat card, so owner scoping,
     admin-only tools and upstream's checks still apply. A list item is checked again
     first: if the list changed since, nothing is done. Each change that worked is a
     ledger action (`created`/`completed`), and the answer is "Done." plus a short phrase
     ("Done. Added Dentist to your calendar.");
   - a refusal ("no", "cancel", "never mind", "don't"): dropped, "Okay, I won't.";
   - anything else: dropped, and answered as a new question.
   The "yes" and its reply are saved in the voice chat like a turn.
3. A yes from a voice that isn't the owner's (private answers on) runs nothing.

**A question keeps its "?", a filler offer is dropped** (`fork/voice/speech.py`): Home
Assistant keeps the microphone open, and skips its own commands for the reply, only when
an answer ends with a question mark. A kept change ends with its confirmation question
(above). Otherwise a trailing filler offer ("Anything else?", "Would you like more
details?") is dropped, and any other last question ("Do you want me to read them?",
"Want me to check the transcripts?") is kept, so the owner can just answer it. Before
2026-11-04 every last "?" became ".", and the Echo stopped listening after a question.
A "yes" to such a question goes to the model with the chat's history; when the model
then calls a change, that change is still only proposed and confirmed by its own
question. While streaming, a sentence that ends with "?" is held back until the next one
arrives or the answer ends.

## One prompt cache for chat and voice

The owner's llama-server keeps its prompt cache warm from ordinary chat, and there is no
memory for a second one. So a voice prompt starts byte for byte like a chat prompt of the
same owner: the same system message (the brain's prompt plus the preset text a new chat
in the browser would send: the Custom preset when it is on and has settings,
`fork/voice/chat_prefix.py`, read from `presets.json` through upstream's own
`validate_and_extract_preset`) and the same tools in the same order (email and the
write tools included: refused or kept at execution, not hidden). Everything that differs
comes after that prefix: the context message (device line) and the voice instructions
with the question. A test checks that a voice turn's messages before the context message
and its tools equal a chat turn's. Assumption: the browser's documents toggle is on (a
chat with it off is offered no `search_documents`).

## Turns and actions (the screen's feeds)

`fork/voice/ledger.py`: one record per voice turn in `<DATA_DIR>/fork_voice.db`
(SQLite), deleted after 7 days, owner-scoped. It is the one place a voice turn's facts
live; later features add to it.

`GET …/actions?since=<epoch ms>&limit=<n>` (newest first, default 8, at most 50):

```json
{"actions": [{"id": "…", "at": "2026-11-03T19:52:10+00:00", "action": "created",
  "kind": "event", "title": "Dentist", "detail": null, "due": "2026-11-10T15:00:00-05:00",
  "all_day": false, "where": "Calendar", "source": "odysseus", "turn_id": "…"}]}
```

`kind` is `todo`, `reminder`, `list_item`, `note`, `event` or `action_item`; `action`
`created` or `completed`; `source` `odysseus` or `athena`, or `device` for a Done tapped
on a device's screen (then `turn_id` is `device:<device id>`: no voice turn made it;
[Done from a device](#done-from-a-device)).

`GET …/turns/latest?since=<epoch ms>`: the newest finished turn started at or after
`since`, else `{"turn": null}`:

```json
{"turn": {"id": "…", "started_at": "…", "ended_at": "…", "question": "…",
  "answer": "the spoken text", "links": [{"url": "https://…", "label": null}],
  "actions": [], "awaiting_confirmation": "Add Dentist to your calendar?",
  "device": {"id": "…", "name": "Kitchen Echo", "area": "Kitchen"},
  "speaker": {"verified": true, "is_owner": true, "name": "Sam"},
  "session_id": "<the chat's id>"}}
```

`device` and `speaker` come from the satellite's context (`POST …/satellite/turn`,
`fork/voice/satellite.py`), claimed at the start of each question; both are null without
it. Links have no labels yet (`label: null`).

## Today for devices

`GET …/today` (`fork/voice/today_route.py`), for the Echo's Today page: the token owner's
day, read from stored data only (no model call). Times are ISO-8601 with the owner's
offset (the insight's zone, `owner_timezone`); the device shows them in its own zone.

```json
{"date": "2026-11-06", "generated": "2026-11-06T07:45:00-05:00",
 "headline": "Two meetings this afternoon; the boiler service is due.",
 "focus": [{"ref": "todo:41:2", "title": "Call the plumber", "why": "Due today"}],
 "events": [{"uid": "abc", "title": "Dentist", "start": "2026-11-06T15:00:00-05:00",
             "end": "2026-11-06T15:30:00-05:00", "all_day": false, "location": "Main St"}],
 "todos": [{"ref": "todo:41:2", "title": "Call the plumber", "due": "2026-11-06", "list": "Home"}],
 "action_items": [{"id": "ai_17", "title": "Send Sam the photos", "source": "athena"}],
 "counts": {"todos_open": 9, "action_items_open": 4}}
```

| Field | From |
|---|---|
| `headline`, `focus` | today's stored [Today insight](today-insight.md), in the owner's order ("↑ first" first, "not today" left out); `null` and `[]` until one is written. A focus to-do ticked since then is left out. Each row: `ref`, `title`, `why` |
| `events` | the calendar occurrences that overlap today (recurring events expanded by the calendar route's `_expand_rrule`, cancelled ones left out), sorted by start, at most 10. An all-day event starts at local midnight of its first day and ends at local midnight after its last; `location` is `null` when empty |
| `todos` | open items of to-do notes (checklist, todo, goal; not archived; the insight's rules, so undated shopping-type lists are left out): due today or overdue (oldest first), then items of pinned notes without a date; at most 8. `due` is the note's date or `null`; `list` the note's title. `ref` is `todo:<note id>:<item index>`, as in the insight |
| `action_items` | Athena's open action items (`GET /action-items?status=open` through the hub's Athena link), without those already filed here as a to-do or event; at most 5. `title` is the item's short title, else its text. `[]` when Athena is down or takes more than 3 s |
| `counts` | `todos_open`: every open to-do item (all dates); `action_items_open`: every open, unfiled action item. The device shows "and N more" from them |

Each section is read on its own: one that fails is empty (and logged), and the route
answers 200 whenever the token holds the `voice` scope.

## Done from a device

`fork/voice/done.py` (contract C5 of the Echo's round 2). The Echo's Today page and its
reminder cards have Done buttons. A tap on the device's own screen is the confirmation,
so these need no spoken "yes". Same `voice` token as the other routes.

```
POST …/todos/done         {"ref": "todo:41:2", "device_id": "…", "title"?: "Call the plumber"} -> {"ok": true, "already": false, "title": "Call the plumber"}
POST …/reminders/done     {"ref": "todo:41", "device_id": "…"}   -> {"ok": true, "already": false, "title": "Take the bins out"}
POST …/action-items/done  {"id": "17", "device_id": "…"}         -> {"ok": true, "already": false, "title": "Send Sam the photos"}
POST …/todos/undo         {"ref": "todo:41:2", "device_id": "…"} -> {"ok": true, "already": false, "title": "Call the plumber"}
POST …/action-items/undo  {"id": "17", "device_id": "…"}         -> {"ok": true, "already": false, "title": "Send Sam the photos"}
```

| Route | What it changes |
|---|---|
| `todos/done` | ticks item 2 of note 41 (the `ref` of `GET …/today`), the same change as the notes page's tick: the item's `done` |
| `reminders/done` | archives note 41 (the `ref` of a `kind: reminder` announcement). This is what the notes page's ✓ Done does to a note (`PATCH archived: true`, for a note with or without items, repeating or not), so a reminder finished on the Echo looks the same as one finished in the browser: the reminder scanner (`ping_notes`) and the notes page skip archived notes, so it doesn't fire again, and it can be restored from the notes archive. Ticking the note's one item or clearing its due date (the plan's other options) would leave a reminder the notes page still shows. The `ref` is taken as the feed carries it: a ref with an item index (`todo:41:2`) ticks that item instead, as `todos/done` |
| `action-items/done` | Athena's `status: done` through the hub's Athena link (`PATCH /action-items/{id}`, the relay's own write; the item is first read with `GET /action-items?status=all`, for its title and whether it is already done). `id` is Athena's id as `GET …/today` gives it (`"17"`); a number and `"ai_17"` are read too |

- **A stale index:** a to-do's `ref` names a line by its index, and the owner may have
  added, removed or moved lines since the device fetched Today. So `todos/done` (and
  `reminders/done` with a ref that has an index) take an optional `title`: the row's text
  the device showed. When the line at the index doesn't read it (compared as `/today`
  titles a line: whitespace collapsed, the first 200 characters, any case), the line that
  does is ticked instead (an unticked one first, then the nearest); when no line reads it,
  409 `{"error": "changed"}` and nothing changes (the device puts the row back and fetches
  Today again). Without `title` (Echo builds before it) the index alone counts, as
  before. A whole-note reminder ref (`todo:41`) ignores `title`, and so does a reminder
  ref with an index whose `title` is the note's own title (what a reminder card shows).
- **Idempotent:** a to-do item already ticked, a note already archived or an item already
  done answers 200 with `"already": true` and changes nothing.
- **Recorded:** each change is a `completed` action in the turn ledger (`kind` `todo`,
  `reminder` or `action_item`; `where` the list's title, `To-dos` or `Athena`; `source`
  `device`; `turn_id` `device:<device id>`), so `GET …/actions` shows it.
- **Undo:** `todos/undo` and `action-items/undo` reopen what a device closed within 10
  minutes of its done (any device of the same owner), and remove that ledger action. A
  to-do line that moved since is found by its text. Later, or for something a device
  didn't close (ticked in the browser, or closed again elsewhere after an undo): 409
  `{"error": "too late to undo"}`; already open: 200 with `"already": true`. A device's
  dones are kept for a day in `fork_voice.db` (table `device_done`), owner-scoped.
  The undo bodies may carry the device's `device_id`; it is not needed (any device of
  the owner may undo) and is ignored.
  A reminder has no undo route: unarchive it on the notes page.
- **Errors:** 400 `{"error": "…"}` for a body that isn't a JSON object, a malformed
  `ref` or `id`, a `title` that isn't text (or over 2000 characters), or no `device_id`
  (done routes; at most 128 characters); 404
  `{"error": "not found"}` for a note that isn't the owner's or doesn't exist, an item
  index past the end, or an action item Athena doesn't have; 503 `{"error": "athena
  unavailable"}` when Athena isn't set up, is down, refuses the hub's token or takes
  more than 5 s; 503 `{"error": "busy"}` on a database error (SQLite "database is
  locked", disk I/O, any SQLAlchemy error; only the exception's type is logged, never the
  line's text): the device tries again later; 409 `{"error": "changed"}` (above) and
  `{"error": "too late to undo"}` (undo routes only) are told apart by their text; 403
  without the `voice` scope.
- Notes are only the token owner's (`Note.owner`). Athena has one owner, as for
  `GET …/today`.

## Private answers

`FORK_VOICE_PRIVATE` (`fork/voice/satellite.py`): `off` (default) answers every voice;
`owner` gives personal answers only when Athena recognised the owner's voice in the
audio the satellite posted. Without the satellite module, every voice is answered.

## The security trade-off

For the owner to keep in mind: the voice token can read the owner's conversations,
calendar, notes, contacts and beliefs (not email), and **anyone in the room can make the
allow-listed changes** (add a to-do, reminder, list item, note or calendar event; mark an
Athena action item done) by asking and then saying "yes". Voice can't delete or edit
anything else, send or read email, or reach anything an ordinary chat couldn't. Whoever
holds the token (Home Assistant) and anyone who can talk to the satellite can ask. Keep
the satellite in a private room and the token in Home Assistant only; with
`FORK_VOICE_PRIVATE=owner`, personal answers and changes need the owner's voice.

## Conversations

`fork/voice/sessions.py`. A follow-up within 5 minutes of the last question continues
the same chat, so "and on Friday?" works. A request names its conversation by its `user`
field or `X-Conversation-Id` header, or else (when it sends earlier turns) by its first
question. A request with neither starts a new chat. A conversation's first question
(no earlier turns, no id) always starts a new chat, and is remembered under itself, so
the next turn (which sends it as its first question) continues that chat. Before
2026-11-04 it wasn't: with Home Assistant, which sends no id, the first reply of every
conversation started a new chat, and a "yes" to the first answer's confirmation found
nothing to confirm. The map is kept in memory per owner; after a restart the next
question starts a new chat.

New chats are named "Voice: <question>" and filed in the **Voice** folder.

## Spoken answers

`fork/voice/speech.py`. Markdown is removed (headings, bold, lists, tables, quotes, code
blocks); each list item or heading becomes a sentence; `[label](url)` keeps the label
unless it only names the link ("transcript", "source", …); bare URLs and `[1]` citations
are removed, and so is a list of links with its lead-in ("Links: …", "Sources: …") and
link names left hanging at the end ("Office move: <url>"). The links are collected, once
each, in order, into `odysseus.links`.

A whole answer (no `stream`) speaks only the text of the rounds that ended without a
tool call, so a model's "Let me search for that" before its tool calls isn't read out.
While streaming, text is sent one finished sentence at a time and never cut inside an
open link or code block, so text-to-speech can start before the answer is done; there a
preamble is spoken, since it is sent before the model's tool calls are known.

## Sandbox results (2026-11-03)

Measured before changes by voice and the shared prefix (then: read-only tools only, the
voice instructions in the system prompt); not run again since.

The sandbox stack ([sandbox.md](sandbox.md)): Qwen3-4B on 4 CPUs, Athena's fictional demo
data, Odysseus on port 7010, the voice token from the Settings route, `curl` to the
endpoint. Small-model results only compare with other sandbox runs.

| Check | Result |
|---|---|
| `GET …/models` with the voice token | `odysseus` |
| no token / a `chat`-only token / the voice token on the token route | 401 / 403 / 403 |
| "What did Alice say about the Q4 budget?" | right (15% cut, revised to 10%, from two conversations), two Athena searches, 2 links apart, no markdown; ~2.5 min |
| follow-up "And when is the spreadsheet due?" (same `user`) | same chat continued; right (Friday after the September 15 conversation); ~1.5 min |
| "Mark the revised spreadsheet action item as done." | nothing changed (the write tools aren't offered); the small model's reply doesn't always use the voice wording; ~20–30 s |
| "Who is moving to Berlin, and when?" (streamed) | chunks arrive as OpenAI `chat.completion.chunk` events; wrong: the small model called no tool and said it didn't know. Chat with the same model searched and answered (Alice, January 12) after a wobble |
| "What is the capital of France?" | "Paris." |

Notes from the run:

- The tool set a voice turn sends the model (seen through a logging proxy): web search,
  fetch and weather, Athena's eight read tools, calendar, notes, contacts, memory,
  documents, beliefs and chat search; not `update_action_item`, `erase_fact`, email or
  image generation.
- The first question after a restart can fail with upstream's "no first stream event"
  read timeout: on CPU, the ~7k-token prompt takes about two minutes to process cold.
  Chat had the same failure. On the server's GPU this doesn't arise.
- Each voice chat landed in the Voice folder, named "Voice: <question>", owned by the
  token's owner.

## Setup (owner)

### One click: Set up Home Assistant

Settings → Integrations → **Voice assistant** → **Set up Home Assistant**
(`fork/voice/ha_routes.py`, `fork/voice/ha_setup.py`). First, in Home Assistant: install
the **Whisper** and **Piper** add-ons (and add both under Settings → Devices & services,
Wyoming Protocol) and **Extended OpenAI Conversation** (HACS, version 3), then restart
Home Assistant. In Odysseus, `ODYSSEUS_TRUSTED_READONLY_MCP` must name Athena's server
(`convo` or `athena`), as it already does for chat; without it Athena's tools aren't
offered to voice.

1. Enter the **Home Assistant address** (`http://homeassistant.local:8123`) and the
   **Odysseus address as Home Assistant reaches it**, and press **Set up Home
   Assistant**. The second box starts with the address the browser uses (e.g.
   `https://odysseus.<tailnet>.ts.net`), or, once a setup succeeded, the address that
   setup used. When Home Assistant runs on the same server, give it the server's
   home-network address (`http://192.168.1.10:7000`; `http://127.0.0.1:7000` when Home
   Assistant uses the host's network): HA then talks to Odysseus without going through
   Tailscale, and a Home Assistant that isn't on the tailnet (or can't resolve its
   names) reaches it at all. The "Conversation agent" step fails with "Home Assistant
   couldn't reach Odysseus at …" when HA can't reach the address given.
2. Home Assistant's login page opens: sign in with an **administrator** account. Open
   Odysseus by its name (or a home-network IP) for this: Home Assistant refuses a
   sign-in for an app on a Tailscale IP (`100.x`).
3. Back in Odysseus, a page lists each step and its result:

| Step | What Odysseus does in Home Assistant |
|---|---|
| Home Assistant | checks the sign-in is an administrator |
| Speech | finds the Wyoming speech-to-text (Whisper) and text-to-speech (Piper) entities, with English, and Piper's first English voice. Only Wyoming ones: never cloud speech. Missing: "Install the Whisper and Piper add-ons …", and nothing is changed |
| Conversation agent | a new Extended OpenAI Conversation entry "Odysseus": API key a new voice token "Home Assistant <date>", base URL `<odysseus>/api/chat/voice/v1` (checked by Home Assistant: it lists the models with the key), one conversation agent "Odysseus" (`conversation.odysseus`): model `odysseus`, no tools (`functions: []`), a one-line prompt (ignored by Odysseus). The integration's default GPT agent and AI task are removed. Older entries named "Odysseus" are removed after the new one works (the integration can't change an entry's key) |
| Assist pipeline | "Odysseus": that speech-to-text, agent and text-to-speech, language `en`, **prefer handling commands locally** on (so "turn off the lights" stays in Home Assistant). An existing "Odysseus" pipeline is updated in place (its wake word fields are kept) |
| Preferred assistant | makes it Home Assistant's preferred assistant, which the Echos use |

What happens to the Home Assistant login: Odysseus uses it for this one run and then
revokes it (`POST /auth/revoke`), whether the run worked or not; it is never stored. The
Home Assistant address is saved (`fork/voice/ha_settings.py`, key `fork_voice_ha_url` in
`data/settings.json`) and handed to each Echo set up afterwards. The voice token is
listed under Settings → API tokens as "Home Assistant <date>". A rerun makes a new one
and deletes the earlier "Home Assistant <date>" tokens (voice scope only) once Home
Assistant uses the new one; a token Home Assistant never got is deleted at once.
Tokens made by hand ("Voice assistant <date>") are left alone. The card shows the last
run's steps.

Security of the flow: only a signed-in admin's browser session may start it (no API
token), and only by a same-origin POST, so another site can't start a run toward a Home
Assistant of its choosing (which would hand it a voice token). The OAuth `state` is
random, single use, bound to that admin and valid 10 minutes; `client_id` is the origin
the browser uses and `redirect_uri` the callback on it, which Home Assistant accepts
(same host).

**Without the browser** (Odysseus can't open Home Assistant's login page), in the
Odysseus checkout or container, with a long-lived token of a Home Assistant
administrator (HA profile → Security):

```bash
HA_TOKEN=… python scripts/ha_voice_setup.py --ha-url http://homeassistant.local:8123 \
  --odysseus-url https://odysseus.<tailnet>.ts.net --owner <odysseus admin>
```

The same steps; the long-lived token is only used, never revoked or stored (delete it in
Home Assistant afterwards). A running Odysseus only notices API tokens made by another
process when its token list changes or it restarts, so the token the script makes is
given to Home Assistant unchecked (`skip_authentication`) and the script says to
restart Odysseus. To avoid the restart, make a token in Settings (**Make a voice
token**) and pass it as `VOICE_TOKEN=…`: then the script makes none and Home Assistant
checks it.

Then pick the Odysseus assistant on the satellite (it is the preferred one already). An
optional second wake word, for example "Hey Athena", is a second assistant with the same
agent, chosen by that wake word on the satellite.

### By hand (fallback)

1. **Token:** Settings → Integrations → **Voice assistant** → **Make a voice token**.
   Copy the API key (shown once), the base URL (`https://<odysseus>/api/chat/voice/v1`) and the model
   (`odysseus`).
2. **Trusted Athena reads:** `ODYSSEUS_TRUSTED_READONLY_MCP` must name Athena's server
   (`convo` or `athena`), as it already does for chat. Without it Athena's tools aren't
   offered to voice.
3. **Home Assistant, speech:** add the Wyoming integration for a local faster-whisper
   (speech-to-text) and Piper (text-to-speech), as add-ons or containers on the owner's
   network.
4. **Home Assistant, conversation agent:** an integration that talks to any
   OpenAI-compatible server with a base URL of your choice, such as Extended OpenAI
   Conversation (HACS). Base URL: the one from step 1; API key: the voice token; model:
   `odysseus`. Turn off its own tool or function calling if it has the option (Odysseus
   ignores the tools it sends); in Extended OpenAI Conversation set Functions to `[]`
   (empty brings back its default tools).
5. **Home Assistant, pipeline:** Settings → Voice assistants → Add assistant: the
   speech-to-text and text-to-speech of step 3, the conversation agent of step 4, and
   **Prefer handling commands locally** on, so "turn off the lights" stays in Home
   Assistant and everything else goes to Odysseus. Pick this assistant on the satellite.
6. **Optional second wake word**, for example "Hey Athena": a second assistant with the
   same agent, chosen by that wake word on the satellite.

To check, without the satellite (on the owner's network, with the voice token):

```bash
curl -s https://<odysseus>/api/chat/voice/v1/chat/completions \
  -H "Authorization: Bearer $VOICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"model":"odysseus","messages":[{"role":"user","content":"What did I talk about yesterday?"}]}'
```

To check (not verified in the sandbox): which field Home Assistant's agent sends the
conversation id in. If it sends neither `user` nor earlier turns, every question starts a
new chat and follow-ups lose their context; a "yes" then confirms nothing either
(it must reach the conversation that asked).

## Satellites: owner-only answers and announcements

Status: built hub side (2026-11-03). Needs the Echo app's satellite post and, for the
speaker check, Athena's `POST /speakers/identify` (MCP 0.22.0); with an older Athena the
check answers "couldn't check".

### Satellite context

`fork/voice/satellite.py`, `fork/voice/satellite_routes.py`. When the owner stops
speaking (the pipeline's VOICE_STOPPED), before Home Assistant has asked Odysseus, the
satellite posts what only it knows:

`POST /api/chat/voice/v1/satellite/turn` (the `voice` token), `multipart/form-data` (a
urlencoded form works without audio):

| Field | |
|---|---|
| `device_id` | required, at most 128 characters |
| `device_name`, `area`, `wake_word` | optional text (100, 100, 64 characters) |
| `started_at` | epoch ms of the wake word (default: now) |
| `audio` | optional WAV (16 kHz mono 16-bit, the spoken command, ≤ 15 s); at most 1 MiB, must start with a RIFF/WAVE header (else 413 / 415) |

The request needs a `Content-Length` (411 without, 413 above ~1.06 MB), so an oversized
upload is refused before it is read. Answer: `{"ok": true, "context_id": "…",
"speaker": {"verified", "is_owner", "name", "score"} | null}`; `speaker` is null without
audio, when Athena couldn't check, or while the check still runs (the route waits at most
0.5 s for it).

The hub keeps the context **in memory** for 30 s, per token owner (at most 8 per owner, 64
owners). The voice turn takes it with `claim(owner, wait_seconds=3.0)`: the newest
unclaimed context of that owner posted in the last 10 s (`CLAIM_SECONDS`), each claimed
once; the owner's older unclaimed contexts are dropped then (they belong to turns Home
Assistant answered itself), so their speaker never counts for a later turn. If the speaker
check is still running, it waits up to `wait_seconds`. The turn puts device and room in its context
("Asked from: Kitchen Echo, Kitchen") and on the ledger (HUB-CORE's side, `fork/voice/routes.py`).
After a restart, or with several workers, a turn may find no context: it is then answered
as if no satellite had posted.

**The speaker check** runs as a background task: the audio goes to Athena's relay route
`POST /speakers/identify` (field `audio`) through the MCP server Odysseus has for Athena
(`SOUND_MODULE_SERVER`, `fork/athena/client.py`, token as `Authorization: Bearer`, 10 s
timeout). Athena compares it with the enrolled voices ([Enroll My Voice](modules/athena.md#enroll-my-voice)):
a match gives `verified: true` and its `is_owner` and `name`; no match above Athena's
threshold gives `verified: true, is_owner: false`; too little speech (< 1 s), a 404 (an
older Athena), a timeout, a refusal or no Athena set up give `speaker: null`. **The audio
is never written or logged**: it is held only by that task and dropped when Athena
answers; only the outcome is logged.

### Barge-in

`fork/voice/interrupts.py`; the Echo side is `odysseus/BargeIn.kt` in `SDarioosh/odysseus-echo`
(0.14.0+odysseus.14).

Saying the wake word while the Echo is still thinking starts a new question. Home
Assistant runs one Assist pipeline per satellite, and the Echo starts the new question
only once Home Assistant has ended the interrupted run (its `HaRunGate`: starting it sooner
leaves the satellite deaf). Home Assistant ends that run only when Odysseus has answered,
and the Echo waits at most 15 s, so a question that looks things up was dropped: the
device went quiet instead of taking the new question.

So on a barge-in the Echo also posts:

`POST /api/chat/voice/v1/satellite/interrupt` (the `voice` token), a form with
`device_id` (the id it posts with `/satellite/turn`). The hub cuts that device's running
voice turn short: the turn and its model call stop (the new question doesn't queue behind
it on the model server), and Home Assistant gets what was already said, usually nothing
(no apology), which it doesn't speak; it ends the run within about a second. Answer:
`{"ok": true, "cut": <turns cut>, "marked": <contexts marked>}`.

- **Which turn:** the one whose satellite context came from that device. The Echo posts
  the context in the background, so Home Assistant's question can arrive first: a context
  posted within 3 s (`ADOPT_SECONDS`) after a turn without one started belongs to that
  turn. When Home Assistant hasn't asked yet, the device's waiting context is marked and
  the turn that claims it answers empty at once. A turn without a context (another
  satellite, Home Assistant's text Assist) is never cut.
- **The cut turn:** the ledger keeps the question with the answer said so far (usually
  empty); a change it kept for a "yes" is dropped (nobody heard the question).
- **After a long "Thinking…"** (over 15 s, when the Echo stops counting the turn as
  running) a wake word interrupts too: the Echo asks whenever Home Assistant still has a
  run for it.
- Without the route (an older hub, HTTP 404) the Echo waits for the old answer as before.
- In this process's memory only, like the satellite contexts.

The sandbox reproduction (a real Home Assistant, an Echo emulator, a slow model) is in
[sandbox.md](sandbox.md#voice-barge-in).

### Owner-only answers

`personal_allowed(ctx)`, setting `FORK_VOICE_PRIVATE`:

- `off` (default, and any value other than `owner`): every voice turn may use personal
  tools, as before.
- `owner`: personal tools (Athena, calendar, notes, contacts, memory, beliefs, documents,
  chat search) run only when the turn's context has a speaker that Athena verified as the
  owner. Otherwise they answer "I can only answer that for the owner of this assistant."
  (`PRIVATE_REPLY`); web and weather stay allowed. No context, no audio and "couldn't
  check" all count as not the owner.

### Announcements

`fork/voice/announce.py`: `await announce(message, entities=None) -> bool` speaks a message
on the satellites through Home Assistant's `assist_satellite.announce` service
(`POST <Home Assistant>/api/services/assist_satellite/announce`, `{"entity_id": [...],
"message": "..."}`, `Authorization: Bearer <FORK_HA_TOKEN>`, 10 s timeout, no redirects).
Home Assistant speaks it with its own local text-to-speech. It never raises; a refusal is
logged without the message. Later features (a spoken daily briefing) call it the same way.

- **Reminders:** when a note reminder fires (the note-reminder scanner in
  `src/builtin_actions.py`, the same dispatch that sends ntfy/email/browser), the hook
  `announce_reminder(title)` also says "Reminder: <title>", in the background: the
  reminder itself never waits for or fails on Home Assistant. Off until there is a Home
  Assistant address, `FORK_HA_TOKEN` and `FORK_VOICE_ANNOUNCE_ENTITIES` are set.
- **Feed:** a reminder is also kept for the devices' feed with `kind: "reminder"`, its
  to-do `ref` (`todo:<note id>`) and target every device, even without Home Assistant
  (`announce_reminder(title, owner=…, ref=…)`). Per-reminder rooms can come later.
- **Test:** `POST /api/fork/voice/announce` (signed-in admin, browser), `{"message"?: str}`
  → `{"ok": true|false, "problem": "…"|null}`. With `"device_id"` it is an announcement to
  that one device instead (titled "Test announcement"): kept for its feed only, or also
  spoken with `"speak": true` (through `deliver`) → `{"ok", "problem", "id", "spoken"}`.
  [`scripts/echo_verify.py`](#checking-a-server-for-the-echos) sends one to a made-up
  device id, so no screen shows it and nothing is said. Spoken, a device target is said
  only on that device's satellite; one that matches none is said nowhere (`spoken:
  false`, `problem` "no Echo for … can speak it").
- **One Home Assistant address:** announcements and the Echo link use
  `fork/voice/ha_settings.py`'s `home_assistant_url()`: the address **Set up Home
  Assistant** saved (only after a setup that succeeded), or `FORK_HA_URL` when it is set,
  which overrides it.
- **Only a local Home Assistant:** the address must be `http(s)://host[:port][/path]` with
  a host that is local, on a private network or in Tailscale's range; a name is resolved
  and every address must be one (`announce.check_url`). **Set up Home Assistant** and
  `scripts/ha_voice_setup.py` apply the same rule before signing in. A public host, a URL
  with credentials, a query or a fragment is refused.

### Rooms and devices

`fork/voice/devices.py`. Each Echo says who and where it is:

`POST /api/chat/voice/v1/satellite/hello` (the `voice` token), a form: `device_id`
(required, ≤ 128 characters), `device_name`, `area` (≤ 100), `app_version_code` (a whole
number). Sent at start and hourly. Answer `{"ok": true}`. Every `POST …/satellite/turn`
records the same (`device_id`, `device_name`, `area`); a field a request leaves blank keeps
what was stored. One row per (owner, device) in `<DATA_DIR>/fork_voice.db`
(`satellite_devices`) with its last-seen time; a device unheard for 90 days is dropped, at
most 64 per owner.

### Device settings

`fork/voice/device_settings.py` (store and checks), `device_settings_routes.py` (routes);
contract C6 of the Echo plan (odysseus-echo `docs/odysseus/plans/2026-11-done-followup-settings-verify.md`).
The owner sets some of each Echo's own settings from Odysseus; the device fetches them, so
the hub never needs to reach the device.

`GET /api/chat/voice/v1/devices/{device_id}/settings` (the `voice` token; the token owner's
device only, another owner's id reads as unset):

```json
{"version": 7, "settings": {"kiosk.return_seconds": 600, "today.enabled": true}}
```

A key that is absent means "the device decides"; nothing set is `{"version": 0,
"settings": {}}`. Answered with `Cache-Control: no-store`. The keys (only these are
stored; the device ignores keys it doesn't know):

| Key | Type | Device setting |
|---|---|---|
| `home.enabled` | bool | clock home screen |
| `home.idle_seconds` | int 0..86400 (0 = never) | back to the clock after |
| `home.weather_enabled` | bool | weather on the clock |
| `today.enabled` | bool | Today page |
| `kiosk.return_seconds` | int 0..86400 (0 = never) | back from other apps after |
| `kiosk.auto_reload` | bool | reload a failed dashboard |
| `record.enabled` | bool | recording |
| `record.clock_button` | bool | record button on the clock |
| `record.privacy_button` | `"record"` / `"mute"` | privacy button |
| `followup.enabled` | bool | answer without the wake word |
| `followup.seconds` | int 3..15 | how long it listens |

- `version` goes up on every change (a save that changes nothing keeps it) and never goes
  back, also when everything is left to the device again. One row per (owner, device) in
  `<DATA_DIR>/fork_voice.db` (`device_settings`); it stays when the device leaves the
  registry.
- On every change the device's [feed](#the-devices-feed) gets a `kind: "settings"` item,
  so the Echo (which polls the feed every 30 s) fetches the settings within 30 s. The
  Echo also fetches them at start and after each hello (hourly). It shows no card for
  that item.
- Each change is logged with owner, device and the changed keys, never the values.

**Settings card.** Settings → Integrations → Voice assistant → **Echo devices**
(`static/js/forkVoiceDevices.js`, admin only) lists the admin's Echos from the
[registry](#rooms-and-devices) (name, room, app version code, last seen); each opens to the
keys above, each "Device decides" or a value, and **Save** sends the whole set:

- `GET /api/fork/voice/devices` (signed-in admin, browser; no API token): `{"devices":
  [{device_id, name, area, app_version_code, last_seen, version, settings}], "keys":
  [{key, type, label, min?, max?, choices?, hint?}]}`, the admin's own devices (those whose
  tokens the admin owns), most recently seen first.
- `PUT /api/fork/voice/devices/{device_id}/settings` (the same), `{"settings": {key: value
  or null}}`: the device's whole set (left out or null: the device decides). 400 for an
  unknown key, a value of the wrong type (`1` is not `true`, `"60"` not `60`) or out of
  range, or another body; 404 for a device not in the admin's registry. Answers `{"version",
  "settings"}`.

### Announcing to a room or device

`fork/voice/announcements.py`: `await deliver(owner, message, target, kind=…, title=…,
ref=…)` keeps the announcement for the devices' feed and speaks it. `target` is `"all"`,
`{"area": "Kitchen"}` or `{"device_id": "…"}`:

- `"all"`: the configured `FORK_VOICE_ANNOUNCE_ENTITIES`, as before (Home Assistant isn't
  asked).
- An area: Home Assistant's `assist_satellite.*` entities in that area. The hub asks once
  per announcement with one **fixed** template through `POST <Home Assistant>/api/template`
  (`areas()`, `area_entities()`, `area_name()`, the entities' names); what the owner said is
  never put into a template. The area matches by id or name (`Living room` =
  `living_room`).
- A device: the satellite whose name starts with the device's name (exactly one), else the
  satellites of the device's room.
- No satellite found for an area or device, or Home Assistant can't list them: it is kept
  for that target's feed (its screens show it) but **spoken nowhere**, never on the
  configured list instead, so "tell the bedroom" is never said in every room. `deliver`
  returns `spoken: false`, `no_satellite: true` and a `problem` such as "no Echo in the
  Bedroom can speak it" (or "no Echo for Bedroom Echo can speak it"). Only `"all"` (the
  announce tool's "everywhere", reminders) uses the configured list.
- The same rules as before: Home Assistant only on the owner's network (`check_url`), its
  token from `FORK_HA_TOKEN`; never raises. Without a Home Assistant address and token the
  announcement is only kept for the feed.

#### The devices' feed

`GET /api/chat/voice/v1/announcements?device_id=…&since=<epoch ms>` (the `voice` token):

```json
{"announcements": [
  {"id": "an_1f2e3d4c5b6a7980", "at": 1762434000000, "kind": "reminder",
   "title": "Take the bins out", "text": "Reminder: Take the bins out",
   "target": {"all": true}, "ref": "todo:41", "expires_at": 1762437600000}
]}
```

- `kind`: `reminder`, `announcement` or `settings`. A `settings` item (title "Settings",
  `ref` `settings:<version>`, target that device, shown 10 minutes) only says the device's
  [hub-managed settings](#device-settings) changed: the device fetches them again and
  **shows no card and speaks nothing for it**. (Echo builds from before the hub-managed
  settings show it as a card reading "Settings changed in Odysseus.") `target`: always an object: `{"all": true}`,
  `{"area": "…"}` or `{"device_id": "…"}`. `ref`: the to-do note of a reminder, else null.
- Only what is meant for that device: every device, its area (as its last hello or turn
  said; compared like Home Assistant's ids) or its `device_id`; only the token owner's.
- Made after `since`, not yet expired (`expires_at`, 1 h after it was made), oldest first,
  at most 50. Rows are deleted 24 h after they were made. The device shows each `id` once.
- `device_id` is required (422 without).

### The announce tool

`fork/voice/announce_tool.py`, in the brain's `home` tool group: "tell the kitchen dinner
is ready". Arguments `message` and `to` (a room or device name; empty or "everywhere" for
every room), matched against the devices above, then against Home Assistant's rooms that
have a satellite. Offered to an admin (or single-user) owner while Home Assistant's address
and token are set. Speaking aloud is a write:

- **Chat:** it asks on an approval card like every brain write (`needs_approval`), and runs
  only with that exact approval, which the tool claims for this exact call
  (`run_approved`); without one it is refused and nothing is said.
- **Voice:** it is on the allow-list of changes kept for a spoken "yes"
  (`fork/voice/confirm.py`, `WRITABLE`): "Announce “Dinner is ready” in the Kitchen?". A
  room nobody knows is refused. The ledger records it as `kind: "announcement"`.
- A room the hub doesn't know is an error that names the known rooms; Home Assistant down
  still shows it on the devices' screens (the result says it wasn't spoken). A room or
  device Home Assistant has no satellite for is spoken nowhere; the result tells the model
  "Not spoken aloud anywhere: no Echo in the Bedroom can speak it…" to pass on.

### Settings

| Variable | Meaning |
|---|---|
| `FORK_VOICE_PRIVATE` | `off` (default) or `owner` |
| `FORK_HA_URL` | optional: overrides the address Set up Home Assistant saved, e.g. `http://192.168.1.20:8123` (local hosts only) |
| `FORK_HA_TOKEN` | a Home Assistant long-lived access token (a secret: `.env` only) |
| `FORK_VOICE_ANNOUNCE_ENTITIES` | comma list, e.g. `assist_satellite.kitchen_echo` |

### Security trade-offs

- A voiceprint is not a password: a recording of the owner, or a close voice, can pass
  Athena's threshold. `FORK_VOICE_PRIVATE=owner` keeps casual visitors out of personal
  answers, nothing more. Without it (the default), anyone near a satellite can ask.
- Contexts are matched to turns by owner and time only (the newest within 10 s; Home
  Assistant doesn't say which satellite asked): two satellites of one owner asking at the
  same moment could swap contexts. Each context is used once, an older unclaimed one is
  dropped rather than used later, and a missing one never grants more.
- The voice token can now also send audio to Athena's identify route; Athena only compares
  it and writes nothing.
- The announce tool lets the owner speak in any room; a guest can't run it by voice with
  `FORK_VOICE_PRIVATE=owner` (the "yes" is refused like every change), and in chat it
  always needs the owner's approval. The feed holds reminder titles for 24 h, readable
  with any of the owner's voice tokens.
- Announcements speak reminder titles aloud in the room, to whoever is there; the
  Home Assistant token can control the owner's home, so it stays in `.env` only and is
  only ever sent to the private host.

Checked in the sandbox (Home Assistant 2026.2.3, Extended OpenAI Conversation 3.0.0, a
fake Odysseus): the agent sends no `user` field, `stream: true`, model `odysseus`, no
tools, and a follow-up in the same Home Assistant conversation carries the earlier
turns, so Odysseus continues the chat by its first question (since 2026-11-04 also the
first follow-up: `fork/voice/sessions.py`). Home Assistant keeps a satellite's
conversation for 5 minutes after its last turn, so a reply after waking the Echo again
continues it too.

## Timings

Every voice turn saves how long each step took, so slow steps can be found and the
effect of a change measured. Two clocks measure a turn and each keeps its own durations
(no step mixes the server's clock with the Echo's, so a clock difference can't skew one):

| Step | Clock | From → to |
|---|---|---|
| Wake word to listening | Echo | wake word → Home Assistant streams the mic |
| You speaking | Echo | speech started → speech stopped |
| Speech to text (HA) | Echo | speech stopped → transcript |
| Odysseus: getting ready | Odysseus | request in → turn ready (chat, satellite context) |
| Odysseus: model's first token | Odysseus | turn ready → first token (prompt processing) |
| Odysseus: thinking | Odysseus | first token → first answer text |
| Odysseus: tool calls (all) | Odysseus | each tool call, start → output, added up |
| Odysseus: first sentence sent | Odysseus | request in → first sentence back to Home Assistant |
| Odysseus: whole answer | Odysseus | request in → answer complete |
| Answer, as the device waited | Echo | transcript → Home Assistant asks for speech |
| Text to speech: first audio | Echo | speech asked for → first audio arrives |
| Answer playing | Echo | first audio → playback done |
| Silence after you stopped | Echo | speech stopped → first audio (the wait you hear) |
| Whole turn | Echo | wake word → turn ended |

"Answer, as the device waited" minus "Odysseus: whole answer" (or "first sentence sent",
when Home Assistant speaks a streamed answer as it comes) is Home Assistant's own share.

- **Odysseus's half** (`fork/voice/timings.py`, `HubTimer`) is read off the brain's
  events in `fork/voice/routes.py`, saved when the turn starts and updated at its end.
  It also keeps the tool names with their seconds, the rounds, the token counts and the
  outcome (answered, fixed reply, interrupted, failed).
- **The Echo's half**: when a turn ends, the Echo posts its stage times to
  `POST /api/chat/voice/v1/satellite/timing` (voice token), JSON `{"device_id",
  "started_at" (epoch ms of the wake word), "marks": {"started", "listening",
  "voice_started", "voice_stopped", "awaiting_response", "awaiting_tts", "starting_tts",
  "streaming_tts", "completed_tts", "ended": ms after the wake word}, "flags":
  {"thinking_sound", "barge_in", "voice_check"}, "start_mode"}` (any mark may be missing).
  It joins the turn of the same device whose request came between the wake word and the
  Echo's end (10 s slack); a command Home Assistant answered itself gets a row of its own,
  with the Echo's times only.
- Stored in `<DATA_DIR>/fork_voice_timings.db`, 90 days: numbers, the device id and tool
  names only, never the question, the answer or audio.
- **Read them:** `python scripts/voice_timings.py` (last 7 days; `--days 1`, `--last 10`
  for the newest turns one by one, `--csv timings.csv` for a spreadsheet), or
  `GET /api/fork/voice/timings?days=7` signed in as admin (JSON).
- Home Assistant's own trace of a run (Settings → Voice assistants → the pipeline's ⋮ →
  Debug) shows its stages too, but only for the last few runs.

## App updates

The Echo app ([odysseus-echo](https://github.com/SDarioosh/odysseus-echo)) updates itself
from Odysseus, not from GitHub (`fork/voice/app_release.py`). The owner publishes a build on
the server; each Echo asks for the release on offer with its voice token and offers the
install (Android shows its own prompt; no silent install).

| Route | Who | What |
|---|---|---|
| `GET /api/chat/voice/v1/app/latest` | API token with the `voice` scope | `{"app": "odysseus-echo", "version_name", "version_code", "size", "sha256", "notes"}`; 404 `{"error": "no release"}` when none is published |
| `GET /api/chat/voice/v1/app/apk` | the same | the APK, streamed from disk: `application/vnd.android.package-archive`, `Content-Length`, `ETag` = the sha256 in quotes, Range requests answered (206); the same 404 |

- A device compares **`version_code`**, never the name (semver ignores `+odysseus.N`), and
  checks the download's size and sha256.
- Storage: `<DATA_DIR>/echo_app/odysseus-echo.apk` and `release.json` (the fields above).
  A `release.json` whose APK is missing or another size counts as no release.
- Publishing, as the user that owns the data folder (no restart needed):

  ```bash
  python scripts/echo_release.py odysseus-echo-release.apk \
      --version-name 0.14.0+odysseus.30 --version-code 1400030 \
      --notes "Kiosk settings; Today page." --data-dir /srv/odysseus/data
  python scripts/echo_release.py --show --data-dir /srv/odysseus/data
  ```

  The script copies the APK to a temp file in `echo_app/` while working out its sha256,
  renames it into place, then writes `release.json` the same way; two publishes at once
  take turns (a lock on `echo_app/.publish.lock`). It refuses a file that
  isn't a zip, and a `--version-code` lower than the published one unless `--force` (an
  Echo would never install it). It doesn't read the APK's manifest: pass what the Echo
  build printed (`./gradlew printVersionName printVersionCode`). Releases signed with a
  different key than the installed app don't install (odysseus-echo `docs/odysseus/build.md`).
- Not built yet: an upload form on the Settings card (the script is the only way in).

## Linking an Echo

An Echo Show running our VACA fork ([odysseus-echo](https://github.com/SDarioosh/odysseus-echo),
`docs/odysseus/setup.md`) is set up from the owner's phone with one QR code, with no typing
on the Echo. The Odysseus part is `fork/voice/echo_link.py`. The Echo's setup page sends the
phone to `GET /echo/link?return=<Echo URL>&name=<name>`. The signed-in admin sees "Link Echo
'Kitchen' at <host>:10801?" and taps **Approve**. Odysseus then makes a token with the
`voice` and `athena:ingest` scopes and nothing else (ask the voice assistant; upload the
Echo's recordings and voice notes through the Athena relay, see
[Athena](modules/athena.md#pairing-a-phone)), named `Echo <name> <YYYY-MM-DD>`
(revoke it in Settings → API tokens like any other), and remembers its id as an Echo token
(`<DATA_DIR>/echo_tokens_<owner>.json`). Echos linked earlier have a `voice`-only token: it
keeps working for the voice assistant, uploads answer 403, and linking the Echo again gives
it the new token. Linking a named Echo again deletes its older tokens of that name, keeping
the new one and the most recently used earlier one (so an Echo the phone never reached still
works); unnamed Echos keep theirs (`echo_link.prune_echo_tokens`). Only Echo tokens are
deleted: named `Echo <name> <date>`, holding nothing but `voice` and `athena:ingest`, and
either remembered as made here or `voice`-only (made before the marks). A token you made by
hand with the same name and scopes is never deleted. It sends the phone back to the Echo with `token`, `base_url` (`<Odysseus
address>/api/chat/voice/v1`) and `ha_url` in the URL fragment (`<Echo URL>#token=…`).

The approve page has both addresses for this Echo, to change before **Approve**:

- **Odysseus address the Echo uses**: empty means the address you opened Odysseus at.
- **Home Assistant address the Echo uses**: the Echo connects to it, and it also sends
  your phone there to sign in to Home Assistant, so it must be one both reach. Empty: the
  Echo asks for it on its own page.
- The page starts with the addresses the last Echo **on the same network** got: an Echo
  reached on the home network (its address is a home-network one) is offered the
  home-network addresses, one reached over Tailscale the Tailscale ones, so pairing at
  home uses the home network and pairing away uses Tailscale. The first time, the Home
  Assistant address saved by **Set up Home Assistant** (or `FORK_HA_URL`) is offered when
  it is on that network, else nothing. Kept in upstream's settings file as
  `fork_voice_echo_addresses`.
- Both must be on the owner's network, by the same rule as the Echo's address below
  (private or Tailscale IPv4, or a name that is one label or ends in `.ts.net`, `.local`,
  `.lan` or `.home.arpa`; never `localhost`). Anything else shows the page again with the
  reason, and no token is made. The Echo's
page passes them on to the Echo and clears them from the address bar. The Echo then sends
the phone to Home Assistant's sign-in, and adds itself to Home Assistant.

- A fragment never reaches a server or a referrer: it stays only in the phone's browser,
  and the jump replaces the page, so it isn't in the back history either. It is a plain
  link, not a form, so the browser shows no "form isn't secure" warning for the Echo's
  http address.
- The Echo's address must be `http://<host>:10801/setup/<code>/odysseus` on a private
  (10/8, 172.16/12, 192.168/16) or Tailscale (100.64/10) address, or a name that is one
  label or ends in `.ts.net`, `.local`, `.lan` or `.home.arpa`. `localhost` and anything
  else are refused.
- When the Echo isn't reached over Tailscale, the page says the token crosses the home
  network unencrypted on its way to the Echo.
- Approve works only from that page (same-origin POST, admin session; API tokens can't),
  at most 10 times per 10 minutes. Neither page is cached, sends a referrer or can be
  framed.
- If Odysseus asks you to sign in first, it opens its home page afterwards: go back to the
  Echo's page and tap Continue again.

## Checking a server for the Echos

`scripts/echo_verify.py` (standard library only) calls the routes an Echo calls, with an
Echo's token (from **Link this Echo**: `voice` and `athena:ingest`), and prints one line per
check, PASS, FAIL or SKIP; the exit code is 1 when one failed. Run it from any machine that
reaches the hub (the server itself, or the Mac over Tailscale):

```bash
python scripts/echo_verify.py --url http://192.168.1.10:7000 --token-file echo.token
# with a real Echo's id (odysseus-echo scripts/odysseus/verify_device.sh prints it) and an
# admin sign-in for the test announcement:
python scripts/echo_verify.py --url http://192.168.1.10:7000 --token-file echo.token \
    --device-id <the Echo's id> --admin-user <admin> --password-file admin.txt
```

| Check | Route | PASS when |
|---|---|---|
| no token is refused | `GET …/today` without a token | 401 or 403 |
| token has the voice scope | `GET …/models` | 200 with model `odysseus` |
| today | `GET …/today` | the fields of [Today for devices](#today-for-devices); prints counts only |
| app/latest, app/apk | `GET …/app/latest`, `…/app/apk` (`Range: bytes=0-3`) | a release with version code, size and sha256, and the APK answers 206 with a zip header and the sha256 as ETag; or 404 "no release" (apk skipped). `--full-apk` downloads it and checks the sha256 |
| satellite/hello | `POST …/satellite/hello` | only with `--device-id`: sends the id alone, so the stored name, room and version stay; `{"ok": true}` |
| announcements feed | `GET …/announcements` | the feed's shape, for `--device-id` or a made-up id |
| Athena through the hub | `GET /api/codex/athena/health` | 200 (403: the token has no `athena:ingest`, link the Echo again) |
| test voice note | `POST /api/codex/athena/chunk` (`codec=wav`, `kind=note`), then `…/sessions/{id}/end` | both answer ok |
| test announcement | `POST /api/fork/voice/announce`, then the feed | only with `--admin-user` and `--password-file`: it is in that device's feed |

What it changes, and nothing else:

- **The test voice note** is 3 s of silence from device `echo-verify`, session
  `echo-verify-<UTC time>`. Silence has no speech, so Athena keeps no conversation and deletes
  the audio after processing; its session row stays (status `done`), because neither the
  hub nor Athena has a route that deletes an audio session. `--no-voice-note` skips it.
- **The test announcement** ("Odysseus verification test, please ignore (<time>).") goes to
  the feed of a made-up device `echo-verify-<UTC time>`: no Echo shows it and nothing is
  spoken; it expires after an hour and is deleted after a day. With `--speak` it goes to
  `--device-id` instead: that Echo shows it and Home Assistant says it aloud on that Echo's
  satellite (by its name, else its room); with none for it, nothing is said anywhere and
  the check fails with "no Echo for … can speak it". The token's
  owner and the admin must be the same user, or the feed check fails.
- **hello** with `--device-id` refreshes that device's last-seen time.

The token is never printed, and of the owner's data only counts and versions are.

### Sandbox run (2026-11-06)

Round 1 end to end in the sandbox: the hub on 127.0.0.1:7010 (fictional notes and events),
Athena's ingest API and MCP HTTP server from its checkout (energy VAD; no whisper-server or
llama-server, so a note with sound in it fails at transcription, as expected), an Echo token
made by `/echo/link`'s approve POST, `curl` and `echo_verify.py` over HTTP:

| Check | Result |
|---|---|
| `/echo/link` approve → handoff fragment with `token`, `base_url`, `ha_url` | pass |
| `GET …/models`, `…/today` (an event, a due to-do, an Athena action item), no token 401 | pass |
| `GET …/app/latest`, `…/app/apk` (200, 206 to a Range, sha256 = ETag) after `echo_release.py` | pass; 404 "no release" before |
| `POST …/satellite/hello` (stored; a bad version 422), `GET …/announcements` (422 without `device_id`) | pass |
| Echo token on `/api/codex/athena/health` 200, on `…/conversations` 403 (no read scope) | pass |
| 3 s WAV voice note, `kind=note`, through `/api/codex/athena/chunk`, `/end` | pass: Athena's session has `kind: "note"`, status `ended`; a stereo WAV and `kind=memo` 400 |
| silent WAV note | Athena: status `done`, no conversation, audio deleted |
| `echo_verify.py` with `--device-id`, admin sign-in | 10 passed |

### Sandbox run, round 2 (2026-11-06)

The same stack, with Done and device settings, over HTTP (an Echo token, a second owner's
Echo token, tokens with only `chat` or `athena:ingest`, an admin session). The answers are
fixtures of the Echo app's parser tests (odysseus-echo `app/src/test/resources/odysseus/hub/`).

| Check | Result |
|---|---|
| `…/today` with a stored insight, events (all-day, daily, timed), to-dos (overdue, today, pinned), Athena's items; another owner's (empty); Athena stopped (`action_items` `[]`, 200); an owner zone of UTC+1 | pass |
| a note due now: the reminder scanner puts a `reminder` (target `{"all": true}`, ref `todo:<note id>`) in the feed | pass |
| hello (form, multipart, repeated; 422 without id or with a bad version, 415 JSON, 413), feed (`since`, other owner, 422) | pass |
| test announcement to a device, area announcement (`{"area": "kitchen"}` reaches the device that said "Kitchen"), API token 403 | pass |
| `todos/done` + undo, `reminders/done` (note archived; an item ref ticks the item), `action-items/done` + undo against Athena: already, 400, 404, 409 (never a device's, after 10 min), 503 with Athena stopped, other owner 404 | pass |
| device settings: GET before any, admin PUT (all keys, partial, null, same again keeps the version), 400s, 404, 403 for a token; a `settings` feed item per change | pass |
| WAV note through the relay, `/end`, duplicate chunk, `kind=memo` and stereo 400 | pass |
| `echo_verify.py` | failed `today` (focus rows naming events) and the feed (`kind: "settings"`); fixed, then 10 passed |

## Later

- The answer's link labels (conversation titles) in `links[].label`.
- More writable tools (tick a to-do done: `manage_notes` `toggle_item`): one entry in
  `confirm.WRITABLE` each.

## Files and tests

- Timings: `fork/voice/timings.py`, `timing_routes.py`, `scripts/voice_timings.py`; tests
  `tests/fork/test_voice_timings.py`.
- Device settings: `fork/voice/device_settings.py`, `device_settings_routes.py`,
  `static/js/forkVoiceDevices.js`; tests `tests/fork/test_voice_device_settings.py`.
- App updates: `fork/voice/app_release.py`, `scripts/echo_release.py`; tests
  `tests/fork/test_echo_app_release.py`.
- Checking a server: `scripts/echo_verify.py`; tests `tests/fork/test_echo_verify.py`.
- `fork/voice/routes.py`, `openai_api.py`, `sessions.py`, `speech.py`, `prompt.py`,
  `chat_prefix.py` (the chat's preset), `policy.py` (what runs), `confirm.py` (changes
  after a yes), `ledger.py` (turns and actions). The satellite's context and
  announcements are `fork/voice/satellite.py` and `announce.py`, optional here (the routes
  import the satellite module lazily and work without it).
- Satellites and announcements: `fork/voice/satellite.py`, `satellite_routes.py`,
  `announce.py`, `announcements.py` (targets, the feed), `devices.py` (the registry),
  `announce_tool.py` (the agent's tool); the hook in `src/builtin_actions.py` (`ping_notes`). Barge-in:
  `fork/voice/interrupts.py`; its sandbox: `scripts/voice_barge_sandbox/`.
- In the brain: `read_only_turn` (`fork/brain/turn.py`: `shared_prefix`, `preset`,
  `gate`, `context_lines`, `no_memory`), `ToolRun.gate` and `read_only_refusal`
  (`fork/brain/tools.py`), `can_read` and `read_only` (`fork/brain/toolset.py`).
- `fork/voice/ha_setup.py` (Home Assistant client and the setup run), `ha_routes.py`
  (sign-in and callback), `ha_settings.py` (the saved address);
  `scripts/ha_voice_setup.py` (the same without a browser); `echo_link.py` (Link this Echo).
- `static/js/forkVoiceAssistant.js`: the Settings card, with "Set up Home Assistant".
- Hooks: `routes/api_token_routes.py` (scope `voice`), `static/index.html` (the card's
  script tag), `requirements.txt` (`websockets`, for Home Assistant's websocket API); the
  routers are in `fork/routes/__init__.py`.
- Done from a device: `fork/voice/done.py`; tests `tests/fork/test_voice_done.py`.
- Tests: `tests/fork/test_voice_assistant.py`, `tests/fork/test_voice_satellite.py`,
  `tests/fork/test_voice_interrupts.py` (barge-in),
  `tests/fork/test_ha_voice_setup.py` (a fake Home Assistant), `tests/fork/test_echo_link.py`.
