"""Fork: the settings table (fork/common/settings.py) and what is generated from it.

``docs/fork/settings.md`` is generated from the table; after changing a row run
``UPDATE_FORK_SETTINGS=1 python -m pytest -q tests/fork/test_fork_settings.py``.
The production override is checked against the table in
``tests/fork/test_fork_override_env.py``; what each reader returns is pinned in
``tests/fork/test_settings_readers.py``.
"""
from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest

from fork.common import settings

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "fork" / "settings.md"

# Every spelling the one on/off parser is given, with its answer for a setting that is
# on by default and for one that is off by default.
FLAG_CASES = [
    # value, default on, default off
    (None, True, False), ("", True, False), (" ", True, False), ("\t\n", True, False),
    ("1", True, True), ("0", False, False), ("00", True, False), ("true", True, True),
    ("TRUE", True, True), (" True\n", True, True), ("yes", True, True), ("YES", True, True),
    ("on", True, True), (" On ", True, True), ("false", False, False),
    ("False", False, False), ("no", False, False), (" NO", False, False),
    ("off", False, False), ("OFF\n", False, False), ("y", True, False), ("n", True, False),
    ("none", True, False), ("enabled", True, False), ("disabled", True, False),
    ("2", True, False), ("-1", True, False), ("t", True, False), ("f", True, False),
    ("o n", True, False), ("on!", True, False),
]


@pytest.mark.parametrize(("value", "on_default", "off_default"), FLAG_CASES)
def test_one_flag_parser(value, on_default, off_default):
    assert settings.flag(value, True) is on_default
    assert settings.flag(value, False) is off_default


def test_every_name_is_listed_once():
    names = [n for row in settings.SETTINGS.values() for n in row.names]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]+", n) for n in names)


def test_rows_point_at_fork_files_that_exist():
    for row in settings.SETTINGS.values():
        assert row.used_in, row.name
        for path in row.used_in:
            assert (REPO / path).is_file(), (row.name, path)
        assert row.doc.strip() and row.type.strip(), row.name


def test_table_defaults_match_the_features_constants():
    from fork.athena import call_recorder
    from fork.beliefs import evidence_intake, structure
    from fork.hub import checklist_auto_archive
    from fork.insight import today

    assert int(settings.STRUCTURE_BATCH.default) == structure.BATCH
    assert structure.MAX_BATCH == 10  # the clamp in settings.belief_structure_batch
    defaults = evidence_intake.Settings()
    assert [int(r.default) for r in (settings.CONFIRM_SOURCES, settings.CONFIRM_TIMES,
                                     settings.CONFIRM_DAYS, settings.STALE_DAYS)] == [
        defaults.confirm_sources, defaults.confirm_times, defaults.confirm_days,
        defaults.stale_days]
    assert today.DEFAULT_SKIP_LISTS == "Shopping,Groceries,Grocery"
    assert (call_recorder.SERVER_ENV, call_recorder.LEGACY_SERVER_ENV,
            call_recorder.DEFAULT_SERVER, call_recorder.DEVICE_ENV,
            call_recorder.DEFAULT_DEVICE) == ("SOUND_MODULE_SERVER", "BARVIS_SOUND_SERVER",
                                              "convo", "SOUND_MODULE_CALL_DEVICE", "computer")
    assert checklist_auto_archive.ENV == "FORK_CHECKLIST_ARCHIVE_MINUTES"
    assert checklist_auto_archive.DEFAULT_MINUTES == 10.0


def test_the_two_athena_server_settings(monkeypatch):
    for name in ("MODULE_ACTION_ITEMS_SERVER", "BARVIS_ACTION_ITEMS_SERVER",
                 "SOUND_MODULE_SERVER", "BARVIS_SOUND_SERVER"):
        monkeypatch.delenv(name, raising=False)
    assert settings.athena_server() == settings.athena_recorder_server() == "convo"
    monkeypatch.setenv("SOUND_MODULE_SERVER", " Srv ")
    monkeypatch.setenv("BARVIS_ACTION_ITEMS_SERVER", "Old")
    assert settings.athena_server() == "old"
    assert settings.athena_recorder_server() == "Srv"
    monkeypatch.setenv("MODULE_ACTION_ITEMS_SERVER", "New")
    assert settings.athena_server() == "new"


def test_env_reference_finds_every_odysseus_row_with_its_default():
    spec = importlib.util.spec_from_file_location(
        "generate_env_reference", REPO / "scripts" / "generate_env_reference.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    found = dict(generator.scan_file(REPO / "fork" / "common" / "settings.py", REPO))
    rows = {n: r for n, r in settings.SETTINGS.items() if n.startswith("ODYSSEUS_")}
    assert set(found) == set(rows)
    for name, row in rows.items():
        assert found[name].default == repr(row.default), name


def test_deploy_doc_names_every_setting():
    text = (REPO / "docs" / "fork" / "deploy.md").read_text(encoding="utf-8")
    missing = [n for n in settings.SETTINGS if f"`{n}" not in text]
    assert not missing


def render() -> str:
    lines = [
        "# Fork settings",
        "",
        "Generated from the table in `fork/common/settings.py` by",
        "`tests/fork/test_fork_settings.py`; don't edit by hand. After changing a row:",
        "",
        "```bash",
        "UPDATE_FORK_SETTINGS=1 python -m pytest -q tests/fork/test_fork_settings.py",
        "```",
        "",
        "Environment variables the fork's code reads, read at call time. Former names are",
        "still read when the current one is unset. An empty value means unset unless the",
        "row says otherwise. Production passes them from `.env` through",
        "`docker/fork.override.yml` ([deploy.md](deploy.md#fork-settings-in-env)). On/off",
        "settings that are on by default are off for `0`, `false`, `no` or `off`; those",
        "that are off by default are on for `1`, `true`, `yes` or `on` (any case).",
        "",
        "| Variable | Former names | Type | Unset means | Meaning | Used in |",
        "|---|---|---|---|---|---|",
    ]
    for row in settings.SETTINGS.values():
        old = ", ".join(f"`{n}`" for n in row.old) or "-"
        default = f"`{row.default}`" if row.default else "(empty)"
        doc = row.doc
        if not row.empty_is_unset:
            doc += " An empty value is a value of its own."
        if row.passed not in (settings.PASS_EMPTY, settings.PASS_BARE):
            doc += f" Production sets `{row.passed}` in the override."
        used = ", ".join(f"`{p}`" for p in row.used_in)
        lines.append(f"| `{row.name}` | {old} | {row.type} | {default} | {doc} | {used} |")
    return "\n".join(lines) + "\n"


def test_settings_doc_is_generated_from_the_table():
    text = render()
    if os.environ.get("UPDATE_FORK_SETTINGS"):
        DOC.write_text(text, encoding="utf-8")
    assert DOC.read_text(encoding="utf-8") == text, (
        "docs/fork/settings.md is stale: UPDATE_FORK_SETTINGS=1 python -m pytest -q "
        "tests/fork/test_fork_settings.py")
