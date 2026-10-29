"""Fork: the belief ledger's file, schema and migrations (``fork/beliefs/evidence_intake.py``).

``<DATA_DIR>/beliefs.db`` (stdlib sqlite3, WAL): :data:`SCHEMA`, the process-wide write
lock, :func:`_connect` (one transaction; it brings an older file up to date first) and
the one-time migrations, in the order ``_connect`` runs them. ``PRAGMA user_version``
records how far a file has been migrated. :func:`stored_module_source` is the name an
owner's Athena evidence is written under.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fork.beliefs import ledger_model
from fork.beliefs.ledger_model import EVENT_KINDS, _now, _stamp, normalize

DB_NAME = "beliefs.db"
NORM_VERSION = 1  # PRAGMA user_version: matching keys of this version
SCHEMA_VERSION = 2  # PRAGMA user_version: subjects and structure columns (Phase 2)
ERASE_VERSION = 3  # PRAGMA user_version: ``erased_at`` (complete forget, stage 4.2)
_WRITE = threading.RLock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS beliefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    about TEXT NOT NULL DEFAULT 'other' CHECK (about IN ('owner', 'person', 'other')),
    text TEXT NOT NULL,
    norm TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'fact'
        CHECK (category IN ('fact', 'preference', 'contact')),
    status TEXT NOT NULL DEFAULT 'tentative'
        CHECK (status IN ('tentative', 'confirmed', 'stale', 'rejected')),
    confirmed_by TEXT CHECK (confirmed_by IN ('source', 'owner', 'rule')),
    first_seen TEXT,
    last_seen TEXT,
    valid_until TEXT,
    memory_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    owner_text TEXT,
    owner_norm TEXT,
    note TEXT,
    subject_id INTEGER,
    attribute TEXT,
    value TEXT,
    valid_from TEXT,
    structured_at TEXT
);
CREATE INDEX IF NOT EXISTS beliefs_owner_status ON beliefs (owner, status);
CREATE INDEX IF NOT EXISTS beliefs_owner_norm ON beliefs (owner, norm);
DROP INDEX IF EXISTS beliefs_owner_owner_norm;
CREATE INDEX IF NOT EXISTS beliefs_owner_corrected ON beliefs (owner, owner_norm)
    WHERE owner_norm IS NOT NULL;
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    belief_id INTEGER NOT NULL REFERENCES beliefs (id) ON DELETE CASCADE,
    owner TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    about TEXT NOT NULL DEFAULT 'other',
    category TEXT NOT NULL DEFAULT 'fact',
    text TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    seen_first TEXT,
    seen_last TEXT,
    times INTEGER NOT NULL DEFAULT 1,
    days INTEGER NOT NULL DEFAULT 1,
    source_confirmed INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    UNIQUE (belief_id, source, source_id),
    UNIQUE (owner, source, source_id)
);
CREATE INDEX IF NOT EXISTS evidence_belief ON evidence (belief_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    belief_id INTEGER NOT NULL REFERENCES beliefs (id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('added', 'evidence', 'confirmed', 'stale',
        'rejected', 'corrected', 'restored', 'retired_upstream', 'unconfirmed',
        'merged')),
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_belief ON events (belief_id);
CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT '',
    key TEXT NOT NULL,
    display TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'thing'
        CHECK (kind IN ('person', 'project', 'thing', 'owner')),
    kind_by TEXT NOT NULL DEFAULT 'auto' CHECK (kind_by IN ('auto', 'owner')),
    contact_id TEXT,
    aliases TEXT NOT NULL DEFAULT '[]',
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (owner, key)
);
CREATE TABLE IF NOT EXISTS subject_sources (
    subject_id INTEGER NOT NULL REFERENCES subjects (id) ON DELETE CASCADE,
    owner TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    source_person_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE (source, source_person_id, owner)
);
CREATE INDEX IF NOT EXISTS subject_sources_subject ON subject_sources (subject_id);
"""


# ------------------------------------------------------------------------ storage
def db_path() -> Path:
    from src.constants import DATA_DIR

    return Path(DATA_DIR) / DB_NAME


