# Morning news brief

A short news brief written by Odysseus every morning: for each category the owner sets
(for example "AI", "Formula 1", "Toronto", "Markets"), the day's main stories, each with
a 2–3 sentence summary and links to the articles. It runs as a built-in scheduled task,
like the [Today insight](today-insight.md), at a time the owner picks (default 07:00 in
the owner's timezone).

Status: **phase 1 built** (task, gathering from SearXNG, summaries, the `/news` page and
window with the category editor, delivery time and Refresh; 2026-11-02). Phase 2 (RSS feeds,
the "seen in the last 3 briefs" filter, push) and phase 3 (chat, Today window) are planned.
This page is the work order for those PRs; where phase 1 differs from the first plan, the
text below says what was built and why.

## What the owner gets

- A **News** window (sidebar, icon rail, `/open news`) and a `/news` page: one section
  per category, a one-line overview of the category, then 3–5 stories. Each story has a
  title, a short summary, its sources and the time it was published. Every story links to
  the article; a story reported by several sites lists each one ("Also: Reuters, BBC").
- (Phase 2) An optional phone push (ntfy) when the brief is ready: "News: 5 categories, 19
  stories", the top headline of each category, and a link to `/news`.
- **Settings on the page:** the categories (add, rename, reorder, turn off, delete), per
  category an optional search query, stories per category (and, phase 2, optional RSS
  feeds), and the delivery time. **Refresh** writes a new brief now.
- Later (phase 3), in the Odysseus web app: "what's in the news today?" in chat is
  answered from the stored brief, and the Today window shows the top headline of each
  category with a link to the News window. The brief does not go to the T5 PDA (owner's
  decision, 2026-11-02).

## Where it belongs

The hub (this repo). News is not module data: nothing is captured or stored for a module,
and the work is a scheduled task, a page and a notification in Odysseus. A separate
module (its own repo and MCP server) would add a service, a token and a deploy step for a
feature with no data of its own to own. If news later grows into a store of its own
(saved articles, reading history, search across past briefs), that is the time to move it
into a module.

## Privacy (owner decision: SearXNG is fine)

The brief has to ask the internet for news, so it is not like the Today insight, where
nothing leaves the network. What leaves, and what never does:

| Leaves the network | Never leaves |
|---|---|
| The category queries the owner typed ("Formula 1"), through the self-hosted SearXNG (as every web search in chat does today: no account, no cookies; the search engines see the server's address) | Chat content, memory, beliefs, notes, transcripts, Athena data: none of it is used to build a query |
| Plain `GET`s of the RSS feeds the owner added and of the article pages, to read their text | The summaries: written by the local model on the server |

Rules the code keeps:

- Queries are built **only** from the category's name or its query field, never from
  memory or other data ("personalised news" would leak the owner's interests and is out
  of scope).
- Search goes **only** through SearXNG (`services/search/providers.searxng_search_api`),
  not `comprehensive_web_search`, whose fallback chain can call DuckDuckGo or a paid API
  (Brave, Tavily, Serper) when a key is set.
- Summaries use the hub's local background model only. No cloud model, no fallback.

**Owner's decision (2026-11-02):** sending the category names through SearXNG every
morning is fine. RSS feeds (phase 2) are an extra source, not a replacement.

## How it works

### The task

Built-in task **News Brief** (action `news_brief`), one row in `fork/tasks.py` `TASKS`:
`housekeeping=True` (seeded for every owner), `silent=True` (posts nothing to the
assistant chat), `model_backed=True` (waits for the local model slot like the other
model-backed tasks). Schedule `*/15 * * * *`, like the Today insight.

The **delivery time** (H:MM, default 7:00) is stored in the owner's `config.json` and
checked in code: a pass writes the brief once the owner's local time has reached it and
there is no brief for today yet. The owner's time is the task's crew timezone, else the
owner's assistant's, else the server's (the Today insight's rule). Every other pass is a
no-op that leaves no Activity entry, so a run that failed or was cancelled is tried again
15 minutes later. With `NEWS_BRIEF_ENABLED=off`, or no enabled category, every pass (and
Refresh) is a no-op.

Built differently from the first plan, which wrote the delivery time into the task's cron
(`M/15 H-(H+1) * * *`) through upstream's task route:

- upstream's `TaskScheduler.ensure_defaults` (run at startup and each time the Tasks list
  is opened) puts every housekeeping task's cron back to its default, so an owner's cron
  would not survive;
- a housekeeping task has no crew, and upstream reads the cron of a task without a crew
  in UTC (`compute_next_run` gets no `tz_name`), so `0/15 7-8` would run at 07:00 UTC, not
  07:00 for the owner, and move by an hour at each DST change.

Both would need upstream hooks; checking the time in code needs none. The Tasks list
therefore shows `*/15 * * * *`, and the delivery time is set on the News page only.

