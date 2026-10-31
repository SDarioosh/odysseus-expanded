"""Fork: every setting the fork's code reads reaches the production container.

Production runs Docker Compose with ``docker/fork.override.yml`` (docs/fork/deploy.md),
and the base ``docker-compose.yml`` has no ``env_file``: a variable reaches the
container only when a compose file lists it under ``environment``. This test finds the
environment variables the fork's Python files read and checks that the override lists
each one, so a new setting can't silently stay host-only.

The fork's code reads its settings through the table in ``fork/common/settings.py``; the
override must pass exactly the table's variables (current and former names), each the
way its row says. Fork files other than the table may not read the environment
themselves; the host-side scripts may (their names are found by scanning).

Found reads: ``os.environ.get(X)``, ``os.getenv(X)`` and ``os.environ[X]``, where X is a
string, a module-level string constant, or a parameter of a small helper such as
``_env(name)`` (``MODULE_<name>`` / ``BARVIS_<name>``) whose callers pass the name.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
OVERRIDE = REPO / "docker" / "fork.override.yml"
FILES_DOC = REPO / "docs" / "fork" / "files.md"
SETTINGS_FILE = "fork/common/settings.py"  # reads the names of its table, not scanned

# Read in fork code but deliberately not passed by the override.
NOT_PASSED = {
    # scripts/belief_eval.py --chat and scripts/brain_eval.py: the eval's login user, run
    # on the host.
    "ODYSSEUS_USER",
    # scripts/brain_eval.py, run on the host against an Odysseus instance: where it is
    # and how it logs in, the chat mode, preset and approval answer it sends, and the
    # (fake) SearXNG it points the instance at.
    "ODYSSEUS_URL", "ODYSSEUS_API_TOKEN", "ODYSSEUS_PASSWORD", "ODYSSEUS_PASSWORD_FILE",
    "ODYSSEUS_MODE", "ODYSSEUS_PRESET", "ODYSSEUS_APPROVAL", "BRAIN_EVAL_SEARXNG",
}


def _table():
    from fork.common import settings

    return settings.SETTINGS


def _fork_python_files() -> list[Path]:
    listed = re.findall(r"^- `([^`]+)`$", FILES_DOC.read_text(encoding="utf-8"), re.MULTILINE)
    return [REPO / p for p in listed
            if p.endswith(".py") and not p.startswith("tests/") and p != SETTINGS_FILE
            and (REPO / p).exists()]


def _is_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _env_key(node: ast.AST) -> ast.AST | None:
    """The key expression of an environment read, else None."""
    if isinstance(node, ast.Call) and node.args:
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "get" and _is_environ(f.value):
            return node.args[0]
        if (isinstance(f, ast.Attribute) and f.attr == "getenv"
                and isinstance(f.value, ast.Name) and f.value.id == "os"):
            return node.args[0]
    if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
            and _is_environ(node.value)):
        return node.slice
    return None


def _template(key: ast.AST, params: set[str]) -> str | None:
    """``{}`` + affixes when ``key`` is built from a function parameter."""
    if isinstance(key, ast.Name) and key.id in params:
        return "{}"
    if isinstance(key, ast.JoinedStr):
        out = []
        for part in key.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value).replace("{", "{{").replace("}", "}}"))
            elif (isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name)
                  and part.value.id in params):
                out.append("{}")
            else:
                return None
        return "".join(out)
    return None


def fork_env_reads(files: list[Path]) -> tuple[set[str], list[str]]:
    """(variable names read, reads that could not be resolved)."""
    trees = {p: ast.parse(p.read_text(encoding="utf-8"), str(p)) for p in files}
    names: set[str] = set()
    unresolved: list[str] = []
    helpers: dict[str, list[str]] = {}  # helper function name -> key templates
    for path, tree in trees.items():
        consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
                  and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
                  for t in n.targets if isinstance(t, ast.Name)}
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        owner = {}
        for fn in funcs:
            for sub in ast.walk(fn):
                owner.setdefault(id(sub), fn)
        for node in ast.walk(tree):
            key = _env_key(node)
            if key is None:
                continue
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
                continue
            if isinstance(key, ast.Name) and key.id in consts:
                names.add(consts[key.id])
                continue
            fn = owner.get(id(node))
            params = {a.arg for a in fn.args.args} if fn else set()
            template = _template(key, params)
            if fn is not None and template is not None:
                helpers.setdefault(fn.name, []).append(template)
                continue
            unresolved.append(f"{path.relative_to(REPO)}:{node.lineno}")
    for tree in trees.values():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in helpers and node.args):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.update(t.format(arg.value) for t in helpers[node.func.id])
    return names, unresolved


def _override_env() -> dict[str, str | None]:
    data = yaml.safe_load(OVERRIDE.read_text(encoding="utf-8"))
    env: dict[str, str | None] = {}
    for item in data["services"]["odysseus"]["environment"]:
        name, sep, value = item.partition("=")
        env[name] = value if sep else None
    return env


def test_every_setting_the_fork_reads_is_passed_to_the_container():
    names, unresolved = fork_env_reads(_fork_python_files())
    assert not unresolved, (
        "environment reads this test can't resolve to a name; use a string or a "
        f"module constant: {unresolved}")
    assert {"ODYSSEUS_TRUSTED_READONLY_MCP", "ODYSSEUS_USER"} <= names  # the scan works
    in_fork = sorted(str(p.relative_to(REPO)) for p in _fork_python_files()
                     if str(p.relative_to(REPO)).startswith("fork/")
                     and any(fork_env_reads([p])))
    assert not in_fork, (
        f"read settings through {SETTINGS_FILE} (add a row there), not os.environ: {in_fork}")
    names |= {n for row in _table().values() for n in row.names}
    passed = set(_override_env())
    missing = sorted(names - passed - NOT_PASSED)
    assert not missing, (
        f"read in fork code but not passed by {OVERRIDE.relative_to(REPO)}: {missing}. "
        "Add '- NAME=${NAME:-}' if an empty value means the same as unset, else "
        "'- NAME', or list it in NOT_PASSED with the reason.")
    stale = sorted(NOT_PASSED - names)
    assert not stale, f"NOT_PASSED lists names no fork code reads: {stale}"


def test_override_passes_settings_unchanged_from_dot_env():
    from fork.common import settings

    env = _override_env()
    expected = {}
    for row in _table().values():
        for name in row.names:
            # "${NAME:-}": empty when unset; a bare name: absent when unset; else fixed.
            expected[name] = {settings.PASS_EMPTY: f"${{{name}:-}}",
                              settings.PASS_BARE: None}.get(row.passed, row.passed)
    assert env == expected


def test_empty_means_unset_exactly_where_the_override_passes_empty(monkeypatch):
    """A row passed as ${NAME:-} must read "" as unset; the bare one must not."""
    from fork.common import settings

    for row in _table().values():
        assert row.empty_is_unset == (row.passed != settings.PASS_BARE), row.name
        for name in row.names:
            for other in row.names:
                monkeypatch.delenv(other, raising=False)
            unset = row.value()
            monkeypatch.setenv(name, "")
            assert (row.value() == unset) == row.empty_is_unset, name


def test_empty_value_means_unset_for_every_dollar_default_setting(monkeypatch):
    """Each ${NAME:-} entry is empty in the container when .env doesn't set it, so
    its reader must give the same result for "" as for unset."""
    from src import (
        belief_consolidation,
        belief_structure,
        call_recorder,
        checklist_auto_archive,
        evidence_intake,
        ink_transcribe,
        insight_push,
        module_action_items,
        module_facts,
        notes_evidence,
        personal_read_grants,
        personal_store_escalation,
        today_insight,
    )
    from fork.brain import settings as brain_settings

    mai = module_action_items
    readers = {
        "MODULE_ACTION_ITEMS_SERVER": mai.server_key,
        "MODULE_NOTIFY_CHANNEL": mai.notify_channel,
        "MODULE_DIGEST_HOUR": mai.digest_hour,
        "MODULE_FILE_ITEMS": mai.file_mode,
        "MODULE_FACTS_SYNC": module_facts.enabled,
        "SOUND_MODULE_SERVER": call_recorder.server_key,
        "SOUND_MODULE_CALL_DEVICE": call_recorder.device,
        "BELIEF_CONFIRM_SOURCES": evidence_intake.settings,
        "BELIEF_CONFIRM_TIMES": evidence_intake.settings,
        "BELIEF_CONFIRM_DAYS": evidence_intake.settings,
        "BELIEF_STALE_DAYS": evidence_intake.settings,
        "BELIEF_CONSOLIDATE": belief_consolidation.enabled,
        "BELIEF_STRUCTURE": belief_structure.enabled,
        "BELIEF_STRUCTURE_BATCH": belief_structure.batch_size,
        "EVIDENCE_NOTES": notes_evidence.enabled,
        "TODAY_INSIGHT_ENABLED": today_insight.enabled,
        "TODAY_INSIGHT_PUSH": insight_push.enabled,
        "ODYSSEUS_PERSONAL_READ_GRANTS": personal_read_grants.enabled,
        "ODYSSEUS_PERSONAL_STORE_ESCALATION": personal_store_escalation.enabled,
        "FORK_CHECKLIST_ARCHIVE_MINUTES": checklist_auto_archive.archive_minutes,
        "INK_AUTO_SEND": ink_transcribe.auto_send_enabled,
        "FORK_BRAIN": brain_settings.enabled,
        "FORK_BRAIN_MAX_ROUNDS": brain_settings.max_rounds,
        "FORK_BRAIN_TOOL_GROUPS": brain_settings.tool_groups,
    }
    # Former names: read only when the new name is unset or empty.
    legacy = {f"BARVIS_{n.removeprefix('MODULE_')}": n for n in readers if n.startswith("MODULE_")}
    legacy["BARVIS_SOUND_SERVER"] = "SOUND_MODULE_SERVER"
    # Read by settings.app_public_url() (fork/common/settings.py) and upstream's
    # src/mcp_oauth.py; an empty value there means unset too.
    by_reading = {"APP_PUBLIC_URL"}

    dollar = {n for n, v in _override_env().items() if v and v.startswith("${")}
    assert dollar == set(readers) | set(legacy) | by_reading
    assert by_reading | set(readers) == {
        r.name for r in _table().values() if r.passed == "empty"}
    for name in sorted(set(readers) | set(legacy)):
        read = readers[legacy.get(name, name)]
        for other in (name, legacy.get(name, name), *legacy):
            monkeypatch.delenv(other, raising=False)
        unset = read()
        monkeypatch.setenv(name, "")
        assert read() == unset, name
        monkeypatch.delenv(name)

    # A bare entry (no "="), because here "" differs from unset.
    monkeypatch.delenv("TODAY_INSIGHT_SKIP_LISTS", raising=False)
    default = today_insight.skip_lists()
    monkeypatch.setenv("TODAY_INSIGHT_SKIP_LISTS", "")
    assert today_insight.skip_lists() != default
    assert _override_env()["TODAY_INSIGHT_SKIP_LISTS"] is None


def test_scan_resolves_helpers_and_constants(tmp_path, monkeypatch):
    src = tmp_path / "m.py"
    src.write_text(
        "import os\n"
        "NAME_ENV = 'A_CONST'\n"
        "def _env(name, default):\n"
        "    return os.environ.get(f'PRE_{name}') or os.environ.get(f'OLD_{name}') or default\n"
        "def f():\n"
        "    return os.getenv('B_LIT'), os.environ[NAME_ENV], _env('X', '1')\n"
        "def g(key):\n"
        "    return os.environ.get(key.upper())\n",
        encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    names, unresolved = fork_env_reads([src])
    assert names == {"A_CONST", "B_LIT", "PRE_X", "OLD_X"}
    assert unresolved == ["m.py:8"]


def test_compose_renders_with_the_override():
    import shutil

    if not shutil.which("docker"):
        pytest.skip("needs docker compose")
    r = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", str(OVERRIDE), "config",
         "--format", "json"],
        check=False, cwd=REPO, capture_output=True, text=True, timeout=60)
    if r.returncode != 0 and "is not a docker command" in r.stderr:
        pytest.skip("needs docker compose")
    assert r.returncode == 0, r.stderr
