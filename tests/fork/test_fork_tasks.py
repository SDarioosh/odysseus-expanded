"""``fork/tasks.py``: the table of the fork's scheduled tasks and its two hooks."""
from __future__ import annotations

import importlib

from fork import tasks


def test_table_rows_match_their_modules():
    assert len({t.action for t in tasks.TASKS}) == len(tasks.TASKS)
    for task in tasks.TASKS:
        mod = importlib.import_module(task.module)
        assert mod.ACTION == task.action
        assert callable(getattr(mod, task.handler))
        assert hasattr(mod, "HOUSEKEEPING_DEFAULT") == task.housekeeping
        if task.model_backed:
            assert task.silent


def test_register_actions_into_empty_registries():
    actions, info = {}, {}
    tasks.register_actions(actions, info)
    assert list(actions) == list(info) == [t.action for t in tasks.TASKS]


def test_register_scheduler_on_a_stand_in_class():
    class Scheduler:
        _SILENT_ACTIONS = frozenset({"upstream_quiet"})
        _MODEL_BACKED_ACTIONS = frozenset({"upstream_model"})

    defaults = {"upstream": {"name": "Upstream"}}
    tasks.register_scheduler(defaults, Scheduler)
    assert list(defaults) == ["upstream"] + [t.action for t in tasks.TASKS if t.housekeeping]
    assert Scheduler._SILENT_ACTIONS == {"upstream_quiet"} | {
        t.action for t in tasks.TASKS if t.silent}
    assert Scheduler._MODEL_BACKED_ACTIONS == {"upstream_model"} | {
        t.action for t in tasks.TASKS if t.model_backed}
    assert isinstance(Scheduler._SILENT_ACTIONS, frozenset)
