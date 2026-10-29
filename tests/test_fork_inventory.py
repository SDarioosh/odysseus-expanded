"""Inventory of what the fork changes relative to upstream Odysseus.

Two checks, both computed against the upstream base, the merge base of the
working tree's ``HEAD`` with ``upstream/dev`` (or, when that ref is missing, the
ref named by ``FORK_UPSTREAM_REF``). Without either ref the tests skip: a CI
checkout or a fresh clone may not have the upstream remote. ``origin/dev`` is
deliberately not used: it is a mirror that can lag upstream, and diffing against
it would report upstream's own newer code as fork changes.

1. Every hunk the fork adds to an upstream file carries a fork marker
   (``Fork:`` or ``Fork (``, older hooks ``Barvis:``) in a comment inside the hunk
   or within ``MARKER_WINDOW`` lines of it, so ``git grep "Fork:"`` finds every
   hook (AGENTS.md, "Fork rules"). Hooks that are not marked yet are listed in
   ``KNOWN_UNMARKED``; the test fails when a new gap appears and when a listed
   gap is fixed (remove it from the list then).

2. Every file the fork adds is listed in ``docs/fork/files.md``. That file is
   generated: run ``UPDATE_FORK_FILES=1 pytest tests/test_fork_inventory.py`` to
   rewrite it after adding, moving or deleting a fork file.

Both look at the working tree (tracked files, staged or not), so a hook or a
file can be checked before it is committed. Untracked files are ignored.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FILES_DOC = "docs/fork/files.md"
UPSTREAM_REF = "upstream/dev"
UPSTREAM_REF_ENV = "FORK_UPSTREAM_REF"

# A marker counts for a hunk when it is inside the hunk or this many lines
# above or below it.
MARKER_WINDOW = 3
# Changed lines at most this far apart form one hunk (one hook).
HUNK_JOIN = 3
MARKER = re.compile(r"\b(?:Fork|Barvis)\b\s*[:(]")

# Upstream files whose fork changes need no marker:
# - Markdown (docs; the generated website/configuration-reference.md),
# - tests (upstream test files the fork adjusted are not hooks).
UNMARKED_OK_SUFFIXES = (".md",)
UNMARKED_OK_PREFIXES = ("tests/",)

# Hooks in upstream files that have no fork marker yet. Each entry is
# (file, anchor): the anchor is a snippet of the hook's added lines, so the
# entry survives upstream merges that move line numbers. Mark the hook
# (``# Fork: ... (fork file)``) and remove its entry here.
KNOWN_UNMARKED: set[tuple[str, str]] = set()

# Paths never listed as fork files (local state, environments).
NOT_FORK_FILES_PREFIXES = ("data/", "venv/", ".venv/", "node_modules/")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _upstream_base() -> str:
    refs = [UPSTREAM_REF]
    if os.environ.get(UPSTREAM_REF_ENV):
        refs.append(os.environ[UPSTREAM_REF_ENV])
    for ref in refs:
        try:
            _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            return _git("merge-base", "HEAD", ref).strip()
        except (subprocess.CalledProcessError, OSError):
            continue
    pytest.skip(
        f"no upstream ref: '{UPSTREAM_REF}' is missing (git fetch upstream) and "
        f"{UPSTREAM_REF_ENV} is unset or not a commit with a merge base"
    )


@pytest.fixture(scope="module")
def upstream_base() -> str:
    return _upstream_base()


# ---------------------------------------------------------------------------
# Hook markers
# ---------------------------------------------------------------------------

@dataclass
class Hunk:
    path: str
    start: int  # first line in the working-tree file, 1-based
    count: int  # lines of the working-tree file the hunk spans
    added: list[str]


_HUNK_HEADER = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")


def _hunks(base: str, path: str) -> list[Hunk]:
    diff = _git(
        "diff", "--no-renames", "--no-ext-diff", "--no-color", "-U0",
        f"--inter-hunk-context={HUNK_JOIN}", base, "--", path,
    )
    hunks: list[Hunk] = []
    for line in diff.splitlines():
        header = _HUNK_HEADER.match(line)
        if header:
            count = int(header[2]) if header[2] is not None else 1
            hunks.append(Hunk(path, int(header[1]), count, []))
        elif hunks and line.startswith("+") and not line.startswith("+++"):
            hunks[-1].added.append(line[1:])
    # Pure deletions and blank-line-only additions are not hooks.
    return [h for h in hunks if h.count and any(a.strip() for a in h.added)]


_IMPORTED = re.compile(r"\bimport\s+(.+)$")
_ASSIGNED = re.compile(r"^\s*(?:const\s+|let\s+|var\s+)?([A-Za-z_$][\w$]*(?:\s*,\s*[A-Za-z_$][\w$]*)*)\s*=(?!=)")


def _introduced_names(lines: list[str]) -> set[str]:
    names: set[str] = set()
    for line in lines:
        imported = _IMPORTED.search(line)
        if imported:
            for part in imported[1].strip("() ").split(","):
                words = part.split()
                if words:
                    names.add(words[-1])
        assigned = _ASSIGNED.match(line)
        if assigned:
            names.update(n.strip() for n in assigned[1].split(","))
    return {n for n in names if len(n) >= 3 and n.isidentifier()}


def _unmarked_hunks(base: str) -> list[Hunk]:
    changed = _git(
        "diff", "--no-renames", "--name-only", "--diff-filter=M", base,
    ).split()
    gaps: list[Hunk] = []
    for path in changed:
        if path.endswith(UNMARKED_OK_SUFFIXES) or path.startswith(UNMARKED_OK_PREFIXES):
            continue
        try:
            new_lines = (REPO / path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: nothing to mark
        hunks = _hunks(base, path)
        marked, unmarked = [], []
        for hunk in hunks:
            lo = max(0, hunk.start - 1 - MARKER_WINDOW)
            hi = hunk.start - 1 + hunk.count + MARKER_WINDOW
            (marked if any(MARKER.search(line) for line in new_lines[lo:hi]) else unmarked).append(hunk)
        if not unmarked:
            continue
        # A hunk that only uses a name a marked hook of the same file
        # introduced (one upstream never had) continues that hook, e.g. the
        # `and not _managed` conditions after `# Fork: ... _managed = ...`.
        base_text = _git("show", f"{base}:{path}")
        fork_names = {
            n for n in _introduced_names([a for h in marked for a in h.added])
            if not re.search(rf"(?<![\w$]){re.escape(n)}(?![\w$])", base_text)
        }
        for hunk in unmarked:
            text = "\n".join(hunk.added)
            if not any(re.search(rf"(?<![\w$]){re.escape(n)}(?![\w$])", text) for n in fork_names):
                gaps.append(hunk)
    return gaps


def test_upstream_hunks_carry_fork_marker(upstream_base: str) -> None:
    gaps = _unmarked_hunks(upstream_base)
    unexpected = []
    seen: set[tuple[str, str]] = set()
    for hunk in gaps:
        text = "\n".join(hunk.added)
        known = {k for k in KNOWN_UNMARKED if k[0] == hunk.path and k[1] in text}
        if known:
            seen |= known
        else:
            first = next(a for a in hunk.added if a.strip()).strip()
            unexpected.append(f"{hunk.path}:{hunk.start} ({len(hunk.added)} lines): {first[:100]}")
    fixed = sorted(KNOWN_UNMARKED - seen)
    assert not unexpected, (
        "Fork hunks in upstream files without a fork marker. Add a comment such as "
        "'# Fork: <what> (<fork file>)' to the hook and record it in FORK.md:\n  "
        + "\n  ".join(unexpected)
    )
    assert not fixed, (
        "These hooks in KNOWN_UNMARKED are marked now (or gone); remove them from "
        "the list:\n  " + "\n  ".join(f"{p}: {a}" for p, a in fixed)
    )


# ---------------------------------------------------------------------------
# Fork file list
# ---------------------------------------------------------------------------

_HEADER = """\
# Fork files

