"""Fork: the belief ledger, between every source and Odysseus memory.

Personal-context plan stages 1.1 (evidence intake) and 1.5 (expiry and decay); the
design is in ``docs/fork/beliefs.md``.

* A **source** (the Athena facts job, later notes, chat ...) submits
  :class:`Evidence`: what it observed, with its own id for it and a link to where it
  came from. :func:`submit` files each item under a **belief**: the one this source id
  already maps to, else a belief of the same owner with the same normalized text, else
  a new ``tentative`` belief.
* A belief is ``tentative``, ``confirmed``, ``stale`` or ``rejected``.
  :func:`confirmation_for` is the rule (the source's own confirmation, or enough
  distinct sources, or enough sightings on enough days); the owner's own decisions
  (:func:`confirm`, :func:`correct`, :func:`reject`) always win and the rule never
  undoes them.
* :func:`reconcile_source` marks evidence a source no longer reports as gone; a belief
  left without live evidence goes ``stale`` unless the owner confirmed it.
  :func:`apply_decay` stales confirmed beliefs not heard of for ``BELIEF_STALE_DAYS``
  and any belief past its ``valid_until``.
* :func:`sync_memory` keeps exactly one Odysseus memory per confirmed belief (text
  ``<fact> (source: <link>)``), updated in place (memory id and ``uses`` kept), and
  deletes the copies of beliefs that are no longer confirmed. Phase 0 copies (a
  ``module_fact_id`` but no ``belief_id``) are adopted, not duplicated.

Storage: ``<DATA_DIR>/beliefs.db`` (stdlib sqlite3, WAL), one file for all owners.
Writes run under one process-wide lock; each public call is one transaction. Calls are
synchronous and short: async callers may call them directly, as the facts job does.

Phase 2 (``docs/fork/beliefs.md``, "Subjects and contacts", "Structure", "Projects"):

* Every belief belongs to a **subject** (``subjects``: the owner, a person, a project or a
  thing; :func:`list_subjects`). :func:`submit` resolves it from the evidence: by the
  source's stable person id (``Evidence.person_id``, kept in ``subject_sources``), else by
  name or alias (case-insensitive), else a new subject. A subject can be linked to an
  Odysseus contact (:func:`link_contact`), merged, renamed and given a kind.
* A confirmed belief can carry **structure** next to its text: ``attribute`` (one of
  :data:`ATTRIBUTES`), ``value``, ``valid_from``; written by the Belief Structure task
  (``src/belief_structure.py``) through :func:`structure_candidates` and
  :func:`apply_structure`. Free text stays what memory shows.
* A subject becomes a **project** when the owner says so, or when it isn't a module person
  and its name says project/launch/release/migration/plan or it has a ``project_status``
  or ``deadline`` belief.

Settings (environment): ``BELIEF_CONFIRM_SOURCES`` (2), ``BELIEF_CONFIRM_TIMES`` (3),
``BELIEF_CONFIRM_DAYS`` (2), ``BELIEF_STALE_DAYS`` (180).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import sys
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_NAME = "beliefs.db"
MODULE_SOURCE = "odysseus-sound"
ABOUT = ("owner", "person", "other")
CATEGORIES = ("fact", "preference", "contact")
STATUSES = ("tentative", "confirmed", "stale", "rejected")
CONFIRMED_BY = ("source", "owner", "rule")
EVENT_KINDS = ("added", "evidence", "confirmed", "stale", "rejected", "corrected",
               "restored", "retired_upstream", "unconfirmed", "merged", "erased", "moved")
NORM_VERSION = 1  # PRAGMA user_version: matching keys of this version
SCHEMA_VERSION = 2  # PRAGMA user_version: subjects and structure columns (Phase 2)
ERASE_VERSION = 3  # PRAGMA user_version: ``erased_at`` (complete forget, stage 4.2)
# An erased belief keeps only a hash of its matching keys, so the same claim coming back
# from any source still finds it (and stays out of memory) without its words being kept.
ERASED_PREFIX = "erased:"
# Per-evidence erase state (``payload.erase``): the module erased it (``done``), only
# retired it (an older module: ``retired``), not reached yet (``pending``); evidence of
# another source keeps its link so the page can say where the words still are (``source``).
ERASE_STATES = ("done", "retired", "pending", "source")
SUBJECT_KINDS = ("person", "project", "thing", "owner")
# The controlled vocabulary of a belief's ``attribute`` (stage 2.2); anything else: other.
ATTRIBUTES = ("lives_in", "works_at", "role", "birthday", "allergy", "diet", "likes",
              "dislikes", "relationship", "owns", "pet", "plans", "decision",
              "routine", "contact_info", "health", "project_status", "deadline", "other")
PROJECT_ATTRIBUTES = ("project_status", "deadline")
PROJECT_WORDS = re.compile(r"\b(project|launch|release|migration|plan)\b", re.IGNORECASE)
GENERIC_SUBJECT = "General"  # the subject of evidence that names none
OWNER_DISPLAY = "Owner"
_OWNER_NAMES = frozenset({"", "me", "i", "myself", "owner", "the owner", "user", "the user"})
MAX_VALUE_CHARS = 120
EVENTS_KEPT = 50  # per belief: the history stays small
PREFERENCE_WORDS = frozenset({
    "like", "likes", "prefer", "prefers", "love", "loves", "hate", "hates", "enjoy",
    "enjoys", "favorite", "favourite", "want", "wants", "dislike",
})
# Memory fields only a belief with Athena evidence carries (the Phase 0 shape).
MODULE_FIELDS = ("module_fact_id", "times_said", "days", "first_said", "last_said")
# Memory fields a mirrored entry carries only sometimes (removed when they no longer apply).
OPTIONAL_FIELDS = (*MODULE_FIELDS, "contact_id")
_RESERVED_PAYLOAD = ("gone", "gone_at")
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


class BeliefNotFound(LookupError):
    """No belief with that id for that owner."""


class SubjectNotFound(LookupError):
    """No subject with that id for that owner."""


class BeliefErased(ValueError):
    """The owner erased this belief: it can't be confirmed, corrected or restored."""


# ----------------------------------------------------------------------- settings
@dataclass(frozen=True)
class Settings:
    confirm_sources: int = 2
    confirm_times: int = 3
    confirm_days: int = 2
    stale_days: int = 180


def settings() -> Settings:
    """The confirmation and decay thresholds from the environment."""
    from fork.common import settings as env

    return Settings(
        confirm_sources=env.belief_confirm_sources(),
        confirm_times=env.belief_confirm_times(),
        confirm_days=env.belief_confirm_days(),
        stale_days=env.belief_stale_days(),
    )


_read_settings = settings  # confirmation_for's ``settings`` argument shadows the name


# ------------------------------------------------------------------------ evidence
@dataclass
class Evidence:
    """One thing a source observed. ``source_id`` is the source's own stable id for it
    (one source id maps to one belief per owner); ``link`` points to where it came from
    (required: evidence without a link is skipped); ``seen_first``/``seen_last`` are ISO
    dates or datetimes; ``times``/``days`` are the source's own totals (distinct
    sightings, distinct days); ``source_confirmed`` is the source's own verdict.
    ``valid_until`` (ISO date, optional) ends the belief on that day. ``person_id``
    (optional) is the source's stable id of the person the evidence is about (e.g.
    Athena's ``list_people`` id): it files the belief under that person's subject
    whatever name the source uses."""

    source: str
    source_id: str
    subject: str
    about: str
    text: str
    link: str
    seen_first: str
    seen_last: str
    times: int = 1
    days: int = 1
    source_confirmed: bool = False
    category: str | None = None
    payload: dict | None = None
    valid_until: str | None = None
    person_id: str | None = None


_OWNER_LEADS = ("the owner", "owner", "i am", "i'm", "i've", "me", "my", "i")
_OWNER_VERBS = {"am": "is", "have": "has"}


def normalize(text: str, subject: str | None = None, about: str | None = None) -> str:
    """Matching key: lowercase, whitespace collapsed, trailing punctuation dropped.

    For a fact about the owner (``about="owner"``) a leading mention of the owner (``the
    owner``, ``owner``, ``me``, ``i``, ``i'm``, ``i am``, ``my`` or the evidence's own
    ``subject``, e.g. the owner's name) becomes the token ``owner``, and ``am``/``have``
    right after it become ``is``/``has``: "I am vegetarian", "Me is vegetarian" and "The
    owner is vegetarian." share one key. A possessive (``my``, ``the owner's``,
    ``<name>'s``) becomes ``owner's``. Facts about people and things keep the plain key."""
    norm = " ".join(str(text or "").lower().split()).rstrip(" .,;:!?…")
    if str(about or "").strip().lower() != "owner":
        return norm
    return _owner_key(norm.replace("\u2019", "'"), subject)


def _owner_key(norm: str, subject: str | None) -> str:
    name = " ".join(str(subject or "").lower().split()).replace("\u2019", "'")
    leads = sorted({*_OWNER_LEADS, *([name] if name else [])}, key=len, reverse=True)
    for lead in leads:
        if not norm.startswith(lead):
            continue
        rest = norm[len(lead):]
        if lead == "my":
            if rest.startswith(" "):
                return "owner's" + rest
            continue
        if rest.startswith("'s ") or rest == "'s":
            return "owner" + rest
        if rest and not rest.startswith(" "):
            continue  # "meals …", "ice …": not a subject mention
        if lead == "i'm":
            rest = " is" + rest
        elif lead == "i've":
            rest = " has" + rest
        elif lead == "i am":
            rest = " is" + rest
        words = rest.split()
        if words and words[0] in _OWNER_VERBS:
            words[0] = _OWNER_VERBS[words[0]]
        return " ".join(["owner", *words])
    return norm


# Kinds a source can give a fact (Athena MCP 0.11.0 ``kind``); they decide the category
# instead of preference words (:func:`category_for`).
FACT_KINDS = ("preference", "plan", "decision", "relationship", "personal", "other")


def category_for(about: str, text: str, kind: str | None = None) -> str:
    """The memory category (fact|preference|contact). A person -> contact. Else a source's
    fact ``kind`` (:data:`FACT_KINDS`) decides: preference, or fact; without one, about the
    owner -> preference when the text has a preference word, else fact."""
    about = str(about or "").strip().lower()
    kind = str(kind or "").strip().lower()
    if kind in FACT_KINDS and about != "person":
        return "preference" if kind == "preference" else "fact"
    if about == "owner":
        words = set(re.findall(r"[a-z]+", str(text or "").lower()))
        return "preference" if words & PREFERENCE_WORDS else "fact"
    if about == "person":
        return "contact"
    return "fact"


