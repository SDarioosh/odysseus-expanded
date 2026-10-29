"""Fork: scripts/eval_common.py, what the evals share. The report layouts are pinned with
failing checks too (the eval goldens only see passing runs); the expected strings are
what the evals printed before they shared this code. Fictional data only.
"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

from scripts import eval_common as ec
from scripts.eval_common import Check


def test_check_ok_compares_expected_and_actual():
    assert Check("c", "s", [1, "a"], [1, "a"]).ok
    assert not Check("c", "s", "1", 1).ok


def test_module_link():
    assert ec.module_link(9004, "eval") == "http://127.0.0.1:8090/view/c9004?sig=eval#t=80"
    assert ec.MODULE == "athena" and ec.NOTES == "notes"


def test_table_with_a_failure():
    checks = [Check("a", "first step", "1", "1"), Check("a", "second " + "x" * 60, "yes", "no"),
              Check("bb", "third", "ok", "ok")]
    out = io.StringIO()
    assert ec.table(checks, out) == 1
    assert out.getvalue() == (
        "case  step                                              expected  actual\n"
        "a     first step                                        1         1       ok\n"
        "a     second xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  yes       no      FAIL\n"
        "bb    third                                             ok        ok      ok\n"
        "\ncases: 1/2   checks: score 2/3\n"
        "FAIL a: second " + "x" * 60 + "\n  expected: yes\n  actual:   no\n")


def test_case_table_with_a_failure_and_an_empty_case(capsys):
    checks = [Check("one", "s1", [1, "é"], [1, "é"]), Check("two", "s2", {"a": 1}, {"a": 2}),
              Check("two", "s3", 1, 1)]
    assert ec.case_table(checks, ("one", "two", "three")) == 1
    assert capsys.readouterr().out == (
        '[one] 1/1\n  ok   s1\n[two] 1/2\n  FAIL s2\n       expected: {"a": 1}\n'
        '       got:      {"a": 2}\n  ok   s3\n[three] 0/0\ncases: 2/3\nscore 2/3\n')


def test_check_list_with_a_failure():
    out = io.StringIO()
    assert ec.check_list([Check("scale", "n1", "0", "0"),
                          Check("scale", "n2", "True", "False")], out) == 1
    assert out.getvalue() == ("\nchecks\n  ok   n1\n  FAIL n2 (expected True, got False)\n"
                              "\nchecks: 1/2\n")


def test_temp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with ec.temp_data_dir(None, "x-eval-") as made:
        assert made.is_dir() and made.name.startswith("x-eval-") and made.parent == tmp_path
    assert not made.exists()
    with ec.temp_data_dir(None, "x-eval-", keep=True) as kept:
        pass
    assert kept.is_dir()
    given = tmp_path / "given"
    given.mkdir()
    try:
        with ec.temp_data_dir(given, "x-eval-") as d:
            assert d == given
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert given.is_dir()  # a given dir is never removed


def test_use_data_dir_restores_the_dir_and_settings(tmp_path, monkeypatch):
    from src import constants

    monkeypatch.setenv("BELIEF_STALE_DAYS", "5")
    monkeypatch.delenv("BELIEF_CONFIRM_DAYS", raising=False)
    before = constants.DATA_DIR
    try:
        with ec.use_data_dir(tmp_path, ec.BELIEF_SETTINGS_ENV):
            assert constants.DATA_DIR == str(tmp_path)
            assert "BELIEF_STALE_DAYS" not in os.environ
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert constants.DATA_DIR == before
    assert os.environ["BELIEF_STALE_DAYS"] == "5"
    assert "BELIEF_CONFIRM_DAYS" not in os.environ
    assert Path(before) != tmp_path
