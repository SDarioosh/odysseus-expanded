"""Fork: the brain's tool calls and trimmed results per chat message (``data/brain.db``).

Upstream's session format is not changed. For each assistant message the brain saves
(by its database id), the turn's tool exchange is kept here, in the order it happened:
the assistant tool-call messages and the trimmed tool results, as OpenAI chat messages.
The next turn's prompt (``fork/brain/prompt.py``) puts them back before that answer, so a
follow-up ("and the second result?") sees what the earlier turn found.

Also kept: one summary per chat of its older turns (``summaries``), written once when a
chat outgrows its context budget (``fork/brain/turn.py``).

Deleting a chat deletes its rows lazily, without a hook in upstream's session delete:
:func:`sweep` drops the rows of every chat that no longer exists in upstream's chat
database. It runs at most every :data:`SWEEP_SECONDS` when a brain turn starts, and the
rows of a deleted message are skipped (and dropped) the next time its chat is read.

Storage follows ``fork/beliefs/ledger_db.py``: stdlib sqlite3, WAL, one transaction per
:func:`_connect`, one process-wide write lock.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DB_NAME = "brain.db"
SWEEP_SECONDS = 3600.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    messages TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, message_id)
);
CREATE TABLE IF NOT EXISTS summaries (
    session_id TEXT PRIMARY KEY,
    upto_message_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

_WRITE = threading.Lock()
_last_sweep = 0.0


def db_path() -> Path:
    from src.constants import DATA_DIR

    return Path(DATA_DIR) / DB_NAME


@contextmanager
def _connect(write: bool = False) -> Iterator[sqlite3.Connection]:
    """One transaction on brain.db (``BEGIN IMMEDIATE`` for writes)."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    finally:
        db.close()


def save_turn(session_id: str, message_id: str, messages: list[dict], *,
              owner: str | None = None) -> None:
    """Keep a turn's tool exchange (``messages``) under its saved answer's id."""
    if not session_id or not message_id or not messages:
        return
    with _WRITE, _connect(write=True) as db:
        db.execute(
            "INSERT OR REPLACE INTO turns (session_id, message_id, owner, messages, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (str(session_id), str(message_id), str(owner or ""),
             json.dumps(messages, ensure_ascii=False), time.time()))


def turns(session_id: str, keep_ids: Iterable[str] | None = None) -> dict[str, list[dict]]:
    """``{message id: tool exchange}`` of a chat. With ``keep_ids`` (the chat's current
    message ids), rows of messages no longer in the chat are dropped."""
    with _connect() as db:
        rows = db.execute("SELECT message_id, messages FROM turns WHERE session_id = ?",
                          (str(session_id),)).fetchall()
    out = {}
    for row in rows:
        try:
            out[row["message_id"]] = json.loads(row["messages"])
        except ValueError:
            continue
    if keep_ids is not None:
        keep = {str(i) for i in keep_ids}
        gone = [m for m in out if m not in keep]
        if gone:
            with _WRITE, _connect(write=True) as db:
                db.executemany("DELETE FROM turns WHERE session_id = ? AND message_id = ?",
                               [(str(session_id), m) for m in gone])
            for m in gone:
                out.pop(m, None)
    return out


def summary(session_id: str) -> tuple[str, str] | None:
    """``(upto_message_id, text)`` of a chat's summary of older turns, or None."""
    with _connect() as db:
        row = db.execute("SELECT upto_message_id, text FROM summaries WHERE session_id = ?",
                         (str(session_id),)).fetchone()
    return (row["upto_message_id"], row["text"]) if row else None


def save_summary(session_id: str, upto_message_id: str, text: str) -> None:
    with _WRITE, _connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO summaries (session_id, upto_message_id, text, "
                   "created_at) VALUES (?, ?, ?, ?)",
                   (str(session_id), str(upto_message_id), text, time.time()))


def delete_chat(session_id: str) -> None:
    """Drop every row of a chat."""
    with _WRITE, _connect(write=True) as db:
        db.execute("DELETE FROM turns WHERE session_id = ?", (str(session_id),))
        db.execute("DELETE FROM summaries WHERE session_id = ?", (str(session_id),))


def stored_chats() -> set[str]:
    with _connect() as db:
        rows = db.execute("SELECT session_id FROM turns UNION SELECT session_id FROM summaries")
        return {r[0] for r in rows.fetchall()}


def _live_chats(ids: set[str]) -> set[str]:
    """The ids among ``ids`` that still exist in upstream's chat database."""
    from core.database import Session as DbSession
    from core.database import SessionLocal

    db = SessionLocal()
    try:
        found = db.query(DbSession.id).filter(DbSession.id.in_(list(ids))).all()
        return {str(r[0]) for r in found}
    finally:
        db.close()


def sweep(*, force: bool = False, live=None) -> int:
    """Drop the rows of deleted chats; returns how many chats were dropped. ``live``:
    the function that says which ids still exist (tests pass their own)."""
    global _last_sweep
    now = time.monotonic()
    if not force and now - _last_sweep < SWEEP_SECONDS:
        return 0
    _last_sweep = now
    try:
        ids = stored_chats()
        if not ids:
            return 0
        alive = (live or _live_chats)(ids)
        gone = ids - set(alive)
        for sid in gone:
            delete_chat(sid)
        return len(gone)
    except Exception:  # noqa: BLE001 -- a sweep never breaks a turn
        logger.warning("brain history: sweep failed", exc_info=True)
        return 0
