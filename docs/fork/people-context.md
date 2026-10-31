# People context: one view of each person across every source (plan)

For every contact, everything the system knows about them and every time you were in
touch, across media: recorded in person, calls, online meetings, texts, email and the
calendar, with what's still open between you. Every line is linked to its source. It
shows as a page per person, in chat ("what's going on with Mia?"), and as a briefing
before you meet them. Status: **planned**; step 3 of
[context-growth.md](context-growth.md#step-3-people-pages-and-briefings).

It builds on what exists:

- the belief ledger's **subjects**, one per person, linked by hand to an Odysseus contact
  ([beliefs.md](beliefs.md#subjects-and-contacts-21));
- Athena's **people** with stable ids across recordings, meetings and texts, and since
  Athena MCP 0.21.0 the phone numbers and emails each person texts from (`list_people`
  JSON `handles`; Athena's
  [docs/messages.md](https://github.com/SDarioosh/Athena/blob/main/docs/messages.md#people));
- the hub's **contacts** (local book or CardDAV: `uid`, `name`, `emails`, `phones`), its
  email and calendar.

## Sources

| Source | Who it names, and by what | What it adds to a person |
|---|---|---|
| Athena recordings, calls, voice notes | Athena person id (speakers, people the summary names) | conversations, facts, to-dos, quotes |
| Athena online meetings | person id (participants, also those who never spoke) | meetings, what was decided |
| Athena text threads | person id **and handle** (phone number or email) | texts, facts, promises both ways |
| Hub email | email address | threads with them |
| Hub calendar | attendee email, or name | past and upcoming events with them |
| Hub contacts | `uid`, phones, emails | the anchor: who the person is |
| Hub notes | names mentioned | notes about them (weak: name only) |

## Identity: which subject is which contact

Resolved in this order, strongest first. Each link records how it was made
(`linked_by`), so the review page can show and undo it.

1. **The owner's link** (`link_contact`) always wins and is never changed automatically.
2. **Phone or email match.** One of an Athena person's handles is one of a contact's
   phones (compared on the last 10 digits) or emails (lowercase): link automatically
   (`linked_by = "handle"`).
3. **Email and calendar addresses.** An address matching a contact's email belongs to
   that contact's subject; an address with no contact gets a subject of its own, which
   step 2 or the owner joins up later.
4. **Name only.** A subject whose name or alias equals one contact's name, and no other
   contact's: a **suggestion** on the review page ("Is this Mia Chen from your
   contacts?"), never an automatic link.
5. **Conflicts.** Two contacts sharing a number, a person whose handles match two contacts,
   or a handle match that disagrees with the owner's link: never automatic; a review card.

Storage: a fork table `subject_identifiers` (`subject_id`, `kind` phone|email,
`value_norm`, `source`, `linked_by`) beside `subject_sources`, filled by the Module Facts
job's `sync_people` pass from the `handles` it now receives. No Athena change is needed for
steps 1–5.

## Timeline

`fork/people/timeline.py`: `timeline(owner, subject_id, since, until, medium, limit)`
merges, newest first:

- Athena: conversations, meetings and text threads with the person
  (`search_conversations` with `person`). Matching by name breaks when two people share a
  name, so Athena gets an additive `person_id` argument on `search_conversations` and
  `list_action_items` (module side first, MCP 0.22.0, per
  [modules.md](modules.md#versioning)).
- Hub email: threads from or to the person's addresses.
- Hub calendar: events where they are an attendee.

Each entry is `{when, medium, title, snippet, link, direction}` (`medium`: in person,
call, meeting, text, email, event). Kept in a small cache per subject (ids, links, at most
200 characters of snippet), refreshed by the hourly job and on demand; a forgotten or
excluded source drops its entries.

## Where things stand (the person summary)

From the subject's confirmed beliefs (`subject_summary`), the last interactions of each
medium, the open loops in both directions (to-dos they're `with` on, promises from texts,
items they own) and upcoming events, the local model writes three to five sentences plus
bullets: what's going on, what's open, what's next. Every claim carries its link. It is
written again when a new interaction arrives (at most once an hour per person unless
asked) and cached with a hash of its inputs, so an unchanged person costs nothing.

## Where it shows

- **People page** (`/people`, `/people/<subject>`): the contact card, where things stand,
  open loops, upcoming, the timeline (filter by medium), the facts (the review page's
  actions: confirm, correct, forget, move), "last in touch".
- **Chat:** a read-only fork tool `person_context(name)` returning the summary, open loops
  and the latest interactions with links; a question about a known person routes to it
  first ([tool-routing.md](features/tool-routing.md)).
- **Briefing before a meeting:** 30 minutes before an event with people the hub knows,
  through ntfy (off by default): last time you spoke, what's open, one or two facts.
- **Today insight:** open loops with people you see today; people you haven't been in
  touch with for a long time, among those you used to talk to often.
- **T5 PDA:** the person card under a calendar event (later).

## Privacy

- Everything stays local; summaries are written by the server's model.
- Forgetting a belief, excluding a chat or deleting a note removes it from the summary
  (written again) and the timeline.
- A **"don't profile"** switch per contact: no summary, no briefing, no Today mentions for
  that person; their conversations stay searchable as before.
- Export and complete forget per person reuse 4.2 ([beliefs.md](beliefs.md#export-42)).

## Phases

| # | What | Done when |
|---|---|---|
| P1 | Identity: handles and addresses link subjects to contacts; name-only suggestions and conflict cards on the review page | On a fictional set (10 people across recordings, texts, email, calendar, contacts): every phone and email case linked, no wrong automatic link, name-only cases offered as suggestions |
| P2 | Timeline: Athena `person_id` argument (module PR first), hub timeline and cache | Each fictional person's timeline has every interaction, in order, with working links |
| P3 | Person summary, `person_context` tool, people page | 10 person questions, half needing two media ("what did Mia and I agree on, in person or by text?"): ≥ 8/10 on the server model |
| P4 | Briefings and Today hooks | The owner keeps the briefing on for a week |
| P5 | Later: how often you're in touch over time, groups (family, a team) from group chats and meetings, birthdays from contacts | Owner decides |

## Open questions for the owner

- Link a person to a contact automatically when the phone number or email matches, or
  always ask first? (Proposal: automatically; it shows on the review page and can be
  undone.)
- Which people get a page and a summary: everyone in contacts, or only people with a few
  interactions?
- Briefings: ntfy, the T5, or both?