### Steps of one run

1. **Gather**, per enabled category, in parallel with a time budget (60 s per category):
   - SearXNG: `searxng_search_api(query, categories="news", time_filter="day")`, about 15
     results. The query is the category's query field, else its name.
   - (Phase 2) RSS/Atom feeds of the category, if any: items published in the last 24 hours (or
     since the previous brief), parsed with the standard library (`xml.etree`; no new
     dependency). Feed URLs go through the same public-address check as page fetches
     (`services/search/content.py` `_public_http_url`), so a feed can't point at the LAN.
   - Each candidate is `{title, url, source, published, snippet}`. Sources are a tuple
     in `fork/news/gather.py` (`SOURCES`, each `source(category, now)`); phase 2 adds the
     RSS source there. A failing source is logged and leaves the others.
2. **Clean up in code** (no model):
   - Drop results without a URL or title, non-http(s) URLs, and results older than the
     lookback (when the date is known).
   - Canonical URL (strip `utm_*`, `fbclid`, fragments, `amp` paths) and dedupe by it.
   - Group near-duplicate titles (token overlap) into one story with several sources.
   - (Phase 2) Drop stories already shown in the last 3 briefs (by canonical URL), so a
     story that trends for days isn't repeated, unless it has a new source
     (`store.recent_briefs` reads them).
   - A story that fits two categories stays in the first one only.
   - Keep at most 12 candidates per category, newest first after grouping.
3. **Read** the top candidates: `fetch_webpage_content(url)` (existing, SSRF-guarded) for
   the first 6 per category, text cut to 1,500 characters. A page that fails or is behind
   a paywall keeps its search snippet.
4. **Summarise**, one local-model call per category (thinking off). Candidates are labelled
   `S1`, `S2`, … (short labels: a small model copies long ids and URLs badly, see the
   Today insight). The model is asked for JSON only:

   ```json
   {"overview": "one sentence about the category today",
    "stories": [{"ref": "S3", "also": ["S7"], "title": "...", "summary": "2-3 sentences"}]}
   ```

   Article text is **untrusted input**: the prompt says so, the call has no tools, and
   the output is used only as text.
5. **Validate in code:** every `ref` must be a candidate label; unknown refs are dropped;
   a label is used once; at most the configured number of stories; title ≤ 120
   characters, summary ≤ 400, overview ≤ 200. **Links and sources come from the
   candidates, never from the model's text**, so a link can't be made up. A bad answer is
   retried once with a corrective message; if that fails, the category is stored with its
   top headlines and snippets and no summaries (still useful, marked "not summarised").
   With no model endpoint configured every category is stored that way. A model call
   that raises (a timeout, a cancelled run) stores nothing; the next pass tries again.
   When no category found any story, nothing is stored either.
6. **Store**, delete briefs older than `NEWS_BRIEF_KEEP_DAYS`, then call each function in
   `fork/news/brief.py` `AFTER_RUN` (`hook(owner, brief, now=now)`; phase 2 adds the push).

### Stored JSON