Generated by `tests/test_fork_inventory.py`; don't edit by hand. Every file the fork
adds to upstream Odysseus (relative to the merge base with `upstream/dev`), grouped by
top-level directory. After adding, moving or deleting a fork file, `git add` it and
regenerate this list:

```bash
UPDATE_FORK_FILES=1 python -m pytest -q tests/test_fork_inventory.py
```

Hooks in upstream files are listed in [FORK.md](../../FORK.md). A file marked "shim"
is an old path of a module that moved into `fork/`; it only aliases the new module.
"""

_LISTED = re.compile(r"^- `([^`]+)`(?: \(shim for `[^`]+`\))?$")
_SHIM = re.compile(r"\A# Shim: moved to (fork/[\w/]+\.py);")


def _shim_target(path: str) -> str | None:
    """The new path a shim at an old path names, else None."""
    if not path.endswith(".py"):
        return None
    try:
        head = (REPO / path).read_text(encoding="utf-8")[:200]
    except (OSError, UnicodeDecodeError):
        return None
    m = _SHIM.match(head)
    return m[1] if m else None


def _fork_files(base: str) -> list[str]:
    added = _git("diff", "--no-renames", "--name-only", "--diff-filter=A", base).split("\n")
    files = {p for p in added if p and not p.startswith(NOT_FORK_FILES_PREFIXES)}
    files.add(FILES_DOC)  # lists itself, also before it is first committed
    return sorted(files)


def _render(files: list[str]) -> str:
    groups: dict[str, list[str]] = {}
    for path in files:
        top = path.split("/", 1)[0] + "/" if "/" in path else "(top level)"
        groups.setdefault(top, []).append(path)
    out = [_HEADER]
    for top in sorted(groups, key=lambda g: (g != "(top level)", g)):
        out.append(f"\n## {top}\n\n")
        for p in groups[top]:
            target = _shim_target(p)
            out.append(f"- `{p}` (shim for `{target}`)\n" if target else f"- `{p}`\n")
    return "".join(out)


def _listed(text: str) -> list[str]:
    return [m[1] for m in (_LISTED.match(line) for line in text.splitlines()) if m]


def test_fork_files_are_listed(upstream_base: str) -> None:
    files = _fork_files(upstream_base)
    doc = REPO / FILES_DOC
    expected = _render(files)
    if os.environ.get("UPDATE_FORK_FILES"):
        doc.write_text(expected, encoding="utf-8")
        return
    current = doc.read_text(encoding="utf-8") if doc.exists() else ""
    listed = _listed(current)
    missing = sorted(set(files) - set(listed))
    stale = sorted(set(listed) - set(files))
    assert not missing and not stale and current == expected, (
        f"{FILES_DOC} is out of date; regenerate it with "
        "UPDATE_FORK_FILES=1 python -m pytest -q tests/test_fork_inventory.py\n"
        f"  fork files not listed ({len(missing)}): {missing[:40]}\n"
        f"  listed but not fork files ({len(stale)}): {stale[:40]}"
    )


# ---------------------------------------------------------------------------
# Helpers of the checks themselves (no git needed)
# ---------------------------------------------------------------------------

def test_marker_pattern() -> None:
    assert MARKER.search("    # Fork: MCP entries (src/skill_mcp_toolsets.py)")
    assert MARKER.search("  ...x, // Fork (static/js/forkModuleShortcuts.js)")
    assert MARKER.search("<!-- Fork: Athena menu (static/js/forkAthenaMenu.js) -->")
    assert MARKER.search("# Barvis: older hook")
    assert not MARKER.search("_fork_on_time = forkModuleWindows")
    assert not MARKER.search("from upstream fork code")


def test_introduced_names() -> None:
    names = _introduced_names([
        "    # Fork: comment",
        "    from src.web_read_recovery import recover_failed_web_read",
        "    approved_result, _fork_recovered = recover(x)",
        "    from src.meeting_auto_join import is_time_critical as _fork_on_time",
        "    if a == b:",
        "  const forkThing = 1;",
    ])
    assert {"recover_failed_web_read", "_fork_recovered", "approved_result",
            "_fork_on_time", "forkThing"} <= names
    assert "is_time_critical" not in names


def test_render_groups_and_parses_back() -> None:
    files = ["AGENTS.md", "src/b.py", "src/a.py", "docs/fork/x.md"]
    text = _render(sorted(files))
    assert _listed(text) == ["AGENTS.md", "docs/fork/x.md", "src/a.py", "src/b.py"]
    assert text.index("## (top level)") < text.index("## docs/") < text.index("## src/")


def test_shims_name_a_fork_file(upstream_base: str) -> None:
    files = set(_fork_files(upstream_base))
    for path in files:
        target = _shim_target(path)
        if target:
            assert target in files, f"{path} is a shim for {target}, which is no fork file"
