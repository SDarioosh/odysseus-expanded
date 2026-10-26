"""Fork: Today insight weights routes (Phase 3), included by ``routes/today_insight_routes.py``.

Code: ``src/insight_weights.py``. Contract: docs/fork/today-insight.md#weights.

* ``GET  /api/insight/weights`` (admin session): today's weights as table rows.
* ``POST /api/insight/weights/reset`` (admin) ``{"key": "list:<id>"}``, or ``{}`` for all.
* ``GET  /api/insight/today/why?ref=<ref>`` (admin): why an item stands where it does
  (tier, rule position, weight and what it is made of, who put it there), rebuilt in
  code from today's insight, today's weights and the candidates; no model call.
* ``GET  /api/codex/insight/weights`` (API token with scope ``insight:read``): the T5
  table (``t5_table``); 200 with empty ``weights`` when none.
"""

from __future__ import annotations

import json
import logging
from datetime import date as Date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.middleware import require_admin
from src import insight_weights as iw
from src import today_insight as ti
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

INSIGHT_READ_SCOPES = {"insight:read"}

TIERS = ("overdue or due today", "due in the next 7 days", "on a pinned note", "undated",
         "due later than a week")
SECTION_NAMES = {"focus": "Do first", "coming_up": "Coming up", "also": "Also worth a look"}
NOTE_TYPE_LABELS = {"checklist": "Checklist notes", "todo": "To-do notes", "goal": "Goal notes"}
GONE_LIST = "a list that no longer exists"


class ResetBody(BaseModel):
    key: str | None = None


def _owner(request: Request) -> str:
    return effective_user(request) or ""


def _today(owner: str) -> Date:
    return ti._now_local(ti.owner_timezone(owner)).date()


def _notes(owner: str) -> list:
    try:
        return ti._load_notes(owner)
    except Exception as e:  # noqa: BLE001 -- labels fall back
        logger.warning("insight weights: couldn't load notes: %s", e)
        return []


def label(key: str, titles: dict[str, str]) -> str:
    """A key as the owner reads it: the list's title, "To-do notes", "Work events" …"""
    prefix, _, value = key.partition(":")
    if prefix == "list":
        return titles.get(value) or GONE_LIST
    if prefix == "note_type":
        return NOTE_TYPE_LABELS.get(value, f"{value.capitalize()} notes")
    if prefix == "event_type":
        return f"{value.capitalize()} events"
    if prefix == "importance":
        return f"{value.capitalize()}-importance events"
    return key


def _titles(notes: list) -> dict[str, str]:
    return {str(n.id): (str(getattr(n, "title", "") or "").strip() or "Untitled")
            for n in notes}


def _rows(weights: dict, titles: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for key, row in (weights.get("keys") or {}).items():
        if not isinstance(row, dict):
            continue
        rows.append({"key": key, "label": label(key, titles),
                     "weight": iw._key_weight(weights, key),
                     **{c: int(row.get(c) or 0) for c in iw.COUNTS},
                     "reset_at": row.get("reset_at")})
    rows.sort(key=lambda r: (-abs(r["weight"]), -r["shown"], r["key"]))
    return rows


def _table(owner: str, weights: dict) -> dict[str, Any]:
    return {"date": weights.get("date"), "min_shown": weights.get("min_shown", iw.MIN_SHOWN),
            "window_days": weights.get("window_days", iw.WINDOW_DAYS),
            "rows": _rows(weights, _titles(_notes(owner)))}


# ------------------------------------------------------------------------- why
def _tier(c: dict) -> int:
    b = c.get("bucket")
    if b in ("overdue", "due_today"):
        return 0
    if b == "due_soon":
        return 1
    if c.get("pinned"):
        return 2
    return 3 if b == "undated" else 4


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _day(value: str | None) -> str:
    try:
        d = Date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value or "")
    return f"{d:%b} {d.day}"


def _when(c: dict) -> str:
    b, time = c.get("bucket"), c.get("time")
    at = f" at {time}" if time else ""
    if b == "overdue":
        return f"Overdue since {_day(c.get('date'))}"
    if b == "due_today":
        return f"Today{at}" if c.get("kind") == "event" else "Due today"
    if b == "due_soon":
        return (f"On {_day(c.get('date'))}{at}" if c.get("kind") == "event"
                else f"Due {_day(c.get('date'))}")
    if c.get("pinned"):
        return "On a pinned note"
    if b == "undated":
        return "No due date"
    return f"Due {_day(c.get('date'))}"


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


def _because(row: dict) -> str:
    shown = row.get("shown", 0)
    if iw.is_event_key(row["key"]):
        bits = []
        if row.get("promoted"):
            bits.append(f"moved up {row['promoted']}")
        if row.get("dismissed"):
            bits.append(f"skipped {row['dismissed']}")
        if row.get("moved"):
            bits.append(f"rescheduled {row['moved']}")
        return f"you {', '.join(bits) or 'kept all'} of {shown}"
    text = f"you finished {row.get('done', 0)} of {shown}"
    for key, word in (("promoted", "moved up"), ("dismissed", "skipped"),
                      ("moved", "rescheduled")):
        if row.get(key):
            text += f", {word} {row[key]}"
    return text


