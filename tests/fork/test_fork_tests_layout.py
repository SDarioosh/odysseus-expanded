"""The fork's tests live in tests/fork/ without shadowing the fork's code package.

``tests/fork`` has no ``__init__.py`` on purpose: pytest's default ``prepend`` import mode
puts test directories on ``sys.path``, and a regular package named ``fork`` found there
would win over the code package at the repo root and break every ``fork.*`` import.
As a plain directory it is at most a namespace portion, which a regular package beats.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"


def test_import_fork_is_the_code_package_at_the_repo_root():
    import fork

    assert Path(fork.__file__).resolve() == REPO / "fork" / "__init__.py"
    assert importlib.import_module("fork.mcp.trust").__file__.startswith(str(REPO / "fork"))


def test_shared_fakes_import_without_touching_the_fork_package():
    from tests.fork.fakes import FakeMcpManager

    assert FakeMcpManager().is_builtin("any") is False
    assert Path(sys.modules["tests.fork.fakes"].__file__).resolve() == TESTS / "fork" / "fakes.py"
    assert Path(sys.modules["fork"].__file__).resolve() == REPO / "fork" / "__init__.py"


def test_tests_fork_is_no_package():
    assert not (TESTS / "fork" / "__init__.py").exists()
    assert not (TESTS / "__init__.py").exists()


def test_fork_test_basenames_are_unique_in_the_suite():
    # Without __init__.py files pytest imports a test file by its basename, so two test
    # files of the same name anywhere under tests/ collide ("import file mismatch").
    mine = {p.name for p in (TESTS / "fork").glob("test_*.py")}
    others = [p for p in TESTS.rglob("test_*.py")
              if p.parent != TESTS / "fork" and p.name in mine]
    assert others == []


def test_fixtures_load_from_tests_fork_conftest(data_dir, fake_local_model):
    from src import constants

    assert constants.DATA_DIR == str(data_dir)
    assert fake_local_model.waited == [] and fake_local_model.sent == []
