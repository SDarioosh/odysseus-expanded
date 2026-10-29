"""Fork: the belief ledger's rows and its confirmation rule (``fork/beliefs/evidence_intake.py``).

Reading and writing one belief's rows inside a transaction (events, evidence, the
belief's dict as callers get it), the rule (:func:`confirmation_for`) and
``_refresh``, which recomputes a belief's dates and status from its evidence.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Any

from fork.beliefs.ledger_model import (
    EVENTS_KEPT,
    MODULE_SOURCE,
    BeliefNotFound,
    Settings,
    _day,
    _int,
    _payload,
    _read_settings,
    is_module_source,
)


def _event(db: sqlite3.Connection, belief_id: int, kind: str, detail: str, stamp: str) -> None:
    db.execute("INSERT INTO events (belief_id, at, kind, detail) VALUES (?, ?, ?, ?)",
               (belief_id, stamp, kind, detail[:300]))
    db.execute("DELETE FROM events WHERE belief_id = ? AND id NOT IN (SELECT id FROM events "
               "WHERE belief_id = ? ORDER BY id DESC LIMIT ?)",
               (belief_id, belief_id, EVENTS_KEPT))


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
    return 2 if is_module_source(e.get("source")) else 1


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
    # Athena under its old and new name is one source
    if len({MODULE_SOURCE if is_module_source(e["source"]) else e["source"]
            for e in live}) >= st.confirm_sources:
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


def _in_chunks(db: sqlite3.Connection, sql: str, head: tuple, ids: list[int]) -> list:
    """``sql`` with ``{marks}`` run over ``ids`` in chunks of 500 (SQLite's variable cap)."""
    out: list = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        out += db.execute(sql.format(marks=",".join("?" * len(chunk))), (*head, *chunk)
                          ).fetchall()
    return out
