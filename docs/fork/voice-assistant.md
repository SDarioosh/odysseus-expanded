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
| `POST /api/fork/voice/token` | signed-in admin (browser) | makes a token with only the `voice` scope (the Settings card) |
| `GET /api/fork/voice/ha` | the same | the saved Home Assistant address and the last setup's report |
| `POST /api/fork/voice/ha/connect` | the same, same-origin | starts "Set up Home Assistant": answers HA's login address ([Setup](#setup-owner)) |
| `GET /api/fork/voice/ha/callback` | the same | Home Assistant's redirect after the login: runs the setup, revokes the login, shows the steps |

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
`created` or `completed`; `source` `odysseus` or `athena`.

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
   **Odysseus address as Home Assistant reaches it** (prefilled with the address the
   browser uses, e.g. `https://odysseus.<tailnet>.ts.net`), and press **Set up Home
   Assistant**.
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
- **Test:** `POST /api/fork/voice/announce` (signed-in admin, browser), `{"message"?: str}`
  → `{"ok": true|false, "problem": "…"|null}`.
- **One Home Assistant address:** announcements and the Echo link use
  `fork/voice/ha_settings.py`'s `home_assistant_url()`: the address **Set up Home
  Assistant** saved (only after a setup that succeeded), or `FORK_HA_URL` when it is set,
  which overrides it.
- **Only a local Home Assistant:** the address must be `http(s)://host[:port][/path]` with
  a host that is local, on a private network or in Tailscale's range; a name is resolved
  and every address must be one (`announce.check_url`). **Set up Home Assistant** and
  `scripts/ha_voice_setup.py` apply the same rule before signing in. A public host, a URL
  with credentials, a query or a fragment is refused.

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

## Linking an Echo

An Echo Show running our VACA fork ([odysseus-echo](https://github.com/SDarioosh/odysseus-echo),
`docs/odysseus/setup.md`) is set up from the owner's phone with one QR code, with no typing
on the Echo. The Odysseus part is `fork/voice/echo_link.py`. The Echo's setup page sends the
phone to `GET /echo/link?return=<Echo URL>&name=<name>`. The signed-in admin sees "Link Echo
'Kitchen' at <host>:10801?" and taps **Approve**. Odysseus then makes a token with only the
`voice` scope, named `Echo <name> <YYYY-MM-DD>` (revoke it in Settings → API tokens like any
other). Linking a named Echo again deletes its older tokens of that name, keeping the new one
and the most recently used earlier one (so an Echo the phone never reached still works);
unnamed Echos keep theirs (`echo_link.prune_echo_tokens`). It sends the phone back to the Echo with `token`, `base_url` (`<the address you
opened Odysseus at>/api/chat/voice/v1`) and `ha_url` (the Home Assistant address saved by
**Set up Home Assistant** or set as `FORK_HA_URL`, or empty) in the URL fragment (`<Echo URL>#token=…`). The Echo's
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

## Later

- The answer's link labels (conversation titles) in `links[].label`.
- More writable tools (tick a to-do done: `manage_notes` `toggle_item`): one entry in
  `confirm.WRITABLE` each.

## Files and tests

- `fork/voice/routes.py`, `openai_api.py`, `sessions.py`, `speech.py`, `prompt.py`,
  `chat_prefix.py` (the chat's preset), `policy.py` (what runs), `confirm.py` (changes
  after a yes), `ledger.py` (turns and actions). The satellite's context and
  announcements are `fork/voice/satellite.py` and `announce.py`, optional here (the routes
  import the satellite module lazily and work without it).
- Satellites and announcements: `fork/voice/satellite.py`, `satellite_routes.py`,
  `announce.py`; the hook in `src/builtin_actions.py` (`ping_notes`). Barge-in:
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
- Tests: `tests/fork/test_voice_assistant.py`, `tests/fork/test_voice_satellite.py`,
  `tests/fork/test_voice_interrupts.py` (barge-in),
  `tests/fork/test_ha_voice_setup.py` (a fake Home Assistant), `tests/fork/test_echo_link.py`.
