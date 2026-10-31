"""Fork: the brain's per-message tool history (fork/brain/history.py, data/brain.db)."""
from __future__ import annotations

import sqlite3

import pytest

from fork.brain import history
from tests.fork.brain_fakes import Brain, calls, text

SEARCH = "mcp__convo__search_transcripts"


def test_turns_round_trip_in_wal(data_dir):
    exchange = [{"role": "assistant", "content": "", "tool_calls": []},
                {"role": "tool", "tool_call_id": "c", "content": "found"}]
    history.save_turn("chat-1", "m2", exchange, owner="alice")
    assert history.turns("chat-1") == {"m2": exchange}
    assert history.turns("chat-2") == {}
    db = sqlite3.connect(str(data_dir / "brain.db"))
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_rows_of_deleted_messages_are_dropped_on_read(data_dir):
    history.save_turn("chat-1", "m2", [{"role": "tool", "content": "a"}])
    history.save_turn("chat-1", "m4", [{"role": "tool", "content": "b"}])
    assert set(history.turns("chat-1", keep_ids=["m4"])) == {"m4"}
    assert set(history.turns("chat-1")) == {"m4"}


def test_sweep_deletes_the_rows_of_deleted_chats(data_dir):
    history.save_turn("chat-1", "m2", [{"role": "tool", "content": "a"}])
    history.save_turn("chat-2", "m2", [{"role": "tool", "content": "b"}])
    history.save_summary("chat-1", "m2", "summary")
    assert history.sweep(force=True, live=lambda ids: {"chat-2"}) == 1
    assert history.turns("chat-1") == {} and history.summary("chat-1") is None
    assert set(history.turns("chat-2")) == {"m2"}


def test_sweep_never_breaks_a_turn(data_dir):
    history.save_turn("chat-1", "m2", [{"role": "tool", "content": "a"}])

    def broken(ids):
        raise RuntimeError("db down")

    assert history.sweep(force=True, live=broken) == 0
    assert set(history.turns("chat-1")) == {"m2"}


@pytest.fixture
def brain(monkeypatch, data_dir):
    return Brain(monkeypatch, data_dir)


async def test_deleting_a_chat_deletes_what_follow_ups_would_see(brain):
    sess = brain.session()
    brain.llm.rounds([calls((SEARCH, {"query": "budget"}))], [text("Alice approved it.")])
    await brain.run(sess, "What did Alice say about the budget?")
    assert history.stored_chats() == {sess.id}
    history.sweep(force=True, live=lambda ids: set())  # the chat was deleted upstream
    assert history.stored_chats() == set()
