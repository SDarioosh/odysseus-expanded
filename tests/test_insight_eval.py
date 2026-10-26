"""Fork: the Today insight eval (scripts/insight_eval.py) through pytest.

Same fixture and checks as the script, in a temp DATA_DIR. Fictional data only.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "insight_eval.py"
_SPEC = importlib.util.spec_from_file_location("insight_eval", _PATH)
insight_eval = importlib.util.module_from_spec(_SPEC)
sys.modules["insight_eval"] = insight_eval
_SPEC.loader.exec_module(insight_eval)


@pytest.fixture(scope="module")
def checks(tmp_path_factory):
    return insight_eval.run_eval(tmp_path_factory.mktemp("insight-eval"))


@pytest.mark.parametrize("case", list(insight_eval.CASES))
def test_case(checks, case):
    mine = [c for c in checks if c.case == case]
    assert mine, f"no checks for {case}"
    failed = [f"{c.step}: expected {c.expected!r}, got {c.actual!r}" for c in mine if not c.ok]
    assert not failed, "\n".join(failed)


def test_full_score_and_settings_restored(tmp_path, capsys):
    from src import constants
    from src import today_insight as ti

    before = (constants.DATA_DIR, ti.owner_timezone)
    assert insight_eval.main(["--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    n = len(insight_eval.CASES)
    assert f"cases: {n}/{n}" in out
    passed, total = out.split("score ")[1].split()[0].split("/")
    assert passed == total and int(total) >= 40
    assert (constants.DATA_DIR, ti.owner_timezone) == before
    # An earlier run in the same dir would mix histories: refused.
    assert insight_eval.main(["--data-dir", str(tmp_path)]) == 2


def test_json_output(capsys):
    assert insight_eval.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["score"] == data["total"] and all(data["cases"].values())


def test_a_mismatch_exits_1(tmp_path, capsys):
    fx = insight_eval.load_fixture(insight_eval.T5_FIXTURE)
    fx["expected"]["also"] = list(reversed(fx["expected"]["also"]))
    path = tmp_path / "t5.json"
    path.write_text(json.dumps(fx))
    assert insight_eval.main(["--t5-fixture", str(path), "--data-dir", str(tmp_path / "d")]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_t5_fixture_is_the_module_copy():
    """The T5 day is a verbatim copy of odysseus-sound's PDA host test fixture; the hub's
    weights block of the learning fixture is a valid feed block."""
    fx = insight_eval.load_fixture(insight_eval.T5_FIXTURE)
    assert set(fx["expected"]) == set(insight_eval.T5_SECTIONS)
    assert set(fx["expected_without_weights"]) == set(insight_eval.T5_SECTIONS)
    day = insight_eval.load_fixture()
    assert day["weights"]["weights"] == day["expected"]["t5_table"]
    assert day["weights"]["date"] == day["today"]
