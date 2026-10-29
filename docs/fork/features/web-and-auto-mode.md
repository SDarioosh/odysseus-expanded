# Web lookups and auto mode

Web questions, fetches of search hits, the Auto / Ask button and recovery from failed web reads.

Each section below was a row of the "Changes from upstream" table in
[FORK.md](../../../FORK.md) until 2026-10-29, moved here word for word; FORK.md now lists
the features and the hooks into upstream files.

## Web lookups with the web toggle on

**Web lookups with the web toggle on.** Two gaps made web questions fail. "search mistral 7b vs qwen3.5-9b" (even "search the web for …") was routed to the Hugging Face model search only; a clause that compares models (vs, versus, compare, benchmark) or names the web (web, online, internet, google) now goes to normal web routing. A message whose words name no tool ("mistral 7b vs qwen3.5-9b") got no grants, so the model's `web_search` call was blocked as "outside server request authority"; with the web toggle on, such a turn now gets a `web_search` grant (not fetch or browser). A turn that may search may also `web_fetch` the pages its search returned in this run (a ranked hit, or a clean short page on a hit's site), one exact call at a time; before, "Compare the fanatik nex e1 pro and e1 max" searched fine and every fetch of the product pages was blocked. Links inside fetched pages don't count. Toggle off, policy, per-tool disables and the approval gate still win.

Files: `src/web_turn_scope.py` (new), hooks in `src/turn_contract.py` (model-discovery rule), `routes/chat_routes.py` (after `request_authority_for_http`) and `src/tool_execution.py` (`execute_tool_block`, before the request-authority denial); test `tests/test_web_turn_scope.py` (new)

Upstreamable: yes, with discussion

## Auto mode: safe tools without asking

**Auto mode: safe tools without asking.** Upstream arms an approval gate once outside content (a search result, a fetched page, an email) is in a run; after that `web_fetch` (it can send data out in its URL) and reads of the owner's own stores waited for approval, so "look up this site" asked on its second step. Each chat has an **Auto / Ask** button in the message box, next to web search (`static/js/forkAutoMode.js`; kept per chat in the browser and sent with every message as `fork_tool_mode`, remembered in server memory per user and chat). A chat that never chose follows the default in Settings → Tools, **Auto** unless the admin picks Ask (`GET` for any signed-in user, `PUT` admin, `/api/fork/auto-mode`, stored as `fork_auto_mode_default`; the first version's `fork_auto_mode` is ignored). In Auto the gate lets through tools whose only effects are reads (web, workspace, the owner's notes, calendar, tasks, documents, email) and `web_fetch` of a URL that can't carry data the model chose: one in the owner's message (or another page of that site without a query string; on GitHub only the same owner/repo), or one a tool returned (`www.` and a trailing `/` ignored), on a public host name, or a page without a query string (path ≤ 200 characters) on a site that a web search in this run returned as a ranked hit (on GitHub only the same owner/repo; links inside pages don't add sites). Everything else still asks: email, writes, deletes, terminal and code, file writes, admin changes, unknown tools and made-up URLs. Upstream lets the browser, `pdf_extract` and `youtube_tool` open any address after outside content because no new private data can be loaded then; auto mode can load it, so in auto mode they follow the same known-URL rule (YouTube ids and handles excepted), and after a private read the browser's click, fill and script actions ask. Token-driven runs never get it. Auto also offers `calculate`, an arithmetic-only calculator (parsed expression, no code, no network, size limits), next to the turn's tools, so numbers are computed rather than guessed.

Files: `src/auto_mode.py`, `routes/auto_mode_routes.py`, `static/js/forkAutoMode.js` (new); hooks in `src/tool_capabilities.py` (gate, URL tracking, calculator capability), `src/tool_types.py` and `src/agent_tools/__init__.py` (calculator tool), `routes/chat_routes.py` (the chat's choice, calculator grant and offer), `src/agent_loop.py` (binds the choice to the run), `app.py` (router), `static/index.html` (one script tag); test `tests/test_auto_mode.py` (new)

Upstreamable: no

## A failed approved web read doesn't end the turn

**A failed approved web read doesn't end the turn.** Upstream answers a failed sealed action with "The approved <tool> action could not run: <error>", so a comparison that already had searches and a fetched page in hand returned only a site's HTTP 429. For `web_fetch`, `web_search`, `pdf_extract` and `youtube_tool` failing on the network or the site (not a policy, gate or approval refusal), the failed result goes to the model with a note to answer from the evidence it has or read another source, and the run continues under the normal gates.

Files: `src/web_read_recovery.py` (new); hook in `src/agent_loop.py` (exact-approval continuation); test `tests/test_web_read_recovery.py` (new)

Upstreamable: yes
