"""Fork: move a belief to the person it is really about, and fix the line at its source.

A fact can land under the wrong person because Athena gave the line that said it
to the wrong voice (someone else's line heard as the owner's). Editing the text doesn't
help: the belief stays under its subject. **Move** on ``/beliefs`` does two things:

1. The ledger moves the belief (``evidence_intake.move``): new subject, confirmed by the
   owner. A later sync can't move it back, and a fact the module re-attributes to the
   same person stays with it.
2. Athena fixes the line (:func:`fix_lines`): for each fact behind the belief,
   ``get_transcript`` reads the line at the link's ``#t=`` second and, when that line is
   the old subject's, ``reassign_line`` gives it to the new person; the module then
   re-summarizes that conversation. A line someone else said is left alone (the fact was
   about the old subject because of its words, not its voice).

Module problems are counted, never raised: the move stands. No module change: both tools
are in the module contract. Docs: ``docs/fork/beliefs.md#move-to-another-person``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from typing import Any

from src import evidence_intake as ledger
from src import module_facts as mf

logger = logging.getLogger(__name__)

MODULE_TIMEOUT = 20.0  # seconds for the module calls of one move
_LINK = re.compile(r"/view/([^/?#]+)[^#]*#t=(\d+)")
_LINE = re.compile(r"^#(\d+) \[(\d+)s\] ([^:]+): ")
_MISSING = re.compile(r"unknown tool|tool not found|no such tool|"
                      r"(reassign_line|get_transcript).*not (found|available|exist)",
                      re.IGNORECASE)


def source_lines(evidence: Iterable[dict]) -> list[tuple[str, int]]:
    """(conversation id, second) of each Athena fact behind a belief, from its
    transcript link; erased evidence has no link and is left out."""
    out: list[tuple[str, int]] = []
    for e in evidence:
        if e.get("source") != mf.SOURCE:
            continue
        m = _LINK.search(str(e.get("link") or ""))
        if m and (m[1], int(m[2])) not in out:
            out.append((m[1], int(m[2])))
    return out


def names_of(subject: dict | None) -> set[str]:
    """Every name a subject goes by (display, aliases, the names sources use), as keys."""
    if not subject:
        return set()
    names = [subject.get("display"), *(subject.get("aliases") or []),
             *(s.get("name") for s in subject.get("sources") or [])]
    return {k for k in (ledger.subject_key(n) for n in names if n) if k}


def module_name(subject: dict) -> str:
    """The name Athena knows this subject by, else its display name."""
    for s in subject.get("sources") or []:
        if s.get("source") == mf.SOURCE and str(s.get("name") or "").strip():
            return str(s["name"]).strip()
    return subject["display"]


def _line_at(transcript: str, second: int) -> tuple[int, str] | None:
    for line in str(transcript or "").splitlines():
        m = _LINE.match(line.strip())
        if m and int(m[2]) == second:
            return int(m[1]), m[3].strip()
    return None


async def fix_lines(evidence: Iterable[dict], old_names: set[str], name: str,
                    call: mf.CallTool | None) -> dict[str, int]:
    """Give each source line of ``evidence`` that one of ``old_names`` said to ``name``.

    Returns counts: ``reassigned``; ``kept`` (the line is someone else's, or already
    ``name``'s); ``not_found`` (no line at that second); ``failed``; ``unsupported``
    (no module, or a module without the tools)."""
    lines = source_lines(evidence)
    stats = dict.fromkeys(("reassigned", "kept", "not_found", "failed", "unsupported"), 0)
    if not lines:
        return stats
    if call is None:
        stats["unsupported"] = len(lines)
        return stats
    new_key = ledger.subject_key(name)
    for n, (conv_id, second) in enumerate(lines):
        try:
            text = await call("get_transcript", {"conversation_id": conv_id,
                                                 "start_seconds": second,
                                                 "end_seconds": second, "max_lines": 10})
            found = _line_at(text, second)
            if found is None:
                stats["not_found"] += 1
                continue
            line_id, speaker = found
            key = ledger.subject_key(speaker)
            if key == new_key or key not in old_names:
                stats["kept"] += 1
                continue
            await call("reassign_line", {"line_id": line_id, "name": name})
            stats["reassigned"] += 1
        except RuntimeError as e:
            if _MISSING.search(str(e)):
                stats["unsupported"] += len(lines) - n
                return stats
            logger.warning("fixing line at %s#t=%d failed: %s", conv_id, second, e)
            stats["failed"] += 1
    return stats


async def move(owner: str | None, belief_id: int, *, subject_id: int | None = None,
               name: str | None = None, fix_line: bool = True,
               call: mf.CallTool | None = None, timeout: float = MODULE_TIMEOUT
               ) -> dict[str, Any]:
    """Move the belief (the ledger raises what it raises), then fix its source lines
    when ``fix_line``. Returns the moved belief with ``lines`` (:func:`fix_lines`
    counts, or None when not asked)."""
    before = await asyncio.to_thread(ledger.get_belief, owner, belief_id)
    old = None
    if before.get("subject_id") is not None:
        try:
            old = await asyncio.to_thread(ledger.get_subject, owner, before["subject_id"])
        except ledger.SubjectNotFound:
            old = None
    moved = await asyncio.to_thread(ledger.move, owner, belief_id, subject_id=subject_id,
                                    name=name)
    result = dict(moved, lines=None)
    if not fix_line or moved.get("subject_id") == before.get("subject_id"):
        return result
    target = await asyncio.to_thread(ledger.get_subject, owner, moved["subject_id"])
    try:
        result["lines"] = await asyncio.wait_for(
            fix_lines(before.get("evidence") or [], names_of(old), module_name(target), call),
            timeout)
    except asyncio.TimeoutError:
        logger.warning("fixing the source lines of belief %s timed out", belief_id)
        result["lines"] = {"reassigned": 0, "kept": 0, "not_found": 0,
                           "failed": len(source_lines(before.get("evidence") or [])),
                           "unsupported": 0}
    return result
