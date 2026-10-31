#!/usr/bin/env python3
"""Fork: report how far the fork is behind upstream Odysseus. Report only.

How many commits upstream ``dev`` has that the fork hasn't merged, since when (the merge
base), and which upstream files the fork hooks were changed upstream since then: those are
where the next upstream sync (docs/fork/upstream-sync.md) can conflict. A hooked file is
an upstream file the fork changes that carries a fork marker (``# Fork: ...``, the
convention ``git grep "Fork:"`` finds), or one the "Hooks into upstream files" table of
FORK.md names. Other upstream files the fork changes (tests, docs) are counted apart.

Never fails: any error (no network, no upstream ref, a shallow clone) is reported and the
exit code stays 0, so CI can run it on every pull request without secrets.

Usage::

    python scripts/upstream_drift.py                          # compares HEAD with upstream/dev
    python scripts/upstream_drift.py --fetch                  # fetches upstream dev first
    python scripts/upstream_drift.py --fetch --summary "$GITHUB_STEP_SUMMARY"

Fictional or public data only: it reads git history, nothing else.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

UPSTREAM_URL = "https://github.com/odysseus-dev/odysseus"
UPSTREAM_BRANCH = "dev"
FETCHED_REF = "refs/remotes/upstream-drift/dev"
DEFAULT_REF = "upstream/dev"
# The fork's markers, as tests/fork/test_fork_inventory.py reads them.
MARKER = re.compile(r"\b(?:Fork|Barvis)\b\s*[:(]")
HOOK_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|")
HOOKS_HEADING = "## Hooks into upstream files"
MAX_LISTED = 60


class DriftError(RuntimeError):
    """Something the report can't be made without (said in the report, never raised)."""


@dataclass
class Drift:
    ref: str
    upstream: str
    base: str
    base_date: str
    behind: int
    ahead: int
    hooked: list[str] = field(default_factory=list)
    hooked_touched: dict[str, int] = field(default_factory=dict)
    other_touched: dict[str, int] = field(default_factory=dict)


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                         check=False)
    if out.returncode != 0:
        raise DriftError(f"git {' '.join(args)}: {out.stderr.strip() or out.returncode}")
    return out.stdout


def fetch(repo: Path, url: str, branch: str) -> str:
    """Fetch upstream's branch into a ref of its own (no remote is added or changed)."""
    git(repo, "fetch", "--quiet", "--no-tags", url, f"+refs/heads/{branch}:{FETCHED_REF}")
    return FETCHED_REF


def table_hooks(text: str) -> set[str]:
    """Paths in the first column of FORK.md's "Hooks into upstream files" table."""
    paths, inside = set(), False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.strip() == HOOKS_HEADING
            continue
        m = HOOK_ROW.match(line) if inside else None
        if m and not set(m[1]) <= {"-", " "}:
            paths.add(m[1])
    return paths


def measure(repo: Path, ref: str, upstream: str) -> Drift:
    base = git(repo, "merge-base", ref, upstream).strip()
    behind = int(git(repo, "rev-list", "--count", f"{ref}..{upstream}"))
    ahead = int(git(repo, "rev-list", "--count", f"{upstream}..{ref}"))
    base_date = git(repo, "show", "-s", "--format=%cs", base).strip()
    # upstream files the fork changes: in the base and changed (not added) on the fork side
    changed = [p for p in git(repo, "diff", "--name-only", "--diff-filter=MD", base, ref,
                              "--").splitlines() if p]
    hooked = set()
    for path in changed:
        try:
            text = git(repo, "show", f"{ref}:{path}")
        except DriftError:
            continue  # deleted by the fork
        if MARKER.search(text):
            hooked.add(path)
    try:
        listed = table_hooks(git(repo, "show", f"{ref}:FORK.md"))
    except DriftError:
        listed = set()
    hooked |= {p for p in listed if p in set(changed)}
    touched = [p for p in git(repo, "diff", "--name-only", base, upstream, "--").splitlines()
               if p]

    def commits(path: str) -> int:
        return int(git(repo, "rev-list", "--count", f"{base}..{upstream}", "--", path))

    drift = Drift(ref=ref, upstream=upstream, base=base, base_date=base_date, behind=behind,
                  ahead=ahead, hooked=sorted(hooked))
    for path in sorted(set(touched) & set(changed)):
        (drift.hooked_touched if path in hooked else drift.other_touched)[path] = commits(path)
    return drift


def report(drift: Drift) -> str:
    behind = (f"`{drift.ref}` is **{drift.behind} commits behind** upstream "
              f"(`{drift.upstream}`) and {drift.ahead} ahead. Merge base "
              f"`{drift.base[:10]}` ({drift.base_date}).")
    hooked = (f"Hooked upstream files: {len(drift.hooked)}; changed upstream since the "
              f"merge base: **{len(drift.hooked_touched)}**.")
    lines = ["## Upstream drift", "", behind, "", hooked]
    if drift.hooked_touched:
        lines += ["", "| Hooked file changed upstream | Upstream commits |", "|---|---|"]
        rows = sorted(drift.hooked_touched.items(), key=lambda kv: (-kv[1], kv[0]))
        lines += [f"| `{p}` | {n} |" for p, n in rows[:MAX_LISTED]]
        if len(rows) > MAX_LISTED:
            lines.append(f"| … and {len(rows) - MAX_LISTED} more | |")
    if drift.other_touched:
        names = ", ".join(f"`{p}`" for p in sorted(drift.other_touched)[:MAX_LISTED])
        more = len(drift.other_touched) - MAX_LISTED
        lines += ["", f"Other upstream files the fork changes (tests, docs) that changed "
                      f"upstream: {len(drift.other_touched)}: {names}"
                      + (f" and {more} more." if more > 0 else ".")]
    lines += ["", "Report only; the sync itself: docs/fork/upstream-sync.md."]
    return "\n".join(lines) + "\n"


def run(argv: list[str] | None = None) -> str:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--ref", default="HEAD", help="the fork side (default HEAD)")
    ap.add_argument("--upstream-ref", default=DEFAULT_REF,
                    help=f"upstream side when not fetching (default {DEFAULT_REF})")
    ap.add_argument("--fetch", action="store_true", help="fetch upstream's branch first")
    ap.add_argument("--upstream-url", default=UPSTREAM_URL)
    ap.add_argument("--upstream-branch", default=UPSTREAM_BRANCH)
    ap.add_argument("--summary", help="also append the report to this file "
                                      "(e.g. $GITHUB_STEP_SUMMARY)")
    args = ap.parse_args(argv)
    repo = Path(args.repo)
    try:
        upstream = (fetch(repo, args.upstream_url, args.upstream_branch) if args.fetch
                    else args.upstream_ref)
        text = report(measure(repo, args.ref, upstream))
    except (DriftError, OSError, ValueError) as e:
        text = f"## Upstream drift\n\nNo report: {e}\n"
    print(text, end="")
    if args.summary:
        try:
            with open(args.summary, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            print(f"(summary not written: {e})", file=sys.stderr)
    return text


def main() -> int:
    try:
        run()
    except Exception as e:  # noqa: BLE001 -- a report, never a failed build
        print(f"## Upstream drift\n\nNo report: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
