# Upstream sync

Upstream Odysseus moves fast: ~340 commits in 30 days, ~478k lines of Python, no releases.
The fork merges upstream `dev` into `barvis` **weekly** so each merge stays small.

## Branches

- `dev`: mirrors `upstream/dev`. Only fast-forwarded from upstream; never committed to.
- `barvis`: upstream plus the fork's patches. Default branch; all PRs go here.
- Upstream remote: `https://github.com/odysseus-dev/odysseus.git`.

Agents never push to `dev` or `barvis` directly; the sync is a PR like any other.

## History before the first sync

The fork was imported on 2026-10-19 as snapshots of upstream `dev` at `9712a54`
(2026-10-16), without upstream's history, so `barvis` and `upstream/dev` had no common
ancestor and `git merge upstream/dev` refused to run. The first sync PR (2026-10-23)
recorded that base once, keeping the fork's files unchanged:

```bash
git merge -s ours --allow-unrelated-histories 9712a54
```

Since then `git merge-base origin/barvis upstream/dev` finds the last synced upstream
commit and the weekly merge below works as is. Don't repeat that step.

## Weekly merge

```bash
git remote add upstream https://github.com/odysseus-dev/odysseus.git   # once
git fetch upstream origin
git checkout -b chore/upstream-sync-<YYYY-MM-DD> origin/barvis
git merge --no-ff upstream/dev
# resolve conflicts (below), run the checks (below), then:
git push -u origin chore/upstream-sync-<YYYY-MM-DD>
```

Open a PR into `barvis` titled `chore: merge upstream dev (<YYYY-MM-DD>)`. The description
lists the upstream range (`git log --oneline origin/barvis..upstream/dev | wc -l` and the
first/last commit), each conflict and how it was resolved, the test results, and the eval
score. The owner fast-forwards `dev` to `upstream/dev`.

Merge, don't rebase: `barvis` is shared history.

## Conflict hot spots

- `src/agent_loop.py`: hook in `_web_search_unavailable_for_turn`; four
  `skill_mcp_toolsets` hooks where `requires_toolsets` is used (active skills, the
  skill-aware tool include, the skill index gate, the `manage_skills view` unlock).
- `routes/chat_routes.py`: `pin_skill_contract` right after `resolve_turn_contract` in
  `chat_stream`. If upstream moves contract resolution, the hook goes after it.
- `services/memory/skills.py`: managed-skill check in `index_for`.
- `routes/skills_routes.py` (no hook): `tests/test_skill_mcp_toolsets.py` fails if the
  invoke message format changes; update `INVOKE_PREFIX` in `src/skill_invocation.py`.
- `src/agent_runtime/`: hooks in `authority.py` (`create_request_authority`) and
  `effect_adapters.py` (`classify`). Upstream is refactoring the agent runtime, so these
  move most.
- `src/tool_capabilities.py`: hook in `ToolRunSecurityContext`.
- `src/mcp_manager.py`: one line in `_connect_http` that keeps tool `annotations`.
- `src/task_scheduler.py`: the `register_housekeeping` hooks after `HOUSEKEEPING_DEFAULTS`
  and the built-in task authority hook in `_execute_action`.
- `static/index.html`: the fork's sidebar links (Conversations, Today, and the other
  module pages).

Before merging, list the files both sides changed since the last sync; those are where
conflicts can happen:

```bash
base=$(git merge-base origin/barvis upstream/dev)
comm -12 <(git diff --name-only "$base" origin/barvis | sort) \
         <(git diff --name-only "$base" upstream/dev | sort)
```

Find every hook with `git grep -n "mcp_trust"` (and `git grep -n "# Fork:"` for newer
hooks; before 2026-10-19 they were tagged `# Barvis:`). The full list is the "Changes from
upstream" table in [../../FORK.md](../../FORK.md).

When resolving:

- Take upstream's version of the file, then re-apply the fork's hook at the matching spot.
  Don't keep old upstream code just because the hook sat next to it.
- If the function a hook lived in is gone or renamed, find where upstream moved that
  logic and put the hook there. If there is no clear place, stop and escalate
  (`ESCALATE: <reason>`); don't redesign the hook inside a merge PR.
- Never fix unrelated upstream code in the merge PR.

## After a merge

1. The fork's tests and the upstream tests around the hooks:

   ```bash
   python -m pytest -q tests/test_mcp_trust.py tests/test_request_authority.py tests/test_tool_policy.py \
     tests/test_skill_mcp_toolsets.py tests/test_modules_setup.py
   python -m py_compile app.py routes/*.py src/*.py
   ```

2. Broader tests for what upstream changed in the hooked areas. Compare failures against
   a clean checkout of the previous `barvis` and the new `upstream/dev`; only new
   failures caused by the fork matter. 23 tests already fail upstream
   (`tests/test_null_owner_gates.py`, `tests/test_runtime_evidence_contract.py`).
3. Bring up the sandbox stack ([sandbox.md](sandbox.md)) on the merged code and re-run
   `scripts/modules_setup.py` against it. Check that module servers still connect.
4. Run each module's eval in the sandbox (Athena:
   `server/eval/bakeoff.py --frontend odysseus --only 1 4 16`) and compare with the last
   sandbox run. Put both numbers in the PR.
5. Add a Server verification section: restart the server's test instance, re-run
   `modules_setup.py` there, and run the full eval. The owner runs it; the server score
   must not drop.

Production (port 7000) is updated only by the owner, after the sync PR is merged:
`scripts/fork_deploy.sh deploy` ([deploy.md](deploy.md)).

## Sending fixes upstream

Offer a change upstream when it is a general fix or feature, not fork-specific. Current
candidates (marked "Upstreamable: yes" in [../../FORK.md](../../FORK.md)): trusted
read-only MCP servers, trusted reads not counted as external effects, the
web-intent routing fix, and HTTP MCP servers keeping tool annotations (the smallest one;
a good first issue to file).

- Upstream asks LLM agents to **open an issue first** instead of a PR
  (`CONTRIBUTING.md`). So: an agent drafts the issue text (problem, repro, proposed fix,
  link to the fork's commit) and gives it to the owner; the owner files it and decides
  on a PR.
- Upstream PRs target `dev`, follow upstream's PR template and Conventional Commits, and
  contain only the general change: no fork names, docs or config.
- Once upstream merges an equivalent change, the next sync drops the fork's version:
  remove the hook and the fork's file in the merge PR, and update [../../FORK.md](../../FORK.md).
