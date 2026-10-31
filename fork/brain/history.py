"""Fork: the brain's tool calls and trimmed results per chat message (``data/brain.db``).

Upstream's session format is not changed. For each assistant message the brain saves
(by its database id), the turn's tool exchange is kept here, in the order it happened:
the assistant tool-call messages and the trimmed tool results, as OpenAI chat messages.
The next turn's prompt (``fork/brain/prompt.py``) puts them back before that answer, so a
follow-up ("and the second result?") sees what the earlier turn found.

Also kept: one summary per chat of its older turns (``summaries``), written once when a
chat outgrows its context budget (``fork/brain/turn.py``).

Each exchange row also keeps whether a tool result in it was untrusted (``tainted``:
web pages, email, notes, Athena, anything upstream's ``tool_result_should_arm_gate``
counts as outside content) and the ranked web-search hits it returned (``search_hits``),
so the next turn re-arms upstream's approval gate and may fetch those pages.

Retention. Rows don't outlive their chat and don't keep private data for long:

- deleting a chat deletes its rows lazily, without a hook in upstream's session delete:
  :func:`sweep` drops the rows of every chat that no longer exists in upstream's chat
  database. It runs on the first chat request after a start (the brain's hook calls
  :func:`sweep_due` whether ``FORK_BRAIN`` is on or not, once ``brain.db`` exists), then at
  most every :data:`SWEEP_SECONDS`, in a worker thread;
- rows older than :data:`MAX_AGE_SECONDS` (30 days) are never read and the sweep deletes
  them, summaries included;
- the rows of a deleted message are skipped (and dropped) the next time its chat is read.

- the hub's Forget (``fork/beliefs/forget.py``) calls :func:`forget_texts`, which drops
  every exchange and summary of that owner that contains the forgotten fact's words or
  the transcript line it was said on.

Not covered: Athena's own transcript retention (``CONVO_TRANSCRIPT_RETENTION_DAYS``); a
stored exchange that quoted an expired transcript line keeps it until the row expires.

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
MAX_AGE_SECONDS = 30 * 24 * 3600.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    messages TEXT NOT NULL,
    created_at REAL NOT NULL,
    tainted INTEGER NOT NULL DEFAULT 0,
    search_hits TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (session_id, message_id)
);
CREATE TABLE IF NOT EXISTS summaries (
    session_id TEXT PRIMARY KEY,
    upto_message_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

# columns added after the first version: a brain.db written by it gets them on open
_ADDED_COLUMNS = (("tainted", "INTEGER NOT NULL DEFAULT 0"),
                  ("search_hits", "TEXT NOT NULL DEFAULT '[]'"))

_WRITE = threading.RLock()  # re-entrant: _connect may add columns under it
_last_sweep: float | None = None  # None: not swept since this process started


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
        have = {r[1] for r in db.execute("PRAGMA table_info(turns)").fetchall()}
        for name, decl in _ADDED_COLUMNS:
            if name not in have:
                with _WRITE:
                    try:
                        db.execute(f"ALTER TABLE turns ADD COLUMN {name} {decl}")
                    except sqlite3.OperationalError:  # another connection added it
                        pass
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
              owner: str | None = None, tainted: bool = False,
              search_hits: Iterable[str] = ()) -> None:
    """Keep a turn's tool exchange (``messages``) under its saved answer's id, with
    whether a result in it was untrusted and the web-search hits it returned."""
    if not session_id or not message_id or not messages:
        return
    with _WRITE, _connect(write=True) as db:
        db.execute(
            "INSERT OR REPLACE INTO turns (session_id, message_id, owner, messages, created_at,"
            " tainted, search_hits) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(session_id), str(message_id), str(owner or ""),
             json.dumps(messages, ensure_ascii=False), time.time(), 1 if tainted else 0,
             json.dumps(sorted({str(u) for u in search_hits or ()}))))


def _cutoff(now: float | None = None) -> float:
    return (time.time() if now is None else now) - MAX_AGE_SECONDS


def exchanges(session_id: str, keep_ids: Iterable[str] | None = None) -> dict[str, dict]:
    """``{message id: {"messages", "tainted", "search_hits"}}`` of a chat, rows older
    than :data:`MAX_AGE_SECONDS` left out. With ``keep_ids`` (the chat's current message
    ids), rows of messages no longer in the chat are dropped."""
    with _connect() as db:
        rows = db.execute("SELECT message_id, messages, tainted, search_hits FROM turns"
                          " WHERE session_id = ? AND created_at >= ?",
                          (str(session_id), _cutoff())).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        try:
            messages = json.loads(row["messages"])
            hits = json.loads(row["search_hits"] or "[]")
        except ValueError:
            continue
        out[row["message_id"]] = {"messages": messages, "tainted": bool(row["tainted"]),
                                  "search_hits": [str(u) for u in hits or ()]}
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


def turns(session_id: str, keep_ids: Iterable[str] | None = None) -> dict[str, list[dict]]:
    """``{message id: tool exchange}`` of a chat (:func:`exchanges` without the flags)."""
    return {k: v["messages"] for k, v in exchanges(session_id, keep_ids).items()}


def summary(session_id: str) -> tuple[str, str] | None:
    """``(upto_message_id, text)`` of a chat's summary of older turns, or None."""
    with _connect() as db:
        row = db.execute("SELECT upto_message_id, text FROM summaries WHERE session_id = ?"
                         " AND created_at >= ?", (str(session_id), _cutoff())).fetchone()
    return (row["upto_message_id"], row["text"]) if row else None


