# Personal context: what to build next, and why

How Odysseus and Athena become as useful as the commercial personal-context assistants
while staying fully local. This is the order of work after the
[personal context phases](personal-context.md) (0 to 4.2, built). Status: **planned**.
Written 2026-10-29.

## Where we stand

What already beats most commercial products:

- Every belief has a source link; one mention is evidence, not a belief
  ([beliefs.md](beliefs.md)).
- The owner can review, correct and forget, and forgetting reaches back to the source
  (Athena's `erase_fact`).
- The Today insight learns from what the owner does with it
  ([today-insight.md](today-insight.md)).
- Nothing leaves the owner's network.

Where we fall behind:

1. **Answer quality.** The Athena eval is at 9/20; the target is 15/20
   ([modules/athena.md](modules/athena.md#eval-and-current-score)). Every new source
   adds more for a weak answer to get wrong.
2. **Few sources.** Recorded conversations, plus Odysseus' own email, calendar, notes and
   to-dos. Not the owner's text messages, screen or places.
3. **No search across sources.** Each module is a separate tool the model must pick;
   picking the wrong one is where answers fail. The shared search is planned only
   ([modules.md](modules.md#shared-search-planned)).
4. **No view per person.** "Brief me before I see Sam" is what makes the commercial
   products feel smart, and we can't answer it in one step yet.

## What other assistants do well

Checked 2026-10-29. Most comparisons online are written by the vendors; treat them as
claims.

| Product | Its strength | What we take |
|---|---|---|
| Limitless (Rewind) | One searchable timeline of everything; prep before meetings. Bought by Meta; no new pendants sold | Step 1 (shared search), step 3 (briefings) |
| Bee | Pulls to-dos out of the day; "facts about you" the user confirms | Already built (Athena action items, belief review page) |
| Omi | Open source; apps that act during a conversation | Later: live triggers on a conversation in progress |
| Screenpipe | Local screen and audio history, its own MCP server | Step 4 (`athena-screen`) |
| ChatGPT / Gemini memory | Uses what it knows without being asked | Beliefs in chat context and Today, once step 1 passes |

Their common weakness is the cloud: the recordings and the model are the vendor's. Being
fully local is how this system competes, so no step below loosens the privacy rules.

## Step 1: answer quality and shared search

Gate for everything after it. More sources before this only add noise.

- **Eval to ≥ 15/20.** The owner's full 20-question run on the server
  ([roadmap.md](roadmap.md#next), item 2). Fix the failure kinds it shows, one PR each.
- **Shared search.** One `context_search` tool in the hub that calls each module's main
  search tool with the shared arguments (`query`, `since`, `until`, `person`, `limit`)
  and merges the dated, linked hits by date and score. Plus the hub's own notes, email and
  calendar as sources of the same tool. A module that fails gives one result line, not a
  failed search.
- **A cross-source eval.** 10 questions whose answer needs two sources ("did I tell Mia
  the date I wrote in my notes?"), using the categories already borrowed from LongMemEval
  ([personal-context-prior-art.md](personal-context-prior-art.md#16-memory-eval)).

Done when: Athena ≥ 15/20 on the server; the cross-source eval has a recorded baseline.

## Step 2: athena-messages

The owner's text messages (iMessage first) as a new module. Full plan:
[athena-messages.md](athena-messages.md). In short: the Mac's Messages app, signed into
the same Apple ID, holds the history; a collector on the Mac sends new messages over
Tailscale to the module on the server, which groups them into threads, summarizes them
with the local model, and serves read-only MCP tools. Its facts and promises join the
belief ledger as a second source.

Done when: its eval passes on the server (≥ 8/10) and the facts source is on.

## Step 3: people pages and briefings

The hub already has subjects (one per person, linked to a contact) with the beliefs about
them ([beliefs.md](beliefs.md#subjects-and-contacts-21)). Build on that:

- **A page per person** in the hub: who they are (confirmed beliefs), the last few times
  you met, talked or texted (from shared search with `person`), open promises both ways
  (Athena action items, message promises, to-dos), upcoming events with them, and
  "not heard from since". Every line links to its source.
- **Briefing before a meeting.** For a calendar event with people the hub knows, a short
  briefing 30 minutes before (ntfy, off by default): last time you spoke, what was left
  open, one or two facts worth remembering. Also on the T5 PDA's calendar screen.
- **Ask about a person in chat** answers from the same page data, so "what's going on
  with Sam?" is one tool call.
- People from Athena (speakers), messages (handles), email (senders) and the calendar
  (attendees) map to one subject through `subject_sources`, with the owner's link to a
  contact deciding when names disagree.

Done when: a person eval (10 questions over fictional people across two modules) passes,
and the owner uses the briefing for a week and keeps it on.

## Step 4: athena-screen

Screen history from Screenpipe on the owner's Mac, behind the module's own MCP server.
Design, outbound traffic to block, and privacy rules are already written:
[personal-context-prior-art.md](personal-context-prior-art.md#future-module-athena-screen-screenpipe).
It comes after messages because screen text is the noisiest and most sensitive source,
and it needs shared search to be useful.

## Step 5: optional sources (owner picks)

Each would be its own module or a source of an existing one, and each starts with a
check that it can run without the cloud.

| Source | How, locally | Answers questions like |
|---|---|---|
| Places | [OwnTracks](https://owntracks.org) on the phone to the owner's own server | "Where was I when Leo called?", "when did I last go to the gym?" |
| Photos | [Immich](https://immich.app) with its local models (faces, search) | "Photos from the dinner with Mia" |
| Browser history | The Mac browsers' own SQLite history, read like `chat.db` | "That article I read about BLE last week" |
| Health and sleep | [Gadgetbridge](https://gadgetbridge.org) for the Amazfit watch (no Zepp cloud) | "How did I sleep before the trip?" (check T-Rex 3 Pro support first) |
| WhatsApp, Signal | Later sources of `athena-messages` | Same as iMessage |

## Later: in the moment

What Omi does with live triggers, once the steps above work: Athena notices during a
conversation that a promise was made or a date set, and the hub offers to file it right
then, instead of after the session ends. Needs streaming summaries in Athena; not planned
in detail.

## Open questions for the owner

- Is this order right, or is any step more urgent (for example briefings before
  messages)?
- Step 5: which sources are wanted at all?
- Briefings: by ntfy, on the T5, or both?
