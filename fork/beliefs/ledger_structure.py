"""Fork: a belief's structure in the ledger (``fork/beliefs/evidence_intake.py``).

The Belief Structure task (``fork/beliefs/structure.py``) reads
:func:`structure_candidates` and writes :func:`apply_structure`; :func:`subject_summary`
is one paragraph on a subject from its confirmed, structured beliefs.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from fork.beliefs.ledger_db import _WRITE, _connect
from fork.beliefs.ledger_model import (
    ATTRIBUTES,
    MAX_VALUE_CHARS,
    PROJECT_ATTRIBUTES,
    _day,
    _int,
    _now,
    _stamp,
)
from fork.beliefs.ledger_reads import list_beliefs
from fork.beliefs.ledger_rows import _BELIEF_SELECT, _set
from fork.beliefs.ledger_subjects import get_subject


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
