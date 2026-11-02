# Fork roadmap and progress

fork-specific status. Upstream's own plans are in [../../ROADMAP.md](../../ROADMAP.md);
don't edit that file.

_Last updated: 2026-11-02_

## Status

One line per item; what each feature does is in [FORK.md](../../FORK.md#fork-features) and
the linked docs. PR numbers are in this repo. The detailed rows (PRs, sandbox scores,
branches) are kept in [status-history.md](status-history.md).

| # | Item | Status | Where |
|---|---|---|---|
| 1 | Trusted read-only MCP servers, trusted reads, web-intent routing | merged (#1) | [mcp-servers.md](features/mcp-servers.md), [tool-routing.md](features/tool-routing.md#web-intent-routing) |
| 2 | Agent and module docs | merged (#2) | `AGENTS.md`, `docs/fork/` |
| 3 | Athena eval ≥ 15/20 | 9/20 | [modules/athena.md](modules/athena.md#eval-and-current-score) |
| 4 | Sandbox stack (whole system on CPU) | working | [sandbox.md](sandbox.md) |
| 5 | HTTP MCP servers keep tool annotations | merged (#5) | [mcp-servers.md](features/mcp-servers.md#http-mcp-servers-keep-tool-annotations) |
| 6 | Production on the fork, deploy and rollback | merged (#8), live since 2026-10-18 | [deploy.md](deploy.md) |
| 7 | `/athena` (was `/sound`) and `/context` slash skills | merged (#9) | [tool-routing.md](features/tool-routing.md#slash-invoked-skills-bring-their-tools) |
| 8 | Several tools in a row | merged (#16) | [tool-routing.md](features/tool-routing.md#several-tools-in-a-row) |
| 9 | Action items and digest on the phone (ntfy) | merged (#17) | [modules/athena.md](modules/athena.md#voice-notes-into-calendar-and-to-dos-action-items-on-the-phone) |
| 10 | Athena page `/modules/sound` | merged (#19) | [modules/athena.md](modules/athena.md#athena-page-conversations-and-who-is-this) |
| 11 | Stdio MCP servers keep working | in `barvis` (#20) | [mcp-servers.md](features/mcp-servers.md#stdio-mcp-servers-keep-working-after-the-request-that-added-them) |
| 12 | One-command deploy with modules | in `barvis` (#21) | [deploy.md](deploy.md#update-production) |
| 13 | Conversations page v2: reader, speaker fixes, Ask in chat | in `barvis` | [modules/athena.md](modules/athena.md#athena-page-conversations-and-who-is-this) |
| 14 | Voice notes into calendar and to-dos | in `barvis` | [modules/athena.md](modules/athena.md#action-items-in-odysseus) |
| 15 | Named to-do lists for voice-note items | in `barvis` | [modules/athena.md](modules/athena.md#action-items-in-odysseus) |
| 16 | Today insight and facts in memory (personal context Phase 0) | in `barvis` | [today-insight.md](today-insight.md), [beliefs.md](beliefs.md#athena-facts-in-odysseus-memory-through-the-belief-ledger) |
| 16 | Notes from voice notes | in `barvis` | [modules/athena.md](modules/athena.md#action-items-in-odysseus) |
| 17 | Records and Today as Odysseus windows | in `barvis` | [windows.md](features/windows.md) |
| 18 | Personal context Phase 1: belief ledger and review page | in `barvis` | [beliefs.md](beliefs.md) |
| 19 | Personal context Phase 2: subjects, structure, projects, ranker | in `barvis` | [beliefs.md](beliefs.md#subjects-and-contacts-21) |
| 20 | Record a call | in `barvis` | [modules/athena.md](modules/athena.md#record-a-call) |
| 21 | Pair a phone | in `barvis`; needs a real phone | [modules/athena.md](modules/athena.md#phone-relay-through-odysseus-paired-by-qr) |
| 22 | Personal context Phase 3: learning from outcomes | in `barvis` | [today-insight.md](today-insight.md#learning-from-outcomes-phase-3) |
| 23 | Personal context 4.1: provenance-aware consolidation | in `barvis` | [beliefs.md](beliefs.md#consolidation-41) |
| 24 | Personal context 4.2: complete forget and export | in `barvis` | [beliefs.md](beliefs.md#complete-forget-42) |
| 25 | Better beliefs (fact kinds, source lines) | in `barvis` | [beliefs.md](beliefs.md#the-athena-facts-job) |
| 26 | Morning news brief | phase 1 built (task, SearXNG, summaries, `/news` page and window; PR #163); phase 3 built (chat block, News in the Today window); phase 2 (RSS, push) planned | [news-brief.md](news-brief.md) |

## Next

- **The brain** ([brain.md](brain.md)): a small fork-owned agent loop that answers chat
  turns as a general personal assistant (web, Athena, workspace), streaming and with a
  fixed tool set, on by default (`FORK_BRAIN=off` turns it off). Built; the
  [SQLite memory plan](memory-sqlite.md) is on hold for it.

- **Cleanup of the fork and Athena** ([cleanup-plan.md](cleanup-plan.md), full plan in
  Athena's `docs/cleanup-plan.md`): safety-net tests, bug fixes, a `fork/` package, one
  Athena client, shared helpers, fewer upstream hooks. Five phases in lanes for subagents
  with an overseer. Planned, not started.

0. **Module tools without a slash command** (PR SDarioosh/odysseus-expanded#24). A plain question
   with a module cue (conversation words, a person the module knows) runs in agent mode
   and is offered the trusted modules' read tools, through the existing slash-skill
   hooks (no upstream lines). Sandbox setup C: 0/4 → 5/5 agent, 4/5 chat
   ([sandbox.md](sandbox.md#plain-module-questions-2026-10-20)). Owner's full run pending.
   "Regular tools" with every tool on still overflows 32k.
1. **Fixed (status item 11): the remaining MCP bug the sandbox found**
   ([sandbox.md](sandbox.md#findings-from-the-first-sandbox-run), finding 2): stdio servers
   added over the API failed on every call. Upstream issues for this and the HTTP
   annotation bug (item 5) are drafted in their PRs, for the owner to file.
2. **Citations to ≥ 15/20.** In the 2026-10-20 sandbox runs every passing answer cited
   its link; links are kept by the module (cite rule at the end of list results, "search
   before saying I don't know" in `INSTRUCTIONS`, SDarioosh/Athena#40). A fork-side
   "Sources:" footer was tried and dropped (it failed a "nothing found" answer). Next:
   the owner's full 20-question run on the server.
3. **Action items, filed** (status items 9, 14). Decided 2026-10-19: no copies, a live
   view only. Changed 2026-10-20: voice-note events and to-dos are filed into the
   Odysseus calendar and to-dos (the module stays the source of truth, status syncs both
   ways); conversation items are suggestions on the Athena page. Next: repeating
   events, merging with the same event from an email.
4. **ntfy notifications** (status item 9). New action items and yesterday's digest go
   to the phone through the ntfy reminder channel.
5. **Conversations view** (status item 10). `/modules/sound` lists conversations, live
   action items (done/dismiss) and "who is this?" cards for unnamed speakers; item 13
   adds the transcript reader, speaker fixes and Ask in chat. Next: the daily digest on
   the page, and follow the user's theme (the page uses the default dark tokens only).
6. **Phone access.** Use Odysseus from the Android phone over Tailscale (Odysseus has a
   PWA). Lock services to Tailscale per the owner's security decision.
7. **Shared search.** One `context` search tool that queries every module with the
   common arguments in [modules.md](modules.md#shared-search-planned) and merges the
   dated, linked hits. Contract first (modules adopt the argument names), then a
   fork-side aggregator.
8. **Upstream:** offer the general fixes from PR #1 and the skill MCP toolsets from
   SDarioosh/odysseus-expanded#9 upstream
   ([upstream-sync.md](upstream-sync.md#sending-fixes-upstream)); weekly syncs.

9. **Handwritten notes** (H1 to H6 built; next: the owner's H5 numbers decide whether
   `INK_AUTO_SEND` is turned on). Write with
   a stylus on a tablet in the PWA (Lenovo Tab Plus), keep the strokes, transcribe with
   the server's vision model, review each page by hand, and send the text to Athena as a
   written note (replaced when the note is edited and sent again). Paper pages can be
   photographed in. Plan, phases and open
   questions:
   [handwritten-notes.md](handwritten-notes.md).

10. **Auto/Ask choice syncs across devices** (planned, not built). The message box's
    Auto/Ask button (SDarioosh/odysseus-expanded#76) keeps each chat's choice in the
    browser and in server memory only, so the same chat opened on another device (Mac,
    phone) starts on the default until clicked, and a server restart forgets it until
    the next message. Next: store the choice with the chat (the session record) so it
    follows the chat everywhere; the button reads it when a chat opens. Needs a small
    hook in upstream's session storage, or a fork table keyed by session id
    (`src/auto_mode.py`).

## Personal context system

The phased plan for taking in the owner's data, confirming beliefs and using them (Today insight first) is in [personal-context.md](personal-context.md).

What to build next to match the commercial personal-context assistants (shared search,
text messages, people pages and briefings, screen, more sources), in order:
[context-growth.md](context-growth.md). Text messages are built in Athena (2026-10-30);
the cross-medium view of each contact is planned in [people-context.md](people-context.md).

Proposed (not decided): move the belief ledger and the Today insight into their own module, `odysseus-context`, so upstream syncs can't break them and a future front end only needs new pages: [context-module.md](context-module.md).

## Future modules (placeholders)

No repos exist yet. Each would follow [modules.md](modules.md). The order of work is in
[context-growth.md](context-growth.md).

- `athena-screen`: screen history from [Screenpipe](https://github.com/screenpipe/screenpipe)
  on the owner's Mac, behind our own MCP server; wanted by the owner, after personal
  context Phase 1. Design and privacy rules:
  [personal-context-prior-art.md](personal-context-prior-art.md#future-module-athena-screen-screenpipe).
- `athena-<name>`: _to be decided by the owner._

## Open questions for the owner

- Which future modules are planned, and in what order?
