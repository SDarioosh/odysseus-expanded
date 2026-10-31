"""Fork: what the fork's deterministic evals share (``scripts/belief_eval.py``,
``belief_eval_scale.py``, ``insight_eval.py``, ``ink_eval.py``).

* :class:`Check`: one expected-vs-actual check, grouped by case.
* :func:`table`, :func:`case_table`, :func:`check_list`: the three report layouts the
  evals print (a column table, checks grouped per case, a list of named checks).
* :func:`temp_data_dir`: the data dir of a run, a temp dir unless one is given.
* :func:`use_data_dir`: point ``src.constants.DATA_DIR`` at it for the run (and run on
  the default ``BELIEF_*`` settings), restored afterwards.
* The fictional sources and links: :data:`MODULE`, :data:`NOTES`, :func:`module_link`.

Output and scores are pinned by ``tests/fork/test_eval_goldens.py``. Import it as
``scripts.eval_common`` (the evals put the repo root on ``sys.path`` first).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

# The belief sources the evals seed: Athena's facts and the hub's notes. A ledger from
# before the rename stores Athena's under its old name (fork/common/athena_names.py), and
# the ledger keeps writing that one there: compare stored sources with source_name().
MODULE, NOTES = "athena", "notes"
MODULE_SOURCES = (MODULE, "odysseus-sound")


def source_name(source: str) -> str:
    """A stored evidence source as the evals name it (Athena's old name -> MODULE)."""
    return MODULE if source in MODULE_SOURCES else source
# A fictional Athena: its transcript links (``/view/c<id>?sig=...#t=<seconds>``).
MODULE_VIEW = "http://127.0.0.1:8090"
# The ledger settings the belief evals unset, so a run is on the defaults.
BELIEF_SETTINGS_ENV = ("BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES",
                       "BELIEF_CONFIRM_DAYS", "BELIEF_STALE_DAYS")


def module_link(fact_id: int, sig: str) -> str:
    """A fictional transcript link of module fact ``fact_id``; ``sig`` tells the evals'
    links apart."""
    return f"{MODULE_VIEW}/view/c{fact_id}?sig={sig}#t={fact_id % 97}"


@dataclass
class Check:
    case: str
    step: str
    expected: Any
    actual: Any

    @property
    def ok(self) -> bool:
        return self.expected == self.actual

    @property
    def name(self) -> str:
        """The step: a check's name where an eval has one case (belief_eval_scale)."""
        return self.step


def failures(checks: Iterable[Check]) -> int:
    return sum(not c.ok for c in checks)


def table(checks: list[Check], out: TextIO | None = None) -> int:
    """Expected vs actual per check in columns (cells cut at 48 characters), the cases
    and checks scores, then each failure in full; returns the failures."""
    out = out or sys.stdout
    rows = [("case", "step", "expected", "actual", "")]
    rows += [(c.case, c.step, c.expected, c.actual, "ok" if c.ok else "FAIL") for c in checks]
    width = [min(48, max(len(row[i]) for row in rows)) for i in range(4)]
    for row in rows:
        cells = [row[i][:width[i]].ljust(width[i]) for i in range(4)]
        print("  ".join(cells + [row[4]]).rstrip(), file=out)
    passed = sum(c.ok for c in checks)
    cases = {}
    for c in checks:
        cases[c.case] = cases.get(c.case, True) and c.ok
    print(f"\ncases: {sum(cases.values())}/{len(cases)}   checks: score {passed}/{len(checks)}",
          file=out)
    for c in checks:
        if not c.ok:
            print(f"FAIL {c.case}: {c.step}\n  expected: {c.expected}\n  actual:   {c.actual}",
                  file=out)
    return len(checks) - passed


def case_table(checks: list[Check], cases: Sequence[str], out: TextIO | None = None) -> int:
    """Per case in ``cases`` order: its score, then a line per check (expected and actual
    as JSON when it fails); then the cases and checks scores. Returns the failures."""
    out = out or sys.stdout
    failed = 0
    for case in cases:
        mine = [c for c in checks if c.case == case]
        ok = sum(c.ok for c in mine)
        print(f"[{case}] {ok}/{len(mine)}", file=out)
        for c in mine:
            mark = "ok  " if c.ok else "FAIL"
            print(f"  {mark} {c.step}", file=out)
            if not c.ok:
                failed += 1
                print(f"       expected: {json.dumps(c.expected, ensure_ascii=False, default=str)}",
                      file=out)
                print(f"       got:      {json.dumps(c.actual, ensure_ascii=False, default=str)}",
                      file=out)
    cases_ok = sum(all(c.ok for c in checks if c.case == case) for case in cases)
    print(f"cases: {cases_ok}/{len(cases)}", file=out)
    print(f"score {sum(c.ok for c in checks)}/{len(checks)}", file=out)
    return failed


def check_list(checks: list[Check], out: TextIO | None = None) -> int:
    """A "checks" section: a line per check by its step (expected and actual when it
    fails) and the score. Returns the failures."""
    out = out or sys.stdout
    print("\nchecks", file=out)
    for chk in checks:
        print(f"  {'ok  ' if chk.ok else 'FAIL'} {chk.name}"
              + ("" if chk.ok else f" (expected {chk.expected}, got {chk.actual})"), file=out)
    failed = failures(checks)
    print(f"\nchecks: {len(checks) - failed}/{len(checks)}", file=out)
    return failed


@contextmanager
def temp_data_dir(data_dir: Path | None, prefix: str, *, keep: bool = False) -> Iterator[Path]:
    """``data_dir`` as given (always kept), or a new temp dir named ``<prefix>...`` that is
    removed on exit unless ``keep``."""
    temp = data_dir is None
    path = Path(tempfile.mkdtemp(prefix=prefix)) if temp else data_dir
    try:
        yield path
    finally:
        if temp and not keep:
            shutil.rmtree(path, ignore_errors=True)


# Imports src.constants only when called: ink_eval sets ODYSSEUS_DATA_DIR before the
# first import of src.constants.
@contextmanager
def use_data_dir(data_dir: str | Path, clear_env: Sequence[str] = ()) -> Iterator[None]:
    """Set ``src.constants.DATA_DIR`` to ``data_dir`` and unset the ``clear_env``
    variables for the block; both are restored afterwards."""
    from src import constants

    saved_dir = constants.DATA_DIR
    saved_env = {k: os.environ.pop(k) for k in clear_env if k in os.environ}
    constants.DATA_DIR = str(data_dir)
    try:
        yield
    finally:
        constants.DATA_DIR = saved_dir
        os.environ.update(saved_env)
