# Odysseus module contract

Every module that plugs into Odysseus follows this contract. It exists so that a new module
works in Odysseus without code changes to Odysseus, and so that Odysseus' trust rules stay safe.

Registered modules:

| Module | What it provides | Doc |
|---|---|---|
| Athena | recorded conversations: transcripts, summaries, action items, facts, digests | [modules/odysseus-sound.md](modules/odysseus-sound.md) |

## Repo and ownership

- One module = one repo, named `odysseus-<name>` or a name of its own (the first,
  Athena, is `SDarioosh/Athena`, formerly `barvis-sound`; GitHub redirects the old URL).
- The module is a separate program. It owns its storage (database, files, models) and is
  the only thing that writes to it.
- Odysseus never reads or writes the module's storage directly. It uses the module's MCP
  server, or a documented HTTP API for things MCP can't do (for example an HTML view a
  link opens).
- The module never imports Odysseus code. It talks to Odysseus over a protocol,
  so Odysseus' AGPL-3.0 doesn't extend to it. The module picks its own license.
- The module repo has its own `AGENTS.md` with a "This is an Odysseus module" section that
  links here.

## Privacy

- Local only. The module's data path uses no cloud STT, LLM or embedding API and sends no
  telemetry. Models run on the owner's server (see
  [architecture.md](architecture.md#services-and-ports)).
- Network listeners bind to loopback, the Docker bridge or the Tailscale interface, never
  to the public internet.
- Tool results contain private data. Only a local model may read them.

## Interface: MCP server

### Transports

- **stdio** for clients that spawn a process (e.g. Hermes Agent, Odysseus with a stdio
  server).
- **Streamable HTTP** for clients that connect over the network (e.g. Odysseus in Docker).
- One command starts each, documented in the module's README. Athena:
  `convo-server mcp` (stdio) and `convo-server mcp --http`.

### Auth (HTTP)

- Every HTTP request needs a token. HTTP refuses to start without one.
- Accept both `Authorization: Bearer <token>` and `?access_token=<token>` in the URL.
  Odysseus' HTTP MCP client only does OAuth and can't send a static header, so Odysseus
  uses the query form: `http://<host>:<port>/mcp?access_token=<token>`.
- Compare tokens in constant time. Use a token separate from any device/ingest token.
  Reference implementation: `BearerAuth` in Athena's
  `server/convo_server/mcp_server.py`.

### Tool naming and descriptions

- Tool names are `snake_case` verbs: `search_*`, `get_*`, `list_*` for reads; a specific
  verb (`update_*`, `name_*`, `create_*`) for writes. Don't prefix the module name:
  Odysseus already qualifies tools as `mcp__<server_id>__<tool>`.
- Don't reuse the names of Odysseus built-ins the model might confuse them with (tasks,
  calendar, notes, contacts, memory, chat search). If the concept overlaps, say in the
  description what makes this tool different ("the user's recorded conversations").
- The description says when to use the tool, in one or two sentences. Every argument
  has a description. Dates are `YYYY-MM-DD` in the module's configured timezone.
- Keep the tool count small (Athena has 13). Every schema is sent with every
  request.

### Annotations (Odysseus trust depends on them)

Odysseus admits a trusted server's tool without approval **only** if the tool declares
`readOnlyHint: true` and not `destructiveHint: true` (`src/mcp_trust.py`). There is no
name heuristic. So:

- A tool that changes nothing: `readOnlyHint: true`, `openWorldHint: false`.
- A tool that changes anything (status, names, records, files, external systems):
  `readOnlyHint: false`. Set `destructiveHint: true` if it deletes or overwrites data,
  `false` if the change is additive or reversible. Set `idempotentHint` honestly.
- Never mark a write tool read-only to avoid approval prompts. That is a security bug.
- A test in the module asserts the annotation of every tool.

### Results

- Return compact text, not raw JSON dumps.
- Every fact in a result carries a **source link** the user can open on the phone (for
  Athena: a signed transcript link that jumps to the moment).
- Odysseus doesn't pass the MCP server's `instructions` to the model. Put the
  citation rule in the tool results themselves: every non-empty result says to put the
  link next to each fact used. Empty results say so plainly ("No conversations found.").
- Long results are paged with explicit arguments (e.g. `start_seconds`) and say how to
  get more.

### Slash skill

