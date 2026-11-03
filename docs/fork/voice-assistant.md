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

- **Voice can only look things up.** Every voice question is a read-only turn: no
  changes, no approvals (nobody can approve by voice), no email.
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
| `GET /api/fork/voice/ha` | the same | the saved Home Assistant address and the last setup's report |
| `POST /api/fork/voice/ha/connect` | the same, same-origin | starts "Set up Home Assistant": answers HA's login address ([Setup](#setup-owner)) |
| `GET /api/fork/voice/ha/callback` | the same | Home Assistant's redirect after the login: runs the setup, revokes the login, shows the steps |

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
  goes: the owner's tools first for questions about their life (as in chat), then one to
  three spoken sentences, no markdown, numbers and times as spoken, links at the end, "I
  can't make changes by voice" for change requests. A first version that also asked for
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
are removed, and so is a list of links with its lead-in ("Links: …", "Sources: …") and
link names left hanging at the end ("Office move: <url>"). The links are collected, once
each, in order, into `odysseus.links`.

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

Checked in the sandbox (Home Assistant 2026.2.3, Extended OpenAI Conversation 3.0.0, a
fake Odysseus): the agent sends no `user` field, `stream: true`, model `odysseus`, no
tools, and a follow-up in the same Home Assistant conversation carries the earlier
turns, so Odysseus continues the chat by its first question.

## Linking an Echo

An Echo Show running our VACA fork ([odysseus-echo](https://github.com/SDarioosh/odysseus-echo),
`docs/odysseus/setup.md`) is set up from the owner's phone with one QR code, with no typing
on the Echo. The Odysseus part is `fork/voice/echo_link.py`. The Echo's setup page sends the
phone to `GET /echo/link?return=<Echo URL>&name=<name>`. The signed-in admin sees "Link Echo
'Kitchen' at <host>:10801?" and taps **Approve**. Odysseus then makes a token with only the
`voice` scope, named `Echo <name> <YYYY-MM-DD>` (revoke it in Settings → API tokens like any
other). It sends the phone back to the Echo with `token`, `base_url` (`<the address you
opened Odysseus at>/api/chat/voice/v1`) and `ha_url` (the Home Assistant address saved by
**Set up Home Assistant**, or empty) in the URL fragment (`<Echo URL>#token=…`). The Echo's
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

- Voice changes with a spoken confirmation ("Add the dentist on Tuesday at three?" —
  "Yes").
- Private answers only for the owner's voice: Athena already keeps the owner's voiceprint
  (Enroll My Voice); the satellite's audio could be checked against it.
- The answer's links and Athena cards on the satellite's screen.

## Files and tests

- `fork/voice/routes.py`, `openai_api.py`, `sessions.py`, `speech.py`, `prompt.py`.
- In the brain: `read_only_turn` (`fork/brain/turn.py`), `read_only_refusal`
  (`fork/brain/tools.py`), `can_read` and `read_only` (`fork/brain/toolset.py`).
- `fork/voice/ha_setup.py` (Home Assistant client and the setup run), `ha_routes.py`
  (sign-in and callback), `ha_settings.py` (the saved address);
  `scripts/ha_voice_setup.py` (the same without a browser); `echo_link.py` (Link this Echo).
- `static/js/forkVoiceAssistant.js`: the Settings card, with "Set up Home Assistant".
- Hooks: `routes/api_token_routes.py` (scope `voice`), `static/index.html` (the card's
  script tag), `requirements.txt` (`websockets`, for Home Assistant's websocket API); the
  routers are in `fork/routes/__init__.py`.
- Tests: `tests/fork/test_voice_assistant.py`, `tests/fork/test_ha_voice_setup.py` (a fake
  Home Assistant), `tests/fork/test_echo_link.py`.
