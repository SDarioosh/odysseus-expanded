"""Fork: state files of the fork's background jobs are written atomically.

Each writer writes a temp file next to the state file and renames it over the file.
The temp name must be unique per process and thread: with one shared name
(``state.tmp``) two writers of the same file at once truncate each other's temp file
or find it already renamed (``FileNotFoundError``).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest


def _belief_structure(owner, state):
    from src import belief_structure
    belief_structure.save_state(owner, state)
    return belief_structure._state_path(owner)


def _module_action_items(owner, state):
    from src import module_action_items
    module_action_items.save_state(owner, state)
    return module_action_items._state_path(owner)


def _module_facts(owner, state):
    from src import module_facts
    module_facts.save_state(owner, state)
    return module_facts._state_path(owner)


def _notes_evidence(owner, state):
    from src import notes_evidence
    notes_evidence.save_state(owner, state)
    return notes_evidence._state_path(owner)


def _today_insight(owner, state):
    from src import today_insight
    today_insight.save(owner, {**state, "date": "2026-10-29"})
    return today_insight.insight_path(owner, "2026-10-29")


def _insight_outcomes(owner, state):
    from src import insight_outcomes
    insight_outcomes.save(owner, {**state, "date": "2026-10-29"})
    return insight_outcomes.outcome_path(owner, "2026-10-29")


def _insight_routines(owner, state):
    from src import insight_routines
    insight_routines._save_cache(owner, state)
    return insight_routines.cache_path(owner)


WRITERS = [_belief_structure, _module_action_items, _module_facts, _notes_evidence,
           _today_insight, _insight_outcomes, _insight_routines]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    return tmp_path


def _leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.rglob("*") if p.name.endswith(".tmp"))


@pytest.mark.parametrize("writer", WRITERS, ids=lambda w: w.__name__.strip("_"))
def test_two_threads_write_the_same_state_file(data_dir, writer):
    # A big state makes each write slow enough for the writers to overlap.
    payloads = [{"items": {f"k{i}": "x" * 200 for i in range(400)}, "writer": n}
                for n in range(2)]
    start = threading.Barrier(2)
    errors: list[BaseException] = []
    paths: list[Path] = []

    def run(n: int) -> None:
        start.wait()
        try:
            for _ in range(60):
                paths.append(writer("someone@example.com", payloads[n]))
        except BaseException as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"{type(errors[0]).__name__}: {errors[0]}"
    data = json.loads(paths[0].read_text(encoding="utf-8"))
    assert data["writer"] in (0, 1)
    assert data["items"] == payloads[0]["items"]
    assert _leftovers(data_dir) == []


@pytest.mark.parametrize("writer", WRITERS, ids=lambda w: w.__name__.strip("_"))
def test_failed_write_leaves_the_file_and_no_temp_file(data_dir, writer, monkeypatch):
    path = writer(None, {"items": {}, "writer": "first"})
    before = path.read_text(encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    monkeypatch.setattr(Path, "replace", lambda self, target: broken_replace(self, target))
    with pytest.raises(OSError, match="disk full"):
        writer(None, {"items": {}, "writer": "second"})
    assert path.read_text(encoding="utf-8") == before
    assert _leftovers(data_dir) == []
