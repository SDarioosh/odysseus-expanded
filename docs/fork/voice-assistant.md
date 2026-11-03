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
                                                               POST /v1/chat/completions
                                                               a read-only brain turn
                                                               (Athena over MCP, calendar, …)
   3. text-to-speech: local Piper (or Kokoro)
   │ reply audio
satellite
```

Nothing leaves the owner's network: every stage is local, and Home Assistant Cloud
(Nabu Casa) speech is not used.

- **Voice can only look things up.** Every voice question is a read-only turn: no
  changes, no approvals (nobody can approve by voice), no email.
- **Each voice conversation is a chat** in the **Voice** folder, so the owner can read
  what was asked and answered, and continue it in the app.
- **Answers are spoken text:** short, no markdown, links taken out of the speech and
  returned apart for a screen.

## The endpoint

`fork/voice/routes.py`, mounted by `fork.routes.mount`.

| Route | Who | What |
|---|---|---|
| `POST /v1/chat/completions` | API token with the `voice` scope | one voice question; OpenAI's chat-completion request and response, `stream: true` supported |
| `GET /v1/models` | the same | `{"data": [{"id": "odysseus", …}]}`, for clients that list models when set up |
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
  never pauses for an approval.
- **The voice instructions** (`fork/voice/prompt.py`) stand where a chat's preset text
  goes: one to three spoken sentences, answer first, no markdown, numbers and times as
  spoken, links at the end, "I can't make changes by voice" for change requests, "I
  don't know" over a guess.
- **Model:** the owner's default chat model (Settings → default model). The brain's
  local-only rule applies: an outside API answers "The assistant runs only on local
  models."
- **Memory:** no memories are extracted from a voice turn (anyone near the satellite
  can speak). Recalled context holds confirmed beliefs, as in chat, but not upstream's
  memory list (the route has no memory manager); the model can still search beliefs
  (`search_beliefs`) and read the memory store (`manage_memory`).

The security trade-off, for the owner to keep in mind: the voice token can read the
owner's conversations, calendar, notes, contacts and beliefs (not email), but it can't
change anything or reach anything an ordinary chat couldn't. Whoever holds the token
(Home Assistant) and anyone who can talk to the satellite can ask. Keep the satellite in
a private room and the token in Home Assistant only.

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
are removed. The links are collected, once each, in order, into `odysseus.links`. While
streaming, text is sent one finished sentence at a time and never cut inside an open
link or code block, so text-to-speech can start before the answer is done.

## Setup (owner)

1. **Token:** Settings → Integrations → **Voice assistant** → **Make a voice token**.
   Copy the API key (shown once), the base URL (`https://<odysseus>/v1`) and the model
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
6. **Optional second wake word**, for example "Hey Athena": a second assistant with the
   same agent, chosen by that wake word on the satellite.

To check, without the satellite (on the owner's network, with the voice token):

```bash
curl -s https://<odysseus>/v1/chat/completions \
  -H "Authorization: Bearer $VOICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"model":"odysseus","messages":[{"role":"user","content":"What did I talk about yesterday?"}]}'
```

To check (not verified in the sandbox): which field Home Assistant's agent sends the
conversation id in. If it sends neither `user` nor earlier turns, every question starts a
new chat and follow-ups lose their context.

## Later

- Voice changes with a spoken confirmation ("Add the dentist on Tuesday at three?" —
  "Yes").
- Private answers only for the owner's voice: Athena already keeps the owner's voiceprint
  (Enroll My Voice); the satellite's audio could be checked against it.
- The answer's links and Athena cards on the satellite's screen.

## Files and tests

- `fork/voice/routes.py`, `openai_api.py`, `sessions.py`, `speech.py`, `prompt.py`.
- In the brain: `read_only_turn` (`fork/brain/turn.py`), `read_only_refusal`
  (`fork/brain/tools.py`), `can_read` and `read_only` (`fork/brain/toolset.py`).
- `static/js/forkVoiceAssistant.js`: the Settings card.
- Hooks: `routes/api_token_routes.py` (scope `voice`), `static/index.html` (the card's
  script tag); the router is in `fork/routes/__init__.py`.
- Tests: `tests/fork/test_voice_assistant.py`.
