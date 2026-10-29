"""The fork's built-in scheduled tasks, in one table, and the hooks that register them.

Each row names an action id (stored in the owner's ``scheduled_tasks.action``: never
change one), the module and function that run it, and how upstream's scheduler treats
it: seeded as a housekeeping task (the module's ``HOUSEKEEPING_DEFAULT``), silent (posts
nothing to the assistant chat) and model-backed (waits for the local model slot).

Upstream calls in twice (FORK.md):

- end of ``src/builtin_actions.py``: :func:`register_actions` (handler and description,
  the module's ``INFO``);
- end of ``src/task_scheduler.py``: :func:`register_scheduler` (``HOUSEKEEPING_DEFAULTS``,
  ``TaskScheduler._SILENT_ACTIONS``, ``TaskScheduler._MODEL_BACKED_ACTIONS``).

Modules are imported when a hook runs, in table order, as the separate hooks did.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ForkTask:
    action: str
    module: str
    handler: str
    housekeeping: bool = False
    silent: bool = False
    model_backed: bool = False


TASKS: tuple[ForkTask, ...] = (
    # Module Action Items: Athena's to-dos and notes filed into the hub
    ForkTask("barvis_sync_action_items", "fork.athena.action_items", "action_sync_action_items",
             housekeeping=True, silent=True),
    # Module Facts: Athena's facts into the belief ledger
    ForkTask("module_sync_facts", "fork.athena.facts", "action_sync_facts",
             housekeeping=True, silent=True),
    # Today Insight
    ForkTask("today_insight", "fork.insight.today", "action_today_insight",
             housekeeping=True, silent=True, model_backed=True),
    # Notes Evidence: the owner's notes into the belief ledger
    ForkTask("notes_evidence", "fork.beliefs.notes_evidence", "action_notes_evidence",
             housekeeping=True, silent=True, model_backed=True),
    # Belief Structure
    ForkTask("belief_structure", "fork.beliefs.structure", "action_belief_structure",
             housekeeping=True, silent=True, model_backed=True),
    # Send Athena to toggled calendar meetings (one task per event, never seeded)
    ForkTask("meeting_auto_join", "fork.athena.meeting_auto_join", "action_meeting_auto_join"),
)


def _module(task: ForkTask) -> Any:
    return importlib.import_module(task.module)


def register_actions(actions: dict, info: dict) -> None:
    """``BUILTIN_ACTIONS`` / ``BUILTIN_ACTION_INFO`` entries for every task."""
    for task in TASKS:
        mod = _module(task)
        actions[task.action] = getattr(mod, task.handler)
        info[task.action] = mod.INFO


def register_housekeeping(defaults: dict) -> None:
    """``HOUSEKEEPING_DEFAULTS`` entries for the seeded tasks."""
    for task in TASKS:
        if task.housekeeping:
            defaults.update(_module(task).HOUSEKEEPING_DEFAULT)


def register_scheduler(defaults: dict, scheduler_cls: Any) -> None:
    """The scheduler hook: seeded tasks, then the silent and model-backed sets."""
    register_housekeeping(defaults)
    scheduler_cls._SILENT_ACTIONS = scheduler_cls._SILENT_ACTIONS | {
        t.action for t in TASKS if t.silent}
    scheduler_cls._MODEL_BACKED_ACTIONS = scheduler_cls._MODEL_BACKED_ACTIONS | {
        t.action for t in TASKS if t.model_backed}