def _day(value: Any) -> str | None:
    """An ISO date (YYYY-MM-DD) from a date, datetime, epoch or ISO string."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo else value).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        return None


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _stamp(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean(item: Evidence, today: str) -> dict | None:
    """The stored shape of one evidence item, or None when it can't be filed."""
    source = str(item.source or "").strip()
    source_id = str(item.source_id if item.source_id is not None else "").strip()
    text = " ".join(str(item.text or "").split())
    link = str(item.link or "").strip()
    if not (source and source_id and text and link):
        return None
    about = str(item.about or "").strip().lower()
    about = about if about in ABOUT else "other"
    cat = str(item.category or "").strip().lower()
    cat = cat if cat in CATEGORIES else category_for(about, text)
    first, last = _day(item.seen_first), _day(item.seen_last)
    first = first or last or today
    last = last or first
    first, last = min(first, last), max(first, last)
    payload = {k: v for k, v in (item.payload or {}).items() if k not in _RESERVED_PAYLOAD}
    valid_until = _day(item.valid_until)
    if valid_until:
        payload["valid_until"] = valid_until
    person_id = str(item.person_id if item.person_id is not None else "").strip()
    return {
        "person_id": person_id or None,
        "source": source, "source_id": source_id, "subject": str(item.subject or "").strip(),
        "about": about, "text": text, "norm": normalize(text, item.subject, about),
        "category": cat,
        "link": link, "seen_first": first, "seen_last": last,
        "times": max(1, _int(item.times, 1)), "days": max(1, _int(item.days, 1)),
        "source_confirmed": 1 if item.source_confirmed else 0,
        "payload": json.dumps(payload, sort_keys=True, ensure_ascii=False),
    }


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


# ----------------------------------------------------------------------- subjects
def subject_key(name: Any) -> str:
    """A subject's matching key: lowercase, whitespace collapsed, surrounding quotes and
    trailing punctuation dropped. Names and aliases match on it."""
    key = " ".join(str(name or "").casefold().replace("\u2019", "'").split())
    return key.strip(" .,;:!?…\"'")


def _clean_name(name: Any) -> str:
    return " ".join(str(name or "").split())[:120]


def _aliases(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(a) for a in value if str(a).strip()] if isinstance(value, list) else []


def _is_owner_name(name: Any) -> bool:
    return subject_key(name) in _OWNER_NAMES


