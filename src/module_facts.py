"""Fork: Athena's facts as evidence for the belief ledger, and so into memory.

A built-in scheduled task ("Module Facts", hourly) reads every current fact Athena
holds (``list_facts {"format": "json", "limit": 1000}``) through the module's MCP server
and submits each one to the belief ledger (``src/evidence_intake.py``) as evidence from
source ``odysseus-sound`` with the module fact id as source id. The ledger decides what is
confirmed and mirrors confirmed beliefs into Odysseus memory, one memory per belief, text
``<fact> (source: <link>)``; see ``docs/fork/beliefs.md``.

* The module's own verdict travels as ``source_confirmed``: the row's ``confirmed`` field,
  or, from a module that doesn't send it yet, membership in a second
  ``list_facts {"confirmed": true, "format": "json", "limit": 1000}`` answer (one extra
  call, made only then).
* A row's ``kind`` (MCP 0.11.0) sets the memory category (:func:`category`) and travels,
  with ``quote`` and ``conversation_id``, in the evidence payload for the review page
  (:func:`payload`). An older module sends none of them; nothing changes then.
* A fact about one occasion carries the module's ``valid_until`` (the last day it is
  worth keeping, e.g. the day of a meeting; Athena MCP 0.13.0): the belief goes stale
  the day after (``apply_decay``), even while the module still returns the fact.
* Facts the module no longer returns (retracted, replaced, merged) are marked gone in the
  ledger (``reconcile_source``); a belief left without live evidence goes stale and
  leaves memory unless the owner confirmed it. Then expiry runs (``apply_decay``) and
  memory is mirrored once.
* Running twice changes nothing. Upstream's "Memory Tidy" may merge, rewrite or delete
  copies like any other memory; the next run restores them. Phase 0 copies (a
  ``module_fact_id`` without ``belief_id``) are adopted, not duplicated.

People (plan stage 2.1): each pass also calls ``list_people {"format": "json"}`` once and
hands the module's people (``id``, ``name``, ``aliases``, ``is_owner``) to the ledger
(``sync_people``): each module person id maps to one ledger subject, so a fact carrying
that ``person_id`` lands on the same subject whatever name the module uses for it. A module
that answers ``list_people`` with text, or has no such tool, is tolerated (logged once);
facts then match subjects by name.

:func:`propagate_forget` carries the owner's "forget" back to the module: it calls the
module's ``retire_fact`` for each Athena fact behind a rejected belief, so the
fact is not submitted again (an older module without the tool is tolerated; the ledger
keeps the belief rejected either way).

Handwritten notes (``src/ink_athena.py``): each run first asks Athena to remove the
written notes of handwritten notes deleted in the hub (:func:`sweep_handwritten`), so the
facts Athena drops with them leave the ledger in the same run.

Odysseus never opens the module's database. Per-owner state (last run, counts) is in
``<DATA_DIR>/module_facts_<owner>.json``. An older module that answers ``list_facts``
with text does nothing (logged once).

Settings (environment; the former ``BARVIS_*`` names are still read as a fallback):

* ``MODULE_ACTION_ITEMS_SERVER``: module MCP server name or id (default ``convo``),
  shared with the action-item task.
* ``MODULE_FACTS_SYNC``: ``on`` (default) or ``off``. ``off`` stops the sync; memories
  already mirrored stay.
* The ledger's own settings (``BELIEF_*``) are in ``src/evidence_intake.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import belief_consolidation as consolidation
from src import evidence_intake as ledger
from src.module_action_items import (
    CallTool,
    _env,
    mcp_caller,
    module_server_id,
    server_key,
)

logger = logging.getLogger(__name__)

ACTION = "module_sync_facts"
TASK_NAME = "Module Facts"
SOURCE = ledger.MODULE_SOURCE
HOUSEKEEPING_DEFAULT = {
    ACTION: {"name": TASK_NAME, "schedule": "cron", "scheduled_time": None,
             "cron_expression": "0 * * * *"},
}
INFO = ("Feed Athena's facts into the belief ledger as evidence (with their "
        "transcript links) and keep Odysseus memory in step with the confirmed beliefs: "
        "new ones added, changed ones updated, retracted or expired ones removed. Reads "
        "them through the module's MCP tools.")
PREFERENCE_WORDS = ledger.PREFERENCE_WORDS
LIST_ALL = {"format": "json", "limit": 1000}
LIST_CONFIRMED = {"confirmed": True, "format": "json", "limit": 1000}
LIST_PEOPLE = {"format": "json"}
FORGET_REASON = "owner forgot it in Odysseus"
_MISSING_TOOL = re.compile(r"unknown tool|tool not found|no such tool|not found: retire_fact"
                           r"|retire_fact.*not (found|available|exist)", re.IGNORECASE)
_LOCKS: dict[str, asyncio.Lock] = {}


# ----------------------------------------------------------------------- settings
def enabled() -> bool:
    """``MODULE_FACTS_SYNC``: on (default) or off."""
    return _env("FACTS_SYNC", "on").lower() not in {"off", "no", "false", "0"}


# -------------------------------------------------------------------------- state
def _state_path(owner: str | None) -> Path:
    from src.constants import DATA_DIR

    slug = "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in (owner or "default"))
    return Path(DATA_DIR) / f"module_facts_{slug}.json"


def load_state(owner: str | None) -> dict[str, Any]:
    try:
        state = json.loads(_state_path(owner).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    return state if isinstance(state, dict) else {}


def save_state(owner: str | None, state: dict[str, Any]) -> None:
    path = _state_path(owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _lock(owner: str | None) -> asyncio.Lock:
    """One facts pass per owner at a time."""
    return _LOCKS.setdefault(owner or "", asyncio.Lock())


# ------------------------------------------------------------------------ parsing
def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_facts_json(result: str) -> list[dict] | None:
    """The module's ``format="json"`` facts, or None when it answered with text.
    Rows without an id or text are skipped; a repeated id keeps its first row."""
    try:
        data = json.loads(result or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        return None
    out, seen = [], set()
    for row in data["facts"]:
        if not isinstance(row, dict):
            continue
        try:
            fact_id = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(row.get("fact") or "").strip()
        if not text or fact_id in seen:
            continue
        seen.add(fact_id)
        out.append({**row, "id": fact_id, "fact": text})
    return out


def parse_people_json(result: str) -> list[dict] | None:
    """The module's ``list_people format="json"`` people (``{"people": [...]}`` or a bare
    list), or None when it answered with text. Rows without an id are skipped."""
    try:
        data = json.loads(result or "")
    except ValueError:
        return None
    rows = data.get("people") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        if isinstance(row, dict) and str(row.get("id") if row.get("id") is not None
                                         else "").strip():
            out.append(row)
    return out


async def read_people(call: CallTool) -> list[dict] | None:
    """The module's people, or None when it can't say (an older module answers text or
    has no ``list_people``): people are an extra, never a reason for the facts to fail."""
    try:
        return parse_people_json(await call("list_people", dict(LIST_PEOPLE)))
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.debug("list_people failed: %s", e)
        return None


# Athena's fact kinds (MCP 0.11.0, ``list_facts`` JSON ``kind``)
FACT_KINDS = ledger.FACT_KINDS
QUOTE_CHARS = 300


def fact_kind(row: dict) -> str | None:
    """The module's kind for a fact (one of :data:`FACT_KINDS`), or None (an older module
    or an older fact)."""
    kind = str(row.get("kind") or "").strip().lower()
    return kind if kind in FACT_KINDS else None


def category(row: dict) -> str:
    """The hub memory category for a module fact (fact|preference|contact). A fact about a
    person is a contact. Else the module's ``kind`` decides (preference or fact); without
    one, a preference word in the bare fact text does (words in the link don't count):
    "wants Bo to write a report" is a request, not a preference, and only the module can
    tell."""
    return ledger.category_for(row.get("about"), row.get("fact"), fact_kind(row))


def payload(row: dict) -> dict:
    """What the review page shows next to a module fact, kept in the evidence payload:
    its ``kind``, the transcript line it was said on (``quote``) and its
    ``conversation_id`` (the page groups tentative beliefs by conversation). Only the
    fields the module sends; erasing the belief clears them (``evidence_intake``)."""
    out: dict[str, str] = {}
    kind = fact_kind(row)
    if kind:
        out["kind"] = kind
    quote = " ".join(str(row.get("quote") or "").split())[:QUOTE_CHARS]
    if quote:
        out["quote"] = quote
    conv = str(row.get("conversation_id") or "").strip()
    if conv:
        out["conversation_id"] = conv
    return out


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def evidence(row: dict, confirmed_ids: set[int] | None = None) -> ledger.Evidence:
    """One module fact as ledger evidence. ``source_confirmed`` is the row's own
    ``confirmed`` field, else membership in ``confirmed_ids``."""
    confirmed = (_truthy(row["confirmed"]) if "confirmed" in row
                 else row["id"] in (confirmed_ids or set()))
    return ledger.Evidence(
        source=SOURCE, source_id=str(row["id"]), subject=str(row.get("subject") or ""),
        about=str(row.get("about") or ""), text=row["fact"],
        link=str(row.get("link") or ""), seen_first=str(row.get("first_said") or ""),
        seen_last=str(row.get("last_said") or ""), times=max(1, _int(row.get("times_said"))),
        days=max(1, _int(row.get("days"))), source_confirmed=confirmed,
        category=category(row), person_id=_person_id(row), payload=payload(row) or None,
        valid_until=str(row.get("valid_until") or "").strip() or None)


def _person_id(row: dict) -> str | None:
    value = row.get("person_id")
    return str(value).strip() or None if value is not None else None


# -------------------------------------------------------------------------- sync
async def sync(owner: str | None, call: CallTool, *, now: datetime | None = None
               ) -> dict[str, int] | None:
    """One pass. Counts, or None when the module answered with text (older module)."""
    rows = parse_facts_json(await call("list_facts", dict(LIST_ALL)))
    confirmed_ids: set[int] | None = None
    if rows and any("confirmed" not in r for r in rows):
        # a module without the per-row flag: ask for its confirmed set (one extra call)
        confirmed = parse_facts_json(await call("list_facts", dict(LIST_CONFIRMED)))
        confirmed_ids = {r["id"] for r in confirmed or []}
    people = await read_people(call) if rows is not None else None
    async with _lock(owner):
        state = load_state(owner)
        if rows is None:
            if not state.get("text_only"):
                logger.info("Athena answered list_facts with text; facts not synced "
                            "(update the module)")
                state["text_only"] = True
                save_state(owner, state)
            return None
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        # no await from here on: the ledger and memory are written in one go
        stats = {"facts": len(rows)}
        if people is None:
            if not state.get("people_text_only"):
                logger.info("Athena has no list_people JSON; facts match subjects "
                            "by name (update the module)")
                state["people_text_only"] = True
        else:
            state.pop("people_text_only", None)
            stats.update(ledger.sync_people(owner, SOURCE, people, now=now, mirror=False))
        stats.update(ledger.submit(owner, [evidence(r, confirmed_ids) for r in rows],
                                   now=now, mirror=False))
        for key, value in ledger.reconcile_source(owner, SOURCE, {str(r["id"]) for r in rows},
                                                  now=now, mirror=False).items():
            stats[key] = stats.get(key, 0) + value
        merged = consolidation.consolidate(owner, SOURCE, now=now, mirror=False)
        stats["merged"] = merged["merged"]
        stats["confirmed"] = stats.get("confirmed", 0) + merged["confirmed"]
        stats["stale"] += ledger.apply_decay(owner, now=now, mirror=False)["stale"]
        memory = ledger.sync_memory(owner, now=now)
        stats.update({f"memory_{k}": v for k, v in memory.items()})
        state.pop("text_only", None)
        state.update(stats, last_run=now.isoformat(timespec="seconds"))
        save_state(owner, state)
    # erased beliefs whose facts the module hasn't erased yet (it was down or older)
    from src import belief_forget

    try:
        stats["erased"] = await belief_forget.retry_pending(owner, call)
    except Exception as e:  # noqa: BLE001 -- the sync stands; the next pass retries
        logger.warning("erase retry failed: %s", e)
    return stats


def changed(stats: dict[str, int]) -> bool:
    """Whether a pass changed anything in the ledger or in memory."""
    return any(stats.get(k) for k in (
        "beliefs_added", "evidence_added", "evidence_updated", "confirmed", "gone", "stale",
        "merged", "unconfirmed", "memory_added", "memory_updated", "memory_removed", "subjects_added",
        "erased",
        "mapped", "aliases_added"))


def summary(stats: dict[str, int]) -> str:
    """Counts only: never fact text."""
    return (f"facts: {stats['facts']} seen, {stats['beliefs_added']} new, "
            f"{stats['confirmed']} confirmed, {stats['unconfirmed']} unconfirmed, "
            f"{stats['gone']} gone, {stats['stale']} stale"
            + (f", {stats['merged']} merged" if stats.get("merged") else "") + "; "
            f"memory: {stats['memory_kept']} kept, {stats['memory_added']} added, "
            f"{stats['memory_updated']} updated, {stats['memory_removed']} removed"
            + (f"; people: {stats['people']} seen, {stats.get('subjects_added', 0)} new"
               if "people" in stats else ""))


async def action_sync_facts(owner: str, **kwargs) -> tuple[str, bool]:
    """Built-in task entry point (see ``register_builtin_action``)."""
    from src.builtin_actions import TaskNoop
    from src.tool_utils import get_mcp_manager

    await sweep_handwritten()
    if not enabled():
        raise TaskNoop("MODULE_FACTS_SYNC is off")
    manager = get_mcp_manager()
    server_id = module_server_id(manager) if manager is not None else None
    if server_id is None:
        raise TaskNoop(f"module MCP server {server_key()!r} is not connected")
    try:
        stats = await sync(owner, mcp_caller(manager, server_id))
    except RuntimeError as e:  # a failed tool call, or MemoryStoreUnreadable
        return f"Facts sync failed: {e}", False
    if stats is None:
        raise TaskNoop("Athena answered list_facts with text; nothing synced")
    if not changed(stats):
        raise TaskNoop(summary(stats))
    return summary(stats), True


async def sweep_handwritten() -> dict[str, int] | None:
    """Handwritten notes deleted in the hub leave Athena first (``src/ink_athena.py``),
    so the facts Athena drops with them are gone from this same pass. Never fails the
    sync: what Athena didn't confirm is asked again next hour."""
    from src import ink_athena

    try:
        stats = await ink_athena.sweep_all()
    except Exception as e:  # noqa: BLE001
        logger.warning("handwritten notes sweep failed: %s", e)
        return None
    if stats["retracted"] or stats["kept"]:
        logger.info("handwritten notes: %d removed from Athena, %d waiting for Athena",
                    stats["retracted"], stats["kept"])
    return stats


# ------------------------------------------------------------------------ forget
def _fact_ids(belief: dict | Iterable[dict]) -> list[int]:
    rows = (belief.get("evidence") or []) if isinstance(belief, dict) else list(belief)
    ids = {_int(e.get("source_id")) for e in rows if e.get("source") == SOURCE}
    return sorted(i for i in ids if i > 0)


async def propagate_forget(owner: str | None, belief: dict | Iterable[dict],
                           call: CallTool | None = None) -> dict[str, int]:
    """Retire the module facts behind a belief the owner forgot.

    ``belief`` is a belief dict (``evidence_intake.get_belief``) or its evidence list
    (what ``evidence_intake.reject`` returns). For each Athena evidence it calls
    ``retire_fact {"fact_id": <id>, "reason": "owner forgot it in Odysseus"}`` through
    the module's MCP server (``call``, else the connected module server). A module
    without that tool is tolerated (logged once per owner). Returns counts: ``retired``,
    ``failed`` and ``unsupported`` (facts not retired because the tool is missing or the
    module isn't connected)."""
    ids = _fact_ids(belief)
    stats = {"retired": 0, "failed": 0, "unsupported": 0}
    if not ids:
        return stats
    if call is None:
        from src.tool_utils import get_mcp_manager

        manager = get_mcp_manager()
        server_id = module_server_id(manager) if manager is not None else None
        if server_id is None:
            logger.info("module MCP server %r is not connected; %d fact(s) not retired",
                        server_key(), len(ids))
            stats["unsupported"] = len(ids)
            return stats
        call = mcp_caller(manager, server_id)
    for n, fact_id in enumerate(ids):
        try:
            await call("retire_fact", {"fact_id": fact_id, "reason": FORGET_REASON})
        except RuntimeError as e:
            if _MISSING_TOOL.search(str(e)):
                stats["unsupported"] = len(ids) - n
                await _note_retire_support(owner, False)
                return stats
            logger.warning("retire_fact %d failed: %s", fact_id, e)
            stats["failed"] += 1
            continue
        stats["retired"] += 1
    if stats["retired"]:
        await _note_retire_support(owner, True)
    return stats


async def _note_retire_support(owner: str | None, supported: bool) -> None:
    async with _lock(owner):
        state = load_state(owner)
        if supported:
            if state.pop("no_retire_fact", None):
                save_state(owner, state)
            return
        if not state.get("no_retire_fact"):
            logger.info("Athena has no retire_fact tool; a forgotten fact stays "
                        "in the module (update the module). Odysseus keeps it rejected.")
            state["no_retire_fact"] = True
            save_state(owner, state)


def register_builtin_action(actions: dict, info: dict) -> None:
    actions[ACTION] = action_sync_facts
    info[ACTION] = INFO


def register_housekeeping(defaults: dict) -> None:
    defaults.update(HOUSEKEEPING_DEFAULT)
