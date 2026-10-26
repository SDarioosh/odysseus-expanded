# Today insight

A short daily insight written by Odysseus from the owner's own data: a headline, what to
do first (with a reason each), what is coming up, and other things worth a look. It is
stage 0.3 and 0.4 of [personal-context.md](personal-context.md#phase-0-today-insight-and-confirmed-facts-decided-in-progress).
Everything runs on the owner's network with the local model; no input or answer leaves it.

Code: `src/today_insight.py` (the job, storage, chat context),
`routes/today_insight_routes.py` (routes), `static/today.html` and `static/js/today.js`
(the page). Test: `tests/test_today_insight.py`.

## The job

Built-in task **Today Insight** (action `today_insight`), seeded for every owner like the
other housekeeping tasks, every 15 minutes (`*/15 * * * *`; change it in Tasks). It posts
nothing to the assistant chat and queues for the model like the other model-backed
built-ins. A run that finds nothing changed leaves no Activity entry.

### Inputs, gathered in code

| Input | Where from |
|---|---|
| Calendar event occurrences from today 00:00 local to +7 days, not cancelled, not ended | the owner's calendars: events starting in the window (`_checkin_calendar_events`, as the check-in uses) and every recurring series, expanded with the calendar route's own `_expand_rrule` (`routes/calendar_routes.py`, skipped dates honoured): occurrence uid, summary, start, end, all-day, location, importance, event type |
| Open items of to-do notes | notes of type `checklist`, `todo`, `goal`, not archived; each item with its index, the note's title, pinned flag, due date and label. Calendar-reminder notes are left out (the event is already a candidate) |
| Memory | `MemoryManager(DATA_DIR).load(owner)`: entries of category `preference`, `contact`, `fact`, and every entry with `source: "odysseus-sound"`; at most 40, most used first, then newest. A trailing ` (source: <link>)` (memory copies of module facts carry one) is cut off: it cost ~1,700 prompt tokens and says nothing to the model |
| Athena | when its MCP server (`MODULE_ACTION_ITEMS_SERVER`, default `convo`) is connected: `get_daily_digest {"date": today}` and `list_action_items {"status": "open", "format": "json"}`. Items already filed into to-dos or the calendar by the Module Action Items task are left out (they are candidates already). A failed call or a disconnected module leaves that input out |

The owner's day and time come from the task's crew timezone (`_resolve_task_timezone`),
else the owner's personal assistant's timezone, else the server's.

### Rules applied before the model sees anything

- Each to-do item and event gets a bucket: `overdue` (due date before today), `due_today`,
  `due_soon` (within 7 days), `undated`; a to-do due later than a week is `later`.
  A note's due date counts for each of its items.
- Done items and ended events are dropped; an all-day event counts for its whole day.
- Shopping-type lists are dropped unless the note has a due date: titles in
  `TODAY_INSIGHT_SKIP_LISTS` (default `Shopping,Groceries,Grocery`), any case, with or
  without a trailing "list" ("shopping list", "Grocery List").
- Candidates, in order: every overdue and due-today item (by date and time), then due soon
  by date, then items of pinned notes, then undated, then later; at most 30. Each keeps
  its bucket and has a ref: `todo:<note id>:<item index>` or `event:<uid>`. The event uid
  is the occurrence uid exactly as the calendar route returns it: the plain uid for a
  single event, `<series uid>::<YYYY-MM-DD>` (all-day) or `<series uid>::<YYYY-MM-DDTHH:MM>`
  for an occurrence of a recurring series, so refs match the T5 feed's `calendar` list.
  The "ended" and "+7 days" checks apply to each occurrence, not the series' first date.
- Input hash: SHA-256 over the candidate list, the memory texts (sorted, so a chat that
  only bumps use counts and reorders them changes nothing), the digest text and the open
  action-item lines. The model runs only when there is no insight for today yet or
  the hash differs from the stored one; otherwise the run is a no-op. With no candidates
  at all, a fixed "nothing is due" insight is stored without the model.

### The model's part

The local model (the hub's background-task chain, `llm_call_async_with_fallback`,
thinking off) only picks candidates and writes the reasons. In the prompt each candidate
has a short label instead of its ref: `T1`, `T2`, … for to-dos and `E1`, … for events, in
candidate order (a small model copied the long ids badly: it dropped the `todo:`/`event:`
prefix, the item index or the occurrence date). It is asked for JSON only, with labels as
`ref`:

```json
{"headline": "1-2 sentences",
 "focus": [{"ref": "...", "why": "..."}],
 "coming_up": [{"ref": "...", "why": "..."}],
 "also": [{"ref": "...", "why": "..."}]}
```

The prompt: overdue and due-today first, but items not due today still count; use the
memory facts to personalise the reasons; keep reasons concrete. The answer is validated in
code: each `ref` is mapped back to a candidate ref (a label; an exact ref; as a last
resort a ref that matches exactly one candidate once the prefix is gone, by its start, as
a bare note id or series uid, or by its end; dates and short fragments never match), and
anything else is dropped. A ref is used once across sections, at most 5 per section, `why`
is cut to 80 characters, the headline to 300. An answer that is not usable JSON, or that
names no candidate at all although there are candidates, is retried once with a
corrective message; if that fails too, the previous insight (if any) stays, nothing
all-empty is stored, and the run is logged as failed in Activity.

The scheduled run waits for chat to go quiet before calling the model (as the other
model-backed built-ins do); the page's Refresh doesn't.

## Stored JSON

One file per owner and day: `<DATA_DIR>/today_insight/<owner>/<YYYY-MM-DD>.json`
(`<owner>` with characters other than letters, digits and `-_.@` replaced by `_`).
Example from the test's fictional data:

```json
{
 "date": "2026-10-21",
 "generated": "2026-10-21T07:00:00Z",
 "headline": "The Q4 deck is overdue; send it before the dentist at 12.",
 "focus": [{"ref": "todo:n1:0", "why": "Overdue since yesterday; Tom waits on it", "title": "Send the Q4 deck"}],
 "coming_up": [{"ref": "event:e1", "why": "12:00 today", "title": "Dentist"}],
 "also": [{"ref": "todo:n4:0", "why": "Sunday long run, pack shoes", "title": "Long run Sunday"}],
 "input_hash": "0944546b50b23f83c93ddba8c7ad63e8491be207d65fb6b29698fc5ebe6e6226",
 "votes": [{"vote": "up", "at": "2026-10-21T07:00:00Z"}]
}
```

`title` is the to-do item's text or the event's summary; readers that don't need it ignore
it. `votes` are 👍/👎 from the page, kept when the insight is rewritten later that day
(Phase 3 learns from them). Old days stay on disk.

## Routes

| Route | Auth | Answer |
|---|---|---|
| `GET /api/codex/insight/today` | API token with scope **`insight:read`** (or a browser session) | today's JSON without `input_hash` and `votes`; 404 `{"error": "no insight yet"}`; 403 without the scope |
| `GET /api/codex/insight/weights` | API token with scope **`insight:read`** | the T5 weight table (see [Weights](#weights)); 200 with `"weights": {}` when none; 403 without the scope |
| `GET /api/insight/today` | admin session (as `/modules/sound`) | the same plus `votes`, `refreshing` (a Refresh is running) and `refresh_error` (why the last one wrote nothing); 404 `{"error", "refreshing", "refresh_error"}` when none |
| `POST /api/insight/today/vote` | admin session | body `{"vote": "up" \| "down"}`; answers `{"ok": true, "votes": [...]}` |
| `POST /api/insight/refresh` | admin session | starts writing it now in the background, even when the inputs didn't change, and answers 202 `{"status": "started" \| "running", "generated": <current insight's generated or null>}` at once (a model call outlasts the 45 s request timeout) |
| `GET /today` | session | the page |

`insight:read` is in `ALLOWED_SCOPES` and in Admin → Tokens. Athena's phone and PC
relays fetch it with the Odysseus token for the T5 feed (stage 0.5); give that token
`insight:read` next to `todos:read`.

## The Today window

**Today** (sidebar, icon rail, `/open today`, an optional shortcut) opens a tool window in
the app; `/today` shows the same view as its own page. It shows the headline, Do first,
Coming up and Also worth a look with each item's title and reason, in the style of the
built-in windows (`static/css/fork-modules.css`, shared with Conversations).

- To-do items have a tick box that uses the notes page's own route
  (`POST /api/notes/<id>/items/<index>/toggle`) and shows the tick at once. The page reads
  the note first and finds the line by text if lines have moved; a line that is gone
  shows "No longer on this list." The title opens the note (`/#open=notes&note=<id>`).
- Events link to the calendar on their day, opened by `static/js/forkEntityHash.js`:
  `/#event-<uid>` for a single event (highlighted), `/#event-<YYYY-MM-DD>` for an
  occurrence of a recurring series (the calendar would resolve its compound uid to the
  series' first day).
- 👍/👎 store a vote and show the latest one. **Refresh** starts a new insight and shows
  "Writing…" while it polls `GET /api/insight/today` every 4 s until `generated` changes
  (then shows it) or `refreshing` turns false (then shows `refresh_error`).

## In chat

"What should I do first today?" is answered from the stored insight, without a new tool:
while memory is on, `src/chat_processor.py` (`build_context_preface`) adds a short block
from `today_insight.context_block(owner, message=message)` next to the saved memories, as
untrusted context, **only when the message is about today's plan**
(`today_insight.wants_insight`): it names today, tonight, this morning/afternoon/evening,
first, priority/priorities, plan, focus, agenda or schedule, or says "what should I do",
"what's next", "what now", "what to do", "where do I start", or is a short "what do I do
(now)?". Slash commands and slash-invoked skills (`/sound`, `/context`) never get it.

Why: with the block on every turn, the sandbox model answered an action-item question
("what did the doctor say…") with "no information in the provided context" and called no
module tool (4 of 4 runs); without the block it called `list_action_items` and passed.

The block's first line says what it is for:

```
Today's plan, for questions about today, priorities or what to do first. For anything else use your tools as usual. (2026-10-21)
<headline>
Do first: 1. <title>: <why> 2. …
Coming up: 1. …
Also: 1. …
```

At most 800 characters. Nothing is added when there is no insight for today (or memory is
off, as in incognito).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `TODAY_INSIGHT_ENABLED` | `on` | `off` makes every run a no-op (the stored insight stays readable) |
| `TODAY_INSIGHT_SKIP_LISTS` | `Shopping,Groceries,Grocery` | list titles left out unless dated |
| `MODULE_ACTION_ITEMS_SERVER` | `convo` | the Athena MCP server |
| `TODAY_INSIGHT_PUSH` | `off` | `on` pushes today's insight to the phone once a day ([Push](#push)) |
| interval | every 15 min | the task's schedule in Tasks |

## Known limits

- Upstream cancels a running model-backed task on any non-passive request ("Stopped by
  user" in Activity), and `/api/codex/*` is not on its passive list. A relay fetching
  `GET /api/codex/insight/today` while the scheduled run is generating cancels that
  generation; the next 15-minute run does it again. Upstream is left unchanged for this.
  A Refresh started from the page is not a scheduled task and isn't cancelled that way.

## Measure

Tests on fictional data (`tests/test_today_insight.py`): buckets, shopping lists by note
type and due date, candidate order and cap, ref validation, the hash no-op, a fake model
(good JSON, garbage once, garbage twice, labels, bare ids, empty sections twice), the
memory-order-independent hash, the Refresh route's 202 and the no-wait flag, storage, votes, `context_block`, the token
route's scope, and the real database loaders. The insight on the owner's data and its
time on the server's model are part of the Server verification (stage 0.7).

## Learning from outcomes (Phase 3)

Stages 3.1–3.5 of [personal-context.md](personal-context.md#phase-3-learning-from-outcomes).
Everything rides the Today Insight task above: no new scheduled task and no new upstream
hook.

### Rule order

The candidates the model sees (at most 30 to-dos and events) are sorted in code by
`(tier, -weight, date, time, input order)` (`order_candidates` in `src/today_insight.py`).
This order is published: the T5 PDA's rule-based fallback (no insight for today) follows
the same rules ([Athena `docs/t5-pda-plan.md`](https://github.com/SDarioosh/barvis-sound/blob/main/docs/t5-pda-plan.md) §5).

| tier | items | within the tier |
|---|---|---|
| 0 | overdue and due today (to-dos and events) | higher weight first, then date, time, input order |
| 1 | due in the next 7 days | higher weight first, then date, time, input order |
| 2 | items of pinned notes (undated or due later than a week) | higher weight first, then input order |
| 3 | undated | higher weight first, then input order |
| 4 | due later than a week | higher weight first, then date, time, input order |

Where the T5 differs by its own layout: its DO FIRST lists the to-dos of tier 0 first and
then today's events in time order (with or without an insight), so there a due-today event
never sits between weighted to-dos; it shows no tier 4; and it caps each section at 5.
`scripts/insight_eval.py` (case `t5_day`) checks that both agree on everything else.

- The tier comes from the bucket only. A weight (an integer in [-6, 6], below) never
  moves an item to another tier, so overdue and due-today items always come first and a
  weight never pushes them out of the 30-candidate cap.
- `date` is `YYYY-MM-DD`, `time` `HH:MM` (empty for to-dos and all-day events, which sort
  first on their day); both are compared as text. Tiers 2 and 3 ignore them.
- Input order: to-dos (notes pinned first, then most recently updated; items in list
  order), then events in the calendar's order.
- Refs the owner marked "not today" on `/today` today are left out.
- Weights only change the order the model sees; code doesn't re-sort the model's picks,
  except for the owner's own "↑ first" and "not today".

### Outcome log

`src/insight_outcomes.py` records what became of each item the insight showed. One file
per owner and day, next to the insight: `<DATA_DIR>/today_insight/<owner>/<day>.outcome.json`.
Ids and counts only, no titles or text (the insight file has those):

```json
{"version": 1, "date": "2026-10-21", "final": false, "finalized": null,
 "pushed": null, "push_tries": 0, "votes": {"up": 1, "down": 0},
 "items": {
  "todo:n1:0": {"kind": "todo", "section": "focus", "position": 1,
                "first_shown": "2026-10-21T07:00:00Z", "bucket": "overdue",
                "date": "2026-10-20", "note_id": "n1", "note_type": "checklist",
                "outcome": "done", "at": "2026-10-21T09:00:00Z", "via": "observed"},
  "event:e1": {"kind": "event", "section": "coming_up", "position": 1,
               "first_shown": "2026-10-21T07:00:00Z", "bucket": "due_today",
               "date": "2026-10-21", "event_type": "health", "importance": "normal",
               "outcome": "open", "at": null, "via": null}}}
```

- **Shown**: every ref in any section of the day's insight. The first section, position,
  time and the candidate's bucket and date are kept when the insight is rewritten.
  `kind` is `todo`, `event`, `loop` (`loop:<action item id>`) or `plan` (`belief:<id>`).
- `votes` counts the 👍/👎 entries of the insight. `pushed` and `push_tries` belong to
  the push.

Each 15-minute pass (`observe`, also on a no-op) updates the outcomes from what it just
gathered (`via: "observed"`). An item the page changed (`via: "today"`) at or after the
time the inputs were gathered (`gathered_at`, before the model call) is left alone: those
inputs are older than the owner's tick.

| outcome | set when |
|---|---|
| `open` | shown, not done yet. A `done` that is un-ticked the same day goes back to `open`. |
| `done` | a to-do's line is ticked (on any surface: notes, chat, T5, phone), or an open loop's action item is among Athena's done items. Events and plans are never done. |
| `moved` | a to-do's note got a later due date, or an event now starts on a later day than when it was shown. |
| `gone` | the note or line was deleted, the event was cancelled or isn't in the calendar window any more, or the loop's action item is in neither the open nor the done items. |
| `dismissed`, `promoted` | the owner's "Not today" and "↑ first" (`via: "today"`). The pass never overwrites them, except `promoted` → `done`. Marking a `done` item changes nothing (the route answers 200 with the item as it is). An open loop dismissed with **Not a task** also has `not_task: true` (cleared by Undo), so the log shows which extractions were wrong. |
| `ignored` | still `open` when the day was finalized (`via: "final"`). |

To-do lines are matched by the ref's index when the line's text is the shown title, else
re-found by title, as the page does; texts are compared on their first 200 characters
(whitespace collapsed, case ignored), where candidate titles are cut. Titles come from the
insight rows and are kept in memory only; after a restart a line the insight no longer
shows is matched by index. Notes
the pass didn't load (an archived, finished checklist) are looked up by id. The pass reads the open action items
(`list_action_items {"status": "open"}`); only when an open loop the log still has open
(or today's insight shows) is missing from them does it also read
`list_action_items {"status": "done", "format": "json"}`, once (`action_rows_done`). Open
loops are left as they are when the module is unreachable. A passed event stays `open` and becomes
`ignored` at the end of the day (weights don't count that for events).

**One record per line.** A to-do ref (`todo:<note>:<index>`) is an index, so after the
owner adds, removes or moves lines the same ref can name another line. When an insight
row's title for a ref differs from the title its record was shown with, the ref names a
new line: the old record moves to a spare key `<ref>#<n>` (for example `todo:n1:0#1`)
and keeps following its own line by title, and the ref gets a fresh record. When the old
line is shown again under another ref, its record moves to that ref instead of a second
record being started (also when a ref the insight no longer shows still holds it). So
each line is counted once by the weights and the trends, and a tick on the page
(`record`, which applies the same rule first) lands on the line the page showed. Spare
keys are never shown and can't be marked; after a restart (titles lost) a spare key is
left as it is until the day is finalized.

**Finalize**: the first pass after local midnight finalizes every earlier day of the last
28 days (the weights' window) that isn't final (`final: true`, `finalized`); a final day
doesn't change any more. Finalizing copies the vote counts from the insight once more, so
votes cast after the day's last pass count. Older days are left alone, and a day known final isn't read
again by the same process. A finalized day's titles leave memory.

**Owner order** (`apply_owner_order`): promoted rows come first in Do first (moved there from
any section, the latest promotion first), dismissed rows are left out. The page, the token
route and the chat block use it; a promoted or dismissed item that a rewrite of the insight
left out keeps its place. Dismissed refs are also left out of the next candidates.

| Route | Auth | Answer |
|---|---|---|
| `POST /api/insight/today/outcome` | admin session | body `{"ref", "outcome": "done" \| "open" \| "dismissed" \| "promoted" \| "clear"}` (`open` un-ticks a `done`, `clear` undoes a mark); `{"ok": true, "item": {...}}`; 404 without an insight today or when the ref wasn't shown; 400 for `done`/`open` on an event or a plan |
| `GET /api/insight/today` | admin session | in the owner's order, plus `outcomes` (`{ref: outcome}`) and `dismissed` (the hidden rows, each with its `section` and `not_task`) |
| `GET /api/codex/insight/today` | `insight:read` | in the owner's order, without outcomes |

On the page each row has **↑ first** (again: undo), **Not today** and **Why first?** (the
why route, rendered by `todayWeights.js`). A tick also posts `done` (un-tick: `open`).
Hidden rows sit under "Not today (n)" with **Undo**. Open loops show "with <person>" and
open their conversation; plans link to `/beliefs`.

Open loops also have **Not a task**, for an action item that was never a to-do (a quick
errand the summary model picked up, like "I'll move my truck"). The page sets the item to
`dismissed` in Athena (`POST /api/modules/sound/action-items/{id}`, the module's
`update_action_item`), so it isn't an open loop again on later days, then posts
`dismissed` with `not_task: true` to hide it today. Its row under "Not today" says "Not a
task"; Undo reopens the module item and clears the mark. The outcome route answers 400 for
`not_task` on anything but a dismissed open loop.

### Weights

Code: `src/insight_weights.py`, routes `routes/insight_weights_routes.py`, UI
`static/js/todayWeights.js`. Test: `tests/test_insight_weights.py`.

**Keys.** A weight belongs to a key; a candidate's weight is the sum of the weights of
its keys, clamped to [-6, 6]. Each key's weight is in [-3, 3].

| key | applies to | shown as |
|---|---|---|
| `note_type:<checklist\|todo\|goal>` | to-do items of that note type | "Checklist notes", "To-do notes", "Goal notes" |
| `list:<note id>` | to-do items of that note | the note's title, resolved live ("a list that no longer exists" once it is gone) |
| `event_type:<work\|personal\|health\|travel\|meal\|social\|admin\|other>` | events | "Work events" … |
| `importance:<low\|normal\|high\|critical>` | events | "High-importance events" … |

An event without an event type has no `event_type:` key. Open loops and plans (`loop:`,
`belief:`) have no keys (weight 0).

**Learning.** Per key, from the outcome files ([Outcome log](#outcome-log)) of the last
`WINDOW_DAYS = 28` days before today (today − 28 … yesterday) that are **final**; days
not finalized yet don't count, and neither does today. After a reset only days after the
reset day count for that key. Each shown item of a day counts once; `gone` counts for
nothing and isn't shown (a stray `open` on a final day counts as `ignored`).

- to-do keys: `score = done + 2*promoted - 2*dismissed - moved - ignored/2`
- event keys: `score = 2*promoted - 2*dismissed - moved` (`done` and `ignored`: a passed
  event is no signal)
- `rate = score / shown`; `base` = the same rate over all to-do items (to-do keys) or
  all events (event keys) of the window, resets not applied; 0 when there are none.
- `weight = clamp(round_half_away(3 * (rate - base)), -3, 3)` when `shown >= MIN_SHOWN`
  (6), else 0. Computed with exact fractions; halves round away from zero (2.5 → 3,
  −2.5 → −3).

Example: the Work list was shown 9 times (7 done, 1 moved up, 1 ignored): score 8.5,
rate 0.944; with a to-do base of 0.222 its weight is round(3 × 0.722) = round(2.17) = 2.

**When.** `current(owner, today)` recomputes when `weights.json`'s `date` isn't today, so
at most once a day: at the first Today Insight run after local midnight, after letting
the outcome log finalize earlier days (its `finalize_pending`). The weights stay the
same during the day; the insight's input hash covers them. A missing or broken file is
recomputed.

**File** `<DATA_DIR>/today_insight/<owner>/weights.json` (written atomically):

```json
{"version": 1, "date": "2026-10-22", "computed": "2026-10-22T00:00:05Z",
 "window_days": 28, "min_shown": 6,
 "base": {"todo": 0.2222, "event": 0.0},
 "keys": {"list:n1": {"weight": 2, "shown": 9, "done": 7, "promoted": 1, "dismissed": 0,
                      "moved": 0, "ignored": 1, "reset_at": null}},
 "resets": {"list:n2": "2026-10-20"}}
```

`keys` has every key seen in the window plus every key with a reset; `base` is rounded to
4 places for reading (the weights use the exact value). A reset older than the window is
dropped from `resets` (every window day is after it anyway).

**Reset.** `reset(owner, key)` (or every key: the stored keys, the reset ones and every
key in the outcome files of the window, final or not) records today as the key's reset
day and recomputes at once: the key's weight is 0 from now on, outcomes up to and
including today no longer count for it, and it learns again from tomorrow's outcomes.

**Routes.**

| Route | Auth | Answer |
|---|---|---|
| `GET /api/insight/weights` | admin session | `{"date", "min_shown", "window_days", "rows": [{"key", "label", "weight", "shown", "done", "promoted", "dismissed", "moved", "ignored", "reset_at"}]}`, largest \|weight\| first |
| `POST /api/insight/weights/reset` | admin session | body `{"key": "list:n1"}`, or `{}` for every key; answers `{"ok": true, …}` with the rows as above; 400 for a malformed key |
| `GET /api/insight/today/why?ref=<ref>` | admin session | `{"ref", "title", "section", "position", "tier", "rule_position", "weight", "contributions", "picked_by", "text"}`, rebuilt in code (no model call); 404 when there is no insight today or the ref is neither in it nor a candidate |
| `GET /api/codex/insight/weights` | API token with scope `insight:read` | the T5 table below; 200 with `"weights": {}` when there are none (never 404) |

For the why answer: `position` is 1-based in the section the page shows it in (owner
marks applied), `tier` one of "overdue or due today", "due in the next 7 days", "on a
pinned note", "undated", "due later than a week"; `rule_position` is 1-based in today's
rule order (null for open loops, plans and items no longer among the candidates);
`contributions` lists every key of the item with its weight, counts, `reset_at` and
`label`; `picked_by` is `owner` when today's outcome file marks the ref `promoted`, else
`model`. `text` example: "Overdue since Oct 20; 1st in the rule order; your Work list
counts +2 (you finished 7 of 9, moved up 1). The model put it first."

On `/today`, **How Today ranks** (collapsed) shows the rows with Reset per row and Reset
all; with no rows it says "No weights yet: Today learns from what you tick, move up or
skip."

**The T5 table.** The T5 tells to-dos apart only by note id (all its to-dos are
checklists), so `t5_table` merges, per to-do note of the owner (not archived, types
`checklist`/`todo`/`goal`), the note's `list:<id>` weight and its `note_type:<type>`
weight into one number:

```json
{"version": 1, "date": "2026-10-22", "weights": {"list:n1": 2, "list:n7": -1}}
```

- `weights["list:<note id>"] = clamp(list weight + note_type weight, -6, 6)`, integers.
- Only non-zero entries, at most 32: largest |weight| first, ties by key (text order).
- The relays put it into the feed as the top-level key `weights`; the T5 applies it only
  in its rule-based fallback, to to-dos, by note id, within the tiers above.

### Push

`src/insight_push.py`, called after every pass (`maybe_push(owner, insight, now=)`).
With `TODAY_INSIGHT_PUSH=on` (default `off`) today's insight goes to the phone once a day
through the module notify channel (`module_action_items.push`: Odysseus' reminder
dispatcher on `MODULE_NOTIFY_CHANNEL`, ntfy or browser; nothing else). It is sent when
all hold:

- `MODULE_NOTIFY_CHANNEL` isn't `off`;
- the insight is today's (the owner's local date) and not the fixed "Nothing is due …"
  one;
- the local hour is at least `MODULE_DIGEST_HOUR` (`off`: never);
- today's outcome file has no `pushed` stamp, and fewer than 3 sends were tried today
  (`push_tries`).

Title `Today`; body the headline, then `First: <title>` of the first focus row after the
owner's own order (`apply_owner_order`), when there is one; key `today-insight:<date>`.
The day is marked pushed only when the send succeeded, so a failed send is tried again on
the next pass, at most 3 times a day. The insight is rewritten during the day; only the
first one past the hour is pushed.

### Routines

`src/insight_routines.py`, from the calendar only. The owner's single events (not
recurring, not all-day, not cancelled) that started in the 6 weeks before today are
grouped by normalized summary (lower case, letters and digits, single spaces: "Gym!" and
"gym" are one) and weekday. A group is a routine when its starts within 60 minutes of the
group's median start fall in at least 3 distinct weeks. Recurring series are skipped:
they already say they repeat. `detect(events, tz, today)` is the pure rule.

Computed at most once a day and cached in
`<DATA_DIR>/today_insight/<owner>/routines.json` (`date`, `routines`, `submitted`).
`summary(owner, today)` gives at most 400 characters of whole lines, most weeks first,
e.g. `- Gym: Tuesdays around 18:00 (4 of the last 6 weeks)`, which joins the prompt as
`ROUTINES (habits seen in the calendar):`.

Each routine goes to the [belief ledger](beliefs.md) once, when first found: source
`routines`, id `<normalized summary>:<weekday>` (0 = Monday), about the owner, text
`Usually has Gym on Tuesdays around 18:00`, link `/#event-<date of its last occurrence>`,
one sighting on one day, never source-confirmed. The ids it submitted are kept in
`submitted`, so a routine found again every day is not submitted again, and one sighting
never meets the repetition rule: the belief stays tentative until the owner confirms it
on `/beliefs` (then the Belief Structure task gives it the attribute `routine`).

### Plans and open loops

`src/insight_inputs.py`: `extra_candidates(owner, today, action_rows)` adds up to 10
candidates after the 30 rule-ordered ones (each part fails soft):

- **Open loops** (≤ 5): open `list_action_items` JSON rows that are the owner's (`mine`
  true, or no `mine` and an owner word such as "Me" as `owner`), are for someone in
  `with` (module MCP ≥ 0.9.0; a row without `with` is never a loop) and aren't filed
  into Odysseus yet (the Module Action Items state's `filed`). `with` lists every other
  person in the conversation, so a row is for someone only when one of its `with`
  people's full or first name appears as a whole word (case ignored) in its `text` or
  `title`: "Lend Tom the tent" is a loop for Tom, "Buy milk" said while talking with Ana
  is not. A leading title (Dr, Mr, Mrs, Ms, Mx, Prof, Sir, Dame) is never a first name:
  "Dr. Patel" matches "Dr. Patel" or "Patel", not another "Dr. …". By due date (undated
  last), then newest. Ref `loop:<action item id>`, `person` the first name of the first
  `with` person so named (the full name after a title: "Dr. Patel"), `link` the row's
  transcript link, bucket from `due_date` as for to-dos. These rows leave the "OPEN
  ACTION ITEMS (context only)" lines; rows that aren't loops stay in them.
- **Plans** (≤ 5): the owner's beliefs with attribute `plans` or `decision`, status
  tentative or confirmed, first seen in the last 7 days, not about another person
  (`about` = person); newest first. Ref `belief:<belief id>`, bucket `undated`. The
  attribute is set by the Belief Structure task, so in practice these are beliefs that
  were confirmed once.
  Only sentences that say a plan or decision count (`PLAN_WORDS`: "plans to", "decided
  to", "wants to", "will" …): "believes a separate server is needed" or "has been
  thinking of selling the truck" is left out. The row's title is a short to-do phrase
  (`plan_title`): the structured value ("Visit Alice in Lisbon"; not when the owner
  corrected the text), else the sentence from after its plan verb ("The owner plans to
  slow down …" -> "Slow down …"), at most 80 characters cut at a word. The whole
  sentence rides along as `text`.

The prompt lists them under `OPEN LOOPS (things the person said they would do for
someone; labels L…)` and `PLANS THIS WEEK (labels P…)`, each block only when it has
rows; the system prompt says loops fit `coming_up` and plans fit `also`. `resolve_ref`
accepts `L1`/`P1` and the exact refs; `validate` keeps `person` and `link` on loop rows.
`validate` empties a `why` that only restates its row (`repeats_title`: ≥ 70 % of its
words are in the title or a plan's `text`, a shortened word counting), and the system
prompt asks for an empty `why` rather than a restated title.
Refs the owner marked "not today" are left out like the others.

### Trends and the insight eval

Code: `src/insight_trends.py`, route `routes/insight_trends_routes.py`, UI
`static/js/todayTrends.js`. Tests: `tests/test_insight_trends.py`,
`tests/test_insight_eval.py`.

**Route.** `GET /api/insight/trends?weeks=8` (admin session; `weeks` 1–26, default 8,
else 422) counts, per ISO week (Monday to Sunday), the week of today (the owner's local
date) first:

```json
{"weeks": [{"week": "2026-W43", "start": "2026-10-19", "insights": 4, "votes_up": 2,
            "votes_down": 0, "shown": 31, "done": 12, "promoted": 2, "dismissed": 3,
            "moved": 1, "ignored": 9}]}
```

- `insights`: days of the week with an insight file.
- `shown`: items of the days' outcome files ([Outcome log](#outcome-log)) that aren't
  `gone`, as the weights count them; `done` … `ignored`: items with that outcome. An item
  still `open` (today) counts as shown only.
- `votes_up`/`votes_down`: the outcome file's `votes`, else (a day without an outcome
  file) the 👍/👎 entries of the insight itself.
- Counts only, no titles. A week without data is in the list with zeros, so the panel
  shows the gap.

**Panel.** On `/today`, **Last weeks** (collapsed, under How Today ranks) loads on first
open and shows one row per week: shown, done, ↑ first, not today, moved, ignored, 👍/👎,
and a small bar of done out of shown. Weeks with nothing shown are greyed; with no data
at all it says "Nothing to show yet: each week fills in as Today shows you items."

**The insight eval.** `scripts/insight_eval.py` checks the rule order and the weights on
fictional days, in a temp `DATA_DIR`, with every `now` given: no model, no network.

```bash
venv/bin/python scripts/insight_eval.py          # expected vs got per check, score N/N
venv/bin/python scripts/insight_eval.py --json   # the same as JSON
venv/bin/python scripts/insight_eval.py --data-dir /tmp/insight-eval --keep  # keep the files
```

Exit code 1 on any mismatch, 2 when `--data-dir` already holds a run. Cases:

| case | checks |
|---|---|
| `rule_order` | without weights: overdue (oldest first) and due today, due soon, pinned, undated, later; an undated shopping list and done items left out; the cap keeps the head of the order |
| `weights` | a 28-day history written day by day through the outcome log (`observe`, `record`, finalize; yesterday is finalized by the first weights read) gives the hand-computed weights: one list always done (+3), one always dismissed (−3), a mixed one (+1), a mostly ignored one (−1); the to-do base; stable during the day |
| `tiers` | with those weights the order changes within tiers only: the dismissed list's overdue item still comes before the favoured list's due-soon item; a cap keeps every overdue and due-today item |
| `min_shown` | a list shown 5 times keeps weight 0; shown 6 times it moves |
| `t5_table` | `t5_table` for the fixture's notes, and it agrees with the candidates' weights |
| `t5_day` | the T5 PDA host test's own day through the hub's `order_candidates` (below) |
| `owner_marks` | "not today" leaves the ref out of today's candidates; "↑ first" puts it first in Do first (`apply_owner_order`); today's marks don't change today's weights |
| `reset` | after `reset` the key is 0 (also on the next read) and its tier is back in rule order; after reset all the order is the rule order and the T5 table is empty |

Fixtures: `scripts/fixtures/insight_eval_day.json` (notes, events, the history pattern
and every expected value, with the weights worked out by hand in `_weights_by_hand`) and
`scripts/fixtures/t5_weights_day.json`.

**Shared with the T5.** `scripts/fixtures/t5_weights_day.json` is a verbatim copy of
Athena's `firmware/pda/test/fixtures/insight_eval_day.json`, the day its PDA host
test uses for the rule-based fallback: a feed with `todos`, `calendar`, `weights` and the
T5's expected plan, `expected` (with the weights) and `expected_without_weights`. The
`t5_day` case turns the feed's notes into hub candidates (`todo_candidates`,
`event_candidates` with the feed's date and time zone), builds a hub weights dict whose
`candidate_weight` is exactly the table's number per note (a number beyond ±3 is split
into `list:` + `note_type:`, as `t5_table` merges them; `t5_table` of that dict gives the
feed's `weights` back), and checks per T5 section (DO FIRST = tier 0, COMING UP = tier 1,
ALSO = pinned then undated) that the hub's to-dos come in the T5's order, with and
without weights, and that each event is in the same section. Not compared, because the
two differ by design: the hub's tier 4 (unpinned, due later than a week: last for the
hub, not on the T5's Today), the hub's cap of 30 against the T5's 5 per section, and
where an event stands among the to-dos of its section. Change the two copies together.
