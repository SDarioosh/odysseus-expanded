"""Fork: the belief ledger's mirror in Odysseus memory (``fork/beliefs/evidence_intake.py``).

:func:`sync_memory` keeps exactly one Odysseus memory per confirmed belief, updated in
place, and :func:`update_vector_index` keeps upstream's memory vector index in step.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from fork.beliefs.ledger_db import _WRITE, _connect
from fork.beliefs.ledger_model import MODULE_SOURCE, OPTIONAL_FIELDS, _int, _now, _stamp
from fork.beliefs.ledger_rows import (
    _BELIEF_SELECT,
    _belief_dict,
    _evidence_rows,
    _in_chunks,
    primary_evidence,
)

logger = logging.getLogger("fork.beliefs.evidence_intake")  # the ledger's one logger


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
    from fork.upstream import memory_vector

    vector = memory_vector()
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