def _contribution_text(row: dict) -> str:
    verb = "counts"
    name = row["label"]
    if row["key"].startswith("list:"):
        name = f"your {name} list" if name != GONE_LIST else name
    else:
        verb = "count"
    return f"{name} {verb} {_signed(row['weight'])} ({_because(row)})"


def _outcome(owner: str, day: str) -> dict:
    """Today's outcome file (docs/fork/today-insight.md#outcome-log), ``{}`` when none."""
    path = iw._owner_dir(owner) / f"{day}.outcome.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _shown_order(insight: dict, outcome: dict) -> dict:
    """The insight as the page shows it (the owner's marks applied)."""
    if not outcome:
        return insight
    try:
        from src import insight_outcomes

        return insight_outcomes.apply_owner_order(insight, outcome)
    except Exception as e:  # noqa: BLE001
        logger.warning("insight weights: apply_owner_order failed: %s", e)
        return insight


def why(owner: str, ref: str) -> dict[str, Any] | None:
    insight = ti.load_today(owner)
    if not insight:
        return None
    tz = ti.owner_timezone(owner)
    now_local = ti._now_local(tz)
    today = now_local.date()
    outcome = _outcome(owner, today.isoformat())
    items = outcome.get("items") if isinstance(outcome.get("items"), dict) else {}
    shown = _shown_order(insight, outcome)

    section = position = None
    title = None
    for s in ti.SECTIONS:
        for i, row in enumerate(shown.get(s) or []):
            if isinstance(row, dict) and row.get("ref") == ref:
                section, position, title = s, i + 1, row.get("title")
                break
        if section:
            break

    weights = iw.current(owner, today)
    notes = ti._load_notes(owner)
    occurrences = ti._load_events(owner, now_local)
    dismissed = {r for r, it in items.items()
                 if isinstance(it, dict) and it.get("outcome") == "dismissed"}
    ordered = ti.order_candidates(ti.todo_candidates(notes, today, tz),
                                  ti.event_candidates(occurrences, now_local),
                                  weights=weights, dismissed=dismissed)
    rule_position = cand = None
    for i, c in enumerate(ordered):
        if c["ref"] == ref:
            rule_position, cand = i + 1, c
            break
    if section is None and cand is None:
        return None

    titles = _titles(notes)
    contribs = [{**row, "label": label(row["key"], titles)}
                for row in iw.contributions(cand or {}, weights)]
    weight = iw.candidate_weight(cand, weights) if cand else 0
    picked_by = "owner" if (items.get(ref) or {}).get("outcome") == "promoted" else "model"

    parts = [_when(cand) if cand else "No longer among today's candidates"]
    if rule_position:
        parts.append(f"{_ordinal(rule_position)} in the rule order")
    parts.extend(_contribution_text(r) for r in contribs if r["weight"])
    if picked_by == "owner":
        last = "You moved it up."
    elif section == "focus" and position == 1:
        last = "The model put it first."
    elif section:
        last = f"The model put it {_ordinal(position)} in {SECTION_NAMES[section]}."
    else:
        last = "The model didn't pick it today."
    return {"ref": ref, "title": title or (cand or {}).get("title"), "section": section,
            "position": position,
            "tier": TIERS[_tier(cand)] if cand else None,
            "rule_position": rule_position, "weight": weight, "contributions": contribs,
            "picked_by": picked_by, "text": f"{'; '.join(parts)}. {last}"}


def setup_insight_weights_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/insight/weights", dependencies=[Depends(require_admin)])
    async def weights(request: Request) -> dict[str, Any]:
        owner = _owner(request)
        return _table(owner, iw.current(owner, _today(owner)))

    @router.post("/api/insight/weights/reset", dependencies=[Depends(require_admin)])
    async def reset(request: Request, body: ResetBody | None = None) -> dict[str, Any]:
        owner = _owner(request)
        key = (body.key if body else None) or None
        try:
            fresh = iw.reset(owner, key)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, **_table(owner, fresh)}

    @router.get("/api/insight/today/why", dependencies=[Depends(require_admin)])
    async def why_route(request: Request, ref: str):
        owner = _owner(request)
        if not ti.load_today(owner):
            return JSONResponse(status_code=404, content={"error": "no insight yet"})
        answer = why(owner, ref)
        if answer is None:
            return JSONResponse(status_code=404, content={"error": "not in today's insight"})
        return answer

    @router.get("/api/codex/insight/weights")
    async def token_weights(request: Request) -> dict[str, Any]:
        from routes.codex_routes import _scope_owner

        owner = _scope_owner(request, INSIGHT_READ_SCOPES)
        return iw.t5_table(owner, _today(owner))

    return router
