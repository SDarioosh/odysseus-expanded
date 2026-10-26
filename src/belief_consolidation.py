"""Fork: provenance-aware consolidation (personal-context plan stage 4.1).

Design: ``docs/fork/beliefs.md`` ("Consolidation (4.1)").

Two parts:

* **Upstream's tidy leaves sourced memories alone.** A memory entry that mirrors a
  belief (``belief_id``) or is a Phase 0 copy of a module fact (``module_fact_id`` from
  Athena) carries provenance: a source, an id and a link that the ledger keeps
  in step. Upstream's Memory Tidy task and its memory audit (the Tidy button, and the
  automatic audit after chat extraction) know only text: they could merge such an entry
  with an unrelated memory, rewrite it without its link, or delete it, and the next
  ledger sync would put it back. :func:`without_sourced`, :func:`split_sourced` and
  :func:`restore_sourced` are the hooks that keep those entries out of both passes and
  write them back unchanged.
* **The fork's own pass merges near-duplicates in the ledger, one source at a time.**
  :func:`consolidate` looks at one owner's tentative and confirmed beliefs whose
  evidence all comes from one source, and merges two of them when they are about the
  same subject, say the same thing in nearly the same words
  (every word that differs is a filler word or another form of the same word; no
  different negation or number) and don't differ in their structured attribute/value.
  The merged belief's evidence moves to the kept one with its source ids and links,
  so the source keeps mapping to it; the kept belief keeps its memory id. Beliefs the owner decided on
  (confirmed, corrected, forgotten) are never merged, and beliefs from two sources are
  never merged here (agreement across sources is confirmation, handled by intake).
  Each source's job calls it for its own source before syncing memory.

Setting (environment): ``BELIEF_CONSOLIDATE`` (on; ``off`` skips the merge pass; the
upstream exclusion always applies).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from src import evidence_intake as ledger

logger = logging.getLogger(__name__)

# Words that flip or end a claim: two texts that differ in one of these are not the same.
NEGATIONS = frozenset({
    "not", "no", "never", "none", "nobody", "nothing", "neither", "nor", "without",
    "longer", "anymore", "stopped", "quit", "former", "formerly", "ex", "dont", "doesnt",
    "didnt", "isnt", "wasnt", "arent", "werent", "cant", "cannot", "wont", "hasnt",
    "havent", "hadnt", "shouldnt", "wouldnt", "couldnt",
})
# Words that don't change a claim when added or dropped.
FILLER = frozenset({"a", "an", "the", "very", "really", "also", "still", "just", "quite",
                    "so", "too", "much", "actually"})
# Also ignorable when a source rewords an item under the same id (``same_rewording``):
# glue words of a rephrased sentence ("a bike with no computer on it" / "a bike that has
# no computer"). Not tense or time words ("had", "was", "used"): those change a claim.
REWORD_FILLER = FILLER | frozenset({
    "that", "which", "who", "with", "has", "have", "is", "are", "be", "on", "in", "of",
    "it", "its", "to", "for", "at", "there", "this", "and",
})
# Endings of one word's forms (after a shared stem of at least 3 letters): plural and
# third person only, so a change of tense ("works at" / "worked at") stays apart.
INFLECTIONS = frozenset({"", "s", "es"})
_WORD = re.compile(r"[a-z0-9]+")


# ------------------------------------------------------------- upstream's tidy
def is_sourced(entry: Mapping) -> bool:
    """Whether a memory entry carries provenance the ledger keeps: a mirrored belief
    (``belief_id``) or a Phase 0 copy of an Athena fact (``module_fact_id``)."""
    if not isinstance(entry, Mapping):
        return False
    if entry.get("belief_id") is not None:
        return True
    return entry.get("source") == ledger.MODULE_SOURCE and "module_fact_id" in entry


def without_sourced(entries: Iterable[Mapping]) -> list:
    """The entries upstream's tidy may change: every entry without provenance."""
    return [e for e in entries if not is_sourced(e)]


