"""Fork: a forget that is complete across hub and module (personal-context stage 4.2).

When the owner forgets a belief on ``/beliefs`` it is erased, not only rejected. In order:

1. The ledger rejects it (``evidence_intake.reject``): it leaves memory at once.
2. Athena erases each fact behind it (``erase_fact``, MCP 0.10.0): the transcript
   line that said it is redacted, its conversation's notes and digests are rewritten
   without it. An older module without ``erase_fact`` gets ``retire_fact`` (1.4): the
   fact is hidden there but its words stay; that evidence stays ``retired`` and the
   facts job tries ``erase_fact`` again every pass (:func:`retry_pending`), as it does
   for a module that was down.
3. The ledger erases its own copy (``evidence_intake.erase``): words cleared, matching
   keys hashed, evidence and events without text, search vector deleted.
4. Copies elsewhere in the hub: memory entries with the same words (a chat-extracted
   memory, a Phase 0 copy; exact match after normalising, never fuzzy) are deleted with
   their vectors, and stored Today insights drop the items that point at the belief and
   any reason or headline that quotes it.

Not touched: the owner's chats (they may quote it) and sources the owner writes
(notes): the answer lists where the words still are (``remaining``) so the owner can
edit them. Erasing can't be undone; an erased belief can't be restored.

Dismiss (:func:`dismiss`) is the lighter choice for a belief that is not worth keeping
but was really said (e.g. "was focused today"): rejected here and ``retire_fact`` in the
module, so it leaves memory and is not offered again, but nothing is erased and it can be
restored.

Docs: ``docs/fork/beliefs.md#complete-forget-42``, ``docs/fork/beliefs.md#dismiss``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable
from typing import Any

from src import evidence_intake as ledger
from src import module_facts as mf

logger = logging.getLogger(__name__)

ERASE_REASON = mf.FORGET_REASON
MODULE_TIMEOUT = 20.0  # seconds for the module calls of one forget; the rest never waits
_MISSING = re.compile(r"unknown tool|tool not found|no such tool|not found: (erase|retire)_fact"
                      r"|(erase|retire)_fact.*not (found|available|exist)", re.IGNORECASE)
_SOURCE_SUFFIX = re.compile(r"\s*\(source: [^)]*\)\s*$")


def _fact_ids(evidence: Iterable[dict]) -> list[int]:
    ids = {mf._int(e.get("source_id")) for e in evidence if e.get("source") == mf.SOURCE}
    return sorted(i for i in ids if i > 0)


async def erase_in_module(fact_ids: Iterable[int], call: mf.CallTool | None
                          ) -> dict[str, str]:
    """``erase_fact`` for each fact id; ``retire_fact`` on a module without it. Returns
    per id: ``erased``, ``retired``, ``failed`` or ``unsupported`` (no module)."""
    ids = list(fact_ids)
    if call is None:
        return {str(i): "unsupported" for i in ids}
    out: dict[str, str] = {}
    tool = "erase_fact"
    for fact_id in ids:
        args = {"fact_id": fact_id, "reason": ERASE_REASON}
        try:
            text = await call(tool, args)
        except RuntimeError as e:
            if tool == "erase_fact" and _MISSING.search(str(e)):
                logger.info("Athena has no erase_fact (update the module): facts are "
                            "only retired there, their words stay until it is updated")
                tool = "retire_fact"
                try:
                    text = await call(tool, args)
                except RuntimeError as e2:
                    out[str(fact_id)] = "unsupported" if _MISSING.search(str(e2)) else "failed"
                    continue
            else:
                logger.warning("%s %d failed: %s", tool, fact_id, e)
                out[str(fact_id)] = "failed"
                continue
        # "No fact #N." is done too: a re-summary gave it a new id, or it is gone
        out[str(fact_id)] = "erased" if tool == "erase_fact" else "retired"
        logger.debug("%s %d: %s", tool, fact_id, str(text)[:80])
    return out


def _plain(text: str) -> str:
    text = _SOURCE_SUFFIX.sub("", str(text or ""))
    return " ".join(re.sub(r"[^\w ]+", " ", text.lower()).split())


def remove_memory_copies(owner: str | None, texts: Iterable[str]) -> int:
    """Delete this owner's memory entries whose words are exactly one of ``texts`` (after
    lowercasing, dropping punctuation and a ``(source: …)`` suffix), and their vectors.
    Returns how many."""
    from src.constants import DATA_DIR
    from src.memory import MemoryManager

    wanted = {p for p in (_plain(t) for t in texts) if p}
    if not wanted:
        return 0
    owner = owner or ""
    with ledger._WRITE:
        manager = MemoryManager(DATA_DIR)
        entries = manager.load_all_for_update()
        keep, removed = [], []
        for entry in entries:
            if (entry.get("owner") or "") == owner and _plain(entry.get("text")) in wanted:
                removed.append(str(entry.get("id") or ""))
            else:
                keep.append(entry)
        if removed:
            manager.save(keep)
            ledger.update_vector_index([], removed)
    return len(removed)


def scrub_insights(owner: str | None, belief_id: int, texts: Iterable[str]) -> int:
    """Stored Today insights of this owner: drop items that point at the belief
    (``belief:<id>``), and any reason or headline that quotes one of ``texts``. Returns
    how many files changed."""
    from src import today_insight as ti

    folder = ti.insight_path(owner, "2000-01-01").parent
    if not folder.is_dir():
        return 0
    quotes = [p for p in (_plain(t) for t in texts) if p]

    def quotes_it(value: Any) -> bool:
        plain = f" {_plain(value)} "
        return any(f" {q} " in plain for q in quotes)

    ref, changed = f"belief:{belief_id}", 0
    for path in sorted(folder.glob("????-??-??.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        before = json.dumps(data, sort_keys=True)
        for section in ti.SECTIONS:
            items = []
            for item in data.get(section) or []:
                if not isinstance(item, dict) or item.get("ref") == ref:
                    continue
                if quotes_it(item.get("why")):
                    item = {**item, "why": ""}
                items.append(item)
            data[section] = items
        if quotes_it(data.get("headline")):
            data["headline"] = ""
        if json.dumps(data, sort_keys=True) != before:
            ti.save(owner, data)
            changed += 1
    return changed


async def forget(owner: str | None, belief_id: int, reason: str = "", *,
                 call: mf.CallTool | None = None, timeout: float = MODULE_TIMEOUT
                 ) -> dict[str, Any]:
    """Forget a belief completely (works on a belief rejected before 4.2 too). Never fails
    because the module is down: the hub side is erased either way and the module side
    is retried by the facts job. Raises ``BeliefNotFound``. Returns ``{"belief",
    "propagated": {"erased", "retired", "failed", "unsupported"}, "erased": {"memory",
    "insights", "remaining", "pending"}}``."""
    evidence = ledger.reject(owner, belief_id, reason)
    # the transcript lines it was said on: erased there, so no other belief shows them
    quotes = [(e.get("payload") or {}).get("quote") for e in evidence]
    if ledger.get_belief(owner, belief_id).get("erased_at"):
        evidence = [e for e in evidence if (e.get("payload") or {}).get("erase") != "done"]
    ids = _fact_ids(evidence)
    outcomes: dict[str, str] = {}
    if ids:
        try:
            outcomes = await asyncio.wait_for(erase_in_module(ids, call), timeout)
        except Exception as e:  # noqa: BLE001 -- the forget stands; the page says so
            logger.warning("belief %s: module not updated: %s", belief_id, e)
            outcomes = {str(i): "failed" for i in ids}
    result = ledger.erase(owner, belief_id, outcomes)
    ledger.scrub_quotes(owner, [q for q in quotes if q])
    memory = insights = 0
    try:
        memory = remove_memory_copies(owner, result["texts"])
    except RuntimeError as e:  # MemoryStoreUnreadable: the next forget or job retries
        logger.error("belief %s erased; memory copies not checked: %s", belief_id, e)
    try:
        insights = scrub_insights(owner, belief_id, result["texts"])
    except OSError as e:
        logger.error("belief %s erased; Today insights not checked: %s", belief_id, e)
    counts = {k: 0 for k in ("erased", "retired", "failed", "unsupported")}
    for state in outcomes.values():
        counts[state] += 1
    return {"belief": result["belief"], "propagated": counts,
            "erased": {"memory": memory, "insights": insights,
                       "remaining": result["remaining"], "pending": result["pending"]}}


async def dismiss(owner: str | None, belief_id: int, reason: str = "", *,
                  call: mf.CallTool | None = None, timeout: float = MODULE_TIMEOUT
                  ) -> dict[str, Any]:
    """Dismiss a belief: rejected here (out of memory, later repeats stay rejected) and
    each Athena fact behind it retired there (``retire_fact``: hidden, its
    transcript words kept). Nothing is erased; Restore brings it back. Never fails because
    of the module. Raises ``BeliefNotFound``, ``BeliefErased``. Returns ``{"belief",
    "propagated": {"retired", "failed", "unsupported"}}``."""
    belief = ledger.get_belief(owner, belief_id)
    if belief.get("erased_at"):
        raise ledger.BeliefErased("this belief was erased; there is nothing to dismiss")
    evidence = ledger.reject(owner, belief_id, reason)
    ids = _fact_ids(evidence)
    counts = {"retired": 0, "failed": 0, "unsupported": 0}
    if ids and call is None:
        counts["unsupported"] = len(ids)
    elif ids:
        try:
            counts = await asyncio.wait_for(mf.propagate_forget(owner, evidence, call), timeout)
        except Exception as e:  # noqa: BLE001 -- the dismissal stands; the page says so
            logger.warning("belief %s dismissed; module not updated: %s", belief_id, e)
            counts = {"retired": 0, "failed": len(ids), "unsupported": 0}
    return {"belief": ledger.get_belief(owner, belief_id), "propagated": counts}


async def retry_pending(owner: str | None, call: mf.CallTool) -> int:
    """The facts job's pass: ``erase_fact`` again for module facts of erased beliefs that
    the module hasn't erased yet (it was down, older, or the claim came back as a new
    fact). Returns how many are erased now."""
    done = 0
    for row in ledger.erase_pending(owner):
        outcomes = await erase_in_module(row["fact_ids"], call)
        if any(v == "erased" for v in outcomes.values()):
            ledger.erase(owner, row["belief_id"], outcomes)
            done += sum(v == "erased" for v in outcomes.values())
        if any(v == "unsupported" for v in outcomes.values()) or all(
                v == "retired" for v in outcomes.values()):
            break  # an older module: nothing more to do this pass
    return done
