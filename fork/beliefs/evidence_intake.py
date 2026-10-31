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

Code: this module is the intake (:func:`submit`, :func:`reconcile_source`,
:func:`apply_decay`) and the owner's actions, and offers every name of the ledger, so
callers import ``fork.beliefs.evidence_intake`` only. The rest is in
``ledger_model.py`` (vocabulary, :class:`Evidence`, matching keys), ``ledger_db.py``
(file, schema, migrations), ``ledger_rows.py`` (rows and the rule),
``ledger_subjects.py``, ``ledger_reads.py``, ``ledger_structure.py`` and
``ledger_memory.py`` (the mirror in Odysseus memory), all in ``fork/beliefs/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from fork.beliefs.ledger_db import (  # noqa: F401 -- re-exported
    _NEW_BELIEF_COLUMNS,
    _WRITE,
    DB_NAME,
    ERASE_VERSION,
    NORM_VERSION,
    SCHEMA,
    SCHEMA_VERSION,
    _connect,
    _migrate_erase,
    _migrate_event_kinds,
    _migrate_norms,
    _migrate_subjects,
    db_path,
    stored_module_source,
)
from fork.beliefs.ledger_memory import (  # noqa: F401 -- re-exported
    memory_fields,
    sync_memory,
    update_vector_index,
)
from fork.beliefs.ledger_model import (  # noqa: F401 -- re-exported
    _OWNER_LEADS,
    _OWNER_NAMES,
    _OWNER_VERBS,
    _RESERVED_PAYLOAD,
    ABOUT,
    ATTRIBUTES,
    CATEGORIES,
    CONFIRMED_BY,
    ERASE_STATES,
    ERASED_PREFIX,
    EVENT_KINDS,
    EVENTS_KEPT,
    FACT_KINDS,
    GENERIC_SUBJECT,
    MAX_VALUE_CHARS,
    MODULE_FIELDS,
    MODULE_SOURCE,
    MODULE_SOURCE_OLD,
    MODULE_SOURCES,
    OPTIONAL_FIELDS,
    OWNER_DISPLAY,
    PREFERENCE_WORDS,
    PROJECT_ATTRIBUTES,
    PROJECT_WORDS,
    STATUSES,
    SUBJECT_KINDS,
    BeliefErased,
    BeliefNotFound,
    Evidence,
    Settings,
    SubjectNotFound,
    _aliases,
    _clean,
    _clean_name,
    _day,
    _int,
    _is_owner_name,
    _now,
    _owner_key,
    _payload,
    _read_settings,
    _stamp,
    category_for,
    is_module_source,
    normalize,
    settings,
    subject_key,
)
from fork.beliefs.ledger_reads import (  # noqa: F401 -- re-exported
    beliefs_by_attribute,
    beliefs_for_contact,
    export_rows,
    get_belief,
    list_beliefs,
    stats,
)
from fork.beliefs.ledger_rows import (  # noqa: F401 -- re-exported
    _BELIEF_SELECT,
    _belief,
    _belief_dict,
    _event,
    _evidence_dict,
    _evidence_rows,
    _expired,
    _in_chunks,
    _is_gone,
    _refresh,
    _set,
    confirmation_for,
    link_rank,
    primary_evidence,
)
from fork.beliefs.ledger_structure import (  # noqa: F401 -- re-exported
    _infer_project,
    apply_structure,
    clean_attribute,
    clean_value,
    structure_candidates,
    subject_summary,
)
from fork.beliefs.ledger_subjects import (  # noqa: F401 -- re-exported
    SUBJECT_COUNTS_SQL,
    SUBJECT_SOURCES_SQL,
    _drop_empty_owner_word,
    _inferred_kind,
    _name_owner,
    _person_kind,
    _resolve_subject,
    _subject,
    _subject_action,
    _subject_dict,
    _subject_extras,
    _Subjects,
    _truthy,
    find_subject,
    get_subject,
    link_contact,
    list_subjects,
    merge_subjects,
    rename_subject,
    set_subject_kind,
    sync_people,
    unlink_contact,
)

logger = logging.getLogger(__name__)


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
    if is_module_source(ev["source"]):
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
                ev = _erased_evidence(ev, "pending" if is_module_source(ev["source"])
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
            module = None  # the name this owner's Athena evidence is stored under
            for item in evidence:
                ev = _clean(item, today.isoformat())
                if ev is None:
                    counts["skipped"] += 1
                    continue
                if is_module_source(ev["source"]):
                    module = module or stored_module_source(db, owner, ev["source"])
                    ev["source"] = module
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
            source = stored_module_source(db, owner, source)
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
                if is_module_source(e["source"]):
                    did = outcomes.get(str(e["source_id"]))
                    state = ("done" if did == "erased" or old == "done" else
                             "retired" if did == "retired" else "pending")
                    pending += state != "done"
                else:
                    state = "source"
                    if e["link"]:
                        remaining.append({"source": e["source"], "link": e["link"]})
                link = "" if is_module_source(e["source"]) else e["link"]
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
                            "b.erased_at IS NOT NULL AND e.source IN (?, ?)",
                            (owner or "", *MODULE_SOURCES)):
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