def split_sourced(entries: Iterable[Mapping]) -> tuple[list, list]:
    """``(plain, sourced)``, each in the original order."""
    plain: list = []
    sourced: list = []
    for entry in entries:
        (sourced if is_sourced(entry) else plain).append(entry)
    return plain, sourced


def restore_sourced(saved: list, pool: Iterable[Mapping]) -> list:
    """``saved`` plus every sourced entry of ``pool`` whose id ``saved`` lacks, unchanged
    (upstream's audit rebuilds the store from what it kept; this puts them back)."""
    ids = {e.get("id") for e in saved if isinstance(e, Mapping)}
    out = list(saved)
    for entry in pool:
        if is_sourced(entry) and entry.get("id") not in ids:
            out.append(entry)
            ids.add(entry.get("id"))
    return out


# ------------------------------------------------------------------ the merge
def enabled() -> bool:
    return (os.environ.get("BELIEF_CONSOLIDATE") or "on").strip().lower() not in (
        "off", "0", "false", "no")


def _words(text: str) -> list[str]:
    return _WORD.findall(str(text or "").lower().replace("'", "").replace("\u2019", ""))


def _guard(words: list[str]) -> tuple[frozenset, tuple]:
    """What two texts must share to be one claim: their negation words and numbers."""
    return (frozenset(w for w in words if w in NEGATIONS),
            tuple(sorted(w for w in words if any(c.isdigit() for c in w))))


def same_word(a: str, b: str) -> bool:
    """One word in two forms: equal, or one stem (at least 3 letters) with plural or
    third-person endings only (flight/flights, prefer/prefers, box/boxes)."""
    if a == b:
        return True
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n >= 3 and a[n:] in INFLECTIONS and b[n:] in INFLECTIONS


def same_claim(a: str, b: str, filler: frozenset = FILLER) -> bool:
    """Whether two matching keys (``beliefs.norm``) say the same thing in nearly the same
    words: the same negations and numbers, and every word that differs is a filler word
    (:data:`FILLER`) or the same word in another form (:func:`same_word`). A different
    name, place or thing ("a rabbit called Luna" / "… called Bruno") is never the same
    claim."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    if _guard(wa) != _guard(wb):
        return False
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        da = [w for w in wa[i1:i2] if w not in filler]
        db = [w for w in wb[j1:j2] if w not in filler]
        if len(da) != len(db) or not all(same_word(x, y) for x, y in zip(da, db)):
            return False
    return True


def same_rewording(a: str, b: str) -> bool:
    """:func:`same_claim` with :data:`REWORD_FILLER`: for one item a source reworded under
    its own id (an Athena re-summary), not for two items that merely look alike."""
    return same_claim(a, b, REWORD_FILLER)


def _structure_agrees(a: Mapping, b: Mapping) -> bool:
    """Both structured: same attribute and value (case-insensitive). Otherwise fine."""
    if not (a["attribute"] and b["attribute"]):
        return True
    if a["attribute"] != b["attribute"]:
        return False
    va, vb = (" ".join(str(v or "").lower().split()) for v in (a["value"], b["value"]))
    return not (va and vb) or va == vb


def _keep_rank(row: Mapping, evidence_count: int) -> tuple:
    """Which belief of a pair survives: confirmed, then mirrored (its memory id stays),
    then more evidence, then the older one."""
    return (row["status"] == "confirmed", row["memory_id"] is not None, evidence_count,
            -row["id"])


def _candidates(db, owner: str, source: str) -> tuple[list, dict[int, int]]:
    """This owner's mergeable beliefs whose evidence is all from ``source``, and each
    one's evidence count."""
    counts: dict[int, int] = {}
    for row in db.execute(
            "SELECT belief_id, COUNT(*) AS n, COUNT(DISTINCT source) AS sources, "
            "MIN(source) AS source FROM evidence WHERE owner = ? GROUP BY belief_id",
            (owner,)):
        if row["sources"] == 1 and row["source"] == source:
            counts[row["belief_id"]] = row["n"]
    rows = [r for r in db.execute(
        "SELECT * FROM beliefs WHERE owner = ? AND status IN ('tentative', 'confirmed') "
        "AND owner_text IS NULL AND (confirmed_by IS NULL OR confirmed_by != 'owner') "
        "ORDER BY id", (owner,)).fetchall() if r["id"] in counts]
    return rows, counts


