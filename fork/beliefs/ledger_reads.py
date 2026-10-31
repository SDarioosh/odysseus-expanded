"""Fork: reading the belief ledger (``fork/beliefs/evidence_intake.py``).

Beliefs with their evidence (:func:`list_beliefs`, :func:`get_belief`, by attribute or
by linked contact), the owner's export and the counts per status.
"""

from __future__ import annotations

from collections.abc import Iterable

from fork.beliefs.ledger_db import _connect
from fork.beliefs.ledger_model import ATTRIBUTES, STATUSES, _aliases, _int
from fork.beliefs.ledger_rows import (
    _BELIEF_SELECT,
    _belief,
    _belief_dict,
    _evidence_rows,
    _in_chunks,
)


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