Each module gets one slash command in Odysseus, e.g. `/sound <question>`, plus a share
of `/context <question>` (every trusted module). For that one message, the model is
offered only the module's tools (plus `ask_user`/`update_plan`), so a small model
finds them among Odysseus' ~130 tools. Mechanism: an Odysseus skill whose
`requires_toolsets` is `mcp:<server name>`; see
[deploy.md](deploy.md#features-and-modules) and `src/skill_invocation.py`.

What the module ships:

- **The skill text is the module's MCP `instructions`.** The module owns it, in its
  own repo (Athena: `INSTRUCTIONS` in `server/convo_server/mcp_server.py`). Write
  it so it works as a procedure: which tool for which question, dates as
  `YYYY-MM-DD`, and "put the link next to each fact". Paragraphs or list items become
  the skill's procedure steps.
- **The module doc names the skill and the server**, e.g. "skill `sound`, server
  `convo`". Pick a short, unused name; it must not clash with an Odysseus slash
  command or skill.
- Odysseus' `scripts/modules_setup.py` creates or updates the skill (source `managed`, no
  skill audit) from the Custom preset text, or from `--skill-text <skill>=<file>`. To
  change the text, change the module's instructions and the preset, then rerun setup.
  Never copy Odysseus code into the module for this; it is configuration.
- Changing the skill's name is a contract change for users (it's what they type).
  Treat it like a tool rename (see "Versioning").

### Shared search (planned)

Goal: one `context` search tool that asks every module at once and merges the hits.
Until it exists, `/context` offers every module's tools and the model calls each
search tool. So that a later aggregator can call every module the same way, each
module's main search tool should accept and return the following. This is contract
text only; no module or Odysseus code implements the aggregator yet.

**Arguments** (all optional except `query`; extra module-specific arguments are fine):

| Argument | Type | Meaning |
|---|---|---|
| `query` | string | free-text search; may be empty when `person` or dates are given |
| `since` | string `YYYY-MM-DD` | from this date (inclusive), module timezone |
| `until` | string `YYYY-MM-DD` | to this date (inclusive), module timezone |
| `person` | string | a person's name as the user says it ("Emily"); the module matches its own people/speakers |
| `limit` | integer | maximum hits (default about 10) |

Athena's `search_conversations` already has `query`, `person`, `since`, `until`,
`limit`. `since`/`until` match its existing names; an aggregator maps "from/to" to them.

**Result** (compact text, one hit per block, best first):

```
<title> (<YYYY-MM-DD HH:MM>, <person>, <person>)
<one or two sentences: the relevant part, quoted or summarized>
link: <source link that opens this item at the right place>
```

- Every hit has a date and a link (the module's usual source link, e.g. Athena's
  signed transcript link with `#t=<seconds>`). No hit without a link.
- The result ends with the usual citation rule ("put the link next to each fact").
- No hits: one line saying so ("No conversations found."), so the aggregator can tell
  "nothing" from an error.
- Errors (bad date, module down) come back as a readable result line, not a protocol
  error, so one module failing doesn't fail the whole search.
- The tool is `readOnlyHint: true`.

A module adds a `context_search` tool (or marks its existing search tool) once the
aggregator is designed; until then, matching argument names and the result shape is
enough.

## Registering a module in Odysseus

