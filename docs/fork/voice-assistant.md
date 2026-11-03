# Voice assistant (Home Assistant)

Status: built (2026-11-03), hub side only. Needs the owner's Home Assistant and a voice
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
                                                               a read-only brain turn
                                                               (Athena over MCP, calendar, …)
   3. text-to-speech: local Piper (or Kokoro)
   │ reply audio
satellite
```

Nothing leaves the owner's network: every stage is local, and Home Assistant Cloud
(Nabu Casa) speech is not used.

- **Voice looks things up, and adds and completes items.** Every voice question is a
  read-only turn (no approvals: nobody can approve by voice; no email) with two
  exceptions while the owner setting is on: adding a to-do, reminder, event, note or list
  line, and marking one item done ([Voice actions](#voice-actions)). Nothing else changes
  by voice.
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
| `POST /api/fork/voice/token` | signed-in admin (browser) | makes a token with only the `voice` scope (the Settings card) |

Only a bearer token with the `voice` scope may call the first two: a browser login may
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
"odysseus": {"session_id": "<the chat's id>", "links": ["https://…/view/12?sig=…#t=34"]}
```

Errors are OpenAI's `{"error": {"message", "type"}}`: 400 for a request we can't answer,
403 without a voice token, 503 when the brain is off (`FORK_BRAIN`), the owner may not
use the agent, or no default chat model is set.

## How a question is answered

`fork/brain/turn.py`, `read_only_turn`: the same loop as chat (the brain,
[brain.md](brain.md)), with the same prompt, history, tools and owner checks, and these
differences:

- **It runs as the token's owner.** Upstream keeps every run driven by an API token away
  from MCP and the personal stores (`delegated_tool_is_blocked` in
  `src/tool_capabilities.py`), and a voice question is about exactly those. A voice turn
  is therefore not a delegated run. In place of that cap:
- **Only tools that can read are offered** (`toolset.read_only`): the read tools,
  personal stores that have read actions (calendar, notes, contacts, memory), and
  Athena's tools that declare `readOnlyHint: true` on a trusted server
  (`ODYSSEUS_TRUSTED_READONLY_MCP`, see [mcp-servers.md](features/mcp-servers.md)). So
  `update_action_item`, `erase_fact`, `generate_image`, document writes and the like are
  never offered. Email tools are left out as well (`LEFT_OUT` in `fork/voice/routes.py`):
  the answer is read aloud, to anyone in the room.
