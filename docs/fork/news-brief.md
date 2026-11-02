# Morning news brief (planned)

A short news brief written by Odysseus every morning: for each category the owner sets
(for example "AI", "Formula 1", "Toronto", "Markets"), the day's main stories, each with
a 2–3 sentence summary and links to the articles. It runs as a built-in scheduled task,
like the [Today insight](today-insight.md), at a time the owner picks (default 07:00 in
the owner's timezone).

Status: **scoped, not built.** This page is the work order for the PRs that build it.

## What the owner gets

- A **News** window (sidebar, icon rail, `/open news`) and a `/news` page: one section
  per category, a one-line overview of the category, then 3–5 stories. Each story has a
  title, a short summary, its sources and the time it was published. Every story links to
  the article; a story reported by several sites lists each one ("Also: Reuters, BBC").
- An optional phone push (ntfy) when the brief is ready: "News: 5 categories, 19
  stories", the top headline of each category, and a link to `/news`.
- **Settings on the page:** the categories (add, rename, reorder, turn off, delete), per
  category an optional search query and optional RSS feeds, stories per category, and the
  delivery time. **Refresh** writes a new brief now.
- Later (phase 3): "what's in the news today?" in chat is answered from the stored brief,
  and the brief can go to the T5 PDA through the phone relay.

## Where it belongs

The hub (this repo). News is not module data: nothing is captured or stored for a module,
and the work is a scheduled task, a page and a notification in Odysseus. A separate
module (its own repo and MCP server) would add a service, a token and a deploy step for a
feature with no data of its own to own. If news later grows into a store of its own
(saved articles, reading history, search across past briefs), that is the time to move it
into a module.

## Privacy (owner decision needed)

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

**Open question for the owner:** is sending the category names through SearXNG every
morning acceptable (it is what an owner-typed web search does today)? If not, the brief
can run on RSS feeds only (no queries at all; the feed sites only see that the server
reads their feed), at the cost of the owner picking feeds per category.

## How it works

### The task

Built-in task **News Brief** (action `news_brief`), one row in `fork/tasks.py` `TASKS`:
`housekeeping=True` (seeded for every owner), `silent=True` (posts nothing to the
assistant chat), `model_backed=True` (waits for the local model slot like the other
model-backed tasks). Default schedule `*/15 7-8 * * *` (07:00, then every 15 minutes
until 08:45), in the task's timezone (`_resolve_task_timezone`: the task's crew, else the
owner's assistant, else the server). The 07:00 run writes the brief; the later passes are
no-ops once today's brief exists and only catch a run that failed or was cancelled.

The delivery time on the page (hour and minute, H:MM) writes the task's cron
(`M/15 H-(H+1) * * *`) through upstream's task route, so
the time shown on `/news` and in Tasks is always the same setting. With
`NEWS_BRIEF_ENABLED=off`, or no categories, a run is a no-op.

### Steps of one run

1. **Gather**, per enabled category, in parallel with a time budget (60 s per category):
   - SearXNG: `searxng_search_api(query, categories="news", time_filter="day")`, about 15
     results. The query is the category's query field, else its name.
   - RSS/Atom feeds of the category, if any: items published in the last 24 hours (or
     since the previous brief), parsed with the standard library (`xml.etree`; no new
     dependency). Feed URLs go through the same public-address check as page fetches
     (`services/search/content.py` `_public_http_url`), so a feed can't point at the LAN.
   - Each candidate is `{title, url, source, published, snippet}`.
2. **Clean up in code** (no model):
   - Drop results without a URL or title, non-http(s) URLs, and results older than the
     lookback (when the date is known).
   - Canonical URL (strip `utm_*`, `fbclid`, fragments, `amp` paths) and dedupe by it.
   - Group near-duplicate titles (token overlap) into one story with several sources.
   - Drop stories already shown in the last 3 briefs (by canonical URL), so a story that
     trends for days isn't repeated, unless it has a new source.
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
6. **Store** and, if on, **push** once (below).

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

### Push

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
| `GET`/`PUT /api/news/config` | admin session | categories, stories per category, delivery time (writes the task's cron) |
| `GET /api/codex/news/today` | token scope `news:read` (phase 3) | the brief, for the phone relay and the T5 |
| `GET /news` | session | the page |

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `NEWS_BRIEF_ENABLED` | `on` | `off`: every run is a no-op |
| `NEWS_BRIEF_PUSH` | `off` | `on`: phone push when the brief is ready |
| `NEWS_BRIEF_STORIES` | `5` | default stories per category (1–10; each category can override it) |
| `NEWS_BRIEF_KEEP_DAYS` | `30` | days of briefs kept |
| delivery time | 07:00 | the task's schedule (page or Tasks) |

Settings go in the `fork/common/settings.py` table, so `docs/fork/settings.md` is generated.

## Files and upstream hooks

New fork files (no new upstream hook expected; each registration point is an existing
fork table):

| File | What |
|---|---|
| `fork/news/brief.py` | the task: `INFO`, `HOUSEKEEPING_DEFAULT`, `action_news_brief`, the run |
| `fork/news/gather.py` | SearXNG and RSS gathering, canonical URLs, dedupe, grouping |
| `fork/news/prompt.py` | prompt, labels, answer validation |
| `fork/news/store.py` | config and daily files, pruning |
| `fork/routes/news_routes.py` | routes above; one row in `fork/routes/__init__.py` `ROUTERS` |
| `static/news.html`, `static/js/news.js` | the page and window; entry in `static/js/forkModuleWindows.js`, `/open news` in `static/js/forkModuleShortcuts.js` |
| `tests/fork/test_news_brief.py`, `tests/fork/test_news_gather.py` | tests on fictional data, fake SearXNG and fake model |

Phase 3 adds the `news:read` scope (in the existing scope hooks) and the chat block (in
the existing `build_context_preface` hook). Each lands in [FORK.md](../../FORK.md) when built.

## Phases (one PR each)

1. **Task and page.** Config file, gather (SearXNG only), clean-up, summarise, validate,
   store, `/news` page with categories editor and Refresh, delivery time. Tests: clean-up
   and dedupe, label validation, made-up links dropped, garbage answer twice → headlines
   only, empty category, no categories → no-op, timezone of the cron.
2. **RSS feeds and push.** Feeds per category, feed URL checks, "seen in the last 3
   briefs" filter, ntfy push.
3. **Chat and devices.** "What's in the news?" answered from the stored brief (asked only
   on news questions, like the Today block), `GET /api/codex/news/today` for the phone
   relay, T5 screen (Athena repo, its own PR).

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
  (see [today-insight.md](today-insight.md#known-limits)). That is why the schedule
  repeats every 15 minutes for two hours after the delivery time: a cancelled run is
  tried again at the next pass, and passes after a written brief are no-ops.
- **Paywalls.** Paywalled articles keep their search snippet; the summary is thinner.