def save_summary(session_id: str, upto_message_id: str, text: str) -> None:
    with _WRITE, _connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO summaries (session_id, upto_message_id, text, "
                   "created_at) VALUES (?, ?, ?, ?)",
                   (str(session_id), str(upto_message_id), text, time.time()))


def _plain(text: str) -> str:
    """Lowercased words only, so a fact matches however the JSON quoted it."""
    import re

    return " ".join(re.sub(r"[^\w ]+", " ", str(text or "").lower()).split())


def _exchange_text(raw: str) -> str:
    """The words of a stored exchange: its messages' texts and tool-call arguments,
    decoded (in the stored JSON a line break is ``\\n``, which would glue an ``n`` onto
    the next word and hide a fact split across lines)."""
    try:
        messages = json.loads(raw or "[]")
    except ValueError:
        return str(raw or "")
    parts: list[str] = []

    def add(value) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                add(item.get("text") if isinstance(item, dict) else item)

    for m in messages if isinstance(messages, list) else [messages]:
        if not isinstance(m, dict):
            add(m)
            continue
        add(m.get("content"))
        for call in m.get("tool_calls") or ():
            if isinstance(call, dict):
                add((call.get("function") or {}).get("arguments"))
    return "\n".join(parts)


def forget_texts(owner: str | None, texts: Iterable[str]) -> int:
    """Drop this owner's stored exchanges, and the summaries of their chats, whose
    words contain one of ``texts`` (a forgotten fact, the line it was said on). Texts
    shorter than three words are ignored: too likely to match unrelated turns. Returns how
    many exchanges were dropped."""
    wanted = [t for t in (_plain(x) for x in texts) if len(t.split()) >= 3]
    if not wanted or not db_path().exists():
        return 0
    owner = owner or ""
    with _WRITE, _connect(write=True) as db:
        rows = db.execute("SELECT session_id, message_id, messages FROM turns WHERE owner = ?",
                          (owner,)).fetchall()
        hit = [(r["session_id"], r["message_id"]) for r in rows
               if any(w in _plain(_exchange_text(r["messages"])) for w in wanted)]
        for session_id, message_id in hit:
            db.execute("DELETE FROM turns WHERE session_id = ? AND message_id = ?",
                       (session_id, message_id))
        owned = {r["session_id"] for r in rows}
        for row in db.execute("SELECT session_id, text FROM summaries").fetchall():
            if row["session_id"] in owned and any(w in _plain(row["text"]) for w in wanted):
                db.execute("DELETE FROM summaries WHERE session_id = ?", (row["session_id"],))
    return len(hit)


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


def expire(now: float | None = None) -> int:
    """Delete the rows older than :data:`MAX_AGE_SECONDS`; returns how many."""
    cutoff = _cutoff(now)
    with _WRITE, _connect(write=True) as db:
        n = db.execute("DELETE FROM turns WHERE created_at < ?", (cutoff,)).rowcount
        n += db.execute("DELETE FROM summaries WHERE created_at < ?", (cutoff,)).rowcount
    return int(n or 0)


def sweep_due() -> bool:
    """True when :func:`sweep` would run now: ``brain.db`` exists and it hasn't run since
    this process started, or not for :data:`SWEEP_SECONDS`."""
    if _last_sweep is not None and time.monotonic() - _last_sweep < SWEEP_SECONDS:
        return False
    try:
        return db_path().exists()
    except Exception:  # noqa: BLE001
        return False


def sweep(*, force: bool = False, live=None) -> int:
    """Expire old rows and drop the rows of deleted chats; returns how many chats were
    dropped. ``live``: the function that says which ids still exist (tests pass their
    own). Blocking: call it from a worker thread (``asyncio.to_thread``)."""
    global _last_sweep
    now = time.monotonic()
    if not force and _last_sweep is not None and now - _last_sweep < SWEEP_SECONDS:
        return 0
    _last_sweep = now
    try:
        expire()
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
