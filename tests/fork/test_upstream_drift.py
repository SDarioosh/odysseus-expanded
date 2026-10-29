"""Fork: the upstream drift report (scripts/upstream_drift.py), on throwaway git repos.

An "upstream" repo with a ``dev`` branch and a "fork" cloned from it that hooks two of
its files (one with a ``# Fork:`` marker, one named in FORK.md's hooks table), changes an
upstream test without a marker and adds a file of its own; upstream then moves on. The
report must count the commits behind, list the hooked files upstream changed (with their
commit counts) apart from the other changed files, and never fail.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("upstream_drift",
                                              REPO / "scripts" / "upstream_drift.py")
drift = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = drift  # its dataclass looks its module up there
spec.loader.exec_module(drift)

FORK_MD = """# Fork

## Hooks into upstream files

| Upstream file | Where | Fork file | What | Offered |
|---|---|---|---|---|
| `src/a.py` | `f` | `fork/x.py` | a hook without a marker | no |

## Fork files

| `not/a/hook.py` | outside the hooks table |
"""


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout


def write(root: Path, path: str, text: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text, encoding="utf-8")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    for key, value in (("GIT_AUTHOR_NAME", "Test"), ("GIT_AUTHOR_EMAIL", "t@example.invalid"),
                       ("GIT_COMMITTER_NAME", "Test"),
                       ("GIT_COMMITTER_EMAIL", "t@example.invalid"),
                       ("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig")),
                       ("GIT_CONFIG_NOSYSTEM", "1")):
        monkeypatch.setenv(key, value)
    up, fork = tmp_path / "upstream", tmp_path / "fork"
    up.mkdir()
    sh(up, "init", "-q", "-b", "dev")
    for path, text in (("app.py", "print(1)\n"), ("src/a.py", "A = 1\n"),
                       ("tests/test_x.py", "def test(): pass\n"), ("README.md", "hi\n")):
        write(up, path, text)
    sh(up, "add", "-A")
    sh(up, "commit", "-q", "-m", "upstream base")
    subprocess.run(["git", "clone", "-q", str(up), str(fork)], check=True)
    sh(fork, "checkout", "-q", "-b", "barvis")
    write(fork, "app.py", "print(1)\nimport fork  # Fork: hook (fork/x.py)\n")
    write(fork, "src/a.py", "A = 2\n")
    write(fork, "tests/test_x.py", "def test(): assert True\n")
    write(fork, "fork/x.py", "X = 1  # Fork: a file of the fork's own\n")
    write(fork, "FORK.md", FORK_MD)
    sh(fork, "add", "-A")
    sh(fork, "commit", "-q", "-m", "fork")
    for message, path, text in (("u1", "app.py", "print(2)\n"),
                                ("u2", "app.py", "print(3)\n"),
                                ("u3", "tests/test_x.py", "def test(): pass  # 2\n"),
                                ("u4", "other.py", "new\n"),
                                ("u5", "README.md", "hello\n")):
        write(up, path, text)
        sh(up, "add", "-A")
        sh(up, "commit", "-q", "-m", message)
    return up, fork


def test_reports_commits_behind_and_hooked_files_upstream_changed(repos, tmp_path):
    up, fork = repos
    summary = tmp_path / "summary.md"
    summary.write_text("before\n", encoding="utf-8")
    text = drift.run(["--repo", str(fork), "--fetch", "--upstream-url", str(up),
                      "--upstream-branch", "dev", "--summary", str(summary)])
    assert "**5 commits behind** upstream" in text and "and 1 ahead" in text
    assert "Hooked upstream files: 2; changed upstream since the merge base: **1**." in text
    assert "| `app.py` | 2 |" in text
    assert "`src/a.py`" not in text  # hooked, but upstream didn't touch it
    assert "changed upstream: 1: `tests/test_x.py`." in text
    assert "other.py" not in text and "README.md" not in text and "fork/x.py" not in text
    assert summary.read_text(encoding="utf-8") == "before\n" + text
    # the fetch went into a ref of its own: no remote added, origin untouched
    assert sh(fork, "remote").split() == ["origin"]
    assert sh(fork, "rev-parse", drift.FETCHED_REF).strip() == sh(up, "rev-parse", "dev").strip()


def test_measure_lists_table_hooks_and_marked_files(repos):
    up, fork = repos
    sh(fork, "fetch", "-q", str(up), f"+refs/heads/dev:{drift.FETCHED_REF}")
    d = drift.measure(fork, "HEAD", drift.FETCHED_REF)
    assert (d.behind, d.ahead) == (5, 1)
    assert d.hooked == ["app.py", "src/a.py"]
    assert d.hooked_touched == {"app.py": 2} and d.other_touched == {"tests/test_x.py": 1}
    assert d.base == sh(fork, "merge-base", "HEAD", drift.FETCHED_REF).strip()


def test_table_hooks_reads_only_the_hooks_table():
    assert drift.table_hooks(FORK_MD) == {"src/a.py"}


def test_never_fails(repos, tmp_path, monkeypatch, capsys):
    _, fork = repos
    text = drift.run(["--repo", str(fork), "--fetch", "--upstream-url",
                      str(tmp_path / "missing"), "--upstream-branch", "dev"])
    assert text.startswith("## Upstream drift\n\nNo report: git fetch")
    text = drift.run(["--repo", str(fork), "--upstream-ref", "upstream/nope"])
    assert "No report" in text
    monkeypatch.setattr(sys, "argv", ["upstream_drift.py", "--repo", str(tmp_path / "nowhere"),
                                      "--summary", str(tmp_path / "no" / "dir" / "s.md")])
    assert drift.main() == 0
    assert "No report" in capsys.readouterr().out
