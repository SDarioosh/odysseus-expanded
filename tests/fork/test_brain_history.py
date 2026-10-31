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


def _age(data_dir, seconds):
    db = sqlite3.connect(str(data_dir / "brain.db"))
    with db:
        db.execute("UPDATE turns SET created_at = created_at - ?", (seconds,))
        db.execute("UPDATE summaries SET created_at = created_at - ?", (seconds,))
    db.close()


def test_rows_older_than_thirty_days_are_never_read_and_the_sweep_deletes_them(data_dir):
    history.save_turn("chat-1", "m2", [{"role": "tool", "content": "old"}])
    history.save_summary("chat-1", "m2", "an old summary")
    _age(data_dir, history.MAX_AGE_SECONDS + 60)
    history.save_turn("chat-1", "m4", [{"role": "tool", "content": "new"}])
    assert set(history.turns("chat-1")) == {"m4"}
    assert history.summary("chat-1") is None
    history.sweep(force=True, live=lambda ids: set(ids))
    db = sqlite3.connect(str(data_dir / "brain.db"))
    assert [r[0] for r in db.execute("SELECT message_id FROM turns")] == ["m4"]
    assert db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0


def test_taint_and_search_hits_are_kept_per_exchange(data_dir):
    history.save_turn("chat-1", "m2", [{"role": "tool", "content": "page"}], tainted=True,
                      search_hits=["https://news.example.org/a"])
    history.save_turn("chat-1", "m4", [{"role": "tool", "content": "sunny"}])
    rows = history.exchanges("chat-1")
    assert rows["m2"]["tainted"] is True
    assert rows["m2"]["search_hits"] == ["https://news.example.org/a"]
    assert rows["m4"]["tainted"] is False and rows["m4"]["search_hits"] == []


def test_a_brain_db_of_the_first_version_gets_the_new_columns(data_dir):
    db = sqlite3.connect(str(data_dir / "brain.db"))
    db.executescript("""CREATE TABLE turns (session_id TEXT NOT NULL, message_id TEXT NOT NULL,
        owner TEXT NOT NULL DEFAULT '', messages TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY (session_id, message_id));""")
    db.execute("INSERT INTO turns VALUES ('chat-1', 'm2', 'alice', '[]', strftime('%s','now'))")
    db.commit()
    db.close()
    assert history.exchanges("chat-1")["m2"]["tainted"] is False
    history.save_turn("chat-1", "m4", [{"role": "tool", "content": "x"}], tainted=True)
    assert history.exchanges("chat-1")["m4"]["tainted"] is True


def test_the_first_request_after_a_start_sweeps(data_dir, monkeypatch):
    monkeypatch.setattr(history, "_last_sweep", None)
    assert history.sweep_due() is False  # no brain.db yet: nothing to sweep
    history.save_turn("chat-1", "m2", [{"role": "tool", "content": "a"}])
    assert history.sweep_due() is True
    assert history.sweep(live=lambda ids: set()) == 1
    assert history.sweep_due() is False  # then at most hourly
    assert history.sweep(live=lambda ids: set()) == 0


async def test_the_hook_sweeps_in_a_worker_thread_even_with_the_brain_off(data_dir, monkeypatch):
    import asyncio
    import threading

    from fork.brain import turn as brain_turn

    monkeypatch.setenv("FORK_BRAIN", "off")
    monkeypatch.setattr(history, "_last_sweep", None)
    history.save_turn("chat-gone", "m2", [{"role": "tool", "content": "a"}])
    threads = []
    real = history.sweep

    def sweep(**kw):
        threads.append(threading.get_ident())
        return real(live=lambda ids: set(), **kw)

    monkeypatch.setattr(history, "sweep", sweep)
    assert await brain_turn.brain_response(object(), sess=None, session_id="s", message="hi",
                                           owner="alice") is None
    for _ in range(100):
        if threads and not brain_turn._SWEEPS:
            break
        await asyncio.sleep(0.01)
    assert threads and threads[0] != threading.get_ident()
    assert history.stored_chats() == set()