- **Every write is refused, never asked** (`tools.read_only_refusal`): a write action of
  an offered store (`manage_calendar` `create_event`, …), a call upstream's approval gate
  wouldn't let through, and a `web_fetch` of an address that is neither in the question
  nor a search hit. The model is told "Not run: this request can only read", and the turn
  never pauses for an approval. The only exceptions are the turn's own actions, by name
  (`voice_add`, `voice_complete`, [below](#voice-actions)), given to `read_only_turn` as
  `actions`; with the setting off it gets none.
- **The voice instructions** (`fork/voice/prompt.py`) stand where a chat's preset text
  goes: the owner's tools first for questions about their life (as in chat), then one to
  three spoken sentences, no markdown, numbers and times as spoken, links at the end, "I
  can't make changes by voice" for change requests. With voice actions on, the last rule
  is replaced (`ACTION_INSTRUCTIONS`), see [below](#voice-actions). A first version that also asked for
  "the answer first" and "I don't know when the tools return nothing" made the sandbox's
  small model skip the tools and answer "I don't know"; the instructions now say tools
  come first.
- **Model:** the owner's default chat model (Settings → default model). The brain's
  local-only rule applies: an outside API answers "The assistant runs only on local
  models."
- **Memory:** no memories are extracted from a voice turn (anyone near the satellite
  can speak). Recalled context holds confirmed beliefs, as in chat, but not upstream's
  memory list (the route has no memory manager); the model can still search beliefs
  (`search_beliefs`) and read the memory store (`manage_memory`).

The security trade-off, for the owner to keep in mind: the voice token can read the
owner's conversations, calendar, notes, contacts and beliefs (not email), and, with voice
actions on, add items and mark them done; it can't change anything else or reach anything
an ordinary chat couldn't. Whoever holds the token (Home Assistant) and anyone who can
talk to the satellite can ask, and add or tick off items without being asked to confirm
(each one is pushed to the owner's phone, so it doesn't go unnoticed). Keep the satellite
in a private room and the token in Home Assistant only; turn voice actions off in
Settings if others use the room.

## Voice actions

`fork/voice/actions.py`. The owner setting **Voice can add and complete items**
(Settings → Integrations → Voice assistant; stored as `fork_voice_actions` in the
settings file, default on). Off: voice turns are exactly read-only, as above. On: the
voice turn is also offered two tools, run at once, without a confirmation, and nothing
else is allowed: edits, deletes, reopening, email, memory, facts (`erase_fact`,
`retire_fact`), documents and image generation are still never offered and refused if
called.

**`voice_add`** (`kind` task / reminder / event / note, `text`, `title`, `due_date`,
`due_time`, `list_name`, `note_title`) builds the item dict Athena's local model makes from
a voice note and files it with the same code, `create_item` in
`fork/athena/action_items_filing.py` (split out of `_file_one`, which now calls it), so a
spoken sentence is filed exactly as the same sentence in a voice note:

| Said | Filed |
|---|---|
| "Remind me to call the dentist tomorrow at 9" | a to-do "call the dentist", due tomorrow 09:00 |
| "Add milk to my shopping list" | the line "milk" on the to-do list "Shopping" (made if missing) |
| "Add a task to fix the bike light" (no date, no list) | a line on the "Voice notes" list |
| "Put lunch with Sam on Friday at noon on my calendar" | a 60-minute event, 30-minute reminder; all-day without a time |
| "Make a note that the garage code is changing" | an Odysseus note |
| "Add to my hardware notes that …" | a line "- …" in the note "Hardware notes" |

The voice-specific parts: dates may also be `today`, `tomorrow` or a weekday ("next
Monday" is a week on), times `9am`, `9:30 pm`, `noon` (`parse_date`, `parse_time`); a
time without a date is today's, or tomorrow's once it has passed. When the request names
exactly one day ("tomorrow", "on Sunday"), that day is used, whatever date the model
passed (the sandbox's small model filed "on Sunday" as a Saturday). "Remind me to…" is a
reminder (a to-do), as in a voice note, even when the model says event, unless the
calendar or an appointment is named. A voice item is not an
Athena item: its calendar uid is `voice:<random>` (never one of Athena's
`odysseus-sound:item:convo:<id>` uids), new notes and lists it makes carry the label
`voice` (not `athena`, so the beliefs job reads its notes as the owner's own), the source
line is "Added by voice", and it is not written into the action-items state file, so the
job never syncs it. The same add again within two minutes (a model or Home Assistant
retry) is made once.

**`voice_complete`** (`description`) searches the unticked lines of the owner's open
to-dos and lists, and Athena's open action items (`list_action_items`, JSON). A voice-note
item the job filed as a to-do is one item, not two: its to-do line is ticked and Athena is
told at once (`update_action_item`, status done; otherwise the job carries it across on
its next pass). Matching: an exact text first, else the items with the largest share of
the description's words (at least half).

- One clear match: marked done ("Marked 'send the spreadsheet to Alice' done.").
- Several: nothing changes, and the tool asks one short question ("Which one: 'call the
  dentist' on Voice notes or 'Call the plumber'?"). The options count as said for the
  next turn of that chat (5 minutes), so "the second one" works.
- None: "I couldn't find an open to-do matching '…'."

**Grounding.** An action runs only on what was said: at least half of the item's words
must be in the spoken request (a list name that wasn't said is dropped, and the item filed
as if none was given); a completion matches only on the words of its description that
were said, so a model that adds words ("the call one" -> "call the plumber") doesn't get
to pick, or on a clarification's option. So an instruction written in a transcript,
note or web page the turn has read can't add or tick anything off. A refused action
tells the model "Not run: …".

**Phone push.** One push per action ("Voice: Marked '…' done."), through the action-items
job's reminder channel (`MODULE_NOTIFY_CHANNEL`, ntfy by default), so the owner notices
changes made by someone else in the room. Its own setting, **Push voice actions to my
phone** (`fork_voice_action_push`, default on). The owner hasn't decided on this default
yet.

**Prompt** (`ACTION_INSTRUCTIONS` in `fork/voice/prompt.py`): the read-only text, with
its last rule replaced by:

> - To add something (a to-do, a reminder, a calendar event, a note, a line on a list:
>   "remind me to…", "add … to my … list", "make a note…", "put … on my calendar"), call
>   voice_add. To mark something done ("mark … done", "I've finished …", "tick off …"),
>   call voice_complete directly with the item in the owner's words; it searches by
>   itself. Both act at once, with no confirmation. Then say the tool's result sentence,
>   and nothing more.
> - You can't edit, move, reopen or delete anything, or change email, memory, facts or
>   documents, by voice. For those, say in one sentence that they can do it in the
>   Odysseus app.
> - Never end with a question or an offer ("Would you like…", "Shall I…", "Let me know
>   if…"). The only question you may ask is which item was meant, when voice_complete
>   asks it.

Why no trailing questions: Extended OpenAI Conversation sets Home Assistant's
`continue_conversation` when a reply ends with "?" or offers more. Home Assistant then
sends the next utterance only to Odysseus and skips its own local commands
(`_intent_agent_only` in HA's `assist_pipeline`), so "turn off the lights" would come to
us. `speech.py` drops such a sentence too (below).

The brain's check for invented actions (`fork/brain/claims.py`) counts `voice_add` as a
calendar or notes write and `voice_complete` as a notes or action-items write.

**What is spoken** (a whole answer, `openai_api.Answer`; Home Assistant's agent doesn't
stream): when a voice action ran, its own result sentences, not the model's retelling of
them; when the request was a command to add or complete something (`actions.is_command`:
"remind me…", "add…", "make a note…", "put…", "mark… done") and no action ran, "Sorry,
nothing was changed. Please say it again."; when the answer claims a change the brain's
check says wasn't made, "Nothing was changed. I can't do that by voice; you can do it in
the Odysseus app." So a voice answer never says something changed that didn't. A
streamed answer is spoken as the model writes it.

Routes: `GET`/`PUT /api/fork/voice/settings` (admin) `{"actions": bool, "push": bool}`.

## Conversations

`fork/voice/sessions.py`. A follow-up within 5 minutes of the last question continues
the same chat, so "and on Friday?" works. A request names its conversation by its `user`
field or `X-Conversation-Id` header, or else (when it sends earlier turns) by its first
question. A request with neither starts a new chat. The map is kept in memory per owner;
after a restart the next question starts a new chat.

New chats are named "Voice: <question>" and filed in the **Voice** folder.

## Spoken answers

`fork/voice/speech.py`. Markdown is removed (headings, bold, lists, tables, quotes, code
blocks); each list item or heading becomes a sentence; `[label](url)` keeps the label
unless it only names the link ("transcript", "source", …); bare URLs and `[1]` citations
are removed, and so is a list of links with its lead-in ("Links: …", "Sources: …") and
link names left hanging at the end ("Office move: <url>"). Any short lead-in that names
links, sources or transcripts at the start of a sentence goes the same way with the link
names after it ("Links to the conversations: [Q4 Budget Cut](…) and [Office move](…)."),
and so do parentheses left holding only a link's name ("(Web )"). The links are
collected, once each, in order, into `odysseus.links`.

A trailing offer or question ("Would you like…", "Let me know if…", "Shall I…?") is
dropped (`strip_offers`), unless it asks which one was meant ("Which one: …?") or is all
there is. In a whole answer that is any such sentence at the end; while streaming, the
last unfinished piece (a model's final sentence has nothing after it, so it is still
held when the stream ends).

A whole answer (no `stream`) speaks only the text of the rounds that ended without a
tool call, so a model's "Let me search for that" before its tool calls isn't read out.
While streaming, text is sent one finished sentence at a time and never cut inside an
open link or code block, so text-to-speech can start before the answer is done; there a
preamble is spoken, since it is sent before the model's tool calls are known.

## Sandbox results (2026-11-03)

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

## Sandbox results, voice actions (2026-11-03)

Same stack as above (Qwen3-4B on 4 CPUs, Athena's demo data, `curl` to the endpoint
without `stream`, as Extended OpenAI Conversation sends it), voice actions on. Final
code; each sentence a new conversation unless noted. Times include the model's tool
round; the first question after a restart took ~2 min (cold prompt).

| Said | Spoken answer | Made | Time |
|---|---|---|---|
| "Remind me to call the dentist tomorrow at 9" | "Added 'call the dentist' to your to-dos, due tomorrow at 9 a.m." | to-do, due 09:00 next day, label `voice` | 128 s (cold) |
| "Add milk to my shopping list" | "Added 'milk' to your Shopping list." | line on a new list "Shopping" | 26 s |
| "Make a note that the bike lock code is 4711" | "Made a note: 'the bike lock code is 4711'." (when the model called the tool; see the open point below) | a note "Bike Lock Code" | 24–31 s |
| "Put lunch with Sam on Friday at noon on my calendar" | "Put 'lunch with Sam' on your calendar for Friday at noon." | event 12:00–13:00, uid `voice:…`, 30-min reminder | 78 s |
| "Mark the revised spreadsheet to-do as done" | "Marked 'Send revised spreadsheet to Alice' done." | Athena item #2 done (`update_action_item`); #1 "Rework the spreadsheet…" left open | 24 s |
| "Mark the call one as done" (after "Add call the plumber to my to-dos") | "Which one: 'call the plumber' on Voice notes or 'call the dentist'?" | nothing | 24 s |
| "The plumber one" (same conversation) | "Marked 'call the plumber' done." | that line ticked | 28 s |
| "Delete my dentist reminder" | "Nothing was changed. I can't do that by voice; you can do it in the Odysseus app." | nothing | 12 s |
| "Mark the tax return as done" | "I couldn't find an open to-do matching 'the tax return'." | nothing | 27 s |
| "Remind me to water the plants on Sunday" | "Added 'water the plants' to your to-dos, due Sunday." | to-do due Sunday | 29 s |
| "What did Alice say about the Q4 budget?" | right (15% cut, revised to 10%, Berlin move) | nothing (reads still work) | 124 s (cold) |

What the run changed in the code:

- The model retold the tools' results wrongly: after "Which one: …?" it said "I've marked
  that as done", and it repeated "Remind me to call the dentist…" back. A whole answer now
  speaks the voice actions' own sentences (`openai_api.Answer.acted`).
- The model completed "the call one" by putting "call the plumber" into the description
  itself. Only the words the owner said now count for matching.
- Upstream's own "Calendar reminder: …" to-dos (the copy of an event's reminder) were
  offered as matches; they are left out now.
- It filed "remind me…" as a calendar event, "on Sunday" as a Saturday, and invented a
  list ("household chores"). Now "remind me" is a reminder unless the calendar is named,
  the day said wins, and a list that wasn't said is dropped.
- "Delete my dentist reminder" changed nothing (as it should) but was answered "I've
  deleted your dentist reminder" plus the brain's correction note. Now: "Nothing was
  changed. I can't do that by voice; …".
- **Open:** for "make a note…", the 4B model often answers "I've made a note" without
  calling `voice_add` (made 3 of 8 times). The answer is then replaced by "Sorry, nothing
  was changed. Please say it again." (`NO_ACTION_REPLY`: a command to add or complete,
  `actions.is_command`, with no action behind it), so it never claims a note that wasn't
  made. A prompt line against it made it worse (0 of 5) and was taken out. The owner's
  `qwen3.5-35b` needs checking on the server (Server verification).

## Setup (owner)

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
   ignores the tools it sends).
5. **Home Assistant, pipeline:** Settings → Voice assistants → Add assistant: the
   speech-to-text and text-to-speech of step 3, the conversation agent of step 4, and
   **Prefer handling commands locally** on, so "turn off the lights" stays in Home
   Assistant and everything else goes to Odysseus. Pick this assistant on the satellite.
   Caveat: with this on, Home Assistant's built-in intents answer some phrasings before
   Odysseus hears them: timers, and, if Home Assistant has a To-do list entity, "add X to
   my shopping list" and "mark X complete" (they go to Home Assistant's list, not
   Odysseus'). Remove or rename that To-do entity, or use other words ("put milk on my
   shopping list"), to reach Odysseus.
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
new chat and follow-ups lose their context.

## Later

- Edits and deletes by voice with a spoken confirmation ("Move the dentist to three?" —
  "Yes").
- Private answers only for the owner's voice: Athena already keeps the owner's voiceprint
  (Enroll My Voice); the satellite's audio could be checked against it.
- The answer's links and Athena cards on the satellite's screen.

## Files and tests

- `fork/voice/routes.py`, `openai_api.py`, `sessions.py`, `speech.py`, `prompt.py`,
  `actions.py` (voice actions; filing through `create_item` in
  `fork/athena/action_items_filing.py`).
- In the brain: `read_only_turn` (`fork/brain/turn.py`), `read_only_refusal`
  (`fork/brain/tools.py`), `can_read` and `read_only` (`fork/brain/toolset.py`).
- `static/js/forkVoiceAssistant.js`: the Settings card.
- Hooks: `routes/api_token_routes.py` (scope `voice`), `static/index.html` (the card's
  script tag); the router is in `fork/routes/__init__.py`.
- Tests: `tests/fork/test_voice_assistant.py`, `tests/fork/test_voice_actions.py`.
