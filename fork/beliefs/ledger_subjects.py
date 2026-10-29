"""Fork: the belief ledger's subjects (``fork/beliefs/evidence_intake.py``).

``_Subjects`` matches names, aliases and source person ids to an owner's subjects inside
one transaction (intake and the subjects migration use it); the rest is the subjects
API: list, get, find, kind, contact link, rename, merge, and a source's people
(:func:`sync_people`).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from fork.beliefs.ledger_db import _WRITE, _connect
from fork.beliefs.ledger_memory import sync_memory
from fork.beliefs.ledger_model import (
    GENERIC_SUBJECT,
    OWNER_DISPLAY,
    PROJECT_WORDS,
    STATUSES,
    SUBJECT_KINDS,
    SubjectNotFound,
    _aliases,
    _clean_name,
    _int,
    _is_owner_name,
    _now,
    _stamp,
    subject_key,
)

logger = logging.getLogger("fork.beliefs.evidence_intake")  # the ledger's one logger


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
