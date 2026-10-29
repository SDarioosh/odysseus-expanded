"""Fork: golden outputs of the fork's evals (scripts/belief_eval.py, belief_eval_scale.py,
insight_eval.py, ink_eval.py; module_slash_eval.py's help only, it needs a live stack).

Each eval runs as the owner runs it (``python scripts/<eval>.py ...``, a subprocess) and
its stdout, ``--help`` and results files must match ``tests/fixtures/eval_goldens/``
byte for byte, after only the volatile parts are masked: timings, the temp dir and the
results path, and the SQLite file size (it depends on the SQLite build, not on the eval).
Scores, tables and JSON layout are compared exactly, so a refactor of the evals'
scaffolding can't change what they print. Fictional data only, no model.

Regenerate after an intended output change:
``UPDATE_EVAL_GOLDENS=1 python -m pytest -q tests/fork/test_eval_goldens.py``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GOLDENS = REPO / "tests" / "fixtures" / "eval_goldens"
UPDATE = os.environ.get("UPDATE_EVAL_GOLDENS") == "1"

_TIME = re.compile(r"\d+(?:\.\d+)?(?=→| ms\b| s\)| / [\d.]+ ms\b)")


def run(*args: str, cwd: Path = REPO) -> str:
    env = {**os.environ, "COLUMNS": "80"}  # argparse wraps help to the terminal width
    for key in ("BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS",
                "BELIEF_STALE_DAYS", "ODYSSEUS_DATA_DIR"):
        env.pop(key, None)
    proc = subprocess.run([sys.executable, *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=600, check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout


def check(name: str, text: str) -> None:
    path = GOLDENS / name
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"no golden {name}; run with UPDATE_EVAL_GOLDENS=1"
    assert text == path.read_text(encoding="utf-8"), f"{name} differs from its golden"


def mask_scale_text(text: str, out: Path) -> str:
    """Timings (ms, s, chunk trends), the db size and the results path."""
    lines = []
    for line in text.replace(str(out), "<out>").splitlines(keepends=True):
        if line.startswith("  beliefs.db (+ wal)"):
            line = re.sub(r"\S+ [KM]B$", "<size>", line)
        lines.append(_TIME.sub("<t>", line))
    return "".join(lines)


def _is_timing(key: str) -> bool:
    return key in ("ms", "seconds") or key.startswith("ms_") or key.endswith("_ms")


def mask_json(value):
    """Timings to "<t>", the temp data dir and the db size to placeholders."""
    if isinstance(value, dict):
        out = {}
        for key, v in value.items():
            if _is_timing(key) and v is not None:
                out[key] = "<t>"
            elif key == "data_dir":
                out[key] = "<tmp>"
            elif key == "db_bytes":
                out[key] = "<size>"
            else:
                out[key] = mask_json(v)
        return out
    if isinstance(value, list):
        return [mask_json(v) for v in value]
    return value


def dump(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def test_belief_eval():
    check("belief_eval.txt", run("scripts/belief_eval.py"))


def test_belief_eval_scale_small(tmp_path):
    out = tmp_path / "scale.json"
    text = run("scripts/belief_eval_scale.py", "--size", "small", "--out", str(out))
    check("belief_eval_scale.small.txt", mask_scale_text(text, out))
    check("belief_eval_scale.small.json", dump(mask_json(json.loads(out.read_text()))))


def test_insight_eval():
    check("insight_eval.txt", run("scripts/insight_eval.py"))


def test_insight_eval_json():
    check("insight_eval.json", run("scripts/insight_eval.py", "--json"))


def test_ink_eval_fixtures():
    check("ink_eval.fixtures.txt", _TIME.sub("<t>", re.sub(
        r"^seconds per page: .*$", "seconds per page: <t>", run("scripts/ink_eval.py",
                                                                "--fixtures"), flags=re.MULTILINE)))


def test_ink_eval_fixtures_json(tmp_path):
    out = tmp_path / "ink.json"
    text = run("scripts/ink_eval.py", "--fixtures", "--json", "--out", str(out))
    assert out.read_text(encoding="utf-8") == text  # --out holds what --json prints
    check("ink_eval.fixtures.json", dump(mask_json(json.loads(text))))


def test_ink_eval_fixtures_stored_per_page():
    check("ink_eval.fixtures_stored_per_page.txt",
          run("scripts/ink_eval.py", "--fixtures", "--stored", "--per-page"))


@pytest.mark.parametrize("script", ["belief_eval", "belief_eval_scale", "insight_eval",
                                    "ink_eval", "module_slash_eval"])
def test_help(script):
    check(f"{script}.help.txt", run(f"scripts/{script}.py", "--help"))
