"""Fork: "Download what Odysseus knows about me" (personal-context stage 4.2).

One local JSON file with this owner's belief ledger (beliefs of every status with their
evidence, links and events; subjects and the source person ids mapped to them) and their
Odysseus memories. Built on request from ``/beliefs`` (``GET /api/beliefs/export``) and
handed to the owner's browser as a download; it is never written to disk on the server
or sent anywhere else.

Format (``format`` / ``version`` say which): ::

    {"format": "odysseus-memory-export", "version": 1, "exported_at": "<UTC ISO>",
     "owner": "<user>", "counts": {...},
     "beliefs": [{..., "evidence": [...], "events": [...]}],
     "subjects": [...], "subject_sources": [...], "memories": [...]}

There is no import: upstream's memory import (``POST /api/memory/import``) extracts new
memories from a document with the model, it doesn't restore entries. An erased belief
is listed without words (id, status, dates), as the ledger keeps it.

Docs: ``docs/fork/beliefs.md#export-42``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src import evidence_intake as ledger

FORMAT = "odysseus-memory-export"
VERSION = 1


def memories(owner: str | None) -> list[dict]:
    """This owner's memory entries (all categories and sources), as stored."""
    from src.constants import DATA_DIR
    from src.memory import MemoryManager

    owner = owner or ""
    return [dict(e) for e in MemoryManager(DATA_DIR).load_all_for_update()
            if (e.get("owner") or "") == owner]


def build(owner: str | None, *, now: datetime | None = None) -> dict[str, Any]:
    """The export for ``owner``. Raises ``MemoryStoreUnreadable`` (a RuntimeError) when
    memory can't be read, rather than exporting an empty list as if there were none."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = ledger.export_rows(owner)
    mem = memories(owner)
    counts = {"beliefs": len(rows["beliefs"]),
              "evidence": sum(len(b["evidence"]) for b in rows["beliefs"]),
              "events": sum(len(b["events"]) for b in rows["beliefs"]),
              "subjects": len(rows["subjects"]), "memories": len(mem)}
    return {"format": FORMAT, "version": VERSION,
            "exported_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "owner": owner or "",
            "counts": counts, **rows, "memories": mem}


def filename(now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"odysseus-memory-{now.strftime('%Y-%m-%d')}.json"