Do this in the sandbox stack first ([sandbox.md](sandbox.md)); the owner repeats it on
the server's test instance (`127.0.0.1:7010`), then on production (port 7000) per
[deploy.md](deploy.md#features-and-modules).

1. **Start the module's MCP server**, HTTP bound to loopback / the Docker bridge /
   Tailscale. On the server, add its deploy command to `FORK_MODULES`
   ([Deploying a module](#deploying-a-module)) so it stays running. HTTP is the tested path. Stdio works too since the owner-task fix
   ([sandbox.md](sandbox.md#findings-from-the-first-sandbox-run), finding 2), but the
   Docker image would need the module installed inside it.
2. **Add it to Odysseus:** Settings > MCP, or `POST /api/mcp/servers`. Give it a short
   name (Athena: `convo`). HTTP URL form: `http://<host>:<port>/mcp?access_token=<token>`.
3. **Trust its read-only tools:** add the server name to `ODYSSEUS_TRUSTED_READONLY_MCP`
   (comma separated, e.g. `ODYSSEUS_TRUSTED_READONLY_MCP=convo,<next>`) in `.env` or
   `docker-compose.override.yml` (production), then restart. Write tools keep normal
   approval.
4. **Add instructions to the preset:** paste the module's usage instructions (its MCP
   `instructions` text) into a Custom preset system prompt, because Odysseus doesn't
   forward them. The next step also uses this text for the module's slash skill.
5. **Run `scripts/modules_setup.py`** (`--dry-run` first). It checks that the module's
   server is connected, sets "Auto" tool mode leaving every Odysseus tool on, and
   creates the module's slash skill (`--module-skill <skill>=<server>`, e.g.
   `sound=convo`) and the shared `/context` skill. See
   [Slash skill](#slash-skill). `--trim` gives the old memory-only tool set with
   "Regular tools". With every tool on, plain questions in "Auto" don't reach module
   tools; `/sound` and `/context` do
   ([sandbox.md](sandbox.md#slash-skills-2026-10-18)).
6. **Run the module's eval** (see "Eval harness"): a few questions in the sandbox, the
   full run by the owner on the server. Record the server score in the module doc.
7. **Document it:** add `docs/fork/modules/<name>.md` (copy the structure of
   [odysseus-sound.md](modules/odysseus-sound.md)), a row in the table at the top of this
   file, and its services in [architecture.md](architecture.md).

## Deploying a module

So that one `scripts/fork_deploy.sh deploy` updates the whole system
([deploy.md](deploy.md#update-production)), a module ships a deploy command:

- An executable in the module repo (Athena: `server/scripts/deploy.sh`), listed in
  `FORK_MODULES`. Odysseus runs it with `deploy`, and with `status` for
  `fork_deploy.sh status`. Nothing else: Odysseus never reaches into the module's files.
- `deploy` works from any directory, as the owner's user, without sudo and without
  input: it updates the module's checkout to its default branch, installs it, starts or
  restarts its services in the background (so they survive logout and reboot), waits
  until its MCP server answers, and exits non-zero if it doesn't. It rolls itself back
  when the new version doesn't come up.
- Its settings live in the module's own config (Athena: `server/.env`), not in
  Odysseus.
- Odysseus deploys modules first, then itself, so `modules_setup.py` finds their MCP
  servers connected.

## Eval harness

- Each module ships an eval script like Athena's `server/eval/bakeoff.py`:
  - fictional seed data (never real recordings), created by a script in the module;
  - a fixed question list, each with the expected source(s) and answer keywords;
  - a pass rule: the frontend called a module tool, cited a link to an expected source,
    and the answer has an expected keyword;
  - a `--frontend odysseus` mode that talks to an Odysseus instance over its API;
  - results written to a gitignored file.
- Each module states a **target score** in its module doc. Athena: ≥ 15/20 (what
  tuned Hermes Agent reached).
- Rerun the eval after: any change to the module's tool contract, any Odysseus change to
  MCP gating/routing/presets, and every upstream merge.
- Agents run it in the sandbox stack with a small model and a few questions
  ([sandbox.md](sandbox.md#run-the-eval)); those scores only compare with other sandbox
  runs. The target score is measured by the owner on the server, from the PR's Server
  verification section.

## Versioning

The **tool contract** is: tool names, argument names and types, which arguments are
required, annotations, the link format in results, and the auth scheme.

Breaking changes (need a coordinated change):

- renaming or removing a tool or argument, or making an optional argument required;
- changing an annotation (especially `readOnlyHint` or `destructiveHint`);
- changing the link format or what a link opens;
- changing the auth scheme or the MCP URL path.

Non-breaking: adding a tool, adding an optional argument, wording changes in descriptions
or results that keep links and the citation rule.

For a breaking change:

1. Open the module PR first. Its description lists the contract change.
2. Open an Odysseus PR that updates the module doc (and preset text, setup or trust config if
   needed). Link the two PRs to each other.
3. Rerun the module eval with `--frontend odysseus` in the sandbox stack with both
   changes in place, and list the full server run under Server verification. Put the
   scores in both PRs.
4. The owner merges the module PR first, then the Odysseus PR.

Bump the version string the module's MCP server reports (Athena: `version=` in
`build_mcp`; the upload routes came with 0.8.0) on every contract change. Plain HTTP routes a
module adds next to its MCP endpoint (Athena's uploads) are part of the contract
like tools: adding one is non-breaking, renaming or removing one is breaking.

## Checklist: adding a new module

- [ ] Repo `odysseus-<name>` with `AGENTS.md` ("This is an Odysseus module" section linking
      here), `CLAUDE.md` (`@AGENTS.md`), `README.md`, `PROGRESS.md`.
- [ ] Owns its storage; nothing else writes to it.
- [ ] Local-only data path; no cloud APIs, no telemetry.
- [ ] MCP server over stdio and Streamable HTTP; HTTP requires a token via Bearer header
      or `?access_token=`.
- [ ] Tool names and descriptions as in "Interface: MCP server"; no clash with Odysseus built-ins.
- [ ] Correct `readOnlyHint` / `destructiveHint` on every tool, with a test.
- [ ] Every result fact has a source link; non-empty results carry the citation rule.
- [ ] MCP `instructions` written to work as the slash skill's procedure; the module doc
      names the skill (e.g. `sound`) and server.
- [ ] Main search tool follows [Shared search (planned)](#shared-search-planned):
      `query`, `since`, `until`, `person`, `limit`; dated hits with links.
- [ ] Eval script with fictional seed data, a `--frontend odysseus` mode and a target
      score.
- [ ] Registered in the sandbox stack, then on the server's test instance: MCP server added, name in
      `ODYSSEUS_TRUSTED_READONLY_MCP`, preset instructions added, `modules_setup.py` run
      with `--module-skill <skill>=<server>` (slash skill created).
- [ ] Eval run against Odysseus; score recorded.
- [ ] `docs/fork/modules/<name>.md`, the table above, and
      [architecture.md](architecture.md) updated.
