"""Fork: built-in tasks recover from a broken sealed authority (src/builtin_task_authority.py)."""

import uuid

import pytest

import core.database as database
from core.database import ScheduledTask
from src.agent_runtime.authority import (
    RequestAuthority, bind_request_authority, seal_task_authority,
)
from src.builtin_actions import BUILTIN_ACTIONS
from src.task_scheduler import TaskScheduler
from tests.helpers.database import disposable_database


@pytest.fixture
def db(tmp_path, monkeypatch):
    with disposable_database(tmp_path) as factory:
        monkeypatch.setattr(database, "SessionLocal", factory)
        monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
        calls = []

        async def fake_action(**kwargs):
            calls.append(kwargs["owner"])
            return "ran", True

        monkeypatch.setitem(BUILTIN_ACTIONS, "today_insight", fake_action)
        monkeypatch.setitem(BUILTIN_ACTIONS, "summarize_emails", fake_action)
        session = factory()
        session.calls = calls
        yield session
        session.close()


def _task(db, action="today_insight", *, prompt=None, snapshot="seal", owner="alice"):
    if snapshot == "seal":
        snapshot = seal_task_authority(prompt, "action", action, owner=owner)
    task = ScheduledTask(id=uuid.uuid4().hex[:8], owner=owner, name=action, task_type="action",
                         action=action, prompt=prompt, request_authority_json=snapshot,
                         schedule="cron", cron_expression="*/15 * * * *", status="active")
    db.add(task)
    db.commit()
    return task


async def _run(task):
    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._session_manager = None
    return await scheduler._execute_action(task)


@pytest.mark.asyncio
async def test_valid_snapshot_runs_unchanged(db):
    task = _task(db)
    before = task.request_authority_json
    assert await _run(task) == ("ran", True)
    assert task.request_authority_json == before


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [None, "not json"])
async def test_missing_or_unreadable_snapshot_is_resealed(db, snapshot):
    task = _task(db, snapshot=snapshot)
    assert await _run(task) == ("ran", True)
    assert db.calls == ["alice"]
    db.commit()
    task = db.get(ScheduledTask, task.id)
    assert await _run(task) == ("ran", True)  # the re-sealed snapshot holds


@pytest.mark.asyncio
async def test_snapshot_for_another_prompt_is_resealed(db):
    task = _task(db, snapshot=seal_task_authority("", "action", "today_insight", owner="alice"))
    assert await _run(task) == ("ran", True)


@pytest.mark.asyncio
async def test_email_account_prompt_is_resealed(db):
    task = _task(db, "summarize_emails", prompt='{"account_id": "acc1"}', snapshot=None)
    assert await _run(task) == ("ran", True)


@pytest.mark.asyncio
async def test_other_prompt_stays_denied_with_reason(db):
    task = _task(db, prompt="do something else", snapshot=None)
    result, ok = await _run(task)
    assert not ok and "no stored authority" in result
    assert db.calls == []


@pytest.mark.asyncio
async def test_non_builtin_action_stays_denied(db):
    task = _task(db, "run_script", snapshot=None)
    result, ok = await _run(task)
    assert not ok and "no matching server request authority" in result


@pytest.mark.asyncio
async def test_duplicate_row_stays_denied(db):
    _task(db)
    task = _task(db, snapshot=None)
    result, ok = await _run(task)
    assert not ok and "no stored authority" in result


@pytest.mark.asyncio
async def test_policy_denial_is_not_overridden(db, monkeypatch):
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: ["scheduled__today_insight"]
                        if key == "disabled_tools" else default)
    task = _task(db)
    result, ok = await _run(task)
    assert not ok and "denied by policy" in result


@pytest.mark.asyncio
async def test_no_heal_inside_a_request(db):
    task = _task(db, snapshot=None)
    with bind_request_authority(RequestAuthority.empty(owner="alice")):
        result, ok = await _run(task)
    assert not ok and db.calls == []


@pytest.mark.asyncio
async def test_other_owner_snapshot_is_resealed(db):
    task = _task(db, snapshot=seal_task_authority(None, "action", "today_insight", owner="bob"))
    assert await _run(task) == ("ran", True)