def plan_merges(rows: list, counts: Mapping[int, int]) -> list[tuple[int, int]]:
    """``[(keep_id, drop_id)]``: within each (subject, about) group, the beliefs in keep
    order; each one joins the first kept belief it repeats. The category is not part of
    the group: two near-identical claims can carry different labels (a preference word
    guessed before the source sent fact kinds, a kind after), and the kept one's stays."""
    groups: dict[tuple, list] = {}
    for row in rows:
        groups.setdefault((row["subject_id"], row["about"]), []).append(row)
    merges: list[tuple[int, int]] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: _keep_rank(r, counts.get(r["id"], 0)), reverse=True)
        kept: list = []
        for row in group:
            target = next((k for k in kept if _structure_agrees(k, row)
                           and same_claim(k["norm"], row["norm"])), None)
            if target is None:
                kept.append(row)
            else:
                merges.append((target["id"], row["id"]))
    return merges


def _merge(db, keep: Mapping, drop: Mapping, stamp: str) -> None:
    db.execute("UPDATE evidence SET belief_id = ?, updated_at = ? WHERE belief_id = ?",
               (keep["id"], stamp, drop["id"]))
    if not keep["attribute"] and drop["attribute"]:
        # the kept belief wasn't structured yet: take the merged one's structure
        ledger._set(db, keep["id"], stamp, attribute=drop["attribute"], value=drop["value"],
                    valid_from=drop["valid_from"], structured_at=drop["structured_at"])
    ledger._event(db, keep["id"], "merged", f"#{drop['id']}: {drop['text']}", stamp)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                  "AND name = 'belief_vectors'").fetchone():
        db.execute("DELETE FROM belief_vectors WHERE belief_id = ?", (drop["id"],))
    db.execute("DELETE FROM beliefs WHERE id = ?", (drop["id"],))  # its events cascade


def consolidate(owner: str | None, source: str, *, now: datetime | None = None,
                mirror: bool = True) -> dict[str, Any]:
    """Merge this owner's near-duplicate beliefs that come only from ``source``.

    Returns ``{"merged": n, "confirmed": n}`` (``confirmed``: kept beliefs the merged
    evidence confirmed by the rule). ``mirror``: sync memory afterwards (a merged
    belief's memory entry goes, the kept one's stays). Nothing happens when
    ``BELIEF_CONSOLIDATE`` is off."""
    counts = {"merged": 0, "confirmed": 0}
    if not enabled() or not source:
        return counts
    owner = owner or ""
    now = ledger._now(now)
    stamp, today, st = ledger._stamp(now), now.date(), ledger.settings()
    with ledger._WRITE:
        with ledger._connect(write=True) as db:
            rows, evidence_counts = _candidates(db, owner, source)
            merges = plan_merges(rows, evidence_counts)
            by_id = {r["id"]: r for r in rows}
            touched: set[int] = set()
            for keep_id, drop_id in merges:
                keep = db.execute("SELECT * FROM beliefs WHERE id = ?", (keep_id,)).fetchone()
                _merge(db, keep, by_id[drop_id], stamp)
                touched.add(keep_id)
                counts["merged"] += 1
            for belief_id in sorted(touched):
                if ledger._refresh(db, belief_id, stamp, st, today) == "confirmed":
                    counts["confirmed"] += 1
        if mirror and counts["merged"]:
            ledger.sync_memory(owner, now=now)
    if counts["merged"]:
        logger.info("belief consolidation (%s): %d merged", source, counts["merged"])
    return counts
