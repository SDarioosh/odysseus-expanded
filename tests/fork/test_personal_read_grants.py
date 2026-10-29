"""Read-only grants for the owner's personal stores (src/personal_read_grants.py)."""
import pytest

from src.agent_runtime.authority import ExactOperation, create_request_authority
from src.personal_read_grants import with_personal_store_reads
from src.tool_policy import ToolPolicy


def op(tool, content):
    return ExactOperation.normalize(tool, content)


def granted(message="I imported it. Do you see them?", **kwargs):
    base = create_request_authority(message, owner="alice", session_id="s", **kwargs)
    return base, with_personal_store_reads(base)


@pytest.mark.parametrize("tool,content", [
    ("manage_contact", '{"action": "list"}'),
    ("manage_contact", '{"action": "search", "name": "Katherine Moss"}'),
    ("manage_notes", '{"action": "search", "query": "groceries"}'),
    ("manage_calendar", '{"action": "list_events"}'),
    ("manage_tasks", '{"action": "list"}'),
    ("manage_memory", '{"action": "search", "query": "x"}'),
])
def test_reads_are_granted_without_naming_the_store(tool, content):
    base, widened = granted()
    assert not base.permits(op(tool, content))
    assert widened.permits(op(tool, content))


@pytest.mark.parametrize("tool,content", [
    ("manage_contact", '{"action": "add", "name": "x"}'),
    ("manage_contact", '{"action": "delete", "uid": "1"}'),
    ("manage_notes", '{"action": "delete", "id": "1"}'),
    ("manage_calendar", '{"action": "create_event", "summary": "x"}'),
    ("manage_tasks", '{"action": "run", "id": "1"}'),
    ("manage_memory", '{"action": "add", "text": "x"}'),
    ("bash", "ls"),
    ("web_fetch", '{"url": "https://example.com"}'),
])
def test_writes_and_other_tools_are_not_granted(tool, content):
    _, widened = granted()
    assert not widened.permits(op(tool, content))


def test_existing_write_grant_from_the_words_is_kept():
    base, widened = granted("Add Katherine Moss to my contacts")
    add = op("manage_contact", '{"action": "add", "name": "Katherine Moss"}')
    assert base.permits(add) and widened.permits(add)


def test_policy_and_disabled_tools_still_win():
    _, widened = granted()
    assert not widened.restrict(disabled_tools={"manage_contact"}).permits(
        op("manage_contact", '{"action": "list"}'))
    assert not widened.restrict(ToolPolicy(block_all_tool_calls=True)).permits(
        op("manage_contact", '{"action": "list"}'))
    blocked = create_request_authority("hi", owner="alice", session_id="s").restrict(
        disabled_tools={"manage_contact"})
    assert not with_personal_store_reads(blocked).permits(op("manage_contact", '{"action": "list"}'))


def test_env_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_PERSONAL_READ_GRANTS", "0")
    base, widened = granted()
    assert widened is base


def test_notes_read_gets_its_owned_scope():
    _, widened = granted()
    assert "notes" in {s.namespace for s in widened.owned_scopes}
