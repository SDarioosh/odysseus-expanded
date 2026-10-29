"""What the fork registers with upstream's task machinery, pinned.

The fork's scheduled tasks are hooked into ``src/builtin_actions.py`` (handler and
description), ``src/task_scheduler.py`` ``HOUSEKEEPING_DEFAULTS`` (the seeded task) and
``TaskScheduler._SILENT_ACTIONS`` / ``_MODEL_BACKED_ACTIONS``. Action ids are stored in
the owner's data (``scheduled_tasks.action``), so they and every flag here must not
change. Fictional data only.
"""
from __future__ import annotations

import ast
import contextlib
import importlib
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]

# action id -> (module, handler) in registration order (the order of BUILTIN_ACTIONS)
ACTIONS = [
    ("barvis_sync_action_items", "fork.athena.action_items", "action_sync_action_items"),
    ("module_sync_facts", "fork.athena.facts", "action_sync_facts"),
    ("today_insight", "fork.insight.today", "action_today_insight"),
    ("notes_evidence", "fork.beliefs.notes_evidence", "action_notes_evidence"),
    ("belief_structure", "fork.beliefs.structure", "action_belief_structure"),
    ("meeting_auto_join", "fork.athena.meeting_auto_join", "action_meeting_auto_join"),
]

# the seeded tasks, in HOUSEKEEPING_DEFAULTS order, exactly as stored defaults
HOUSEKEEPING = {
    "barvis_sync_action_items": {
        "name": "Module Action Items", "schedule": "cron", "scheduled_time": None,
        "cron_expression": "*/5 * * * *", "ship_paused": True,
        "old_cron_expressions": ["*/15 * * * *"], "legacy_names": ["Barvis Action Items"]},
    "module_sync_facts": {"name": "Module Facts", "schedule": "cron", "scheduled_time": None,
                          "cron_expression": "0 * * * *"},
    "today_insight": {"name": "Today Insight", "schedule": "cron", "scheduled_time": None,
                      "cron_expression": "*/15 * * * *"},
    "notes_evidence": {"name": "Notes Evidence", "schedule": "cron", "scheduled_time": None,
                       "cron_expression": "0 3 * * *"},
    "belief_structure": {"name": "Belief Structure", "schedule": "cron",
                         "scheduled_time": None, "cron_expression": "15 * * * *"},
}

SILENT = {"barvis_sync_action_items", "module_sync_facts", "today_insight",
          "notes_evidence", "belief_structure"}
MODEL_BACKED = {"today_insight", "notes_evidence", "belief_structure"}


def _upstream_class_set(name: str) -> set[str]:
    """The literal ``frozenset({...})`` upstream assigns to ``TaskScheduler.<name>``."""
    tree = ast.parse((ROOT / "src" / "task_scheduler.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TaskScheduler")
    for node in cls.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == name):
            return set(ast.literal_eval(node.value.args[0]))
    raise AssertionError(f"TaskScheduler.{name} not found")


def test_builtin_actions_handlers_and_descriptions():
    from src.builtin_actions import BUILTIN_ACTION_INFO, BUILTIN_ACTIONS

    ids = [a for a, _, _ in ACTIONS]
    assert list(BUILTIN_ACTIONS)[-len(ids):] == ids
    assert list(BUILTIN_ACTION_INFO)[-len(ids):] == ids
    for action, module, handler in ACTIONS:
        mod = importlib.import_module(module)
        assert mod.ACTION == action
        assert BUILTIN_ACTIONS[action] is getattr(mod, handler)
        assert BUILTIN_ACTION_INFO[action] == mod.INFO
        assert isinstance(mod.INFO, str) and mod.INFO


def test_housekeeping_defaults_seed_the_same_tasks():
    from src.task_scheduler import HOUSEKEEPING_DEFAULTS

    assert list(HOUSEKEEPING_DEFAULTS)[-len(HOUSEKEEPING):] == list(HOUSEKEEPING)
    for action, defaults in HOUSEKEEPING.items():
        assert HOUSEKEEPING_DEFAULTS[action] == defaults
        mod = importlib.import_module(next(m for a, m, _ in ACTIONS if a == action))
        assert HOUSEKEEPING_DEFAULTS[action] is mod.HOUSEKEEPING_DEFAULT[action]
    # Send Athena joins are created per calendar event, never seeded
    assert "meeting_auto_join" not in HOUSEKEEPING_DEFAULTS


def test_silent_and_model_backed_actions():
    from src.task_scheduler import TaskScheduler

    assert isinstance(TaskScheduler._SILENT_ACTIONS, frozenset)
    assert isinstance(TaskScheduler._MODEL_BACKED_ACTIONS, frozenset)
    assert TaskScheduler._SILENT_ACTIONS - _upstream_class_set("_SILENT_ACTIONS") == SILENT
    assert (TaskScheduler._MODEL_BACKED_ACTIONS - _upstream_class_set("_MODEL_BACKED_ACTIONS")
            == MODEL_BACKED)


def test_scheduler_decisions_per_action(monkeypatch):
    """What the scheduler does with a task of each action: posts to chat or not, waits
    for the model slot or not."""
    from core import database
    from src import assistant_log
    from src.task_scheduler import TaskScheduler

    posted: list[str] = []
    monkeypatch.setattr(assistant_log, "log_to_assistant",
                        lambda owner, text, category=None: posted.append(text))

    class _Stop(Exception):
        pass

    def _no_db():
        raise _Stop

    monkeypatch.setattr(database, "SessionLocal", _no_db)
    scheduler = TaskScheduler.__new__(TaskScheduler)
    table = [a for a, _, _ in ACTIONS] + ["tidy_sessions", "unknown_action"]
    silent = {}
    for action in table:
        task = SimpleNamespace(name="A task", action=action, owner="alex", task_type="action",
                               output_target="session")
        posted.clear()
        with contextlib.suppress(Exception):  # past the silent check, posting is what counts
            scheduler._log_to_assistant(None, task, "done")
        silent[action] = not posted
    assert silent == {
        "barvis_sync_action_items": True, "module_sync_facts": True, "today_insight": True,
        "notes_evidence": True, "belief_structure": True, "meeting_auto_join": False,
        "tidy_sessions": True, "unknown_action": False,
    }

    model = {a: a in TaskScheduler._MODEL_BACKED_ACTIONS for a in table}
    assert model == {
        "barvis_sync_action_items": False, "module_sync_facts": False, "today_insight": True,
        "notes_evidence": True, "belief_structure": True, "meeting_auto_join": False,
        "tidy_sessions": False, "unknown_action": False,
    }
