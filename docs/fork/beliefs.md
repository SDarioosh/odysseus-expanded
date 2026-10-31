# Belief ledger: evidence in, confirmed beliefs out

Stages 1.1 (evidence intake), 1.3 (the [review page](#review-page-13)), 1.4 (forgetting
propagates), 1.5 (expiry and decay) and 4.2 ([complete forget](#complete-forget-42) and
[export](#export-42)) of
[personal-context.md](personal-context.md#phase-1-one-intake-a-review-page-and-forgetting),
and, of Phase 2, the hub side of 2.1 ([subjects and contacts](#subjects-and-contacts-21)),
2.2 ([structure](#structure-22)) and 2.4 ([projects](#projects-24)).
Code: `src/evidence_intake.py` (the ledger), `src/module_facts.py` (its first source),
`src/notes_evidence.py` (the owner's notes, [Sources](#sources)) and
`src/belief_structure.py` (the Belief Structure task); tests
`tests/fork/test_evidence_intake.py`, `tests/fork/test_module_facts.py`,
`tests/fork/test_notes_evidence.py`, `tests/fork/test_subjects.py` and
`tests/fork/test_belief_structure.py`.

Phase 0 copied Athena's confirmed facts straight into Odysseus memory. Now a
**belief ledger** sits between the sources and memory:

```
source (Athena, notes, chat …) ── Evidence ──▶ ledger (beliefs.db) ── confirmed ──▶ Odysseus memory
                                                        ▲
                                    owner: confirm / correct / forget / restore
```

- Every source submits **evidence**: what it observed, its own id for it, and a link to
  where it came from.
- The ledger keeps **beliefs**, each with its evidence, a status and a short event
  history.
- Only **confirmed** beliefs are mirrored into Odysseus memory, one memory per belief, so
  chat context, the Memory page and the Today insight see them.
- The owner's decisions (from the review page, stage 1.3) always win over the counting
  rule.

## Storage

`<DATA_DIR>/beliefs.db`: stdlib `sqlite3` in WAL mode, one file for all owners, created
on first use. No upstream model or database changes. Odysseus never opens a module's
database; the ledger holds only what sources submitted.

Matching runs on indexes so intake stays flat as the ledger grows: `(owner, norm)` and a
partial index `beliefs_owner_corrected` on `(owner, owner_norm)` over corrected beliefs
only (a full index there, almost all NULLs, was replaced on open), and
`(owner, subject_id)` for the per-subject counts. `submit` and `reconcile_source` run
`PRAGMA optimize` after each batch, and so does a migration.

| Table | Columns |
|---|---|
| `beliefs` | `id`, `owner` (`''` without auth), `subject` (the name the source used), `about` (`owner`, `person`, `other`), `text`, `norm` (matching key), `category` (`fact`, `preference`, `contact`), `status` (`tentative`, `confirmed`, `stale`, `rejected`), `confirmed_by` (`source`, `owner`, `rule` or null), `first_seen`, `last_seen`, `valid_until` (ISO dates), `memory_id` (the mirrored memory), `created_at`, `updated_at` (UTC ISO), `owner_text` (the owner's correction; when set, memory shows it), `owner_norm`, `note` (e.g. the reason given when forgetting); Phase 2: `subject_id` (the [subject](#subjects-and-contacts-21)), `attribute`, `value`, `valid_from`, `structured_at` ([structure](#structure-22)) |
| `evidence` | `id`, `belief_id`, `owner`, `source`, `source_id`, `subject`, `about`, `category`, `text` (what the source said), `link`, `seen_first`, `seen_last` (ISO dates), `times`, `days`, `source_confirmed` (0/1), `payload` (JSON of extra fields; `gone: true` and `gone_at` when the source stopped reporting it, `valid_until` when given), `updated_at`. Unique on (`belief_id`, `source`, `source_id`) and on (`owner`, `source`, `source_id`): one source id maps to one belief per owner. |
| `events` | `id`, `belief_id`, `at`, `kind` (`added`, `evidence`, `confirmed`, `stale`, `rejected`, `corrected`, `restored`, `retired_upstream` (a source stopped reporting it), `unconfirmed` (it no longer meets the rule), `merged` (a near-duplicate from the same source was merged into it, [Consolidation](#consolidation-41))), `detail`. A ledger created before `unconfirmed` existed has its `events` table rebuilt with the wider CHECK on first open (rows and ids kept). Only changes are logged (not every hourly re-submit), and only the newest 50 per belief are kept. |
| `subjects` | see [Subjects and contacts](#subjects-and-contacts-21) |
| `subject_sources` | see [Subjects and contacts](#subjects-and-contacts-21) |

## Statuses and the confirmation rule

| Status | Meaning | In memory |
|---|---|---|
| `tentative` | Evidence, not yet enough to act on | no |
| `confirmed` | By its source (`source`), by the rule (`rule`) or by the owner (`owner`) | yes |
| `stale` | No source reports it any more, not seen for too long, or past its `valid_until` | no |
| `rejected` | The owner forgot it | no |

`confirmation_for(belief, evidence_rows, settings)` counts only **live** evidence (not
gone). A belief is confirmed when:

1. **(a)** any evidence has `source_confirmed`: the source's own threshold (Athena:
   said in ≥ 3 conversations on ≥ 2 days), or
2. **(b)** at least `BELIEF_CONFIRM_SOURCES` (2) distinct sources report it (a fact said in
   a conversation and written in a note confirms twice), or
3. **(c)** at least `BELIEF_CONFIRM_TIMES` (3) sightings summed across its evidence, on at
   least `BELIEF_CONFIRM_DAYS` (2) distinct days. Sources report totals, not every date,
   so distinct days are the larger of the biggest per-evidence `days` and the number of
   distinct first/last-seen dates across the evidence; this never over-counts.

The owner's decisions win and the rule never undoes them: an owner-confirmed belief stays
confirmed whatever the evidence does, and a rejected belief stays rejected even when new
evidence (from any source, even source-confirmed) arrives for it.

The rule is re-checked whenever a belief's evidence changes. A belief confirmed by the
rule or its source that no longer meets it (one of its two sources went away, or a
threshold was raised) goes back to `tentative` and leaves memory: event `unconfirmed`
("no longer meets the rule"), counted as `unconfirmed` by `submit` and
`reconcile_source`.

## Submitting evidence (for a new source)

```python
from src import evidence_intake as ledger

counts = ledger.submit(owner, [
    ledger.Evidence(
        source="notes",              # the source's name, stable
        source_id="note-42:3",       # the source's own stable id for this observation
        subject="Kath", about="person",   # owner | person | other
        text="Kath lives in Lisbon",
        link="/#open=notes&note=note-42",  # required: evidence without a link is skipped
        seen_first="2026-10-01", seen_last="2026-10-14",  # ISO dates or datetimes
        times=2, days=2,             # the source's totals for this observation
        source_confirmed=False,      # True only if the source has its own confirmation
        category=None,               # fact | preference | contact; None: from about + text
        payload={"note_id": 42},     # extra fields, kept as JSON
        valid_until=None,            # optional ISO date: the belief ends that day
        person_id=None,              # optional: the source's stable id of the person
    ),
], now=None)
# {"beliefs_added", "evidence_added", "evidence_updated", "confirmed", "unconfirmed",
#  "unchanged", "skipped"}
ledger.reconcile_source(owner, "notes", present_ids={"note-42:3", ...})
```

- Each item is filed under the belief its (`owner`, `source`, `source_id`) already maps
  to; else under a belief of the same owner whose text (or the owner's correction) has the
  same **normalized** text (lowercase, whitespace collapsed, trailing punctuation
  dropped); else a new `tentative` belief.
- For facts about the owner (`about="owner"`) the key also folds the owner's leading
  mention: `the owner`, `owner`, `me`, `i`, `i'm`, `i am` or the evidence's own
  `subject` (e.g. the owner's name, as the module sends it) becomes `owner`, a following
  `am`/`have` becomes `is`/`has`, and `my`, `the owner's` and `<name>'s` become
  `owner's`. So "The owner is allergic to peanuts." (a note), "Me is allergic to
  peanuts." (a conversation, subject `Me`) and "I am allergic to peanuts" are one belief.
  Facts about people and things keep the plain key. An owner correction is keyed with the
  belief's `about` and `subject`. Ledgers written before this are re-keyed once when
  opened (`PRAGMA user_version` 1).
- Re-submitting the same item changes nothing (`unchanged`); new totals or dates update
  the evidence; the belief's `first_seen`/`last_seen` are the min/max over its evidence.
- When a source changes the text behind an id: if that evidence is the belief's only one
  and the owner hasn't decided on it, the belief follows (memory is updated in place).
  If the owner confirmed or dismissed it and the source only rewords it (same subject,
  the same words apart from glue words such as "with", "that", "has", "on", "it"; the
  same negations and numbers; `same_rewording` in `src/belief_consolidation.py`), the
  evidence stays with the owner's decision: an Athena re-summary rewords facts under
  their id, and this was a second copy of the fact in memory. Otherwise the evidence
  moves to the belief that matches the new text, or a new one ("plays the cello" ->
  "plays the viola" is a new claim). A changed `subject` or `about` (a renamed speaker)
  follows the same way.
- A source that lists everything it currently holds calls `reconcile_source` after
  `submit` with the ids it still has: the other ids are marked gone (event
  `retired_upstream`). A belief left without live evidence goes `stale` (and leaves
  memory) unless the owner confirmed it; owner-confirmed beliefs keep their status and
  show the evidence as gone. A gone id that is submitted again comes back.
- `submit`, `reconcile_source` and `apply_decay` mirror memory at the end; a job that
  calls several of them passes `mirror=False` and calls `sync_memory(owner)` once.
- Each new source gets its own on/off setting, off by default until tested (plan 1.2).

## Expiry and decay (1.5)

`apply_decay(owner, now=None, stale_days=None)` runs at the end of every facts-job pass:

- A belief confirmed by its source or the rule whose `last_seen` is older than
  `BELIEF_STALE_DAYS` (180) days goes `stale` (event `stale`, "not seen since …") and
  leaves memory. Tentative beliefs aren't demoted by age.
- Any tentative or confirmed belief whose `valid_until` has passed goes `stale`, owner
  confirmed or not (it is an explicit end date, not decay).
- Owner-confirmed beliefs never decay by age.
- A stale belief comes back only on new evidence (a newer `seen_last`, a gone id
  reported again) that is itself within the window; the same old evidence re-submitted
  every hour doesn't flap it.

## Owner actions (for the review page, 1.3)

| Call | Effect |
|---|---|
| `confirm(owner, id)` | `confirmed` by `owner`; in memory; never decays |
| `correct(owner, id, text)` | `owner_text = text`, `confirmed` by `owner`; memory shows the corrected text (same memory id and `uses`); evidence that says the corrected text later lands on this belief; clears `structured_at`, so the Belief Structure task structures it again |
| `reject(owner, id, reason="")` | `rejected`, out of memory, `note = reason`; returns the evidence list so the caller can propagate the forget |
| `restore(owner, id)` | `rejected`/`stale` → `tentative`, then the rule (may confirm it again); to keep an old belief for good, confirm it |
| `erase(owner, id, outcomes=None)` | a rejected belief loses its words for good ([Complete forget](#complete-forget-42)); `confirm`, `correct` and `restore` raise `BeliefErased` (a `ValueError`) on it afterwards |
| `list_beliefs(owner, status=None, subject=None, limit=500, *, subject_id=None, attribute=None)` | beliefs with their evidence (`status`: one or a list; `subject` case-insensitive on the source's name; `subject_id` one subject; `attribute` one of the [vocabulary](#structure-22)), by subject then most recently seen |
| `get_belief(owner, id)` | one belief with evidence and its 20 newest events |
| `stats(owner)` | counts per status, `total`, and `structured` (beliefs with `structured_at`) |

A belief dict has `id`, `owner`, `subject`, `about`, `text`, `owner_text`, `shown_text`,
`category`, `status`, `confirmed_by`, `first_seen`, `last_seen` ("not heard since"),
`valid_until`, `memory_id`, `created_at`, `updated_at`, `note`, `subject_id`,
`subject_display`, `subject_kind`, `contact_id` (of its subject), `attribute`, `value`,
`valid_from`, `structured_at`, `erased_at` (set once erased), `link` and `source` (of
the citing evidence, see [Memory mirroring](#memory-mirroring)) and `evidence` (each: `id`, `source`, `source_id`, `subject`,
`about`, `category`, `text`, `link`, `seen_first`, `seen_last`, `times`, `days`,
`source_confirmed`, `gone`, `payload`, `updated_at`). Another owner's id raises
`BeliefNotFound` (a `LookupError`). Owner actions update memory right away; if the memory
store is unreadable the decision is still saved and the next facts pass mirrors it.

### Forgetting propagates (1.4)

Since 4.2 the review page's Forget erases ([Complete forget](#complete-forget-42)); this
`retire_fact` path is what an older module still gets.

`src.module_facts.propagate_forget(owner, belief_or_evidence, call=None)` calls the
module tool `retire_fact {"fact_id": <id>, "reason": "owner forgot it in Odysseus"}` for
each Athena evidence of a forgotten belief, through the connected module MCP
server (or `call`). It returns `{"retired", "failed", "unsupported"}`. A module without
`retire_fact` is tolerated (logged once per owner); the fact then keeps coming back from
the module, attaches to the rejected belief and stays out of memory. The review page
calls `reject`, then `propagate_forget` with what `reject` returned.

## Review page (1.3)

`/beliefs` ("What I know about you", sidebar: **Wisdom**, formerly Beliefs, in the Athena group; it opens
as a window in the app, `mountBeliefs` in `static/js/beliefs.js`, and `/beliefs` still works as a page) is where the
owner sees and decides. Code: `routes/beliefs_routes.py`, `static/beliefs.html`,
`static/js/beliefs.js`; tests `tests/fork/test_beliefs_routes.py`. Same auth as the Today
page (admin session; API tokens are refused), and every call is scoped to the logged-in
owner: another owner's belief id answers 404.

| Route | Does |
|---|---|
| `GET /api/beliefs?status=&subject=&attribute=` | `{"beliefs": [...], "stats": {...}}`. `status`: `tentative`, `confirmed`, `stale`, `rejected` or `all`; without it, all but rejected. `subject`: case-insensitive exact match. `attribute`: one of the [attribute vocabulary](#structure-22) (structured beliefs only). 400 for an unknown status or attribute. |
| `GET /api/beliefs/{id}` | the belief with its evidence and 20 newest events |
| `POST /api/beliefs/{id}/confirm` | `confirm`: confirmed by the owner (also a stale belief: restored and kept for good) |
| `POST /api/beliefs/{id}/correct` `{"text"}` | `correct`; 400 on empty text |
| `POST /api/beliefs/{id}/restore` | `restore`: back to tentative, then the rule |
| `POST /api/beliefs/{id}/forget` `{"reason"?}` | forget completely (`src/belief_forget.py`, [4.2](#complete-forget-42)): `reject`, `erase_fact` in the module, `erase` here, copies elsewhere in the hub; answers `{"belief", "propagated": {"erased", "retired", "failed", "unsupported"}, "erased": {"memory", "insights", "remaining", "pending"}}`. Also erases a belief rejected before 4.2. Confirm, correct and restore answer 409 on an erased belief |
| `POST /api/beliefs/{id}/dismiss` `{"reason"?}` | [dismiss](#dismiss) (`belief_forget.dismiss`): `reject` here, `retire_fact` in the module, nothing erased; answers `{"belief", "propagated": {"retired", "failed", "unsupported"}}`. 409 on an erased belief |
| `GET /api/beliefs/export` | the owner's ledger and memories as one JSON download ([Export](#export-42)) |
| `GET /api/beliefs/subjects?kind=` | `{"subjects": [...], "stats"}` (`list_subjects`: counts per status, aliases, source person ids); `kind`: `person`, `project`, `thing` or `owner`; 400 for another kind |
| `GET /api/beliefs/subjects/{id}` | `{"subject", "beliefs", "summary", "stats"}`: the subject, its beliefs of every status but rejected (with `attribute`, `value`, `valid_from`) and `subject_summary` |
| `POST /api/beliefs/subjects/{id}/kind` `{"kind"}` | `set_subject_kind`: `person`, `project` or `thing` |
| `POST /api/beliefs/subjects/{id}/rename` `{"display"}` | `rename_subject` |
| `POST /api/beliefs/subjects/{id}/link` `{"contact_id"}` | `link_contact`; the id must be one of the owner's contacts (else 400) |
| `POST /api/beliefs/subjects/{id}/unlink` | `unlink_contact` |
| `POST /api/beliefs/subjects/merge` `{"keep_id", "drop_id"}` | `merge_subjects`; answers the kept subject |
| `GET /api/beliefs/contacts` | the owner's Odysseus contacts for the link picker, `{"contacts": [{"id", "name", "subject_id"}]}` by name (`subject_id`: the subject the contact is linked to, or null). Read with the contacts routes' own listing (`routes/contacts`: the local book or CardDAV, owner-scoped the same way) |
| `GET /api/beliefs/contact/{contact_id}?status=` | `{"contact_id", "beliefs"}` (`beliefs_for_contact`; `status` as for `/api/beliefs`). For a later contact-card integration; no upstream UI uses it yet |
| `POST /api/beliefs/run` | the facts job for this owner now (`module_facts.sync`, at most 60 s); `{"summary": <counts line>, "stats"}`. 503 when the module isn't connected, 409 when `MODULE_FACTS_SYNC=off`, 502 on a failed call, 504 on timeout |

Subject routes answer 400 with the ledger's message for invalid input (a name another
subject has, a contact already linked to another subject, the owner subject's kind,
dropping the owner subject, merging a subject into itself) and 404 for an unknown or
another owner's subject. Belief ids are `int` path parameters (`{belief_id:int}`), so a
literal path another router adds under `/api/beliefs/` (such as `/search`) still reaches
that router.

The page has a count per status, a search box, a **By status / By subject** toggle (kept
for the browser session), and in By status tabs Tentative / Confirmed / Stale / Rejected
(the tab is kept for the browser session), a subject filter, an attribute filter (the
vocabulary of [Structure](#structure-22)) and the beliefs grouped by subject (the
subject's name opens it). On the Tentative tab, beliefs heard in an Athena conversation
(evidence payload `conversation_id`, MCP 0.11.0) are grouped by that conversation instead,
newest first (by the day its facts were first said, so a fact said again later doesn't
move an old conversation up; on the same day, the newest beliefs first), each card naming
its subject (a button that opens it), with **Dismiss all N** on the group (one confirm,
then each belief is dismissed as with its own button, the group's buttons stay off until
all are done; a toast counts them); other tentative beliefs follow, grouped by subject.
When the search route is missing, the page's own search also matches the kind and the
quoted line.
Each card shows the text (the owner's correction marked "edited"), its subject's kind
icon, a status chip with how it was confirmed (by its source, by the rule, by you), the
attribute and value when structured ("Lives in: Lisbon (since …)"), the kind Athena gave
the fact (Preference, Plan, Decision, Relationship, Personal; else the category), first and
last seen
("not heard since" for stale ones), and one line per evidence: source name, times ×
days, the dates and a link that opens in a new tab (Athena's are signed
transcript links), then the transcript line the fact was said on (`quote`) when Athena
sends it. Gone evidence is marked "no longer reported".

| Status | Buttons |
|---|---|
| tentative | Confirm, Edit, Dismiss, Forget |
| confirmed | Edit, Dismiss, Forget |
| stale | Confirm (restores and keeps it), Dismiss, Forget |
| rejected | Restore, Erase (a dismissed belief, or one forgotten before 4.2); the chip reads "Dismissed" |
| rejected, erased | none: the card reads "Erased" |

Forget (and Erase) asks first: "Forget this for good? … This cannot be undone." The toast
says what the module did (erased, only hidden by an older module, not reached: retried
hourly) and whether the words are still in a place the owner keeps (a note). An erased
belief's evidence lines say per source "erased there", "hidden there; its words stay
until Athena is updated", "not erased there yet (retried hourly)" or "still written
there: edit it yourself". **Download** (top right) saves the [export](#export-42).

Edit is an inline text field (Save / Cancel; Enter / Escape) that calls `correct`.
**Sync now** calls `/api/beliefs/run` and reloads.

**By subject** (plan stage 2.1) lists the subjects (the owner first, then by name), with a
name filter and a kind filter. Each card shows the name, a kind chip (You, Person,
Project, Thing), the aliases, the counts per status, the linked contact's name and the
names the sources use, and these buttons:

| Button | Does |
|---|---|
| Open | the subject: its summary paragraph (`subject_summary`; "no summary" while nothing is confirmed) and its beliefs (all but rejected) grouped by attribute in vocabulary order, each group under a label chip ("Lives in", "Works at" …), unstructured ones under "Other"; every belief card has its usual buttons |
| Kind (dropdown) | Person, Project or Thing (`kind`); not on the owner subject |
| Rename | inline field; the old name stays an alias; a name another subject has is refused (merge instead) |
| Link contact / Change contact | a picker over `/api/beliefs/contacts` with a search box; a contact already linked to another subject is shown with that subject's name and can't be picked (merge the two instead) |
| Unlink | removes the contact link |
| Merge into… | pick the subject this one is the same as, confirm; its beliefs, names and person ids move there and it is removed (not on the owner subject; it can be the target) |

**What linking does.** The subject gets the contact's `uid` (`contact_id`); memory
mirrors right away, so every confirmed belief about the subject carries `contact_id` in
its memory entry (text unchanged), and `beliefs_for_contact` (`GET
/api/beliefs/contact/{id}`) answers for that contact. Contact cards in Odysseus don't show
beliefs yet (no upstream UI change).

**Search.** The box at the top calls `GET /api/beliefs/search?q=…&status=all` (the
ranked search of stage 2.3, `routes/belief_search_routes.py`) and shows the hits as
belief cards with their `why`. While that route isn't installed (404) the page falls back
to a plain text match over the beliefs (every word in the text, the subject or the
structured value) and says so.

**What Forget does.** After a confirmation prompt, the belief is rejected here first
(out of memory at once, and it stays rejected whatever arrives later). Then each
Athena fact behind it is retired in the module with `retire_fact`. The toast
reports the `propagated` counts: "Forgotten; the module retired 1 fact", or "Forgotten
here; the module could not be reached or cannot retire facts yet" (`unsupported`: the
module isn't connected or lacks `retire_fact`), or "could not retire it" (`failed`: the
call failed, timed out after 30 s, or raised). Forgetting never fails because of the
module: the local rejection always stands. A belief with no Athena evidence
makes no module call.

**The "No fact" case.** After a re-summary the module may no longer know the fact id
the ledger holds and answers `No fact #N.` That is a normal answer, not an error, so
`propagate_forget` counts it as `retired`: the fact is gone from the module anyway.
`Fact #N was already retired.` counts the same way.

### Dismiss

The owner decided (2026-10-23) that Forget, which erases the transcript lines too, is too
strong for a belief that is only not worth keeping: something really said but true for a
day ("Marisol was very focused today"). **Dismiss** handles those, with no confirmation
prompt because it can be undone:

1. `reject` here: rejected, out of memory at once, and later evidence for it stays rejected.
2. `propagate_forget` ([1.4](#forgetting-propagates-14)): `retire_fact` for each
   Athena fact behind it, so the module stops reporting it and a repeat linked
   "same" doesn't come back as a new fact. The transcript, summary and links keep their
   words. Module calls share the page's 30 s timeout.

Nothing is erased. The belief shows on the Rejected tab ("Dismissed"), where Restore
brings it back as tentative and Erase forgets it completely. A module that is down or
lacks `retire_fact` is counted (`failed`, `unsupported`) and not retried: the fact may be
reported again, but it attaches to the rejected belief and stays out of memory. Code:
`dismiss` in `src/belief_forget.py`; tests in `tests/fork/test_beliefs_routes.py`.

### Move to another person

The owner reported (2026-10-23) a fact about someone else filed under the owner: the
module heard that person's line as the owner's voice. Edit only changes the words; the
belief stays under its subject. **Move** (on tentative and confirmed cards) picks a
person, or types a new name, and has two steps:

1. `move` in `src/evidence_intake.py`: the belief gets the new subject (`subject_id` or a
   name: an existing subject or alias, else a new person; the owner's own words such as
   "me" give the owner subject). Its `about`, matching keys and category follow the
   subject (a person's belief is a `contact` memory). It is confirmed by the owner, so a
   later sync keeps it there, and event `moved` records it. A fact the module later
   re-attributes to that same subject (new subject and words, same fact id) stays with
   the moved belief instead of splitting off as a new one.
2. With "They said it" ticked (the default when a conversation is behind it),
   `src/belief_move.py` fixes the line in Athena: `get_transcript` reads the line
   at each link's `#t=` second and, when the old subject said it, `reassign_line` gives
   it to the new person (under the name the module knows them by), so the module
   re-summarizes that conversation. A line someone else said is left alone: the fact was
   about the old subject because of its words. No module change; both tools are in the
   contract.

`POST /api/beliefs/{id}/move` {`subject_id` | `name`, `fix_line`} answers the belief plus
`lines`: `{"reassigned", "kept", "not_found", "failed", "unsupported"}`. Module problems
never undo the move (20 s budget); the toast says when a line still needs fixing by hand
(ask in chat to reassign it). Tests: `tests/fork/test_belief_move.py`.

## Subjects and contacts (2.1)

Hub side of plan stage 2.1. Every belief belongs to one **subject**: the owner, a person, a
project or a thing. Names are no longer matched by string alone: a module's stable person
id maps to one subject, whatever name the module uses for that person today.

| Table | Columns |
|---|---|
| `subjects` | `id`, `owner`, `key` (normalized name: lowercase, whitespace collapsed, quotes and trailing punctuation dropped), `display`, `kind` (`person`, `project`, `thing`, `owner`), `kind_by` (`auto` or `owner`: who set the kind), `contact_id` (an Odysseus contact's `uid`, or null), `aliases` (JSON list of other names), `note`, `created_at`, `updated_at`. Unique on (`owner`, `key`). |
| `subject_sources` | `subject_id`, `owner`, `source` (e.g. `odysseus-sound`), `source_person_id` (the source's stable person id), `name` (the name the source gave last), `updated_at`. Unique on (`source`, `source_person_id`, `owner`): one person id maps to one subject per owner. |

**Resolution** (`submit`, for each new belief and whenever the source changes an item's
subject or `about`):

1. `about="owner"` → the owner's one subject of kind `owner` (created on first use, named
   "Owner" unless the first name it hears is a real name; `Me`, `I`, `Owner`, `the owner`
   are not names). A real name the source uses for the owner becomes an alias.
2. `Evidence.person_id` already mapped in `subject_sources` → that subject (a new name for
   it becomes an alias).
3. A subject whose key or one of whose aliases matches the name (case-insensitive) → that
   subject.
4. Else a new subject: `person` for `about="person"`, `project` when the name matches
   [the project words](#projects-24), else `thing`. Evidence without a subject goes to
   the subject "General".

A person id seen for the first time is mapped to the subject found. Evidence with a person
id that joins an existing belief (same text) maps the id to that belief's subject. The
belief's `subject` column keeps the source's name; `subject_id` is the subject.

**Migration.** A ledger from before Phase 2 (`PRAGMA user_version` < 2) gets the new
columns on first open, and every belief is filed under a subject from its `subject` and
`about` (steps 1, 3, 4 above). Lossless: no text, status or evidence changes.

**People from Athena.** The facts job calls `list_people {"format": "json"}`
(`{"people": [{"id", "name", "aliases", "is_owner", …}]}` or a bare list) and
`sync_people(owner, "odysseus-sound", people)`:

- `is_owner` → the owner subject (an owner subject still called "Owner" takes the real
  name; "Owner" stays an alias); the mapping is moved there if it pointed elsewhere.
- When no person is marked `is_owner`, a person whose name is an owner word (`Me`, `I`,
  `Myself`, `Owner`, `the owner`, `User`, `the user`) is the owner too: its id maps to the
  owner subject and the name becomes an owner alias (the module's own facts about the
  owner carry that person id). A "Me" person subject an earlier pass created is removed
  when nothing else uses it. When an `is_owner` person is reported, such a name is an
  ordinary person.
- Else the subject the id maps to, else one whose name or alias matches the person's
  name or aliases, else a new `person`. A matched inferred `thing` or `project` becomes a
  `person`.
- Names and aliases the subject doesn't have become aliases.
- A person the module no longer lists (merged or deleted there) keeps its mapping: nothing
  is deleted.

**Functions** (all scoped to the owner; another owner's id raises `SubjectNotFound`, a
`LookupError`; invalid input raises `ValueError`):

| Call | Effect |
|---|---|
| `list_subjects(owner, kind=None)` | subjects (the owner first, then by name), each with `counts` (per status and `total`), `aliases` and `sources` (`[{"source", "person_id", "name"}]`) |
| `get_subject(owner, id)` | one subject, same shape |
| `find_subject(owner, name)` | the subject whose name or alias matches, or None |
| `set_subject_kind(owner, id, kind)` | `person`, `project` or `thing`; `kind_by = owner`, so inference never changes it again; the owner subject keeps its kind |
| `link_contact(owner, id, contact_id)` / `unlink_contact(owner, id)` | link a subject to an Odysseus contact (`uid` from `routes/contacts`); one contact links to one subject (a second link raises: merge the subjects) |
| `merge_subjects(owner, keep_id, drop_id)` | beliefs and person ids move to `keep_id`; the dropped name and its aliases join the aliases; its contact link is kept when the kept subject has none, and its kind when the kept one is an inferred `thing`; the owner subject can be kept, never dropped |
| `rename_subject(owner, id, display)` | new name; the old one becomes an alias; a name another subject has raises (merge instead) |
| `beliefs_for_contact(owner, contact_id, status=None)` | the beliefs of the subject linked to that contact (empty when none is) |
| `sync_people(owner, source, people, now=None, mirror=True)` | see above; counts `people`, `subjects_added`, `mapped`, `aliases_added` |
| `subject_summary(owner, id)` | one paragraph from the subject's confirmed beliefs grouped by attribute, e.g. `Priya Nair (person). lives in: Porto (since 2025-03-01). allergy: peanuts. Also: Priya ran the design review.`; empty when nothing is confirmed. For the insight and chat (2.3). |

Subject changes that affect memory (kind, contact, merge) mirror memory right away.

## Structure (2.2)

Confirmed beliefs get optional structure next to their text: `attribute`, `value`,
`valid_from` (a date) and `structured_at` (when it was written). The text stays what
memory shows; structure is for consumers that look beliefs up
(`beliefs_by_attribute(owner, attribute, subject_id=None, *, status="confirmed")`,
`subject_summary`).

**Attribute vocabulary** (`evidence_intake.ATTRIBUTES`); anything else becomes `other`:

| Attribute | For |
|---|---|
| `lives_in` | a home town or address |
| `works_at` | an employer |
| `role` | a job title or role |
| `birthday` | a birthday |
| `allergy` | an allergy |
| `diet` | a diet (vegetarian, no alcohol …) |
| `likes` / `dislikes` | preferences |
| `relationship` | how a person relates to the owner or someone else |
| `owns` | a thing someone owns |
| `pet` | a pet |
| `plans` | a stated plan |
| `decision` | a decision taken |
| `routine` | a regular habit ("Gym on Tuesdays around 18:00"; the calendar routines of the [Today insight](today-insight.md#routines) are submitted as tentative evidence, source `routines`, for the owner to confirm) |
| `contact_info` | a phone number, email or handle |
| `health` | a health fact |
| `project_status` | where a project stands |
| `deadline` | a due date |
| `other` | none of the above |

Only attributes a consumer uses are added (plan risk: over-modelling).

**The Belief Structure task** (`src/belief_structure.py`; built-in, hourly at :15 after
the facts job; posts nothing to chat; queued with the other model-backed tasks):

- Takes up to 40 **confirmed** beliefs per run with `structured_at` empty
  (`structure_candidates(owner, limit)`: `id`, `subject`, `kind`, `text`, `first_seen`,
  `last_seen`): never tried first, then the longest ago tried (`<DATA_DIR>/
  belief_structure_<owner>.json` keeps when each waiting belief was last tried), so
  beliefs the model keeps getting wrong can't starve the rest. A correction (`correct`)
  or a source edit of the belief's text clears `structured_at`, so the belief is
  structured again; the old structure stays until then.
- Sends `BELIEF_STRUCTURE_BATCH` (5) per call to the local model (background workload,
  thinking off, waits for chat to go quiet; the answer budget grows with the batch). The
  facts are numbered 1..n, each with subject name, subject kind, text and the dates it
  was said (`said`), as data, never as instructions. The answer:
  `{"items": [{"n", "attribute", "value", "valid_from", "valid_to"}]}`, mapped back by
  number (a small model mixes up long ids); one retry on unusable JSON, then the batch
  waits for the next run.
- **An item must fit its belief.** Its value shares a content word (4+ letters, not a stop
  word, the same first five letters) with the belief's text; a value without one ("cat",
  "42") shares any word; `other`, and `birthday` with a date in the text, always fit.
  Otherwise the item is **rejected** (counted) and the belief waits. This catches a model
  that merges two facts into one item under the first one's number (seen with batches of
  10 on a 4B model: an appointment filed as "health: avoid hard food, nuts").
- **No guessed dates.** A `valid_from`/`valid_to` is kept only when its year is in the
  text, or it falls within a year after the belief was last said and the text names a
  month or day ("by October 31", said 2026-10-15 → 2026-10-31; a guessed 2023 is
  dropped).
- Writing (`apply_structure(owner, id, attribute, value, valid_from=None,
  valid_to=None, *, expected_text=None)`): unknown attribute → `other`; value cut at 120
  characters; dates must be whole ISO dates. A `valid_to` becomes `valid_until` only when
  the belief has none yet, and never when it is in the past or before the belief was last
  seen, so a wrong end date can't expire a current belief. A belief whose text changed
  while the model answered, or that is no longer confirmed, is left for the next run.
- Result line, counts only: `structure: 12 to do, 11 structured (2 other), 1 waiting, 1
  rejected` (`, 1 failed` when a batch had no usable answer). Nothing to do: no model
  call, not logged in Activity. `BELIEF_STRUCTURE=off` stops it.

## Projects (2.4)

A subject of kind `project` groups everything known about a project. It becomes one:

- by the owner: `set_subject_kind(owner, id, "project")` (the review page's Kind dropdown);
- at creation, when it isn't a person (`about` isn't `person`, no source person id) and its
  name matches `\b(project|launch|release|migration|plan)\b` (case-insensitive):
  "Project Heron", "Spring launch";
- after structuring, when one of its beliefs gets `project_status` or `deadline` and the
  subject is still an inferred `thing` that no source maps to a person.

Inference never overrides the owner (`kind_by = owner`) and never turns a person into a
project. `subject_summary` gives the insight and chat one paragraph per project (used by
2.3, retrieval).

## Retrieval (2.3)

One ranker over the ledger, used by the Today insight, chat and the review page. Code:
`src/belief_search.py`, `routes/belief_search_routes.py`; test
`tests/fork/test_belief_search.py`.

```python
from src import belief_search
hits = belief_search.search_beliefs(owner, "where does Tom live", k=10,
                                    status=("confirmed",), subject_id=None,
                                    attribute=None, kinds=None)
# each hit: the belief dict (as list_beliefs returns it) + "score" (0..1) + "why"
```

**Ranking.** The score adds four parts; each weight is larger than the sum of the ones
after it, so the order of the parts is also the order of importance:

| Part | Weight | When |
|---|---|---|
| subject | 0.55 | the query names the belief's subject: its name or an alias (case-insensitive, whole words, `'s` allowed), or a person's first name when no other subject has it; "I", "me", "my", "myself", "mine" mean the owner's subject when the query names no one else |
| attribute | 0.25 | the query uses a word of the belief's `attribute` (synonym map below); an unstructured belief whose text has such a word gets 60 % of it |
| semantic | 0.12 | cosine similarity of the query to `"<subject>: <shown text>"`, scaled from `MIN_SIMILARITY` (0.5, adds nothing below) to 1; only with a local embedding lane |
| keyword | 0.08 | token Jaccard of the query and the same text (lowercase words, stop words dropped): the fallback and tie-breaker |

**Candidates.** Only the beliefs that can score are read (light rows, no evidence; the
full belief dicts are loaded for the top `k` only): when the query names a subject, that
subject's beliefs (plus the owner's when "I/me/my" is in it too, without the subject
part); else, when it uses attribute words, the beliefs with those attributes and the
unstructured ones (no attribute or `other`); else the newest `SCAN_LIMIT` (2000) by
`last_seen`. The `status`, `subject_id`, `attribute` and `kinds` filters always apply.

Only beliefs that score above 0 are returned. Ties: `last_seen` (newest first), then id;
the same call always gives the same order. `why` names the parts that scored, in that
order ("subject match, attribute, keyword"), plus "; no embeddings" when no lane was used.
`status` is one status, several, `"all"` or None; `subject_id`, `attribute` and `kinds`
(subject kinds) narrow the pool. `search_details(...)` returns the same hits with
`semantic` (a lane was used), `embedded`, `pending` (see below) and `candidates` (beliefs
scored).

**Synonym map.** Phrases (`PHRASES`) are read first, and their words then count no more:
"do for a living", "for a living", "job title", "works as" → `role` and `works_at`;
"related to", "relationship with/to", "who is … to me" → `relationship`; "going to",
"plan(ning/ned) to" → `plans`. Then single words (`SYNONYMS`, query word → attribute):

| Words | Attribute |
|---|---|
| allergy, allergies, allergic | `allergy` |
| live, lives, living, home, address, based | `lives_in` |
| work, works, working, job, employer, employed, company, profession, occupation | `works_at` |
| role, title, position, job, profession, occupation | `role` |
| birthday, birthdays, born | `birthday` |
| diet, vegetarian, vegan, pescatarian, eat, eats, alcohol | `diet` |
| like, likes, love, loves, favourite, favorite, enjoy, enjoys | `likes` |
| dislike, dislikes, hate, hates | `dislikes` |
| wife, husband, partner, sister, brother, mother, father, son, daughter, friend, colleague, boss, manager, married, related, relation, relationship, relative | `relationship` |
| own, owns | `owns` |
| pet, pets, dog, dogs, cat, cats | `pet` |
| plan, plans, planning, planned | `plans` |
| decide, decides, decided, deciding, decision, decisions | `decision` |
| phone, email, number, handle | `contact_info` |
| health, medication, condition | `health` |
| status, progress | `project_status` |
| deadline, due | `deadline` |

**Embeddings.** Only the hub's own lane, never a remote API: `belief_search.embedder()`
takes the memory index Odysseus loaded at start-up (`MemoryVectorStore`, whose lanes come
from `embedding_lanes.build_embedding_lanes`) and uses its first healthy lane that is
local (FastEmbed, or a configured endpoint on loopback, a private or Tailscale address,
a container name or a `.local` host). No index, an unhealthy one, or only a remote lane:
no semantic part. A lane that fails during a search is logged and the search answers
from the other parts.

**Vector cache.** `belief_vectors` in `beliefs.db`: `belief_id` (primary key, deleted with
the belief), `text_hash` (of the embedded text), `model` (lane name and fingerprint),
`vector` (float32 BLOB), `updated_at`. A search embeds, lazily and in batches of 50, the
beliefs of its pool whose vector is missing or whose text or model changed, at most
`EMBED_PER_CALL` (200) per call, lowest ids first; the others are counted as `pending`
and done on the next call, so a request never waits on more than 200. A second search
over unchanged beliefs embeds only the query. `reindex(owner)` drops the owner's vectors
(and those of deleted beliefs) and recomputes all of them (tentative, confirmed, stale)
without the cap.

**Prompt block.** `context_for(owner, query, k=8, max_chars=1200)`: the top confirmed
hits as

```
What I know (confirmed). Use your tools for anything else, and cite the link next to each fact you use:
- Priya Nair: Priya Nair lives in Porto (http://…/view/…?sig=…#t=20)
- Sam Rivera (you): I am allergic to penicillin (http://…)
```

Lines that don't fit `max_chars` are skipped; "" when nothing matches.

**Where it is used.**

- *Today insight*: `select_memory` gets the query `memory_query(candidates)` (the day's
  candidate titles, list names and locations, one per line) and puts the ranker's top
  40 confirmed beliefs first (texts without links), then the other memory entries most
  used first, up to 40. With an empty ledger or no hit, the old selection (most used
  first). The input hash still uses the sorted texts, so a reorder alone doesn't rerun
  the model.
- *Chat*: the existing memory-context hook (`build_context_preface` →
  `today_insight.context_block`, label "saved insight: today and beliefs") adds
  `belief_search.chat_block(owner, message)` as untrusted context when
  `wants_beliefs(message, owner)`: "what do you know about / tell me about / who is
  <known subject>", or an attribute question about a known subject or the owner ("where
  does Tom live", "am I allergic to anything", "when is Mom's birthday?", "does Jonas
  have any allergies", "what's the status of Project Heron", "how is Ben related to
  me?", "what does Priya do for a living?"). Known subjects: the
  owner's subjects with a confirmed belief. Never on slash turns or slash-invoked skills,
  never with words about what was said or happened (said, told, asked, mentioned,
  promised, recommended, latest, a meeting, a conversation, a voice note, a month or
  weekday, last week …): those questions must reach the module's tools, so none of
  Athena's 21 bakeoff questions gets the block (tested). "What is <person>
  allergic to?" is such a bakeoff question and is left to the module; the yes/no form
  ("Is Priya allergic to nuts?") and "allergies" get the block. Without a ledger file the
  gate returns at once and creates nothing.
- *Review page*: `GET /api/beliefs/search?q=&k=&status=` (admin session like the other
  belief routes; API tokens refused; scoped to the logged-in owner) answers `{"query",
  "results", "semantic", "pending"}`. `k` 1–50 (default 10); `status` one status, a
  comma list or `all` (default `confirmed`; 400 for an unknown one). Mounted before the
  review page's router so `search` isn't read as a belief id.

## Memory mirroring

`sync_memory(owner)` keeps exactly one Odysseus memory per confirmed belief:

- Text: `(owner_text or text) + " (source: <link>)"` (chat context injects only a
  memory's text, so the link must be in it for answers to cite it). Category from the
  belief.
- The link (and the memory's `source`) come from one **citing evidence**
  (`primary_evidence`, ranked by `link_rank`): live evidence before gone evidence, then
  1. an absolute `http(s)://` link from `odysseus-sound` (the signed transcript link),
  2. any other absolute link,
  3. a relative link (a note's `/#open=notes&note=…`),

  and the newest (`seen_last`, then id) within the same rank. So a note that repeats a
  conversation fact confirms it but doesn't take over its citation: answers keep citing
  the transcript.
- Fields: `source` (the citing evidence's source: `odysseus-sound`, `notes` …),
  `belief_id`, `owner`, `subject`, `about`, `link`, `first_seen`, `last_seen`,
  `synced_at`, `subject_id`, `subject_kind` (`owner`, `person`, `project`, `thing`), and
  `contact_id` while the subject is linked to a contact (removed when unlinked). The text
  doesn't change; a subject change updates the entry in place. A belief with Athena evidence also keeps the Phase 0 fields
  `module_fact_id`, `times_said`, `days`, `first_said`, `last_said`.
- Updated in place (memory id and `uses` kept); copies of beliefs that are no longer
  confirmed, duplicates and copies of unknown beliefs are deleted; nothing is written
  when nothing changed, so a second run leaves `memory.json` byte-identical.
- A Phase 0 copy (`source: odysseus-sound`, `module_fact_id`, no `belief_id`) is adopted
  by the belief that has that fact id as evidence: same memory id and `uses`, no
  duplicate. Until the ledger has heard from the module at all, such copies are left
  alone.
- The owner's own memories and other owners' entries are never touched. Upstream's
  Memory Tidy and memory audit leave mirrored entries alone
  ([Consolidation](#consolidation-41)).
- Same discipline as upstream's memory writers: a strict read (`load_all_for_update`,
  never saving over a store it couldn't read) and the save right after, under the
  ledger's lock.
- `sync_memory(owner, belief_ids=[...])` reconciles only those beliefs' entries (added,
  updated or removed) and leaves every other entry exactly as it is. The owner actions
  and subject edits use it (one click no longer re-reads and compares every mirrored
  belief); the jobs run the full sync.

## Complete forget (4.2)

Code: `src/belief_forget.py` (the order and the hub's copies), `erase` in
`src/evidence_intake.py` (the ledger's copy); tests `tests/fork/test_belief_forget.py`,
`tests/fork/test_beliefs_routes.py`; the eval case `erased` ([Measure](#measure-16)). The
owner decided (2026-10-23): one Forget that always erases, with a confirmation, and no
restore; later repeats are erased too; beliefs forgotten before 4.2 get an Erase button,
nothing is erased automatically.

`belief_forget.forget(owner, id, reason, call=...)`, in this order:

| Step | Where | What |
|---|---|---|
| 1 | ledger | `reject`: rejected, out of memory at once (the mirrored entry and its vector) |
| 2 | Athena | `erase_fact {"fact_id", "reason"}` per fact behind it (MCP 0.10.0): the transcript line that said it reads `[forgotten]` in every conversation that said it, the notes and digests are hidden and rewritten by the module's local model without it, search and embeddings are redone, kept audio of the line is deleted, later repeats linked "same" are erased too. A module without `erase_fact` gets `retire_fact` (the fact is hidden there, its words stay). "No fact #N." counts as done (re-summarized ids). Module calls share one timeout (30 s on the page) |
| 3 | ledger | `erase`: `text`, `owner_text`, `note`, `subject`, `attribute`, `value`, `valid_from` cleared, `subject_id` null; `norm` and `owner_norm` become `erased:<sha256>`; every evidence row keeps no `text` and `subject`, and no `link` unless its source still holds the words (not Athena); `payload` is `{"erase": state}`; event details are cleared and an `erased` event added; the `belief_vectors` row is deleted. The write runs with `secure_delete` and the WAL is checkpointed after it, so the words leave `beliefs.db` and its `-wal` file |
| 4 | memory | other memory entries of the owner with exactly its words (lowercased, punctuation and a `(source: …)` suffix dropped): a chat-extracted memory, a copy the owner made. Never fuzzy, never another owner's. Their vectors go too |
| 5 | Today insight | stored insight files (`today_insight/<owner>/*.json`) drop items with ref `belief:<id>`, and any reason or headline that quotes its words. Today's insight is rebuilt on its own (its inputs changed) |

Per evidence, `payload.erase` is `done` (the module erased it), `retired` (an older module
only hid it), `pending` (the module wasn't reached, or the claim came back as a new fact)
or `source` (another source, e.g. a note: the owner edits it there; its link stays so the
page and the answer's `remaining` can say where). Every Module Facts pass ends with
`retry_pending`: `erase_fact` again for module evidence of erased beliefs that isn't
`done` (stats key `erased`), so a module that was down or older catches up.

Staying forgotten: the hashed keys still match the same claim, so evidence that says it
again from any source (a note, the module under a new fact id) joins the erased belief
without its words (module evidence as `pending`, so the next pass erases it in the module
too) and never reaches memory. Only exact matches after normalising, never fuzzy:
a reworded claim is a new belief.

"Complete" on the hub side means: no copy of the words in `beliefs.db` (belief, evidence,
events, vector), in memory or its vectors, or in stored Today insights. Not touched, by
design: the owner's chats (they may quote it; the owner deletes a chat themselves), the
owner's own sources (notes: listed in `remaining`), and backups or copies made outside
Odysseus. Erasing can't be undone.

## Export (4.2)

**Download** on `/beliefs` (`GET /api/beliefs/export`, `src/belief_export.py`; same admin
auth and owner scope as the page) returns one JSON file, `odysseus-memory-<date>.json`,
built per request and sent only to the owner's browser (`Cache-Control: no-store`; never
stored on the server or sent elsewhere):

```json
{"format": "odysseus-memory-export", "version": 1, "exported_at": "…Z", "owner": "…",
 "counts": {"beliefs", "evidence", "events", "subjects", "memories"},
 "beliefs": [{"id", "text", "shown_text", "status", "…": "…", "evidence": [{"source",
   "source_id", "text", "link", "…": "…"}], "events": [{"at", "kind", "detail"}]}],
 "subjects": [{"id", "key", "display", "kind", "contact_id", "aliases", "…": "…"}],
 "subject_sources": [{"subject_id", "source", "source_person_id", "name", "…": "…"}],
 "memories": [{"id", "text", "category", "source", "…": "…"}]}
```

Beliefs of every status, each with its evidence (with links) and every kept event; the
matching keys stay out. An erased belief appears without words. If memory can't be read
the route answers 503 instead of an export without memories. There is no import back:
upstream's memory import (`POST /api/memory/import`) asks the model to pull memories out
of a document, it doesn't restore entries, so a round trip wouldn't be faithful.

## Consolidation (4.1)

Code: `src/belief_consolidation.py`; test `tests/fork/test_belief_consolidation.py`. Two parts.

**Upstream's tidy leaves sourced memories alone.** A memory entry with provenance is one
that mirrors a belief (`belief_id`) or is a Phase 0 copy of a module fact (`source:
odysseus-sound` with `module_fact_id`). Upstream has two passes that tidy memory by text
alone: the built-in task **Memory Tidy** (`consolidate_memory`, run after every 5 new
memories, so the mirror's own additions set it off) and the **memory audit** (the Tidy
button on the memory page, and the automatic audit after chat extraction). Either could
merge a mirrored entry into an unrelated memory, rewrite it without its `(source: …)`
link, or delete it, and the next ledger sync would put it back with a new memory id. Two
hooks keep them out:

- Memory Tidy (`src/builtin_actions.py`, `action_consolidate_memory`): sourced entries
  are dropped from each owner's group before the duplicate check and the model call, so
  they are never compared, sent to the model, rewritten or deleted.
- Memory audit (`services/memory/memory_extractor.py`, `audit_memories`): sourced
  entries are split off before the fingerprint, the model call and the 50 % safety net,
  and written back unchanged (from the fresh strict read when the audit is per owner).
  The owner's plain memories are audited as before.

**The fork's own pass merges near-duplicate beliefs, one source at a time.**
`consolidate(owner, source)` runs in each source's job (Module Facts for
`odysseus-sound`, Notes Evidence for `notes`), after intake and before memory sync. Intake
already joins evidence whose matching key is equal; this pass catches the same claim in
slightly different words (a module that writes "Prefers window seats on long flights"
one week and "… on long flight" the next under two fact ids, or two notes saying it).

- **Candidates:** the owner's tentative and confirmed beliefs whose evidence all comes
  from that one source. Never merged: beliefs the owner confirmed or corrected,
  rejected and stale beliefs, beliefs with evidence from two sources (agreement across
  sources is confirmation, rule (b), not a duplicate), other owners' beliefs.
- **Same claim:** same subject and `about` (the category may differ; the kept belief keeps its own); the words of the two matching keys
  may differ only in filler words (`the`, `very`, `still` …) and in another form of the
  same word (plural or third person: flight/flights, prefer/prefers). A different name,
  place, thing or tense stays apart ("a rabbit called Luna" / "… called Bruno",
  "works at" / "worked at"), and so does any difference in a negation word (`not`, `no`,
  `never`, `doesn't`, `longer` …) or a number. Two structured beliefs must also agree on
  attribute and value.
- **Which one stays:** confirmed before tentative, then the one already in memory (its
  memory id and `uses` stay), then more evidence, then the older belief.
- **The merge:** the other belief's evidence moves to the kept one with its source ids,
  links and dates, so the source keeps mapping those ids to the kept belief and a
  resubmit changes nothing. The kept belief gets an event `merged` (`#<id>: <its text>`)
  and takes the other's structure if it had none. The merged belief is deleted (its
  events and cached vector with it); its memory entry goes at the memory sync. The kept
  belief's dates and status are recomputed: merged sightings can confirm it by rule (c).
- Result lines of both jobs gain `, N merged` when something was merged.
- `BELIEF_CONSOLIDATE=off` skips the merge pass; the upstream exclusion always applies.
- On the fictional scale corpus (`scripts/belief_eval_scale.py`, distinct beliefs only)
  the pass plans 0 merges: 0.1 s for `medium` (3000 beliefs), 0.4 s for `large`
  (10 000).

## The Athena facts job

The built-in task **Module Facts** (hourly, silent) now calls `list_facts {"format":
"json", "limit": 1000}` (every current fact, not only confirmed ones) and submits each as
evidence from `odysseus-sound` with the fact id as `source_id`, `first_said`/`last_said`
as seen dates, `times_said`/`days` as totals and the row's `confirmed` as
`source_confirmed`. A module that doesn't send `confirmed` per row yet gets a second call
`{"confirmed": true, "format": "json", "limit": 1000}`, whose ids are the confirmed set.
Each pass also calls `list_people {"format": "json"}` once and maps the module's people to
subjects ([Subjects and contacts](#subjects-and-contacts-21)) before the facts are
submitted; a fact row's `person_id` travels as `Evidence.person_id`, and its
`valid_until` (Athena MCP 0.13.0: the last day a fact about one occasion is worth
keeping) as `Evidence.valid_until`. A module that answers
`list_people` with text or lacks the tool is tolerated (logged once, state
`people_text_only`); its facts match subjects by name.
A row's `kind` (MCP 0.11.0: preference, plan, decision, relationship, personal, other)
sets the evidence category: `preference` for a preference, else `fact` (a fact about a
person stays `contact`); without a kind, a preference word in the text decides, as
before, which filed requests such as "wants Bo to write a report" as preferences. `kind`,
`quote` (≤ 300 characters) and `conversation_id` go into the evidence payload for the
review page (`module_facts.payload`); erasing a belief clears the payload, and Forget
also removes its quoted lines from every other belief's evidence (`scrub_quotes`: facts
said on the same line), since Athena erased that line. A belief's category follows its
live evidence when all of it (with the belief's own `about`) agrees, also for a belief
the owner confirmed or one with several sources, so beliefs filed before kinds existed
are relabelled; Move uses the evidence's kind too. Tests:
`tests/fork/test_module_fact_kinds.py`.
Then `reconcile_source(owner, "odysseus-sound", ids)`, `apply_decay` and one
`sync_memory`. Result line, counts only: `facts: 12 seen, 1 new, 1 confirmed,
0 unconfirmed, 0 gone, 0 stale; memory: 11 kept, 1 added, 0 updated, 0 removed; people:
4 seen, 0 new` (the people part only when the module answered JSON); a pass with no change
is not logged in Activity.

## Sources

| Source | `source` | Task | Setting | Default |
|---|---|---|---|---|
| Athena facts | `athena` (`odysseus-sound` before the migration) | Module Facts (hourly) | `MODULE_FACTS_SYNC` | on |
| The owner's notes | `notes` | Notes Evidence (daily, 03:00) | `EVIDENCE_NOTES` | off |

Athena's source was `odysseus-sound` until 2026-10-29 (the examples on this page still say
so). The ledger reads both names as one source, and keeps writing the name an owner's
ledger already holds, so a ledger never holds both; `scripts/athena_names_migrate.py`
renames the stored one ([stored names](modules/athena.md#stored-names)).

### Notes (1.2)

Code: `src/notes_evidence.py`; test `tests/fork/test_notes_evidence.py`. The built-in task
**Notes Evidence** (daily, silent, queued with the other model-backed tasks) reads the
owner's notes and asks the local model, one call per note, for the durable facts each one
states.

- **Read:** the owner's notes of type `note` (plain notes; not checklists, to-dos or
  goals) that aren't archived.
- **Skipped:** notes labelled `odysseus-sound` (filed from voice notes, so their facts
  already come through the module), calendar reminders (source and label `calendar`),
  and notes whose title and text together are shorter than 40 characters.
- **Only changed notes go to the model.** The state file
  `<DATA_DIR>/notes_evidence_<owner>.json` keeps, per note, a hash of its title and text
  (and of the prompt version) and the evidence ids it produced. A note whose hash is
  unchanged isn't sent again; pinning or recolouring a note doesn't count as a change.
  When the model's answer isn't usable JSON (after one retry) the note keeps its
  previous facts and is tried again on the next run.
- **The model** (background workload, thinking off, waits for chat to go quiet) gets
  the note as data, never as instructions, and answers `{"facts": [{"subject", "about"
  (owner, person, other), "fact"}]}`: facts that stay true for months, each one sentence
  that names its subject (`The owner …` for the owner); none from instructions, to-do or
  shopping lists, code or text quoted from someone else; at most 8 per note. Rows
  without a subject or fact are dropped; a fact is cut at 300 characters.
- **Evidence:** `source="notes"`, `source_id="<note id>:<index>"`, link
  `/#open=notes&note=<note id>` (the hash `static/js/forkEntityHash.js` opens on load),
  `seen_first = seen_last` = the date of the note's last edit, `times = days = 1`,
  `source_confirmed = False`.
- **Never source-confirmed.** A note is one sighting written by the owner, with no
  repetition to count. A fact that is also said in a conversation confirms by rule (b)
  (two sources); anything else stays tentative until the owner confirms it on the review
  page.
- **Gone:** after each pass `reconcile_source(owner, "notes", ids)` with the ids of every
  note still read (re-extracted or unchanged), so facts a changed note no longer states,
  and the facts of notes deleted, archived, relabelled or shortened, are marked gone.
- Result line, counts only: `notes: 2 read, 3 skipped, 14 unchanged, 5 facts; ledger:
  3 new, 1 confirmed, 1 gone, 1 stale` (`, 1 failed` when an answer wasn't usable); a
  pass that read nothing and changed nothing is not logged in Activity.
- **Off by default** (`EVIDENCE_NOTES=off`; `on` enables it): notes hold drafts, plans
  and other people's words, so the source stays off until it has been checked on real
  notes and the owner reviews tentative beliefs on the review page (plan 1.3 before 1.2).
  Turning it off stops the task; evidence already submitted stays in the ledger.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `BELIEF_CONFIRM_SOURCES` | 2 | rule (b): distinct sources that confirm a belief |
| `BELIEF_CONFIRM_TIMES` | 3 | rule (c): sightings across sources |
| `BELIEF_CONFIRM_DAYS` | 2 | rule (c): distinct days across sources |
| `BELIEF_STALE_DAYS` | 180 | decay: days without a sighting before a belief confirmed by its source or the rule goes stale |
| `MODULE_FACTS_SYNC` | on | `off` stops the facts job (memories already mirrored stay) |
| `EVIDENCE_NOTES` | off | `on` lets the Notes Evidence task submit facts from the owner's plain notes |
| `BELIEF_STRUCTURE` | on | `off` stops the Belief Structure task (structure already written stays) |
| `BELIEF_STRUCTURE_BATCH` | 5 | beliefs per model call of the Belief Structure task (1 to 10) |
| `BELIEF_CONSOLIDATE` | on | `off` skips the merge of near-duplicate beliefs within a source ([Consolidation](#consolidation-41)) |

Values below 1 count as 1; a value that isn't a number falls back to the default.

## Measure (1.6)

`scripts/belief_eval.py` is the memory eval: a fictional history seeded through the
ledger's public API (`submit`, `reconcile_source`, `apply_decay`, `confirm`, `reject`,
`correct`, `restore`, `list_beliefs`, `sync_memory`) with every `now` given, so a run is
deterministic. No model is involved; `tests/fork/test_belief_eval.py` runs the same checks
through pytest.

```bash
venv/bin/python scripts/belief_eval.py                     # temp dir, removed after
venv/bin/python scripts/belief_eval.py --data-dir /tmp/belief-eval --keep
```

- The history: one owner (Sam Rivera), three people (Priya Nair, Tomas Berg, Lena Okafor)
  and Project Heron, from March to October 2026. `odysseus-sound` is simulated the way the
  facts job drives it (each pass: submit everything the module lists, `reconcile_source`,
  `apply_decay`, one `sync_memory`); `notes` adds single sightings.
- Cases: said once (tentative), source-confirmed (3 times on 2 days), two sources
  (conversation + note), gone upstream (stale on reconcile), forgotten (rejected; the
  same fact under the same and a new module id stays rejected), erased (4.2: no words
  in the belief, its evidence or events; said again under a new module id it joins the
  erased belief without its words, as `pending`), corrected (memory shows
  the correction in the same memory; the original and the corrected text both join the
  belief), `valid_until` passed, decay (confirmed at day 179, stale at day 195, no flap
  when the module repeats the old sighting), text variant (casing, spaces, punctuation
  join; then 3 sightings on 3 days confirm by the rule), owner-confirmed (stays confirmed
  when the module drops it and past 180 days), restored after a mistaken forget, a
  two-source belief losing one source (back to tentative), gone then seen again, two
  notes on one day, one note, a preference category, and what the ledger knows about
  Priya Nair.
- Memory: exactly the expected confirmed beliefs are mirrored, one memory each, as
  `<shown text> (source: <link>)`; no memory keeps a corrected-away text; the owner's own
  memory is untouched; a second `sync_memory` writes nothing (`memory.json`
  byte-identical).
- Output: one row per check (`case`, `step`, `expected`, `actual`, `ok`/`FAIL`, with
  `status/confirmed_by/mem|out` for beliefs), then `cases: 19/19   checks: score 80/80`
  and the details of any failure. Exit code 1 on any mismatch, 2 when the data dir already
  holds this eval's beliefs or evidence ids for the owner (run `--cleanup` first).
- `--data-dir` (default: a temp dir) and `--owner` (default `eval-owner`) choose where and
  for whom to seed; an explicit `--data-dir` is kept, `--keep` keeps the temp dir. To look
  at the seeded ledger on the review page, seed the owner you log in as (`--owner ''`
  without auth) into a scratch dir and start Odysseus on it with
  `ODYSSEUS_DATA_DIR=/tmp/belief-eval`.

`--chat` (optional, not scored, the only non-deterministic part) then asks a running
Odysseus "What do you know about Priya Nair?" with Athena's bakeoff client, and
prints whether the answer has each expected fact (peanut allergy, the corrected
birthday, the design review) and its link, and whether it still gives the old birthday.
Dates match in either order and any case ("21 May", "21st of May", "May 21st",
"2026-05-21"); a module link counts when its path (`/view/<id>`) is cited, whatever the
query string, and a note link when its `#…` id is:

```bash
ODYSSEUS_URL=http://127.0.0.1:7010 ODYSSEUS_PASSWORD_FILE=$SBX/admin.txt \
  venv/bin/python scripts/belief_eval.py --chat --owner admin \
  --data-dir <the test instance's data dir> --bakeoff $SOUND/server/eval/bakeoff.py
```

It seeds that instance's `beliefs.db` and `memory.json` (the sandbox instance on
`127.0.0.1:7010`, never production; the server's test instance is retired). Other module evidence of that owner is left alone,
but the instance's own Module Facts pass retires the eval's module facts (ids 9001+), so
ask right after seeding or set `MODULE_FACTS_SYNC=off` on that instance.

### Cleanup

Each run writes a manifest, `belief_eval.<owner>.json` in the data dir: the ids of the
beliefs it created and of the own memory it seeded ("Sam Rivera keeps the spare bike
key …"). A kept dir prints its cleanup command at the end (`--chat` always does):

```bash
venv/bin/python scripts/belief_eval.py --cleanup --data-dir <dir> --owner admin
# cleanup: 16 beliefs, 0 kept, 0 missing, 8 memories, 1 own_memory
```

It deletes exactly those beliefs (with their evidence and events), then runs
`sync_memory` so their memory copies go, then deletes that one own memory, then the
manifest. A recorded belief that another source now backs (evidence that isn't the eval's
module ids 9001–9099 or `belief-eval-note-*` notes) is kept and counted as `kept`. Other
beliefs, other owners and the owner's other memories are never touched. Without a
manifest it exits 2. After a cleanup the same owner can be seeded again.

### At scale (2.5)

`scripts/belief_eval_scale.py` is the same kind of eval at a few thousand entries (plan
stage 2.5): a deterministic fictional corpus (seeded RNG) goes through the ledger's
public API with `now` fixed at 2026-10-21, and the script measures the ledger, the
Belief Structure task and retrieval. No model, no network;
`tests/fork/test_belief_eval_scale.py` runs the small preset through pytest.

```bash
venv/bin/python scripts/belief_eval_scale.py                 # medium, temp dir, removed after
venv/bin/python scripts/belief_eval_scale.py --size small    # 30 subjects, 300 beliefs (seconds)
venv/bin/python scripts/belief_eval_scale.py --size large    # 1000 subjects, 10000 beliefs
venv/bin/python scripts/belief_eval_scale.py --data-dir /tmp/belief-scale --keep --out /tmp/scale.json
```

| Preset | Subjects (people / projects / things, plus the owner) | Beliefs | Queries |
|---|---|---|---|
| `small` | 30 (20 / 4 / 6) | 300 | up to 60 |
| `medium` (default) | 300 (200 / 40 / 60) | 3000 | 200 |
| `large` | 1000 (666 / 134 / 200) | 10000 | 200 |

**The corpus.** One owner (Robin Vale), people with nicknames as aliases (about a quarter
of them; the module sometimes uses the nickname), projects named so they are inferred as
projects ("Project Kestrel", "Wren migration"), and things ("Blue van"). Beliefs come from
23 text templates, one per kind of fact ("<name> lives in <city>", "<name> is allergic
to <x>", "<project> is due on <date>", "The <thing> is kept in <place>" …), each with
its attribute. They arrive as evidence from `odysseus-sound` (fact ids 100001+, with
the module's person ids and a `list_people` sync first) and `notes`
(`scale-note-<n>`), on dates between January and October 2026, and each belief has a
planned fate:

| Fate | Share | How | Ends |
|---|---|---|---|
| source | 42 % | the module confirms it (3–6 times on 2–4 days) | confirmed |
| two sources | 8 % | once in a conversation, once in a note | confirmed (rule) |
| rule | 5 % | two module ids, 3 sightings on 2 days | confirmed (rule) |
| corrected | 3 % | said once, the owner corrects it | confirmed (owner) |
| owner | 2 % | said once, the owner confirms it | confirmed (owner) |
| aged | 5 % | module-confirmed, last heard before April | stale (decay) |
| gone | 3 % | module-confirmed, then no longer listed | stale (reconcile) |
| rejected | 4 % | module-confirmed, the owner forgets it | rejected |
| once | 28 % | one sighting | tentative |

So about 60 % end confirmed. The same `--seed` (default 7) and size give the same corpus;
the run prints its digest (`corpus 95f5dd30b8c21f46` for medium).

**What it does, in order, and what it times.** `sync_people`; submit every module item
in calls of 1000 (`mirror=False`), then the notes; `reconcile_source`, `apply_decay`; a
first `sync_memory` (writes every mirrored belief); the owner's decisions one call each
(`correct`, `reject`, `confirm`: each mirrors memory, as on the review page); the hourly
facts pass with nothing new (the same module items again, `reconcile_source`,
`apply_decay`); `sync_memory` twice (the second must write nothing and leave
`memory.json` byte-identical); `list_beliefs` (all, confirmed, the review page's default
500, one subject), `list_subjects`, `stats`. Then the Belief Structure task
(`belief_structure.run`) over every confirmed belief with a fake model that maps each
template to its attribute and value, and the retrieval questions.

**Retrieval** (stage 2.3, `src/belief_search.py`). A fixed question set generated from
the corpus, each with the belief ids it expects (the confirmed beliefs it asks about):
by subject name ("What do you know about Kaia Lindqvist?", ~25 %), by alias (the
nickname, ~15 %), about the owner, one per attribute the owner has ("Am I allergic to
anything?", "Where do I live?", "What do you know about me?"), and by attribute ("Where
does … live?", "When is Project Kestrel due?", "Where is the Blue van kept?"). Each is
asked with `search_beliefs(owner, q, k=10, status=("confirmed",))`:

- **top-1**: share of questions whose first hit is an expected belief;
- **top-5**: mean over questions of expected beliefs among the first five, out of
  min(5, expected);
- **p50 / p95**: latency per question in ms;
- **leaks**: hits that aren't confirmed beliefs (must be 0).

It runs twice: the **default** lane, then the **keyword** fallback forced (the module's
`embedder()` replaced by one that answers None, "no lane"; the run prints what it
replaced). A script process has no embedding lane loaded, so in the sandbox both lanes
rank without embeddings ("default lane used embeddings: no"); the default lane
uses embeddings only where the hub's memory index is loaded. While `src/belief_search.py`
isn't importable the part is skipped with a message and the run still passes.

**Checks and thresholds.** Exit 0 when all hold, 1 otherwise, 2 when the data dir already
has beliefs for the owner (`--owner` or a fresh `--data-dir`):

- every belief ends in its planned status; one belief per planned item; the expected
  number of subjects, and every project inferred from its name;
- one memory per confirmed belief; a second `sync_memory` writes nothing and leaves
  `memory.json` byte-identical;
- the Belief Structure task structures every confirmed belief, each with the planned
  attribute and value;
- when retrieval runs: top-5 recall of the default lane ≥ `--min-top5` (0.9) and no
  leaks. The keyword lane's numbers are reported, not gated.

**Reading the numbers.** Medium and large on the sandbox CPU (a reference, not a target;
timings vary by machine, counts don't):

| Measure | Medium | Large |
|---|---|---|
| beliefs (confirmed / tentative / stale / rejected) | 3000 (1815 / 830 / 232 / 123) | 10000 (6038 / 2732 / 796 / 434) |
| evidence rows / subjects | 3432 / 301 | 11287 / 1001 |
| `beliefs.db` / `memory.json` | 2.9 MB / 1.3 MB | 9.7 MB / 4.2 MB |
| submit, new evidence, per 1000 (first → last chunk) | ~115 ms, flat (115 → 106) | ~125 ms, flat (126 → 101) |
| submit, unchanged (the hourly pass), per 1000 | ~45 ms, flat | ~50 ms, flat |
| `sync_memory` first / no-op | ~95 / ~85 ms | ~340 / ~215 ms |
| owner action (correct, reject, confirm), mean / p95 | ~45 / ~50 ms | ~135 / ~165 ms |
| `list_beliefs` all / review page (500) / one subject | ~85 / ~20 / ~1 ms | ~240 / ~35 / ~1 ms |
| `list_subjects` | ~5 ms | ~50 ms |
| structure (fake model): as planned, per batch of 5, hourly runs | 1815 / 1815, ~13 ms, 46 | 6038 / 6038, ~13 ms, 151 |
| retrieval (the 2.3 ranker, no embedding lane): top-1, top-5, p50 / p95 | 0.92, 0.97, ~75 / ~110 ms | 0.91, 0.97, ~365 / ~460 ms |

- Per-chunk submit times should stay flat as the ledger grows, and they do: about
  110–130 ms per 1000 new items at medium and large, whatever is already in the ledger
  (the text match is two index searches; see [Storage](#storage)). Chunks that grow
  chunk after chunk mean a lookup per item scales with the ledger size again: check the
  query plans (`tests/fork/test_ledger_perf.py`).
- `list_subjects` counts every subject's beliefs in one grouped query: ~5 ms at medium,
  ~50 ms at large.
- An owner action syncs only its own belief's memory entry (`sync_memory(...,
  belief_ids=...)`); what still grows is reading and writing `memory.json` (~4 MB at
  large).
- "hourly runs to structure all" is how long a backlog of this size takes the real task
  (40 per run).
- Retrieval: the per-kind top-5 (`alias`, `attribute`, `name`, `owner`) shows which
  questions miss; the JSON lists up to 10 misses per lane.

**Results file.** The full numbers (corpus digest and fates, ledger counts, every timing
with the per-chunk times, memory, structure, retrieval per lane with misses, the checks)
go to `--out`, by default `data/eval/belief_eval_scale.<size>.json` (under `data/`,
gitignored).

## Feature summaries

Each section below was a row of the "Changes from upstream" table in
[FORK.md](../../FORK.md) until 2026-10-29, moved here word for word; FORK.md now lists the
features and the hooks into upstream files.

### Athena facts in Odysseus memory, through the belief ledger

**Athena facts in Odysseus memory, through the belief ledger.** A built-in task "Module Facts" (hourly, posts nothing to chat) reads all the module's current facts (`list_facts` `format=json`; plus `confirmed=true` once when rows lack the `confirmed` flag) through its MCP server and submits each as evidence to the belief ledger (`DATA_DIR/beliefs.db`, sqlite, one file for all owners): beliefs are tentative, confirmed (by the module's own flag, ≥ `BELIEF_CONFIRM_SOURCES` sources, or ≥ `BELIEF_CONFIRM_TIMES` sightings on ≥ `BELIEF_CONFIRM_DAYS` days), stale (no source reports it any more, not seen for `BELIEF_STALE_DAYS`, or past `valid_until`) or rejected; the owner's confirm/correct/reject/restore always win. Only confirmed beliefs are mirrored into memory, one memory per belief for its owner (text `<fact> (source: <link>)`, `belief_id`, `source`, `link`, subject, dates; module-sourced ones keep `module_fact_id`), updated in place (memory id and uses kept), removed when no longer confirmed; Phase 0 copies are adopted, not duplicated; idempotent, so a copy Memory Tidy deleted comes back. `propagate_forget` calls the module's `retire_fact` for a forgotten belief (an older module without it is tolerated). Strict memory reads (`load_all_for_update`), the owner's own memories and other owners' entries untouched. `MODULE_FACTS_SYNC=off` stops it; a text-only module is a no-op. Never touches the module's database. [docs/fork/beliefs.md](beliefs.md)

Files: `src/evidence_intake.py`, `src/module_facts.py` (new), hooks in `src/builtin_actions.py` (action registry) and `src/task_scheduler.py` (housekeeping default, silent action); tests `tests/fork/test_evidence_intake.py`, `tests/fork/test_module_facts.py` (new)

Upstreamable: no

### The owner's notes as belief evidence (off by default)

**The owner's notes as belief evidence (off by default).** A built-in task "Notes Evidence" (daily 03:00, posts nothing to chat, queued as model-backed) reads the owner's unarchived plain notes (type `note`; not notes labelled `odysseus-sound` or `handwritten` (their text goes to Athena), calendar reminders or notes under 40 characters), sends each changed note (per-note hash in `DATA_DIR/notes_evidence_<owner>.json`) to the local model once (background, thinking off, the note as data) for up to 8 durable facts as JSON, and submits them to the belief ledger as source `notes`, id `<note id>:<index>`, link `/#open=notes&note=<id>`, never source-confirmed: a fact also said in a conversation confirms by the two-source rule, the rest waits for the owner. Facts a note no longer states, and those of notes no longer read, are marked gone. `EVIDENCE_NOTES=on` enables it. [docs/fork/beliefs.md](beliefs.md#notes-12)

Files: `src/notes_evidence.py` (new), hooks in `src/builtin_actions.py` (action registry) and `src/task_scheduler.py` (housekeeping default; silent and model-backed action); test `tests/fork/test_notes_evidence.py` (new)

Upstreamable: no

### Subjects, contacts, structure and projects in the belief ledger

**Subjects, contacts, structure and projects in the belief ledger.** Every belief belongs to a subject (`subjects` in `DATA_DIR/beliefs.db`: the owner, a person, a project or a thing; existing ledgers are migrated once, losslessly). A module's stable person id (`list_people` `format=json`, read once per Module Facts pass, text or a missing tool tolerated; a fact's `person_id`) maps to one subject, so a fact lands on the same person whatever name the module uses; otherwise names and aliases match case-insensitively. Subjects can be renamed (old name kept as alias), merged, given a kind and linked to an Odysseus contact (`beliefs_for_contact`); memory entries carry `subject_id`, `subject_kind` and `contact_id` (text unchanged). A built-in task "Belief Structure" (hourly at :15, posts nothing to chat, queued as model-backed; `BELIEF_STRUCTURE=off` stops it) gives up to 40 confirmed beliefs per run an `attribute` from a fixed vocabulary, a `value` and `valid_from`, 5 per local-model call (`BELIEF_STRUCTURE_BATCH`; background, thinking off, the beliefs as numbered data with the dates they were said); an item whose value doesn't fit its belief's text is rejected, a date is kept only when the text backs it, and a past end date never expires a belief; a correction re-structures. A subject becomes a `project` when the owner says so, or (if not a person) by its name or a `project_status`/`deadline` belief; `subject_summary` gives one paragraph per subject. [docs/fork/beliefs.md](beliefs.md#subjects-and-contacts-21)

Files: `src/evidence_intake.py`, `src/module_facts.py` (fork files), `src/belief_structure.py` (new), hooks in `src/builtin_actions.py` (action registry) and `src/task_scheduler.py` (housekeeping default; silent and model-backed action); tests `tests/fork/test_subjects.py`, `tests/fork/test_belief_structure.py` (new)

Upstreamable: no

### Upstream's memory tidy leaves sourced memories alone; the ledger merges its own near-duplicates (personal context 4.1)

**Upstream's memory tidy leaves sourced memories alone; the ledger merges its own near-duplicates (personal context 4.1).** Upstream's Memory Tidy task (`consolidate_memory`, run after every 5 new memories, so the belief mirror set it off) and its memory audit (the Tidy button, and the automatic audit after chat extraction) tidied memories by text alone: they could merge a memory mirrored from the belief ledger into an unrelated one, rewrite it without its `(source: …)` link, or delete it, and the next sync put it back with a new id. Both now skip entries with a `belief_id` (or a Phase 0 `module_fact_id` from Athena) and write them back unchanged; the owner's own memories are tidied as before. Instead, the Module Facts and Notes Evidence jobs merge near-duplicate beliefs of their own source only (same subject and category; words differ only in filler or plural/third-person forms; no different negation, number or structured value; never owner-decided beliefs, never across sources): the evidence moves with its source ids and links, the kept belief keeps its memory id, event `merged`. `BELIEF_CONSOLIDATE=off` skips the merge. [docs/fork/beliefs.md](beliefs.md#consolidation-41)

Files: `src/belief_consolidation.py` (new); hooks in `src/builtin_actions.py` (`action_consolidate_memory`) and `services/memory/memory_extractor.py` (`audit_memories`); fork files `src/evidence_intake.py` (event kind `merged`), `src/module_facts.py`, `src/notes_evidence.py`; test `tests/fork/test_belief_consolidation.py` (new)

Upstreamable: no

### A forget that is complete, and an export (personal context 4.2)

**A forget that is complete, and an export (personal context 4.2).** Forget on `/beliefs` now erases for good, after a confirmation and with no restore: Athena's `erase_fact` (MCP 0.10.0) redacts the transcript line that said the fact in every conversation that said it and has its local model rewrite the notes and digests without it (an older module gets `retire_fact`; the facts job retries every hour); the ledger clears the belief's words and keeps only hashed matching keys, so the claim said again joins the erased belief without its words; memory entries with exactly its words and stored Today insights that point at or quote it are cleaned. A belief forgotten before gets an Erase button. **Download** on `/beliefs` returns the owner's ledger (beliefs, evidence with links, events, subjects) and memories as one JSON file. Chats and the owner's notes are not touched (the answer lists the notes). [docs/fork/beliefs.md](beliefs.md#complete-forget-42)

Files: `src/belief_forget.py`, `src/belief_export.py` (new); fork files `src/evidence_intake.py` (`erase`, `erased_key`, `export_rows`, event kind `erased`), `src/module_facts.py` (retry at the end of a pass), `routes/beliefs_routes.py`, `static/beliefs.html`, `static/js/beliefs.js`, `scripts/belief_eval.py` (case `erased`); tests `tests/fork/test_belief_forget.py` (new), `tests/fork/test_beliefs_routes.py`; no upstream file changed

Upstreamable: no

### Move a belief to the person it is about

**Move a belief to the person it is about.** A line someone else said, heard as the owner's voice, filed their fact under the owner, and Edit only changed the words. **Move** on a `/beliefs` card files the belief under another person (picked, or a new name), confirmed by the owner, and by default has Athena give the source line to that person (`get_transcript` finds the line at the link's second, `reassign_line` moves it when the old subject said it; the module re-summarizes). A fact the module then re-attributes stays with the moved belief. [docs/fork/beliefs.md](beliefs.md#move-to-another-person)

Files: `src/belief_move.py` (new); fork files `src/evidence_intake.py` (`move`, event kind `moved`, re-attributed evidence stays in `_attach`), `routes/beliefs_routes.py` (`POST /api/beliefs/{id}/move`), `static/js/beliefs.js`; test `tests/fork/test_belief_move.py` (new); no upstream file changed

Upstreamable: no

### One ranker over the belief ledger

**One ranker over the belief ledger.** `search_beliefs` ranks an owner's beliefs for a query by subject named (name, alias, unique first name; "I/me/my" for the owner), attribute words (a small synonym map: allergic → allergy, lives → lives_in …), similarity through the hub's own local embedding lane (the memory index's FastEmbed or a local configured endpoint; never a remote API; vectors cached in `belief_vectors` in `beliefs.db`, at most 200 embedded per call) and keyword overlap as the fallback; deterministic ties. The Today insight puts the beliefs ranked for the day's candidates first in its memory input (old selection when the ledger has nothing). Chat adds a ≤ 1200-character "What I know (confirmed)" block with links, as untrusted context, through the existing memory-context hook, when a message asks what the hub knows about a known subject or an attribute of one (`wants_beliefs`; never on slash turns or questions about what was said, so module questions keep reaching their tools). `GET /api/beliefs/search?q=&k=&status=` (admin session) serves the review page. [docs/fork/beliefs.md](beliefs.md#retrieval-23)

Files: `src/belief_search.py`, `routes/belief_search_routes.py` (new); `src/today_insight.py` (`select_memory`, `context_block`); hooks in `app.py` (router, mounted before the review page's), `src/chat_processor.py` (the Today hook's context label, one line: "saved insight: today and beliefs"); test `tests/fork/test_belief_search.py` (new), `tests/fork/test_today_insight.py`

Upstreamable: no

### Belief review page

**Belief review page.** `/beliefs` ("What I know about you", sidebar: Wisdom (was Beliefs), under Today) lists the belief ledger by status (Tentative / Confirmed / Stale / Rejected tabs with counts) and subject, each belief with how it was confirmed, first and last seen ("not heard since" when stale) and its sources with their links. Confirm, Edit (correct), Forget and Restore call the ledger's owner actions; Forget rejects the belief here first, then retires the Athena facts behind it with the module's `retire_fact` and reports `{"retired", "failed", "unsupported"}` (a module that is down never blocks the forget; "No fact #N." counts as retired). Sync now runs the facts job for the owner (`POST /api/beliefs/run`, 60 s budget). A By subject view renames, merges, sets the kind of and links subjects to the owner's contacts (`/api/beliefs/subjects…`, `/api/beliefs/contacts`), and opens a subject to its summary and beliefs by attribute; an attribute filter and a search box (ranked search when installed, else a text match). Admin session only, scoped to the owner. [docs/fork/beliefs.md](beliefs.md#review-page-13)

Files: `routes/beliefs_routes.py`, `static/beliefs.html`, `static/js/beliefs.js` (new); hooks in `app.py` (router), `static/index.html` (sidebar link); test `tests/fork/test_beliefs_routes.py` (new)

Upstreamable: no