class _Subjects:
    """One owner's subjects, for matching inside one transaction: subject keys first, then
    alias keys (an alias never shadows another subject's own key)."""

    def __init__(self, db: sqlite3.Connection, owner: str):
        self.db, self.owner = db, owner
        self.by_key: dict[str, int] = {}
        self.owner_id: int | None = None
        rows = db.execute("SELECT id, key, kind, aliases FROM subjects WHERE owner = ? "
                          "ORDER BY id", (owner,)).fetchall()
        for row in rows:
            self.by_key.setdefault(row["key"], row["id"])
            if row["kind"] == "owner" and self.owner_id is None:
                self.owner_id = row["id"]
        for row in rows:
            for alias in _aliases(row["aliases"]):
                self.by_key.setdefault(subject_key(alias), row["id"])

    def row(self, subject_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()

    def create(self, display: str, kind: str, stamp: str) -> int:
        key = subject_key(display)
        n = 1
        while key in self.by_key:  # only the owner subject can get here
            n += 1
            display = f"{OWNER_DISPLAY} {n}"
            key = subject_key(display)
        cur = self.db.execute(
            "INSERT INTO subjects (owner, key, display, kind, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (self.owner, key, display, kind, stamp, stamp))
        self.by_key[key] = cur.lastrowid
        if kind == "owner":
            self.owner_id = cur.lastrowid
        return cur.lastrowid

    def add_alias(self, subject_id: int, name: Any, stamp: str) -> bool:
        """Add ``name`` as an alias unless it already names this subject."""
        name = _clean_name(name)
        key = subject_key(name)
        row = self.row(subject_id)
        if not key or row is None or key == row["key"]:
            return False
        aliases = _aliases(row["aliases"])
        if any(subject_key(a) == key for a in aliases):
            return False
        self.db.execute("UPDATE subjects SET aliases = ?, updated_at = ? WHERE id = ?",
                        (json.dumps([*aliases, name], ensure_ascii=False), stamp, subject_id))
        self.by_key.setdefault(key, subject_id)
        return True

    def owner_subject(self, name: Any, stamp: str) -> int:
        """The owner's one ``owner`` subject (created on first use, named after the first
        real name it hears, else "Owner"); a real name becomes an alias."""
        name = _clean_name(name)
        if self.owner_id is None:
            display = OWNER_DISPLAY if _is_owner_name(name) or subject_key(name) in self.by_key \
                else name
            self.create(display, "owner", stamp)
        if not _is_owner_name(name):
            self.add_alias(self.owner_id, name, stamp)
        return self.owner_id

    def mapped(self, source: str | None, person_id: str | None) -> int | None:
        if not (source and person_id):
            return None
        row = self.db.execute("SELECT subject_id FROM subject_sources WHERE owner = ? AND "
                              "source = ? AND source_person_id = ?",
                              (self.owner, source, person_id)).fetchone()
        return row["subject_id"] if row else None

    def map(self, subject_id: int, source: str | None, person_id: str | None, name: Any,
            stamp: str, *, move: bool = False) -> bool:
        """Map (source, person id) to a subject. An existing mapping is kept unless
        ``move``. Returns whether a mapping was added or moved."""
        if not (source and person_id):
            return False
        current = self.mapped(source, person_id)
        if current is None:
            self.db.execute("INSERT INTO subject_sources (subject_id, owner, source, "
                            "source_person_id, name, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (subject_id, self.owner, source, person_id, _clean_name(name),
                             stamp))
            return True
        if current != subject_id and move:
            self.db.execute("UPDATE subject_sources SET subject_id = ?, updated_at = ? WHERE "
                            "owner = ? AND source = ? AND source_person_id = ?",
                            (subject_id, stamp, self.owner, source, person_id))
            return True
        return False


def _inferred_kind(about: str, name: str, person_id: str | None) -> str:
    if about == "person" and name:
        return "person"
    if not person_id and PROJECT_WORDS.search(name or ""):
        return "project"
    return "thing"


def _resolve_subject(cache: _Subjects, about: str, subject: Any, source: str | None,
                     person_id: str | None, stamp: str) -> int:
    """The subject an item belongs to: the owner subject for ``about="owner"``; else the
    subject the source's person id maps to, else the one whose name or alias matches,
    else a new one. A person id is mapped to the subject found; a name the subject
    doesn't have yet becomes an alias."""
    name = _clean_name(subject)
    if about == "owner":
        subject_id = cache.owner_subject(name, stamp)
        cache.map(subject_id, source, person_id, name, stamp)
        return subject_id
    subject_id = cache.mapped(source, person_id)
    if subject_id is not None:
        if name:
            cache.add_alias(subject_id, name, stamp)
        return subject_id
    display = name or GENERIC_SUBJECT
    subject_id = cache.by_key.get(subject_key(display))
    if subject_id is None:
        subject_id = cache.create(display, _inferred_kind(about, name, person_id), stamp)
    elif person_id and about == "person":
        _person_kind(cache, subject_id, stamp)
    cache.map(subject_id, source, person_id, name, stamp)
    return subject_id


def _person_kind(cache: _Subjects, subject_id: int, stamp: str) -> None:
    """A subject a source knows as a person becomes kind person (if it is still an
    inferred thing or project)."""
    row = cache.row(subject_id)
    if row is not None and row["kind"] in ("thing", "project") and row["kind_by"] == "auto":
        cache.db.execute("UPDATE subjects SET kind = 'person', updated_at = ? WHERE id = ?",
                         (stamp, subject_id))


def _event(db: sqlite3.Connection, belief_id: int, kind: str, detail: str, stamp: str) -> None:
    db.execute("INSERT INTO events (belief_id, at, kind, detail) VALUES (?, ?, ?, ?)",
               (belief_id, stamp, kind, detail[:300]))
    db.execute("DELETE FROM events WHERE belief_id = ? AND id NOT IN (SELECT id FROM events "
               "WHERE belief_id = ? ORDER BY id DESC LIMIT ?)",
               (belief_id, belief_id, EVENTS_KEPT))


def _payload(raw: Any) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _evidence_dict(row: Mapping) -> dict:
    payload = _payload(row["payload"])
    return {
        "id": row["id"], "belief_id": row["belief_id"], "source": row["source"],
        "source_id": row["source_id"], "subject": row["subject"], "about": row["about"],
        "category": row["category"], "text": row["text"], "link": row["link"],
        "seen_first": row["seen_first"], "seen_last": row["seen_last"],
        "times": row["times"], "days": row["days"],
        "source_confirmed": bool(row["source_confirmed"]),
        "gone": bool(payload.get("gone")), "payload": payload,
        "updated_at": row["updated_at"],
    }


def _evidence_rows(db: sqlite3.Connection, belief_ids: Iterable[int]) -> dict[int, list[dict]]:
    ids = sorted(set(belief_ids))
    out: dict[int, list[dict]] = {i: [] for i in ids}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for row in db.execute(f"SELECT * FROM evidence WHERE belief_id IN ({marks}) "
                              "ORDER BY id", chunk):
            out[row["belief_id"]].append(_evidence_dict(row))
    return out


def link_rank(e: Mapping) -> int:
    """How well an evidence's link serves as the belief's citation: 2 for an absolute
    ``http(s)://`` link from Athena (the signed transcript link), 1 for any other
    absolute link, 0 for a relative one (e.g. a note's ``/#open=notes…``)."""
    link = str(e.get("link") or "").strip().lower()
    if not link.startswith(("http://", "https://")):
        return 0
    return 2 if e.get("source") == MODULE_SOURCE else 1


def primary_evidence(evidence: list[dict]) -> dict | None:
    """The evidence whose link and source represent the belief in memory: live before
    gone, then by :func:`link_rank` (transcript link, other absolute link, relative
    link), then the most recent (``seen_last``, then id)."""
    if not evidence:
        return None
    pool = [e for e in evidence if not e.get("gone")] or evidence
    return max(pool, key=lambda e: (link_rank(e), e.get("seen_last") or "", e.get("id") or 0))


_BELIEF_SELECT = ("SELECT b.*, s.display AS subject_display, s.kind AS subject_kind, "
                  "s.contact_id AS contact_id FROM beliefs b "
                  "LEFT JOIN subjects s ON s.id = b.subject_id")


def _belief_dict(row: Mapping, evidence: list[dict]) -> dict:
    main = primary_evidence(evidence)
    keys = row.keys() if hasattr(row, "keys") else ()
    joined = {k: (row[k] if k in keys else None)
              for k in ("subject_display", "subject_kind", "contact_id")}
    return {
        "id": row["id"], "owner": row["owner"], "subject": row["subject"],
        "about": row["about"], "text": row["text"], "owner_text": row["owner_text"],
        "shown_text": row["owner_text"] or row["text"], "category": row["category"],
        "status": row["status"], "confirmed_by": row["confirmed_by"],
        "first_seen": row["first_seen"], "last_seen": row["last_seen"],
        "valid_until": row["valid_until"], "memory_id": row["memory_id"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "note": row["note"], "link": main["link"] if main else "",
        "source": main["source"] if main else "", "evidence": evidence,
        "subject_id": row["subject_id"], **joined,
        "attribute": row["attribute"], "value": row["value"], "valid_from": row["valid_from"],
        "structured_at": row["structured_at"],
        "erased_at": row["erased_at"] if "erased_at" in keys else None,
    }


def _belief(db: sqlite3.Connection, owner: str, belief_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM beliefs WHERE id = ? AND owner = ?",
                     (_int(belief_id, -1), owner)).fetchone()
    if row is None:
        raise BeliefNotFound(f"belief {belief_id} not found")
    return row


def _set(db: sqlite3.Connection, belief_id: int, stamp: str, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE beliefs SET {cols}, updated_at = ? WHERE id = ?",
               (*fields.values(), stamp, belief_id))


# ----------------------------------------------------------------- the rule
def confirmation_for(belief: Mapping, evidence_rows: Iterable[Mapping],
                     settings: Settings | None = None) -> str | None:
    """How ``belief`` counts as confirmed: ``"owner"``, ``"source"``, ``"rule"``, or None.

    The owner's decisions win: an owner-confirmed belief is ``"owner"`` and a rejected
    one None, whatever the evidence says. Otherwise only live (not gone) evidence counts:
    (a) any item the source itself calls confirmed -> ``"source"``; (b) at least
    ``confirm_sources`` distinct sources -> ``"rule"``; (c) at least ``confirm_times``
    sightings summed across sources on at least ``confirm_days`` distinct days ->
    ``"rule"``. Distinct days across sources is the larger of the biggest per-source
    ``days`` and the number of distinct first/last-seen dates (sources report totals,
    not every date, so this never over-counts)."""
    st = settings if settings is not None else _read_settings()
    if belief["status"] == "rejected":
        return None
    if belief["confirmed_by"] == "owner":
        return "owner"
    live = [e for e in evidence_rows if not _is_gone(e)]
    if not live:
        return None
    if any(bool(e["source_confirmed"]) for e in live):
        return "source"
    if len({e["source"] for e in live}) >= st.confirm_sources:
        return "rule"
    times = sum(max(1, _int(e["times"], 1)) for e in live)
    dates = {d for e in live for d in (e["seen_first"], e["seen_last"]) if d}
    days = max(max(max(1, _int(e["days"], 1)) for e in live), len(dates))
    if times >= st.confirm_times and days >= st.confirm_days:
        return "rule"
    return None


def _is_gone(e: Mapping) -> bool:
    if isinstance(e, dict) and "gone" in e:
        return bool(e["gone"])
    return bool(_payload(e["payload"]).get("gone"))


def _expired(row: Mapping, today: date, st: Settings) -> str | None:
    """Why a belief is past it ("valid until ...", "not seen since ..."), or None."""
    valid_until = _day(row["valid_until"])
    if valid_until and valid_until < today.isoformat():
        return f"valid until {valid_until}"
    last = _day(row["last_seen"])
    if (row["confirmed_by"] != "owner" and last
            and last < (today - timedelta(days=st.stale_days)).isoformat()):
        return f"not seen since {last}"
    return None


def _refresh(db: sqlite3.Connection, belief_id: int, stamp: str, st: Settings, today: date,
             *, revive: bool = False, stale_when_empty: bool = True) -> str | None:
    """Recompute dates and status of one belief from its evidence. Returns the new status
    when it changed, else None. ``revive``: new evidence may bring a stale belief back."""
    row = db.execute("SELECT * FROM beliefs WHERE id = ?", (belief_id,)).fetchone()
    if row is None:
        return None
    evidence = _evidence_rows(db, [belief_id])[belief_id]
    fields: dict[str, Any] = {}
    if evidence:
        first = min(e["seen_first"] or e["seen_last"] for e in evidence)
        last = max(e["seen_last"] or e["seen_first"] for e in evidence)
        if (first, last) != (row["first_seen"], row["last_seen"]):
            fields.update(first_seen=first, last_seen=last)
        until = [e["payload"]["valid_until"] for e in evidence
                 if not e["gone"] and e["payload"].get("valid_until")]
        if until and max(until) != row["valid_until"]:
            fields["valid_until"] = max(until)
    if fields:
        _set(db, belief_id, stamp, **fields)
        row = db.execute("SELECT * FROM beliefs WHERE id = ?", (belief_id,)).fetchone()
    status, by = row["status"], row["confirmed_by"]
    if status == "rejected" or by == "owner":
        return None
    live = [e for e in evidence if not e["gone"]]
    if not live:
        if stale_when_empty and status != "stale":
            _set(db, belief_id, stamp, status="stale", confirmed_by=None)
            _event(db, belief_id, "stale", "no source reports it any more", stamp)
            return "stale"
        return None
    if status == "stale":
        if not revive or _expired(row, today, st):
            return None
        _set(db, belief_id, stamp, status="tentative", confirmed_by=None)
        _event(db, belief_id, "evidence", "seen again", stamp)
        status, by = "tentative", None
        changed: str | None = "tentative"
    else:
        changed = None
    rule = confirmation_for(row, live, st)
    if rule and status != "confirmed":
        _set(db, belief_id, stamp, status="confirmed", confirmed_by=rule)
        _event(db, belief_id, "confirmed", f"by {rule}", stamp)
        return "confirmed"
    if rule and rule != by:
        _set(db, belief_id, stamp, confirmed_by=rule)
    elif not rule and status == "confirmed":
        _set(db, belief_id, stamp, status="tentative", confirmed_by=None)
        _event(db, belief_id, "unconfirmed", "no longer meets the rule", stamp)
        return "unconfirmed"
    return changed


# ----------------------------------------------------------------------- intake
# Two indexed lookups, not one ``norm = ? OR owner_norm = ?``: SQLite planned the OR as a
# scan of the owner's beliefs, which made intake quadratic. ``owner_norm`` (set only by a
# correction) has a partial index over the non-null rows: a full index is almost all
# NULLs, and with statistics the planner scans it instead of searching it.
MATCH_SQL = ("SELECT id FROM beliefs INDEXED BY beliefs_owner_norm "
             "WHERE owner = ? AND norm = ? AND id != ? "
             "UNION SELECT id FROM beliefs INDEXED BY beliefs_owner_corrected "
             "WHERE owner = ? AND owner_norm IS NOT NULL AND owner_norm = ? AND id != ? "
             "ORDER BY 1 LIMIT 1")


def erased_key(norm: str | None) -> str | None:
    """The matching key an erased belief keeps: a hash of ``norm``, never the words."""
    if not norm or norm.startswith(ERASED_PREFIX):
        return norm or None
    return ERASED_PREFIX + hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _match(db: sqlite3.Connection, owner: str, norm: str, exclude: int | None = None
           ) -> int | None:
    other = exclude or -1
    for key in (norm, erased_key(norm)):  # a live belief, else an erased one
        row = db.execute(MATCH_SQL, (owner, key, other, owner, key, other)).fetchone()
        if row:
            return row["id"]
    return None


def _erased(db: sqlite3.Connection, belief_id: int) -> bool:
    row = db.execute("SELECT erased_at FROM beliefs WHERE id = ?", (belief_id,)).fetchone()
    return bool(row and row["erased_at"])


def _erased_evidence(ev: dict, state: str) -> dict:
    """What evidence filed under an erased belief keeps: no words, a link only for a
    source that still holds them (not Athena, which erases them itself)."""
    out = dict(ev, subject="", text="", payload=json.dumps({"erase": state}))
    if ev["source"] == MODULE_SOURCE:
        out["link"] = ""
    return out


def _subject_for(cache: _Subjects, ev: dict, stamp: str) -> int:
    return _resolve_subject(cache, ev["about"], ev["subject"], ev["source"], ev["person_id"],
                            stamp)


def _new_belief(db: sqlite3.Connection, owner: str, ev: dict, stamp: str,
                cache: _Subjects) -> int:
    cur = db.execute(
        "INSERT INTO beliefs (owner, subject, about, text, norm, category, status, "
        "first_seen, last_seen, created_at, updated_at, subject_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 'tentative', ?, ?, ?, ?, ?)",
        (owner, ev["subject"], ev["about"], ev["text"], ev["norm"], ev["category"],
         ev["seen_first"], ev["seen_last"], stamp, stamp, _subject_for(cache, ev, stamp)))
    _event(db, cur.lastrowid, "added", f"from {ev['source']}", stamp)
    return cur.lastrowid


_EV_COLS = ("subject", "about", "category", "text", "link", "seen_first", "seen_last",
            "times", "days", "source_confirmed", "payload")


def _note_person(cache: _Subjects, belief_id: int, ev: dict, stamp: str) -> None:
    """Evidence with a person id that joins an existing belief: an unmapped person id maps
    to that belief's subject (unless that is the owner and the item isn't about them)."""
    if not ev["person_id"] or cache.mapped(ev["source"], ev["person_id"]) is not None:
        return
    row = cache.db.execute("SELECT b.subject_id, s.kind FROM beliefs b LEFT JOIN subjects s "
                           "ON s.id = b.subject_id WHERE b.id = ?", (belief_id,)).fetchone()
    if row is None or row["subject_id"] is None:
        return
    if (row["kind"] == "owner") != (ev["about"] == "owner"):
        return
    cache.map(row["subject_id"], ev["source"], ev["person_id"], ev["subject"], stamp)
    if ev["subject"] and row["kind"] != "owner":
        cache.add_alias(row["subject_id"], ev["subject"], stamp)


def _reworded(row: Mapping, ev: dict) -> bool:
    """The source says the same thing about the same subject in other glue words."""
    from fork.beliefs.consolidation import same_rewording

    return ((row["subject"], row["about"]) == (ev["subject"], ev["about"])
            and same_rewording(normalize(row["text"], row["subject"], row["about"]),
                               ev["norm"]))


def _attach(db: sqlite3.Connection, owner: str, ev: dict, stamp: str, cache: _Subjects
            ) -> tuple[str, set[int], int, bool]:
    """File one item. Returns (count key, beliefs touched, beliefs added, changed)."""
    row = db.execute("SELECT * FROM evidence WHERE owner = ? AND source = ? AND source_id = ?",
                     (owner, ev["source"], ev["source_id"])).fetchone()
    if row is None:
        added = 0
        belief_id = _match(db, owner, ev["norm"])
        if belief_id is None:
            belief_id, added = _new_belief(db, owner, ev, stamp, cache), 1
        else:
            _event(db, belief_id, "evidence", f"from {ev['source']}", stamp)
            if _erased(db, belief_id):
                # said again after an erase: filed without its words; module evidence is
                # erased there by the next pass (``belief_forget.retry_pending``)
                ev = _erased_evidence(ev, "pending" if ev["source"] == MODULE_SOURCE
                                      else "source")
            else:
                _note_person(cache, belief_id, ev, stamp)
        db.execute(
            f"INSERT INTO evidence (belief_id, owner, source, source_id, {', '.join(_EV_COLS)}, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (belief_id, owner, ev["source"], ev["source_id"], *(ev[c] for c in _EV_COLS), stamp))
        return "evidence_added", {belief_id}, added, True
    old_payload = _payload(row["payload"])
    was_gone = bool(old_payload.get("gone"))
    if _erased(db, row["belief_id"]):
        # an erased belief's evidence: only counts and dates follow the source
        keep = ("seen_first", "seen_last", "times", "days", "source_confirmed")
        if not was_gone and all(row[c] == ev[c] for c in keep):
            return "unchanged", {row["belief_id"]}, 0, False
        payload = {k: v for k, v in old_payload.items() if k not in _RESERVED_PAYLOAD}
        db.execute(f"UPDATE evidence SET {', '.join(f'{c} = ?' for c in keep)}, payload = ?, "
                   "updated_at = ? WHERE id = ?",
                   (*(ev[c] for c in keep), json.dumps(payload, sort_keys=True), stamp,
                    row["id"]))
        return "evidence_updated", {row["belief_id"]}, 0, True
    same = (not was_gone and all(row[c] == ev[c] for c in _EV_COLS if c != "payload")
            and json.dumps({k: v for k, v in old_payload.items() if k not in _RESERVED_PAYLOAD},
                           sort_keys=True, ensure_ascii=False) == ev["payload"])
    belief_id = row["belief_id"]
    if same:
        if ev["person_id"]:
            _note_person(cache, belief_id, ev, stamp)
        return "unchanged", {belief_id}, 0, False
    touched, added = {belief_id}, 0
    belief = db.execute("SELECT * FROM beliefs WHERE id = ?", (belief_id,)).fetchone()
    others = db.execute("SELECT COUNT(*) FROM evidence WHERE belief_id = ? AND id != ?",
                        (belief_id, row["id"])).fetchone()[0]
    decided = belief["status"] == "rejected" or belief["confirmed_by"] == "owner"
    target = None
    # the source changed what it says
    if normalize(row["text"], row["subject"], row["about"]) != ev["norm"]:
        target = _match(db, owner, ev["norm"], exclude=belief_id)
        # the same claim, it stays with the owner's decision: the source now gives it to
        # the subject the owner moved it to (``move``), or rewords it in nearly the same
        # words about the same subject (an Athena re-summary; "cello" -> "viola" splits)
        moved = (belief["confirmed_by"] == "owner"
                 and (row["subject"], row["about"]) != (ev["subject"], ev["about"]))
        agrees = (target is None and decided and (moved or _reworded(row, ev))
                  and _subject_for(cache, ev, stamp) == belief["subject_id"])
        if not agrees and (others or decided or target is not None):
            # a different claim now: move it to the belief it matches, or a new one
            if target is None:
                target, added = _new_belief(db, owner, ev, stamp, cache), 1
            else:
                _event(db, target, "evidence", f"from {ev['source']}", stamp)
                _note_person(cache, target, ev, stamp)
            db.execute("UPDATE evidence SET belief_id = ? WHERE id = ?", (target, row["id"]))
            belief_id = target
            touched.add(target)
    if target is None and not others and not decided:
        # the belief's only evidence: it follows the source (an edit, a renamed speaker)
        new: dict[str, Any] = {k: ev[k] for k in ("text", "norm", "subject", "about",
                                                  "category")}
        if (ev["subject"], ev["about"]) != (belief["subject"], belief["about"]) \
                or belief["subject_id"] is None:
            new["subject_id"] = _subject_for(cache, ev, stamp)
        elif ev["person_id"]:
            _note_person(cache, belief_id, ev, stamp)
        if any(belief[k] != v for k, v in new.items()):
            if new["text"] != belief["text"] and not belief["owner_text"]:
                new["structured_at"] = None  # the shown text changed: structure it again
            _set(db, belief_id, stamp, **new)
    if was_gone:
        _event(db, belief_id, "evidence", f"back in {ev['source']}", stamp)
    cols = ", ".join(f"{c} = ?" for c in _EV_COLS)
    db.execute(f"UPDATE evidence SET {cols}, updated_at = ? WHERE id = ?",
               (*(ev[c] for c in _EV_COLS), stamp, row["id"]))
    for touched_id in touched:
        _follow_category(db, touched_id, stamp)
    return "evidence_updated", touched, added, True


def _follow_category(db: sqlite3.Connection, belief_id: int, stamp: str) -> None:
    """A belief's category follows its live evidence when all of it (about the belief's
    own ``about``) agrees, also for a belief the owner decided on or one with several
    sources: the category is a label the sources derive (a source that starts sending
    fact kinds relabels its old beliefs), not something the owner chose."""
    b = db.execute("SELECT about, category, erased_at FROM beliefs WHERE id = ?",
                   (belief_id,)).fetchone()
    if b is None or b["erased_at"]:
        return
    cats = {r["category"] for r in db.execute(
        "SELECT category, about, payload FROM evidence WHERE belief_id = ?", (belief_id,))
        if r["about"] == b["about"] and not _payload(r["payload"]).get("gone")}
    if len(cats) == 1:
        cat = cats.pop()
        if cat in CATEGORIES and cat != b["category"]:
            _set(db, belief_id, stamp, category=cat)


def submit(owner: str | None, evidence: list[Evidence], *, now: datetime | None = None,
           mirror: bool = True) -> dict[str, int]:
    """File evidence from a source, apply the rule, and (``mirror``) update memory.

    Returns counts: ``beliefs_added``, ``evidence_added``, ``evidence_updated``,
    ``confirmed`` (beliefs that became confirmed), ``unconfirmed`` (beliefs confirmed by
    their source or the rule that no longer meet it, e.g. after a threshold was raised;
    back to tentative, event ``unconfirmed``), ``unchanged`` (items identical to what the
    ledger had) and ``skipped`` (items without source, id, text or link)."""
    owner = owner or ""
    now = _now(now)
    stamp, today, st = _stamp(now), now.date(), settings()
    counts = dict.fromkeys(("beliefs_added", "evidence_added", "evidence_updated",
                            "confirmed", "unconfirmed", "unchanged", "skipped"), 0)
    with _WRITE:
        with _connect(write=True, optimize=True) as db:
            seen: set[int] = set()
            fresh: set[int] = set()
            cache = _Subjects(db, owner)
            for item in evidence:
                ev = _clean(item, today.isoformat())
                if ev is None:
                    counts["skipped"] += 1
                    continue
                key, touched, added, changed = _attach(db, owner, ev, stamp, cache)
                counts[key] += 1
                counts["beliefs_added"] += added
                seen |= touched
                if changed:
                    fresh |= touched
            for belief_id in sorted(seen):
                result = _refresh(db, belief_id, stamp, st, today, revive=belief_id in fresh)
                if result in ("confirmed", "unconfirmed"):
                    counts[result] += 1
        if mirror:
            sync_memory(owner, now=now)
    return counts


def reconcile_source(owner: str | None, source: str, present_ids: set[str], *,
                     now: datetime | None = None, mirror: bool = True) -> dict[str, int]:
    """Mark this source's evidence whose id is not in ``present_ids`` as gone.

    A belief left without live evidence goes stale (and leaves memory) unless the owner
    confirmed it; a rule-confirmed belief that no longer meets the rule goes back to
    tentative (event ``unconfirmed``). Owner-confirmed and rejected beliefs keep their
    status. Returns counts:
    ``gone`` (evidence newly gone), ``stale`` and ``unconfirmed`` (beliefs)."""
    owner = owner or ""
    now = _now(now)
    stamp, today, st = _stamp(now), now.date(), settings()
    present = {str(i) for i in present_ids}
    counts = {"gone": 0, "stale": 0, "unconfirmed": 0}
    with _WRITE:
        with _connect(write=True, optimize=True) as db:
            touched: set[int] = set()
            for row in db.execute("SELECT * FROM evidence WHERE owner = ? AND source = ?",
                                  (owner, source)).fetchall():
                payload = _payload(row["payload"])
                if row["source_id"] in present or payload.get("gone"):
                    continue
                payload.update(gone=True, gone_at=stamp)
                db.execute("UPDATE evidence SET payload = ?, updated_at = ? WHERE id = ?",
                           (json.dumps(payload, sort_keys=True, ensure_ascii=False), stamp,
                            row["id"]))
                _event(db, row["belief_id"], "retired_upstream",
                       f"{source} {row['source_id']}", stamp)
                counts["gone"] += 1
                touched.add(row["belief_id"])
            for belief_id in sorted(touched):
                result = _refresh(db, belief_id, stamp, st, today)
                if result == "stale":
                    counts["stale"] += 1
                elif result == "unconfirmed":
                    counts["unconfirmed"] += 1
        if mirror:
            sync_memory(owner, now=now)
    return counts


def apply_decay(owner: str | None, *, now: datetime | None = None,
                stale_days: int | None = None, mirror: bool = True) -> dict[str, int]:
    """Expiry (stage 1.5). A belief confirmed by its source or the rule whose last
    sighting is older than ``stale_days`` (``BELIEF_STALE_DAYS``) goes stale; so does any
    tentative or confirmed belief whose ``valid_until`` has passed. Owner-confirmed
    beliefs never decay (only an explicit ``valid_until`` ends them). Returns
    ``{"stale": n}``."""
    owner = owner or ""
    now = _now(now)
    stamp, today = _stamp(now), now.date()
    st = settings()
    if stale_days is not None:
        st = Settings(st.confirm_sources, st.confirm_times, st.confirm_days,
                      max(1, int(stale_days)))
    counts = {"stale": 0}
    with _WRITE:
        with _connect(write=True) as db:
            for row in db.execute("SELECT * FROM beliefs WHERE owner = ? AND status IN "
                                  "('tentative', 'confirmed')", (owner,)).fetchall():
                why = _expired(row, today, st)
                if why is None:
                    continue
                if why.startswith("not seen") and row["status"] != "confirmed":
                    continue  # decay demotes confirmed beliefs; tentative ones just wait
                _set(db, row["id"], stamp, status="stale", confirmed_by=None)
                _event(db, row["id"], "stale", why, stamp)
                counts["stale"] += 1
        if mirror:
            sync_memory(owner, now=now)
    return counts


# ---------------------------------------------------------------- owner actions
def _owner_action(owner: str | None, belief_id: int, now: datetime | None, apply) -> dict:
    owner = owner or ""
    now = _now(now)
    with _WRITE:
        with _connect(write=True) as db:
            row = _belief(db, owner, belief_id)
            apply(db, row, _stamp(now), now.date())
        try:
            sync_memory(owner, now=now, belief_ids=[row["id"]])
        except RuntimeError as e:  # MemoryStoreUnreadable: the next facts run mirrors it
            logger.error("belief %s saved, memory not updated: %s", row["id"], e)
        return get_belief(owner, row["id"])


def _not_erased(row: Mapping) -> None:
    if row["erased_at"]:
        raise BeliefErased("this belief was erased; it can't be brought back")


def confirm(owner: str | None, belief_id: int, *, now: datetime | None = None) -> dict:
    """The owner confirms a belief: confirmed by the owner, in memory, never decays."""
    def apply(db, row, stamp, _today):
        _not_erased(row)
        if row["status"] == "confirmed" and row["confirmed_by"] == "owner":
            return
        _set(db, row["id"], stamp, status="confirmed", confirmed_by="owner")
        _event(db, row["id"], "confirmed", "by owner", stamp)
    return _owner_action(owner, belief_id, now, apply)


def reject(owner: str | None, belief_id: int, reason: str = "", *,
           now: datetime | None = None) -> list[dict]:
    """The owner forgets a belief: rejected, out of memory, and new evidence for it stays
    rejected. Returns its evidence, so the caller can retire it at the sources too
    (``src.module_facts.propagate_forget``)."""
    reason = " ".join(str(reason or "").split())[:300]

    def apply(db, row, stamp, _today):
        if row["status"] == "rejected":
            return
        _set(db, row["id"], stamp, status="rejected", confirmed_by=None,
             note=reason or row["note"])
        _event(db, row["id"], "rejected", reason or "by owner", stamp)
    return _owner_action(owner, belief_id, now, apply)["evidence"]


def erase(owner: str | None, belief_id: int, outcomes: Mapping[str, str] | None = None, *,
          now: datetime | None = None) -> dict:
    """The owner forgot a belief for good (stage 4.2): it must already be rejected
    (:func:`reject`). Its words leave the ledger: text, correction, note, subject name
    and structure are cleared, its matching keys become hashes (:func:`erased_key`, so the
    claim said again still finds it and stays out of memory), its evidence keeps no text
    (and no link, except evidence of a source that still holds the words), its events keep
    no details, and its search vector is deleted. ``outcomes``: per Athena fact id,
    what the module did (``erased``, ``retired``, else not reached). Idempotent: run
    again, it updates the evidence states. Returns ``{"belief", "texts", "remaining",
    "pending"}``: ``texts`` the words it held (to find copies elsewhere; only on the first
    erase), ``remaining`` ``[{"source", "link"}]`` where the words still are,
    ``pending`` how many module facts aren't erased there yet. Raises
    :class:`BeliefNotFound`, ``ValueError`` when the belief isn't rejected."""
    owner = owner or ""
    stamp = _stamp(_now(now))
    outcomes = {str(k): v for k, v in (outcomes or {}).items()}
    with _WRITE:
        with _connect(write=True) as db:
            db.execute("PRAGMA secure_delete = ON")
            row = _belief(db, owner, belief_id)
            if row["status"] != "rejected":
                raise ValueError("forget the belief before erasing it")
            first = not row["erased_at"]
            evidence = db.execute("SELECT * FROM evidence WHERE belief_id = ? ORDER BY id",
                                  (row["id"],)).fetchall()
            texts = ([t for t in (row["text"], row["owner_text"], *(e["text"] for e in evidence))
                      if t] if first else [])
            remaining, pending = [], 0
            for e in evidence:
                old = _payload(e["payload"]).get("erase")
                if e["source"] == MODULE_SOURCE:
                    did = outcomes.get(str(e["source_id"]))
                    state = ("done" if did == "erased" or old == "done" else
                             "retired" if did == "retired" else "pending")
                    pending += state != "done"
                else:
                    state = "source"
                    if e["link"]:
                        remaining.append({"source": e["source"], "link": e["link"]})
                link = "" if e["source"] == MODULE_SOURCE else e["link"]
                db.execute("UPDATE evidence SET subject = '', text = '', link = ?, payload = ?, "
                           "updated_at = ? WHERE id = ?",
                           (link, json.dumps({"erase": state}), stamp, e["id"]))
            if first:
                _set(db, row["id"], stamp, text="", norm=erased_key(row["norm"]),
                     owner_text=None, owner_norm=erased_key(row["owner_norm"]), note=None,
                     subject="", subject_id=None, attribute=None, value=None,
                     valid_from=None, structured_at=stamp, erased_at=stamp)
                db.execute("UPDATE events SET detail = '' WHERE belief_id = ?", (row["id"],))
                _event(db, row["id"], "erased", "", stamp)
                if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                              "name = 'belief_vectors'").fetchone():
                    db.execute("DELETE FROM belief_vectors WHERE belief_id = ?", (row["id"],))
        _checkpoint()
        return {"belief": get_belief(owner, row["id"]), "texts": texts,
                "remaining": remaining, "pending": pending}


def _checkpoint() -> None:
    """Move the write-ahead log into the ledger file, so erased words leave the -wal
    file too (``secure_delete`` zeroes the freed pages)."""
    try:
        db = sqlite3.connect(str(db_path()), timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
    except sqlite3.Error as e:  # pragma: no cover - a busy reader; the next one does it
        logger.debug("ledger checkpoint skipped: %s", e)


def erase_pending(owner: str | None) -> list[dict]:
    """Erased beliefs with Athena evidence the module hasn't erased yet:
    ``[{"belief_id", "fact_ids"}]`` (for ``belief_forget.retry_pending``)."""
    out: dict[int, list[int]] = {}
    with _connect() as db:
        for r in db.execute("SELECT e.belief_id, e.source_id, e.payload FROM evidence e "
                            "JOIN beliefs b ON b.id = e.belief_id WHERE b.owner = ? AND "
                            "b.erased_at IS NOT NULL AND e.source = ?",
                            (owner or "", MODULE_SOURCE)):
            if _payload(r["payload"]).get("erase") != "done" and _int(r["source_id"]) > 0:
                out.setdefault(r["belief_id"], []).append(_int(r["source_id"]))
    return [{"belief_id": b, "fact_ids": sorted(ids)} for b, ids in sorted(out.items())]


def correct(owner: str | None, belief_id: int, text: str, *,
            now: datetime | None = None) -> dict:
    """The owner corrects a belief: ``owner_text`` is what memory shows from now on; the
    belief is confirmed by the owner and its structure is redone (``structured_at``
    cleared). Raises ValueError for empty text."""
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("a correction needs text")

    def apply(db, row, stamp, _today):
        _not_erased(row)
        _set(db, row["id"], stamp, owner_text=text,
             owner_norm=normalize(text, row["subject"], row["about"]),
             status="confirmed", confirmed_by="owner", structured_at=None)
        _event(db, row["id"], "corrected", "by owner", stamp)
    return _owner_action(owner, belief_id, now, apply)


def move(owner: str | None, belief_id: int, *, subject_id: int | None = None,
         name: str | None = None, now: datetime | None = None) -> dict:
    """The owner says a belief is about someone else (a line given to the wrong voice):
    it moves to ``subject_id``, or to the subject called ``name`` (created as a person
    when there is none; the owner's names give the owner subject). Its ``about``, matching
    keys and category follow the new subject, and it is confirmed by the owner, so the
    source can't move it back (:func:`_attach`). Raises ValueError (no target),
    :class:`SubjectNotFound`, :class:`BeliefErased`."""
    if subject_id is None and not _clean_name(name):
        raise ValueError("pick a subject or give a name")

    def apply(db, row, stamp, _today):
        _not_erased(row)
        cache = _Subjects(db, row["owner"])
        if subject_id is not None:
            target = _subject(db, row["owner"], subject_id)
        elif _is_owner_name(name):
            target = cache.row(cache.owner_subject("", stamp))
        else:
            display = _clean_name(name)
            found = cache.by_key.get(subject_key(display))
            target = cache.row(found if found is not None
                               else cache.create(display, "person", stamp))
        if target["id"] == row["subject_id"]:
            return
        about = {"owner": "owner", "person": "person"}.get(target["kind"], "other")
        shown = row["owner_text"] or row["text"]
        fields: dict[str, Any] = dict(
            subject_id=target["id"], subject=target["display"], about=about,
            norm=normalize(row["text"], target["display"], about),
            category=category_for(about, shown, _belief_kind(db, row["id"])),
            status="confirmed",
            confirmed_by="owner", structured_at=None)
        if row["owner_text"]:
            fields["owner_norm"] = normalize(row["owner_text"], target["display"], about)
        _set(db, row["id"], stamp, **fields)
        _event(db, row["id"], "moved", f"to {target['display']} by owner", stamp)
    return _owner_action(owner, belief_id, now, apply)


def _belief_kind(db: sqlite3.Connection, belief_id: int) -> str | None:
    """The fact kind a source gave this belief (live evidence first), or None."""
    rows = db.execute("SELECT payload FROM evidence WHERE belief_id = ? ORDER BY id",
                      (belief_id,)).fetchall()
    payloads = [_payload(r["payload"]) for r in rows]
    for p in sorted(payloads, key=lambda p: bool(p.get("gone"))):
        if str(p.get("kind") or "").strip().lower() in FACT_KINDS:
            return str(p["kind"]).strip().lower()
    return None


def scrub_quotes(owner: str | None, quotes: Iterable[str]) -> int:
    """Remove ``quotes`` (transcript lines a source sent with its evidence) from every
    evidence payload of this owner: after a forget erased a line, beliefs said on the
    same line must not keep showing its words. Returns how many evidence rows changed."""
    wanted = {q for q in quotes if q}
    if not wanted:
        return 0
    owner = owner or ""
    changed = 0
    with _WRITE:
        with _connect(write=True) as db:
            db.execute("PRAGMA secure_delete = ON")
            for r in db.execute("SELECT id, payload FROM evidence WHERE owner = ?"
                                " AND payload LIKE '%quote%'", (owner,)).fetchall():
                payload = _payload(r["payload"])
                if payload.get("quote") in wanted:
                    payload.pop("quote")
                    db.execute("UPDATE evidence SET payload = ? WHERE id = ?",
                               (json.dumps(payload, sort_keys=True, ensure_ascii=False),
                                r["id"]))
                    changed += 1
    return changed


def restore(owner: str | None, belief_id: int, *, now: datetime | None = None) -> dict:
    """A rejected or stale belief goes back to tentative, then the rule applies (it may
    confirm it again). To keep an old belief for good, confirm it instead."""
    def apply(db, row, stamp, today):
        _not_erased(row)
        if row["status"] not in ("rejected", "stale"):
            return
        _set(db, row["id"], stamp, status="tentative", confirmed_by=None)
        _event(db, row["id"], "restored", "by owner", stamp)
        _refresh(db, row["id"], stamp, settings(), today, stale_when_empty=False)
    return _owner_action(owner, belief_id, now, apply)


# ------------------------------------------------------------------------- reads
def list_beliefs(owner: str | None, status: str | Iterable[str] | None = None,
                 subject: str | None = None, limit: int = 500, *,
                 subject_id: int | None = None, attribute: str | None = None) -> list[dict]:
    """This owner's beliefs with their evidence, by subject then most recently seen.
    ``status``: one status or several; ``subject``: case-insensitive exact match on the
    source's subject name; ``subject_id``: one subject (:func:`list_subjects`);
    ``attribute``: one of :data:`ATTRIBUTES` (structured beliefs only)."""
    owner = owner or ""
    where, args = ["b.owner = ?"], [owner]
    if status:
        wanted = [status] if isinstance(status, str) else list(status)
        bad = [s for s in wanted if s not in STATUSES]
        if bad:
            raise ValueError(f"unknown status: {', '.join(map(str, bad))}")
        where.append(f"b.status IN ({','.join('?' * len(wanted))})")
        args += wanted
    if subject:
        where.append("b.subject = ? COLLATE NOCASE")
        args.append(subject.strip())
    if subject_id is not None:
        where.append("b.subject_id = ?")
        args.append(_int(subject_id, -1))
    if attribute is not None:
        if attribute not in ATTRIBUTES:
            raise ValueError(f"unknown attribute: {attribute}")
        where.append("b.attribute = ?")
        args.append(attribute)
    with _connect() as db:
        rows = db.execute(f"{_BELIEF_SELECT} WHERE {' AND '.join(where)} "
                          "ORDER BY b.subject COLLATE NOCASE, b.last_seen DESC, b.id "
                          "LIMIT ?", (*args, max(1, int(limit)))).fetchall()
        evidence = _evidence_rows(db, [r["id"] for r in rows])
    return [_belief_dict(r, evidence[r["id"]]) for r in rows]


def get_belief(owner: str | None, belief_id: int) -> dict:
    """One belief with its evidence and recent events (newest first). Raises
    :class:`BeliefNotFound`."""
    owner = owner or ""
    with _connect() as db:
        row = _belief(db, owner, belief_id)
        row = db.execute(f"{_BELIEF_SELECT} WHERE b.id = ?", (row["id"],)).fetchone()
        out = _belief_dict(row, _evidence_rows(db, [row["id"]])[row["id"]])
        out["events"] = [dict(e) for e in db.execute(
            "SELECT at, kind, detail FROM events WHERE belief_id = ? ORDER BY id DESC LIMIT 20",
            (row["id"],))]
    return out


def export_rows(owner: str | None) -> dict[str, list[dict]]:
    """Everything the ledger holds for this owner, for the owner's export
    (``src/belief_export.py``): ``beliefs`` (every status, each with its ``evidence`` and
    all kept ``events``, oldest first), ``subjects`` and ``subject_sources``. Matching keys
    (``norm``, ``owner_norm``) are internal and left out; an erased belief has no words."""
    owner = owner or ""
    with _connect() as db:
        rows = db.execute(f"{_BELIEF_SELECT} WHERE b.owner = ? ORDER BY b.id",
                          (owner,)).fetchall()
        ids = [r["id"] for r in rows]
        evidence = _evidence_rows(db, ids)
        events: dict[int, list[dict]] = {i: [] for i in ids}
        for e in _in_chunks(db, "SELECT e.belief_id, e.at, e.kind, e.detail FROM events e "
                            "JOIN beliefs b ON b.id = e.belief_id WHERE b.owner = ? AND "
                            "e.belief_id IN ({marks}) ORDER BY e.id", (owner,), ids):
            events[e["belief_id"]].append({"at": e["at"], "kind": e["kind"],
                                           "detail": e["detail"]})
        subjects = [dict(r) for r in db.execute(
            "SELECT * FROM subjects WHERE owner = ? ORDER BY id", (owner,))]
        sources = [dict(r) for r in db.execute(
            "SELECT * FROM subject_sources WHERE owner = ? ORDER BY subject_id, source",
            (owner,))]
    beliefs = []
    for r in rows:
        b = _belief_dict(r, evidence[r["id"]])
        b["events"] = events[r["id"]]
        beliefs.append(b)
    for s in subjects:
        s["aliases"] = _aliases(s.get("aliases"))
    return {"beliefs": beliefs, "subjects": subjects, "subject_sources": sources}


def stats(owner: str | None) -> dict[str, int]:
    """Counts per status, plus ``total`` and ``structured`` (beliefs the Belief Structure
    task has given an attribute)."""
    out = dict.fromkeys(STATUSES, 0)
    with _connect() as db:
        for row in db.execute("SELECT status, COUNT(*) AS n FROM beliefs WHERE owner = ? "
                              "GROUP BY status", (owner or "",)):
            out[row["status"]] = row["n"]
        structured = db.execute("SELECT COUNT(*) FROM beliefs WHERE owner = ? AND "
                                "structured_at IS NOT NULL", (owner or "",)).fetchone()[0]
    out["total"] = sum(out[s] for s in STATUSES)
    out["structured"] = structured
    return out


def beliefs_by_attribute(owner: str | None, attribute: str, subject_id: int | None = None,
                         *, status: str | Iterable[str] | None = "confirmed") -> list[dict]:
    """Structured beliefs with this ``attribute`` (one of :data:`ATTRIBUTES`), optionally
    of one subject; confirmed ones unless ``status`` says otherwise (None: any status).
    Raises ValueError for an unknown attribute."""
    return list_beliefs(owner, status=status, subject_id=subject_id, attribute=attribute)


# ----------------------------------------------------------------- subjects (2.1)
# Belief counts per subject and status, and source person ids, in one query each (not
# one per subject); ``{where}`` narrows to one subject.
SUBJECT_COUNTS_SQL = ("SELECT subject_id, status, COUNT(*) AS n FROM beliefs "
                      "INDEXED BY beliefs_owner_subject WHERE owner = ?{where} "
                      "GROUP BY subject_id, status")
SUBJECT_SOURCES_SQL = ("SELECT subject_id, source, source_person_id, name FROM subject_sources "
                       "WHERE owner = ?{where} ORDER BY subject_id, source, source_person_id")


def _subject_extras(db: sqlite3.Connection, owner: str, subject_id: int | None = None
                    ) -> tuple[dict[int, dict[str, int]], dict[int, list[dict]]]:
    where, args = ("", (owner,)) if subject_id is None else \
        (" AND subject_id = ?", (owner, subject_id))
    counts: dict[int, dict[str, int]] = {}
    for r in db.execute(SUBJECT_COUNTS_SQL.format(where=where), args):
        if r["subject_id"] is not None:
            counts.setdefault(r["subject_id"], {})[r["status"]] = r["n"]
    sources: dict[int, list[dict]] = {}
    for r in db.execute(SUBJECT_SOURCES_SQL.format(where=where), args):
        sources.setdefault(r["subject_id"], []).append(
            {"source": r["source"], "person_id": r["source_person_id"], "name": r["name"]})
    return counts, sources


def _subject_dict(db: sqlite3.Connection, row: Mapping,
                  extras: tuple[dict, dict] | None = None) -> dict:
    counts_by, sources_by = extras or _subject_extras(db, row["owner"], row["id"])
    found = counts_by.get(row["id"], {})
    counts = {s: found.get(s, 0) for s in STATUSES}
    counts["total"] = sum(counts[s] for s in STATUSES)
    sources = sources_by.get(row["id"], [])
    return {
        "id": row["id"], "owner": row["owner"], "key": row["key"], "display": row["display"],
        "kind": row["kind"], "kind_by": row["kind_by"], "contact_id": row["contact_id"],
        "aliases": _aliases(row["aliases"]), "note": row["note"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "sources": sources, "counts": counts,
    }


def _subject(db: sqlite3.Connection, owner: str, subject_id: Any) -> sqlite3.Row:
    row = db.execute("SELECT * FROM subjects WHERE id = ? AND owner = ?",
                     (_int(subject_id, -1), owner)).fetchone()
    if row is None:
        raise SubjectNotFound(f"subject {subject_id} not found")
    return row


def list_subjects(owner: str | None, kind: str | None = None) -> list[dict]:
    """This owner's subjects (the owner first, then by name), each with ``counts`` of its
    beliefs per status and ``total``, its ``aliases`` and the source person ids mapped to
    it (``sources``). ``kind``: person, project, thing or owner."""
    owner = owner or ""
    if kind is not None and kind not in SUBJECT_KINDS:
        raise ValueError(f"unknown kind: {kind}")
    where, args = "owner = ?", [owner]
    if kind is not None:
        where += " AND kind = ?"
        args.append(kind)
    with _connect() as db:
        rows = db.execute(f"SELECT * FROM subjects WHERE {where} ORDER BY kind != 'owner', "
                          "display COLLATE NOCASE, id", args).fetchall()
        extras = _subject_extras(db, owner)
        return [_subject_dict(db, r, extras) for r in rows]


def get_subject(owner: str | None, subject_id: int) -> dict:
    """One subject (see :func:`list_subjects`). Raises :class:`SubjectNotFound`."""
    owner = owner or ""
    with _connect() as db:
        return _subject_dict(db, _subject(db, owner, subject_id))


def find_subject(owner: str | None, name: str) -> dict | None:
    """The subject whose name or alias matches ``name`` (case-insensitive), or None."""
    owner = owner or ""
    with _connect() as db:
        subject_id = _Subjects(db, owner).by_key.get(subject_key(name))
        return _subject_dict(db, _subject(db, owner, subject_id)) if subject_id else None


def _subject_action(owner: str | None, subject_id: int, now: datetime | None, apply,
                    *, mirror: bool = True) -> dict:
    owner = owner or ""
    now = _now(now)
    with _WRITE:
        with _connect(write=True) as db:
            row = _subject(db, owner, subject_id)
            kept = apply(db, row, _stamp(now))
            touched = [r["id"] for r in db.execute(
                "SELECT id FROM beliefs WHERE owner = ? AND subject_id = ?",
                (owner, kept or row["id"]))]
        if mirror:
            try:
                sync_memory(owner, now=now, belief_ids=touched)
            except RuntimeError as e:  # MemoryStoreUnreadable: the next facts run mirrors it
                logger.error("subject %s saved, memory not updated: %s", row["id"], e)
        return get_subject(owner, kept or row["id"])


def set_subject_kind(owner: str | None, subject_id: int, kind: str, *,
                     now: datetime | None = None) -> dict:
    """The owner sets a subject's kind (person, project or thing); inference never changes
    it afterwards. The owner subject keeps its kind. Raises ValueError."""
    if kind not in ("person", "project", "thing"):
        raise ValueError(f"kind must be person, project or thing, not {kind!r}")

    def apply(db, row, stamp):
        if row["kind"] == "owner":
            raise ValueError("the owner subject keeps its kind")
        db.execute("UPDATE subjects SET kind = ?, kind_by = 'owner', updated_at = ? "
                   "WHERE id = ?", (kind, stamp, row["id"]))
    return _subject_action(owner, subject_id, now, apply)


def link_contact(owner: str | None, subject_id: int, contact_id: str, *,
                 now: datetime | None = None) -> dict:
    """Link a subject to an Odysseus contact (its ``uid``). A contact links to one subject
    per owner: linking it to a second raises ValueError (merge the two subjects instead)."""
    contact_id = str(contact_id or "").strip()
    if not contact_id:
        raise ValueError("a contact id is required")

    def apply(db, row, stamp):
        other = db.execute("SELECT id FROM subjects WHERE owner = ? AND contact_id = ? "
                           "AND id != ?", (row["owner"], contact_id, row["id"])).fetchone()
        if other is not None:
            raise ValueError(f"contact {contact_id} is linked to subject {other['id']}")
        db.execute("UPDATE subjects SET contact_id = ?, updated_at = ? WHERE id = ?",
                   (contact_id, stamp, row["id"]))
    return _subject_action(owner, subject_id, now, apply)


def unlink_contact(owner: str | None, subject_id: int, *, now: datetime | None = None) -> dict:
    """Remove a subject's contact link."""
    def apply(db, row, stamp):
        if row["contact_id"] is not None:
            db.execute("UPDATE subjects SET contact_id = NULL, updated_at = ? WHERE id = ?",
                       (stamp, row["id"]))
    return _subject_action(owner, subject_id, now, apply)


def rename_subject(owner: str | None, subject_id: int, display: str, *,
                   now: datetime | None = None) -> dict:
    """The owner renames a subject; the old name becomes an alias. Raises ValueError for
    an empty name or one another subject already has (merge them instead)."""
    display = _clean_name(display)
    key = subject_key(display)
    if not key:
        raise ValueError("a subject needs a name")

    def apply(db, row, stamp):
        other = db.execute("SELECT id FROM subjects WHERE owner = ? AND key = ? AND id != ?",
                           (row["owner"], key, row["id"])).fetchone()
        if other is not None:
            raise ValueError(f"subject {other['id']} is already called {display!r}")
        aliases = [a for a in _aliases(row["aliases"]) if subject_key(a) != key]
        if row["key"] != key and not any(subject_key(a) == row["key"] for a in aliases):
            aliases.append(row["display"])
        db.execute("UPDATE subjects SET key = ?, display = ?, aliases = ?, updated_at = ? "
                   "WHERE id = ?", (key, display, json.dumps(aliases, ensure_ascii=False),
                                    stamp, row["id"]))
    return _subject_action(owner, subject_id, now, apply, mirror=False)


def merge_subjects(owner: str | None, keep_id: int, drop_id: int, *,
                   now: datetime | None = None) -> dict:
    """Merge ``drop_id`` into ``keep_id``: its beliefs and source person ids move, its
    name and aliases join the kept aliases, its contact link is kept when the kept subject
    has none, and its kind when the kept one is an inferred ``thing``. The dropped subject
    is deleted. The owner subject can be kept, not dropped. Raises ValueError,
    :class:`SubjectNotFound`."""
    if _int(keep_id, -1) == _int(drop_id, -2):
        raise ValueError("a subject can't be merged into itself")

    def apply(db, keep, stamp):
        drop = _subject(db, keep["owner"], drop_id)
        if drop["kind"] == "owner":
            raise ValueError("the owner subject can only be kept")
        db.execute("UPDATE beliefs SET subject_id = ? WHERE subject_id = ? AND owner = ?",
                   (keep["id"], drop["id"], keep["owner"]))
        db.execute("UPDATE subject_sources SET subject_id = ? WHERE subject_id = ?",
                   (keep["id"], drop["id"]))
        aliases = _aliases(keep["aliases"])
        keys = {keep["key"], *(subject_key(a) for a in aliases)}
        for name in (drop["display"], *_aliases(drop["aliases"])):
            if subject_key(name) not in keys:
                keys.add(subject_key(name))
                aliases.append(name)
        kind, kind_by = keep["kind"], keep["kind_by"]
        if kind == "thing" and kind_by == "auto" and drop["kind"] != "thing":
            kind, kind_by = drop["kind"], drop["kind_by"]
        db.execute("DELETE FROM subjects WHERE id = ?", (drop["id"],))
        db.execute("UPDATE subjects SET aliases = ?, contact_id = ?, kind = ?, kind_by = ?, "
                   "updated_at = ? WHERE id = ?",
                   (json.dumps(aliases, ensure_ascii=False),
                    keep["contact_id"] or drop["contact_id"], kind, kind_by, stamp,
                    keep["id"]))
    return _subject_action(owner, keep_id, now, apply)


def beliefs_for_contact(owner: str | None, contact_id: str,
                        status: str | Iterable[str] | None = None) -> list[dict]:
    """The beliefs of the subject linked to this Odysseus contact (none when no subject
    is linked); ``status`` as in :func:`list_beliefs`."""
    owner = owner or ""
    contact_id = str(contact_id or "").strip()
    if not contact_id:
        return []
    with _connect() as db:
        row = db.execute("SELECT id FROM subjects WHERE owner = ? AND contact_id = ?",
                         (owner, contact_id)).fetchone()
    return list_beliefs(owner, status=status, subject_id=row["id"]) if row else []


def sync_people(owner: str | None, source: str, people: Iterable[Mapping], *,
                now: datetime | None = None, mirror: bool = True) -> dict[str, int]:
    """A source's people (Athena's ``list_people`` JSON: ``id``, ``name``,
    ``aliases``, ``is_owner``) as subjects. Each id maps to a subject: the one it already
    maps to, the owner subject for ``is_owner``, else the subject whose name or alias
    matches, else a new ``person``. Names and aliases the subject doesn't have become
    aliases; a matched inferred ``thing`` becomes a ``person``. A person the source no
    longer lists (e.g. merged there) keeps its mapping. When the source marks no one as
    the owner, a person whose name is an owner word (Me, I, Myself, Owner, User …) is the
    owner: its id maps to the owner subject and its name becomes an owner alias (a stray
    subject an earlier pass made for it, still empty, is removed). Returns counts
    ``people``, ``subjects_added``, ``mapped`` (new or moved mappings) and
    ``aliases_added``."""
    owner = owner or ""
    now = _now(now)
    stamp = _stamp(now)
    counts = dict.fromkeys(("people", "subjects_added", "mapped", "aliases_added"), 0)
    with _WRITE:
        with _connect(write=True) as db:
            cache = _Subjects(db, owner)
            people = [p for p in people if isinstance(p, Mapping)]
            owner_reported = any(_truthy(p.get("is_owner")) for p in people)
            for person in people:
                person_id = str(person.get("id") if person.get("id") is not None else "").strip()
                name = _clean_name(person.get("name"))
                if not person_id:
                    continue
                counts["people"] += 1
                aliases = person.get("aliases") if isinstance(person.get("aliases"), list) \
                    else []
                if _truthy(person.get("is_owner")):
                    had_owner = cache.owner_id is not None
                    subject_id = cache.owner_subject(name, stamp)
                    counts["subjects_added"] += 0 if had_owner else 1
                    _name_owner(cache, subject_id, name, stamp)
                    moved = cache.map(subject_id, source, person_id, name, stamp, move=True)
                elif name and _is_owner_name(name) and not owner_reported:
                    # an owner word with no is_owner person: the module's owner by another name
                    had_owner = cache.owner_id is not None
                    stray = cache.mapped(source, person_id)
                    subject_id = cache.owner_subject(name, stamp)
                    counts["subjects_added"] += 0 if had_owner else 1
                    counts["aliases_added"] += cache.add_alias(subject_id, name, stamp)
                    moved = cache.map(subject_id, source, person_id, name, stamp, move=True)
                    if stray is not None and stray != subject_id:
                        _drop_empty_owner_word(cache, stray)
                        cache.by_key[subject_key(name)] = subject_id
                else:
                    subject_id = cache.mapped(source, person_id)
                    if subject_id is None:
                        subject_id = next((cache.by_key[k] for k in map(
                            subject_key, (name, *aliases)) if k and k in cache.by_key), None)
                    if subject_id is None:
                        if not name:
                            continue
                        subject_id = cache.create(name, "person", stamp)
                        counts["subjects_added"] += 1
                    else:
                        _person_kind(cache, subject_id, stamp)
                    moved = cache.map(subject_id, source, person_id, name, stamp)
                    for alias in (name, *aliases):
                        counts["aliases_added"] += cache.add_alias(subject_id, alias, stamp)
                counts["mapped"] += int(moved)
                if not moved and name:
                    db.execute("UPDATE subject_sources SET name = ? WHERE owner = ? AND "
                               "source = ? AND source_person_id = ? AND name != ?",
                               (name, owner, source, person_id, name))
        if mirror:
            sync_memory(owner, now=now)
    return counts


def _drop_empty_owner_word(cache: _Subjects, subject_id: int) -> None:
    """Remove a subject an earlier people pass made for the owner under an owner word
    ("Me"): only when nothing else uses it (no beliefs, source ids, contact, owner's kind)."""
    row = cache.row(subject_id)
    db = cache.db
    if (row is None or row["kind"] == "owner" or row["kind_by"] != "auto"
            or row["contact_id"] or not _is_owner_name(row["display"])
            or db.execute("SELECT 1 FROM beliefs WHERE subject_id = ? LIMIT 1",
                          (subject_id,)).fetchone()
            or db.execute("SELECT 1 FROM subject_sources WHERE subject_id = ? LIMIT 1",
                          (subject_id,)).fetchone()):
        return
    db.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
    for key in [k for k, v in cache.by_key.items() if v == subject_id]:
        del cache.by_key[key]


def _name_owner(cache: _Subjects, subject_id: int, name: str, stamp: str) -> None:
    """An owner subject still called "Owner" takes the owner's real name (the old name
    stays an alias)."""
    row = cache.row(subject_id)
    key = subject_key(name)
    if (row is None or _is_owner_name(name) or row["key"] != subject_key(OWNER_DISPLAY)
            or (key in cache.by_key and cache.by_key[key] != subject_id)):
        return
    aliases = [a for a in _aliases(row["aliases"]) if subject_key(a) != key]
    cache.db.execute("UPDATE subjects SET key = ?, display = ?, aliases = ?, updated_at = ? "
                     "WHERE id = ?", (key, name, json.dumps([*aliases, row["display"]],
                                                            ensure_ascii=False),
                                      stamp, subject_id))
    cache.by_key[key] = subject_id


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


# ---------------------------------------------------------------- structure (2.2)
def structure_candidates(owner: str | None, limit: int = 40) -> list[dict]:
    """Confirmed beliefs the Belief Structure task hasn't structured yet, or whose shown
    text changed since (a correction or a source edit clears ``structured_at``), oldest
    first: ``id``, ``subject`` (the subject's name), ``kind`` (the subject's kind),
    ``text`` (what memory shows), ``first_seen`` and ``last_seen`` (when it was said)."""
    owner = owner or ""
    with _connect() as db:
        rows = db.execute(f"{_BELIEF_SELECT} WHERE b.owner = ? AND b.status = 'confirmed' "
                          "AND b.structured_at IS NULL ORDER BY b.id LIMIT ?",
                          (owner, max(1, int(limit)))).fetchall()
    return [{"id": r["id"], "subject": r["subject_display"] or r["subject"] or "",
             "kind": r["subject_kind"] or "", "text": r["owner_text"] or r["text"],
             "first_seen": r["first_seen"] or "", "last_seen": r["last_seen"] or ""}
            for r in rows]


def clean_attribute(value: Any) -> str:
    """One of :data:`ATTRIBUTES` (anything else: ``other``)."""
    key = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key if key in ATTRIBUTES else "other"


def clean_value(value: Any) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS - 1].rstrip() + "…"


def apply_structure(owner: str | None, belief_id: int, attribute: Any, value: Any,
                    valid_from: Any = None, valid_to: Any = None, *,
                    expected_text: str | None = None, now: datetime | None = None) -> bool:
    """Write a belief's structure: ``attribute`` (unknown -> ``other``), ``value`` (at most
    120 characters), ``valid_from`` (an ISO date or None) and ``structured_at`` = now;
    ``valid_to`` becomes ``valid_until`` only when the belief has none yet, and never when
    it is in the past or before the belief was last seen (a guessed end date must not
    expire a current belief). Then a
    ``project_status`` or ``deadline`` makes the belief's subject a project (when it is an
    inferred thing that no source knows as a person). Returns False (nothing written) when
    the belief is gone, no longer confirmed, or its shown text isn't ``expected_text``."""
    owner = owner or ""
    now = _now(now)
    stamp = _stamp(now)
    attribute = clean_attribute(attribute)
    with _WRITE, _connect(write=True) as db:
        row = db.execute("SELECT * FROM beliefs WHERE id = ? AND owner = ?",
                         (_int(belief_id, -1), owner)).fetchone()
        if row is None or row["status"] != "confirmed":
            return False
        if expected_text is not None and (row["owner_text"] or row["text"]) != expected_text:
            return False
        fields: dict[str, Any] = {"attribute": attribute, "value": clean_value(value),
                                  "valid_from": _day(valid_from), "structured_at": stamp}
        until = _day(valid_to)
        if (until and not row["valid_until"] and until >= now.date().isoformat()
                and until >= (row["last_seen"] or "")):
            fields["valid_until"] = until
        _set(db, row["id"], stamp, **fields)
        if attribute in PROJECT_ATTRIBUTES and row["subject_id"] is not None:
            _infer_project(db, row["subject_id"], stamp)
    return True


def _infer_project(db: sqlite3.Connection, subject_id: int, stamp: str) -> None:
    row = db.execute("SELECT kind, kind_by FROM subjects WHERE id = ?", (subject_id,)).fetchone()
    if row is None or row["kind"] != "thing" or row["kind_by"] != "auto":
        return
    if db.execute("SELECT 1 FROM subject_sources WHERE subject_id = ? LIMIT 1",
                  (subject_id,)).fetchone():
        return  # a source knows it as a person
    db.execute("UPDATE subjects SET kind = 'project', updated_at = ? WHERE id = ?",
               (stamp, subject_id))


def subject_summary(owner: str | None, subject_id: int) -> str:
    """One paragraph on a subject from its confirmed beliefs, grouped by attribute in
    vocabulary order (``value``, ``since <valid_from>``); unstructured beliefs follow as
    their text under "Also". Empty when nothing about it is confirmed. Raises
    :class:`SubjectNotFound`."""
    subject = get_subject(owner, subject_id)
    beliefs = list_beliefs(owner, status="confirmed", subject_id=subject["id"])
    if not beliefs:
        return ""
    groups: dict[str, list[str]] = {}
    loose: list[str] = []
    for b in sorted(beliefs, key=lambda b: (b["valid_from"] or "", b["id"])):
        shown = b["owner_text"] or b["text"]
        if not b["attribute"] or b["attribute"] == "other" or not b["value"]:
            loose.append(shown.rstrip(" .") + ".")
            continue
        item = b["value"] + (f" (since {b['valid_from']})" if b["valid_from"] else "")
        groups.setdefault(b["attribute"], []).append(item)
    parts = [f"{a.replace('_', ' ')}: {'; '.join(groups[a])}." for a in ATTRIBUTES
             if a in groups]
    if loose:
        parts.append("Also: " + " ".join(loose))
    return f"{subject['display']} ({subject['kind']}). " + " ".join(parts)


# ------------------------------------------------------------------------- memory
def memory_fields(belief: dict) -> dict[str, Any]:
    """The memory fields that mirror a confirmed belief (besides id, owner, uses ...)."""
    evidence = belief.get("evidence") or []
    main = primary_evidence(evidence)
    link = main["link"] if main else ""
    shown = belief.get("owner_text") or belief["text"]
    fields: dict[str, Any] = {
        "text": f"{shown} (source: {link})" if link else shown,
        "category": belief["category"],
        "source": main["source"] if main else "beliefs",
        "belief_id": belief["id"],
        "subject": belief["subject"],
        "about": belief["about"],
        "link": link,
        "first_seen": belief["first_seen"] or "",
        "last_seen": belief["last_seen"] or "",
        "subject_id": belief.get("subject_id"),
        "subject_kind": belief.get("subject_kind") or "",
    }
    if belief.get("contact_id"):
        fields["contact_id"] = belief["contact_id"]
    module = primary_evidence([e for e in evidence if e["source"] == MODULE_SOURCE])
    if module is not None:  # the Phase 0 copy's fields, kept for older readers
        fields.update(module_fact_id=_int(module["source_id"]), times_said=module["times"],
                      days=module["days"], first_said=module["seen_first"] or "",
                      last_said=module["seen_last"] or "")
    return fields


def _in_chunks(db: sqlite3.Connection, sql: str, head: tuple, ids: list[int]) -> list:
    """``sql`` with ``{marks}`` run over ``ids`` in chunks of 500 (SQLite's variable cap)."""
    out: list = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        out += db.execute(sql.format(marks=",".join("?" * len(chunk))), (*head, *chunk)
                          ).fetchall()
    return out


def sync_memory(owner: str | None, *, now: datetime | None = None,
                belief_ids: Iterable[int] | None = None) -> dict[str, int]:
    """Exactly one Odysseus memory per confirmed belief of this owner.

    ``belief_ids`` (the owner actions, subject edits): only the memory entries of these
    beliefs are looked at and changed (added, updated or removed); every other entry is
    left exactly as it is. Without it (the jobs), the whole store is reconciled.

    A mirrored entry carries ``belief_id``; a Phase 0 copy (``source: odysseus-sound``,
    ``module_fact_id``, no ``belief_id``) is adopted by the belief that has that fact id
    as evidence. Entries are updated in place (memory id and ``uses`` kept), copies of
    beliefs that are no longer confirmed are deleted, and nothing is written when
    nothing changed. The owner's own memories and other owners' entries are never
    touched. Same discipline as upstream's memory writers: a strict read
    (``load_all_for_update``) and the save right after. Returns
    kept/added/updated/removed."""
    from src.constants import DATA_DIR
    from src.memory import MemoryManager

    owner = owner or ""
    now = _now(now)
    stamp = _stamp(now)
    counts = {"kept": 0, "added": 0, "updated": 0, "removed": 0}
    only = None if belief_ids is None else {_int(i, -1) for i in belief_ids}
    if only is not None and not only:
        return counts
    with _WRITE:
        with _connect() as db:
            if only is None:
                rows = db.execute(f"{_BELIEF_SELECT} WHERE b.owner = ? AND "
                                  "b.status = 'confirmed' ORDER BY b.id", (owner,)).fetchall()
                fact_rows = db.execute("SELECT source_id, belief_id FROM evidence "
                                       "WHERE owner = ? AND source = ?",
                                       (owner, MODULE_SOURCE)).fetchall()
                link_rows = db.execute("SELECT id, memory_id FROM beliefs WHERE owner = ? "
                                       "AND memory_id IS NOT NULL", (owner,)).fetchall()
            else:
                ids = sorted(only)
                rows = _in_chunks(db, f"{_BELIEF_SELECT} WHERE b.owner = ? AND "
                                  "b.status = 'confirmed' AND b.id IN ({marks}) ORDER BY b.id",
                                  (owner,), ids)
                fact_rows = _in_chunks(db, "SELECT source_id, belief_id FROM evidence WHERE "
                                       "owner = ? AND source = ? AND belief_id IN ({marks})",
                                       (owner, MODULE_SOURCE), ids)
                link_rows = _in_chunks(db, "SELECT id, memory_id FROM beliefs WHERE owner = ? "
                                       "AND memory_id IS NOT NULL AND id IN ({marks})",
                                       (owner,), ids)
            evidence = _evidence_rows(db, [r["id"] for r in rows])
            want = {r["id"]: memory_fields(_belief_dict(r, evidence[r["id"]])) for r in rows}
            by_fact = {e["source_id"]: e["belief_id"] for e in fact_rows}
            linked = {r["id"]: r["memory_id"] for r in link_rows}
        manager = MemoryManager(DATA_DIR)
        entries = manager.load_all_for_update()
        out: list[dict] = []
        indexed: list[dict] = []
        removed: list[str] = []
        placed: dict[int, str] = {}
        for entry in entries:
            if (entry.get("owner") or "") != owner:
                out.append(entry)
                continue
            belief_id = entry.get("belief_id")
            if belief_id is None:
                if entry.get("source") != MODULE_SOURCE or "module_fact_id" not in entry:
                    out.append(entry)  # the owner's own memory
                    continue
                belief_id = by_fact.get(str(_int(entry.get("module_fact_id"))))
                if belief_id is None and (not by_fact or only is not None):
                    out.append(entry)  # the ledger hasn't heard from the module yet
                    continue
            belief_id = _int(belief_id, -1)
            if only is not None and belief_id not in only:
                out.append(entry)  # a partial sync: not one of the beliefs asked for
                continue
            if belief_id not in want or belief_id in placed:
                counts["removed"] += 1
                removed.append(str(entry.get("id") or ""))
                continue
            fields = want[belief_id]
            stale_keys = [k for k in OPTIONAL_FIELDS if k in entry and k not in fields]
            if stale_keys or any(entry.get(k) != v for k, v in fields.items()):
                if entry.get("text") != fields["text"]:
                    indexed.append(entry)
                for key in stale_keys:
                    entry.pop(key)
                entry.update(fields, synced_at=stamp, timestamp=int(now.timestamp()))
                counts["updated"] += 1
            else:
                counts["kept"] += 1
            placed[belief_id] = str(entry.get("id") or "")
            out.append(entry)
        for belief_id, fields in want.items():
            if belief_id in placed:
                continue
            entry = manager.add_entry(fields["text"], source=fields["source"],
                                      category=fields["category"], owner=owner or None)
            entry.update(fields, synced_at=stamp, timestamp=int(now.timestamp()))
            out.append(entry)
            indexed.append(entry)
            placed[belief_id] = entry["id"]
            counts["added"] += 1
        if counts["added"] or counts["updated"] or counts["removed"]:
            manager.save(out)
            update_vector_index(indexed, removed)
        changes = {b: m for b, m in placed.items() if linked.get(b) != m}
        dropped = [b for b in linked if b not in placed]
        if changes or dropped:
            with _connect(write=True) as db:
                for belief_id, memory_id in changes.items():
                    db.execute("UPDATE beliefs SET memory_id = ? WHERE id = ?",
                               (memory_id, belief_id))
                for belief_id in dropped:
                    db.execute("UPDATE beliefs SET memory_id = NULL WHERE id = ?",
                               (belief_id,))
    return counts


def update_vector_index(indexed: list[dict], removed: list[str]) -> None:
    """Best effort, like upstream's manage_memory: the index Odysseus loaded, if any."""
    ai = sys.modules.get("src.ai_interaction")
    vector = getattr(ai, "_memory_vector", None) if ai is not None else None
    if vector is None or not getattr(vector, "healthy", False):
        return
    for entry in indexed:
        try:
            vector.add(entry["id"], entry["text"])
        except Exception:
            logger.debug("memory vector add failed", exc_info=True)
    for memory_id in removed:
        try:
            vector.remove(memory_id)
        except Exception:
            logger.debug("memory vector remove failed", exc_info=True)