@contextmanager
def _connect(write: bool = False, *, optimize: bool = False) -> Iterator[sqlite3.Connection]:
    """One transaction on beliefs.db (``BEGIN IMMEDIATE`` for writes). ``optimize``: run
    ``PRAGMA optimize`` after the commit (the batch writers: cheap, and it keeps the
    planner's statistics current as the ledger grows)."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(SCHEMA)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version < NORM_VERSION:
            _migrate_norms(db)
        _migrate_event_kinds(db)
        if version < SCHEMA_VERSION:
            _migrate_subjects(db)
        if version < ERASE_VERSION:
            _migrate_erase(db)
        if version < SCHEMA_VERSION:
            db.execute("PRAGMA optimize")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        if optimize:
            db.execute("PRAGMA optimize")
    finally:
        db.close()


def stored_module_source(db: sqlite3.Connection, owner: str, source: str) -> str:
    """The name Athena's evidence of ``owner`` is written under: ``source`` unless it is
    one of Athena's names; then the old one while this owner's ledger still holds it (a
    ledger ``scripts/athena_names_migrate.py`` hasn't renamed), else the new one. So one
    ledger never holds both names, and one Athena fact can't count as two sources."""
    if not ledger_model.is_module_source(source):
        return source
    old = ledger_model.MODULE_SOURCE_OLD
    if (db.execute("SELECT 1 FROM evidence WHERE owner = ? AND source = ? LIMIT 1",
                   (owner, old)).fetchone()
            or db.execute("SELECT 1 FROM subject_sources WHERE source = ? AND owner = ? "
                          "LIMIT 1", (old, owner)).fetchone()):
        return old
    return ledger_model.MODULE_SOURCE


def _migrate_norms(db: sqlite3.Connection) -> None:
    """Once per ledger file: recompute the matching keys of owner beliefs (the owner's
    leading mention became the ``owner`` token)."""
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] < NORM_VERSION:
            for row in db.execute("SELECT id, subject, about, text, owner_text FROM beliefs "
                                  "WHERE about = 'owner'").fetchall():
                owner_norm = (normalize(row["owner_text"], row["subject"], row["about"])
                              if row["owner_text"] else None)
                db.execute("UPDATE beliefs SET norm = ?, owner_norm = ? WHERE id = ?",
                           (normalize(row["text"], row["subject"], row["about"]), owner_norm,
                            row["id"]))
            db.execute(f"PRAGMA user_version = {NORM_VERSION}")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def _migrate_event_kinds(db: sqlite3.Connection) -> None:
    """Once per ledger file: widen the ``events.kind`` CHECK to today's kinds (an older
    ledger lacks ``unconfirmed`` or ``merged``). SQLite can't alter a CHECK, so the table
    is rebuilt."""
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
                     ).fetchone()[0]
    if all(f"'{k}'" in sql for k in EVENT_KINDS):
        return
    kinds = ", ".join(f"'{k}'" for k in EVENT_KINDS)
    db.execute("BEGIN IMMEDIATE")
    try:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' "
                         "AND name = 'events'").fetchone()[0]
        if not all(f"'{k}'" in sql for k in EVENT_KINDS):
            db.execute("CREATE TABLE events_new (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "belief_id INTEGER NOT NULL REFERENCES beliefs (id) ON DELETE CASCADE, "
                       f"at TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ({kinds})), "
                       "detail TEXT NOT NULL DEFAULT '')")
            db.execute("INSERT INTO events_new (id, belief_id, at, kind, detail) "
                       "SELECT id, belief_id, at, kind, detail FROM events")
            db.execute("DROP TABLE events")
            db.execute("ALTER TABLE events_new RENAME TO events")
            db.execute("CREATE INDEX IF NOT EXISTS events_belief ON events (belief_id)")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


_NEW_BELIEF_COLUMNS = (("subject_id", "INTEGER"), ("attribute", "TEXT"), ("value", "TEXT"),
                       ("valid_from", "TEXT"), ("structured_at", "TEXT"))


def _migrate_subjects(db: sqlite3.Connection) -> None:
    """Once per ledger file (``PRAGMA user_version`` 2): add the Phase 2 belief columns
    (``subject_id``, ``attribute``, ``value``, ``valid_from``, ``structured_at``) and file
    every belief under a subject, from its ``subject`` and ``about``: about the owner ->
    the owner's one ``owner`` subject; a person -> kind ``person``; else ``thing`` (or
    ``project`` when the name says so)."""
    from fork.beliefs.ledger_subjects import _resolve_subject, _Subjects

    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            cols = {r[1] for r in db.execute("PRAGMA table_info(beliefs)")}
            for name, decl in _NEW_BELIEF_COLUMNS:
                if name not in cols:
                    db.execute(f"ALTER TABLE beliefs ADD COLUMN {name} {decl}")
            db.execute("CREATE INDEX IF NOT EXISTS beliefs_owner_subject "
                       "ON beliefs (owner, subject_id)")
            db.execute("CREATE INDEX IF NOT EXISTS beliefs_owner_attribute "
                       "ON beliefs (owner, attribute)")
            stamp = _stamp(_now(None))
            caches: dict[str, _Subjects] = {}
            for row in db.execute("SELECT id, owner, subject, about FROM beliefs "
                                  "WHERE subject_id IS NULL ORDER BY id").fetchall():
                cache = caches.setdefault(row["owner"], _Subjects(db, row["owner"]))
                subject_id = _resolve_subject(cache, row["about"], row["subject"], None, None,
                                              stamp)
                db.execute("UPDATE beliefs SET subject_id = ? WHERE id = ?",
                           (subject_id, row["id"]))
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def _migrate_erase(db: sqlite3.Connection) -> None:
    """Once per ledger file (``PRAGMA user_version`` 3): the ``erased_at`` column."""
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] < ERASE_VERSION:
            cols = {r[1] for r in db.execute("PRAGMA table_info(beliefs)")}
            if "erased_at" not in cols:
                db.execute("ALTER TABLE beliefs ADD COLUMN erased_at TEXT")
            db.execute(f"PRAGMA user_version = {ERASE_VERSION}")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
