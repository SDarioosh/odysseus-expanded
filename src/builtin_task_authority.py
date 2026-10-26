"""Fork: keep built-in housekeeping tasks runnable when their sealed authority breaks.

Every scheduled action runs only with the request authority sealed into its row
(``scheduled_tasks.request_authority_json``, ``src/agent_runtime/authority.py``). A
built-in task (``HOUSEKEEPING_DEFAULTS``: Today Insight, Module Action Items, Email
Summary, ...) gets that snapshot from the server when it is seeded. When the snapshot is
missing, unreadable, sealed for another owner or for a different prompt/action, or grants
nothing, every run fails with "Scheduled action has no matching server request authority"
and the owner gets an error notification every few minutes.

The server would seal exactly the same snapshot again for the canonical built-in row, so
``recover`` re-seals it there, under the same rules the seeding uses
(``seal_task_authority`` with no active request authority), and logs why. It never widens
anything else:

* only ``task_type == "action"`` rows whose action is a built-in housekeeping action;
* only the owner's single row for that action (a second, model-made copy stays denied);
* only an empty prompt, or the ``{"account_id": ...}`` the task page stores for email
  actions;
* never while a request authority is active (a model turn can't heal its own task);
* never when the action is denied by policy (``disabled_tools``): that stays a failure.

Called from the hook in ``TaskScheduler._execute_action`` (``src/task_scheduler.py``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def problem(snapshot: Any, prompt: Any, task_type: Any, action: Any, owner: Any) -> str:
    """Why a stored snapshot does not admit the task, in a few words (for the run log);
    "" when it does (a denied run then comes from policy, ``disabled_tools``)."""
    from src.agent_runtime.authority import RequestAuthority, _owner, task_operation

    if not snapshot:
        return "no stored authority"
    try:
        value = json.loads(snapshot)
        if value["task_input"] != [prompt, task_type, action]:
            return "task changed after its authority was stored"
        authority = RequestAuthority.from_dict(value["authority"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return "stored authority is unreadable"
    if authority.owner != _owner(owner):
        return "stored authority belongs to another owner"
    operation = task_operation(task_type, action, prompt)
    if operation is None or not any(g.permits(operation) for g in authority.grants):
        return "stored authority grants nothing for this action"
    return ""


def _plain_prompt(prompt: Any) -> bool:
    if not prompt:
        return True
    try:
        value = json.loads(prompt)
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and set(value) <= {"account_id"}


def _only_row(task: Any) -> bool:
    from core.database import ScheduledTask, SessionLocal

    db = SessionLocal()
    try:
        return db.query(ScheduledTask).filter(
            ScheduledTask.owner == task.owner,
            ScheduledTask.task_type == "action",
            ScheduledTask.action == task.action,
        ).count() == 1
    finally:
        db.close()


def healable(task: Any) -> bool:
    from src.agent_runtime.authority import active_request_authority
    from src.task_scheduler import HOUSEKEEPING_DEFAULTS

    return ((task.task_type or "") == "action"
            and (task.action or "") in HOUSEKEEPING_DEFAULTS
            and _plain_prompt(task.prompt)
            and active_request_authority() is None
            and _only_row(task))


def recover(task: Any, disabled_tools: Any = ()) -> tuple[Any | None, str]:
    """(authority, reason). ``authority`` is a re-sealed, restricted authority that admits
    the task (the row's ``request_authority_json`` is updated; the caller commits), or
    None when the task stays denied; ``reason`` says what was wrong."""
    from src.agent_runtime.authority import (
        restore_task_authority, seal_task_authority, task_operation,
    )

    reason = problem(getattr(task, "request_authority_json", None), task.prompt,
                     task.task_type, task.action, task.owner)
    if not reason:
        return None, "action denied by policy"
    try:
        if not healable(task):
            return None, reason
        snapshot = seal_task_authority(task.prompt, task.task_type, task.action,
                                       owner=task.owner)
        authority = restore_task_authority(snapshot, task.prompt, task.task_type,
                                           task.action, owner=task.owner)
        authority = authority.restrict(disabled_tools=disabled_tools or ())
        operation = task_operation(task.task_type, task.action, task.prompt)
        if operation is None or not authority.permits(operation):
            return None, reason
    except Exception as e:  # noqa: BLE001 -- the run reports the original reason
        logger.warning("built-in task %s: re-sealing its authority failed: %s", task.action, e)
        return None, reason
    task.request_authority_json = snapshot
    logger.warning("built-in task %r (%s, owner %r): %s; re-sealed its authority",
                   task.name, task.action, task.owner, reason)
    return authority, reason
