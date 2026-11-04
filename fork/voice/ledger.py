"""Fork: the voice turn ledger (docs/fork/voice-assistant.md, "Turns and actions").

One record per voice turn, and the changes a turn made, in one small SQLite file
(``<DATA_DIR>/fork_voice.db``, stdlib sqlite3 like ``fork/brain/history.py``). It is the
one place a voice turn's facts live: what was asked and answered, its links, the device
and speaker it came from, a confirmation question still open, and its actions. The
device's screen reads it through ``GET …/actions`` and ``GET …/turns/latest``
(``fork/voice/routes.py``); later features add to these records here.

Rows older than :data:`KEEP_SECONDS` (7 days) are deleted on every write. Everything is
owner-scoped: a reader only ever gets its own owner's rows.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_NAME = "fork_voice.db"
KEEP_SECONDS = 7 * 24 * 3600
DEFAULT_LIMIT = 8
MAX_LIMIT = 50

ACTIONS = frozenset({"created", "completed"})
KINDS = frozenset({"todo", "reminder", "list_item", "note", "event", "action_item"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    question TEXT NOT NULL DEFAULT '',
    answer TEXT,
    links TEXT NOT NULL DEFAULT '[]',
    awaiting_confirmation TEXT,
    device TEXT,
    speaker TEXT,
    session_id TEXT
);
CREATE INDEX IF NOT EXISTS turns_owner_started ON turns (owner, started_at);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    at REAL NOT NULL,
    action TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    detail TEXT,
    due TEXT,
    all_day INTEGER NOT NULL DEFAULT 0,
    place TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'odysseus'
);
CREATE INDEX IF NOT EXISTS actions_owner_at ON actions (owner, at);
CREATE INDEX IF NOT EXISTS actions_turn ON actions (turn_id);
"""


def db_path() -> Path:
    from src.constants import DATA_DIR

    return Path(DATA_DIR) / DB_NAME


@contextmanager
def _connect(write: bool = False) -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)
        if write:
            db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            if write:
                db.execute("ROLLBACK")
            raise
        if write:
            db.execute("COMMIT")
    finally:
        db.close()


def _owner(owner: Any) -> str:
    return str(owner or "").strip().casefold()


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")


def _prune(db: sqlite3.Connection, now: float) -> None:
    cut = now - KEEP_SECONDS
    db.execute("DELETE FROM actions WHERE at < ?", (cut,))
    db.execute("DELETE FROM turns WHERE started_at < ?", (cut,))


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def start_turn(owner: Any, question: str, *, session_id: str | None = None,
               device: dict | None = None, speaker: dict | None = None,
               now: float | None = None) -> str:
    """A new turn record; returns its id. Finished by :func:`finish_turn`."""
    now = time.time() if now is None else float(now)
    turn_id = uuid.uuid4().hex
    with _connect(write=True) as db:
        _prune(db, now)
        db.execute(
            "INSERT INTO turns (id, owner, started_at, question, device, speaker, session_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (turn_id, _owner(owner), now, str(question or ""), _json(device),
             _json(speaker), session_id))
    return turn_id


def finish_turn(turn_id: str, *, answer: str, links: list | None = None,
                awaiting_confirmation: str | None = None, now: float | None = None) -> None:
    """The turn's answer, its links (urls, or ``{"url", "label"}``) and an open
    confirmation question."""
    now = time.time() if now is None else float(now)
    rows = []
    for link in links or ():
        if isinstance(link, dict) and link.get("url"):
            rows.append({"url": str(link["url"]), "label": link.get("label") or None})
        elif isinstance(link, str) and link:
            rows.append({"url": link, "label": None})
    with _connect(write=True) as db:
        db.execute(
            "UPDATE turns SET ended_at = ?, answer = ?, links = ?, awaiting_confirmation = ?"
            " WHERE id = ?",
            (now, str(answer or ""), json.dumps(rows, ensure_ascii=False),
             awaiting_confirmation or None, turn_id))


def add_action(turn_id: str, owner: Any, *, action: str, kind: str, title: str,
               where: str, detail: str | None = None, due: str | None = None,
               all_day: bool = False, source: str = "odysseus",
               now: float | None = None) -> dict:
    """Record one change a voice turn made; returns it as the feeds show it."""
    if action not in ACTIONS or kind not in KINDS:
        raise ValueError(f"unknown action {action!r} / kind {kind!r}")
    now = time.time() if now is None else float(now)
    action_id = uuid.uuid4().hex
    with _connect(write=True) as db:
        _prune(db, now)
        db.execute(
            "INSERT INTO actions (id, turn_id, owner, at, action, kind, title, detail, due,"
            " all_day, place, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (action_id, turn_id, _owner(owner), now, action, kind, str(title or ""),
             detail, due, 1 if all_day else 0, str(where or ""), source))
        row = db.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    return _action(row)


def _action(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "at": iso(row["at"]), "action": row["action"],
            "kind": row["kind"], "title": row["title"], "detail": row["detail"],
            "due": row["due"], "all_day": bool(row["all_day"]), "where": row["place"],
            "source": row["source"], "turn_id": row["turn_id"]}


def _since(since_ms: Any) -> float:
    try:
        return max(0.0, float(since_ms) / 1000.0)
    except (TypeError, ValueError):
        return 0.0


def actions(owner: Any, *, since_ms: Any = 0, limit: Any = DEFAULT_LIMIT) -> list[dict]:
    """This owner's voice actions at or after ``since_ms`` (epoch ms), newest first."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = DEFAULT_LIMIT
    n = max(1, min(MAX_LIMIT, n))
    with _connect() as db:
        rows = db.execute(
            "SELECT * FROM actions WHERE owner = ? AND at >= ? ORDER BY at DESC, rowid DESC"
            " LIMIT ?", (_owner(owner), _since(since_ms), n)).fetchall()
    return [_action(r) for r in rows]


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def latest_turn(owner: Any, *, since_ms: Any = 0) -> dict | None:
    """The newest finished turn of this owner started at or after ``since_ms``."""
    with _connect() as db:
        row = db.execute(
            "SELECT * FROM turns WHERE owner = ? AND started_at >= ? AND ended_at IS NOT NULL"
            " ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (_owner(owner), _since(since_ms))).fetchone()
        if row is None:
            return None
        acts = db.execute("SELECT * FROM actions WHERE turn_id = ? ORDER BY at DESC, rowid DESC",
                          (row["id"],)).fetchall()
    return {
        "id": row["id"], "started_at": iso(row["started_at"]), "ended_at": iso(row["ended_at"]),
        "question": row["question"], "answer": row["answer"] or "",
        "links": _loads(row["links"], []), "actions": [_action(a) for a in acts],
        "awaiting_confirmation": row["awaiting_confirmation"],
        "device": _loads(row["device"], None), "speaker": _loads(row["speaker"], None),
        "session_id": row["session_id"],
    }