One file per owner and day: `<DATA_DIR>/news_brief/<owner>/<YYYY-MM-DD>.json` (owner folder
names as in [today-insight.md](today-insight.md#owner-folder-names)). Categories in
`<DATA_DIR>/news_brief/<owner>/config.json`. Example (fictional):

```json
{
 "date": "2026-11-03",
 "generated": "2026-11-03T12:02:41Z",
 "categories": [
  {"name": "Formula 1",
   "overview": "Testing results dominate ahead of the season finale.",
   "summarised": true,
   "stories": [
    {"title": "Team X tops final test day",
     "summary": "Two or three sentences ...",
     "url": "https://example.com/f1/test-day",
     "source": "example.com",
     "published": "2026-11-03T08:10:00Z",
     "also": [{"url": "https://example.org/...", "source": "example.org"}]}
   ]}
 ],
 "errors": [{"category": "Markets", "error": "no results"}]
}
```

Old days stay on disk for 30 days (`NEWS_BRIEF_KEEP_DAYS`), then are deleted by the task.

### Push (phase 2)

After a successful run, once a day, when `NEWS_BRIEF_PUSH=on` (default `off`) and
`MODULE_NOTIFY_CHANNEL` isn't `off`: through the module notify channel
(`fork/athena/action_items_push.push`, Odysseus' reminder dispatcher, ntfy or browser).
Title `News`, body the top headline per category (≤ 600 characters), key
`news-brief:<date>`. Unlike the Today push it doesn't wait for `MODULE_DIGEST_HOUR`: the
task's own time is the delivery time.

### Routes

| Route | Auth | Answer |
|---|---|---|
| `GET /api/news/today` | admin session | today's brief, plus `refreshing` and `refresh_error`; 404 when none |
| `POST /api/news/refresh` | admin session | 202, writes a new brief in the background (a run outlasts the request timeout) |
| `GET`/`PUT /api/news/config` | admin session | categories (`name`, `query`, `enabled`, `stories` or null), `delivery_time` (H:MM); GET also gives `default_stories`, `enabled` (the setting) and `timezone`; 400 with the reason when invalid (empty or repeated name, more than 20 categories, stories outside 1–10, a bad time) |
| `GET /news` | session | the page |

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `NEWS_BRIEF_ENABLED` | `on` | `off`: every run is a no-op |
| `NEWS_BRIEF_PUSH` | `off` | (phase 2) `on`: phone push when the brief is ready |
| `NEWS_BRIEF_STORIES` | `5` | default stories per category (1–10; each category can override it) |
| `NEWS_BRIEF_KEEP_DAYS` | `30` | days of briefs kept |
| delivery time | 7:00 | on the News page, in `config.json` (not the task's cron, see [The task](#the-task)) |

Settings go in the `fork/common/settings.py` table, so `docs/fork/settings.md` is generated.

## Files and upstream hooks

New fork files (no new upstream hook expected; each registration point is an existing
fork table):

| File | What |
|---|---|
| `fork/news/brief.py` | the task: `INFO`, `HOUSEKEEPING_DEFAULT`, `action_news_brief`, `run`, `generate_now` (Refresh), `AFTER_RUN` |
| `fork/news/gather.py` | sources (`SOURCES`: SearXNG; RSS in phase 2), canonical URLs, dedupe, grouping, reading pages |
| `fork/news/prompt.py` | prompt, labels, answer validation, the headlines-only fallback |
| `fork/news/store.py` | config and daily files, pruning, `latest_brief` and `recent_briefs` for phases 2 and 3 |
| `fork/routes/news_routes.py` | routes above; one row in `fork/routes/__init__.py` `ROUTERS` |
| `static/news.html`, `static/js/news.js` | the page and window; entry in `static/js/forkModuleWindows.js` (in Tools after Notes, not in the Athena group), `/open news` and "Open News" in `static/js/forkModuleShortcuts.js`; styles under `nb-` in `static/css/fork-modules.css` |
| `tests/fork/test_news_brief.py`, `tests/fork/test_news_gather.py` | tests on fictional data, fake SearXNG and fake model |

Settings rows are in `fork/common/settings.py` (passed by `docker/fork.override.yml`).
No upstream file was changed in phase 1.

Phase 3 adds the chat block (in the existing `build_context_preface` hook) and a News
section in the Today window (`static/js/today.js`, a fork file). Each lands in [FORK.md](../../FORK.md) when built.

## Phases (one PR each)

1. **Task and page** (built). Config file, gather (SearXNG only), clean-up, summarise, validate,
   store, `/news` page with categories editor and Refresh, delivery time. Tests: clean-up
   and dedupe, label validation, made-up links dropped, garbage answer twice → headlines
   only, empty category, no categories → no-op, the delivery time in the owner's zone
   (`tests/fork/test_news_brief.py`, `tests/fork/test_news_gather.py`).
2. **RSS feeds and push.** Feeds per category, feed URL checks, "seen in the last 3
   briefs" filter, ntfy push.
3. **Chat and the Today window.** "What's in the news?" answered from the stored brief
   (added only on news questions, like the Today block), and a News section in the Today
   window: the top headline per category, each linking to its story in the News window.
   No T5 PDA and no token route (owner's decision, 2026-11-02).

## Measure

- Sandbox: a full run on fictional categories with a fake SearXNG (recorded fixture
  results) and the small CPU model: time per category, share of answers usable on the
  first try, no link that isn't a candidate URL.
- Server verification (owner): one Refresh with the owner's categories on
  `qwen3.5-35b`: time to finish (target: under 5 minutes for 6 categories), stories per
  category, links open the right article, summaries match the articles (spot-check 5).
  Deploy: `scripts/fork_deploy.sh deploy`.

## Known limits and risks

- **SearXNG's news engines** vary by install; some return little. `searxng_search_api`
  already falls back to the general category when the news category is empty. RSS feeds
  (phase 2) are the steady source for a category that searches badly.
- **Model time.** One call per category on the local model; six categories take a few
  minutes, so the brief is ready a few minutes after the delivery time.
- **Cancellation.** Upstream cancels a running model-backed task on a non-passive request
  (see [today-insight.md](today-insight.md#known-limits)). That is why the task runs every
  15 minutes: a cancelled run is tried again at the next pass after the delivery time,
  and passes after a written brief are no-ops.
- **Late server.** A server that was off at the delivery time writes the brief at the
  first pass after it comes back (the same day, any hour).
- **Paywalls.** Paywalled articles keep their search snippet; the summary is thinner.
