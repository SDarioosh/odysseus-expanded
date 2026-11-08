"""Fork: one transaction on a fork SQLite file, opened the one way.

:func:`connect` opens ``path`` (its folder made first) in autocommit mode with
``sqlite3.Row`` rows and a 30 s busy timeout. The first time a process opens a
``(path, schema)`` pair it switches the file to WAL and runs ``schema`` (``CREATE ...
IF NOT EXISTS`` statements); later opens skip both, unless the file has gone (then they
run again). The key is the resolved path, so tests with a temp ``DATA_DIR`` each get
their own. ``columns`` (``(table, column, declaration)``) are columns added after the
first schema: each one missing from an older file is added with ``ALTER TABLE ... ADD
COLUMN`` in the same once-per-process step (so ``schema`` must not index them).
``write=True`` wraps the block in ``BEGIN IMMEDIATE`` and commits it, or
rolls it back when the block raises; a read runs without a transaction.

Used by the voice stores in ``fork/voice/`` (ledger, devices, device settings,
announcements, dones, timings). ``fork/brain/history.py`` and
``fork/beliefs/ledger_db.py`` keep their own: their reads run in a transaction and
they migrate older files on open.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_lock = threading.Lock()
_ready: set[tuple] = set()  # (resolved path, schema, columns) set up in this process

Columns = tuple[tuple[str, str, str], ...]


def _add_columns(db: sqlite3.Connection, columns: Columns) -> None:
    for table, column, declaration in columns:
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if column in have:
            continue
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        except sqlite3.OperationalError as exc:  # another process added it first
            if "duplicate column" not in str(exc).lower():
                raise


def _set_up(db: sqlite3.Connection, key: tuple, schema: str, columns: Columns = ()) -> None:
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(schema)
    _add_columns(db, columns)
    _ready.add(key)


@contextmanager
def connect(path: Path | str, schema: str, write: bool = False,
            columns: Columns = ()) -> Iterator[sqlite3.Connection]:
    """One connection to ``path`` with ``schema`` (and ``columns``) in place; a
    transaction when ``write``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = (str(path.resolve()), schema, tuple(columns))
    fresh = not path.exists()
    db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        if fresh or key not in _ready:
            with _lock:
                if fresh or key not in _ready:
                    _set_up(db, key, schema, columns)
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

