"""Fork: the day at a glance for voice devices (docs/fork/voice-assistant.md#today-for-devices).

``GET /api/chat/voice/v1/today`` (scope ``voice``, the same token check as the other voice
routes, :func:`fork.voice.routes.voice_owner`): the token owner's day, shaped for a device
screen (the Echo's Today page):

* ``headline`` and ``focus``: the stored Today insight (``fork/insight/today.py``), in the
  owner's order; ``null`` and ``[]`` while there is none yet. A to-do the owner ticked
  since the insight was written is left out of ``focus``.
* ``events``: today's calendar occurrences (recurring events expanded, the calendar route's
  own expansion through the insight's loader), sorted by start, at most 10.
* ``todos``: open to-do items due today or overdue, then those of pinned notes without a
  date, at most 8.
* ``action_items``: Athena's open action items (its ``GET /action-items?status=open``
  through the hub's link, as the relay's ``GET /api/codex/athena/action-items``), at most
  5, without the ones already filed here as a to-do or event; ``[]`` when Athena is down
  or takes longer than :data:`ATHENA_TIMEOUT`.
* ``counts``: all open to-do items and all open action items, for "and N more".

Only stored data is read: no model call. Each section fails on its own into an empty list
and a log line, so the route answers 200 whenever the token is good. :func:`shape_today`
is the pure part (tested with fictional data); the loaders around it are replaced in tests.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Any, Iterable

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

MAX_EVENTS = 10
MAX_TODOS = 8
MAX_ACTION_ITEMS = 5
MAX_TITLE = 200
ATHENA_TIMEOUT = 3.0  # seconds; the page shows the rest without Athena


# ------------------------------------------------------------------------- shaping
def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _clean(text: Any, limit: int = MAX_TITLE) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _events(occurrences: Iterable[Any], now_local: datetime) -> list[dict]:
    """Occurrences that overlap today (local), sorted by start, at most :data:`MAX_EVENTS`.
    All-day events start at local midnight of their first day and end at local midnight
    after their last day, so every time has an offset."""
    from fork.insight.today_candidates import _occurrence_time

    tz = now_local.tzinfo
    today = now_local.date()
    day_start = datetime.combine(today, time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    out = []
    for ev in occurrences or ():
        if not isinstance(ev, dict) or (ev.get("status") or "") == "cancelled":
            continue
        uid = str(ev.get("uid") or "").strip()
        start = _occurrence_time(ev.get("dtstart"), tz)
        if not uid or start is None:
            continue
        end = _occurrence_time(ev.get("dtend"), tz) or start
        all_day = bool(ev.get("all_day"))
        if all_day:
            first = start.date()
            last = max(first, (end - timedelta(seconds=1)).date())
            if not first <= today <= last:
                continue
            start = datetime.combine(first, time.min, tzinfo=tz)
            end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=tz)
        elif not (start < day_end and (end > day_start or start >= day_start)):
            continue
        out.append({"uid": uid,
                    "title": _clean(ev.get("summary")) or "(no title)",
                    "start": _iso(start), "end": _iso(max(end, start)),
                    "all_day": all_day,
                    "location": _clean(ev.get("location")) or None,
                    "_sort": (start, not all_day, _clean(ev.get("summary")).lower())})
    out.sort(key=lambda e: e["_sort"])
    for e in out:
        del e["_sort"]
    return out[:MAX_EVENTS]


def _todos(candidates: list[dict]) -> list[dict]:
    """Due today or overdue (oldest date first), then items of pinned notes without a
    date, in note order; at most :data:`MAX_TODOS`."""
    due = [c for c in candidates if c.get("bucket") in ("overdue", "due_today")]
    due.sort(key=lambda c: c.get("date") or "")  # stable: note order within a day
    pinned = [c for c in candidates if c.get("bucket") == "undated" and c.get("pinned")]
    return [{"ref": c["ref"], "title": c["title"], "due": c.get("date"),
             "list": c.get("list")} for c in [*due, *pinned][:MAX_TODOS]]


def _open_action_items(rows: Iterable[Any], filed: Iterable[str] = ()) -> list[dict]:
    """Athena's open rows not yet filed here, as the device shows them (all of them)."""
    filed = {str(i) for i in filed or ()}
    out = []
    for row in rows or ():
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        if str(row.get("status") or "open") != "open" or str(row["id"]) in filed:
            continue
        title = _clean(row.get("title")) or _clean(row.get("text"))
        if title:
            out.append({"id": str(row["id"]), "title": title, "source": "athena"})
    return out


def _focus(insight: dict | None, open_todo_refs: set[str] | None) -> list[dict]:
    """The insight's focus rows (``ref``, ``title``, ``why``); a to-do no longer open is
    left out when the open to-dos are known (``open_todo_refs`` not None)."""
    out = []
    for row in (insight or {}).get("focus") or ():
        if not isinstance(row, dict) or not row.get("ref"):
            continue
        ref = str(row["ref"])
        if open_todo_refs is not None and ref.startswith("todo:") and ref not in open_todo_refs:
            continue
        out.append({"ref": ref, "title": _clean(row.get("title")),
                    "why": _clean(row.get("why"), 200)})
    return out


def shape_today(*, now_local: datetime, insight: dict | None = None,
                occurrences: Iterable[Any] | None = None,
                todo_candidates: list[dict] | None = None,
                action_rows: Iterable[Any] | None = None,
                filed: Iterable[str] = ()) -> dict:
    """Contract C1 from what was loaded. ``now_local``: aware, in the owner's zone.
    ``todo_candidates``: :func:`fork.insight.today_candidates.todo_candidates` (None: the
    to-dos couldn't be read). ``action_rows``: Athena's JSON rows (None: unreachable)."""
    todos_all = todo_candidates or []
    open_refs = {c["ref"] for c in todos_all} if todo_candidates is not None else None
    actions = _open_action_items(action_rows or (), filed)
    headline = _clean((insight or {}).get("headline"), 300) or None
    return {
        "date": now_local.date().isoformat(),
        "generated": _iso(now_local),
        "headline": headline,
        "focus": _focus(insight, open_refs),
        "events": _events(occurrences or (), now_local),
        "todos": _todos(todos_all),
        "action_items": actions[:MAX_ACTION_ITEMS],
        "counts": {"todos_open": len(todos_all), "action_items_open": len(actions)},
    }


# ------------------------------------------------------------------------- loading
def _now_local(owner: str, now: datetime | None = None) -> datetime:
    from fork.insight import today as ti

    tz: tzinfo
    try:
        tz = ti.owner_timezone(owner)
    except Exception:  # noqa: BLE001
        logger.warning("voice today: owner timezone unavailable", exc_info=True)
        tz = timezone.utc
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(tz)


def load_insight(owner: str, now: datetime) -> dict | None:
    from fork.insight import outcomes as io
    from fork.insight import today as ti

    found = ti.load(owner, now.date().isoformat())
    if not found:
        return None
    try:
        return io.ordered(owner, found)
    except Exception:  # noqa: BLE001 -- the stored order is fine too
        logger.warning("voice today: owner order failed", exc_info=True)
        return found


def load_todos(owner: str, now: datetime) -> list[dict]:
    from fork.insight import today as ti

    return ti.todo_candidates(ti._load_notes(owner), now.date(), now.tzinfo)


def load_events(owner: str, now: datetime) -> list[dict]:
    """The insight's occurrences (events starting from yesterday on, series expanded) and
    the single events that started before that and haven't ended (a week's holiday)."""
    from fork.insight import today as ti

    out = ti._load_events(owner, now)
    have = {str(e.get("uid")) for e in out if isinstance(e, dict)}
    out.extend(e for e in _ongoing_events(owner, now) if str(e.get("uid")) not in have)
    return out


def _ongoing_events(owner: str, now: datetime) -> list[dict]:
    """Non-recurring events that started before the insight's window (local midnight -1
    day, naive) and end after it; :func:`_events` keeps those that overlap today."""
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    from fork.upstream import expand_rrule as _expand_rrule

    since = datetime.combine(now.date(), time.min) - timedelta(days=1)
    db = SessionLocal()
    try:
        rows = (db.query(CalendarEvent)
                .join(CalendarCal, CalendarEvent.calendar_id == CalendarCal.id)
                .filter(CalendarCal.owner == owner, CalendarEvent.status != "cancelled",
                        CalendarEvent.dtstart < since, CalendarEvent.dtend > since,
                        (CalendarEvent.rrule.is_(None)) | (CalendarEvent.rrule == ""))
                .order_by(CalendarEvent.dtstart.desc()).limit(100).all())
        out = []
        for ev in rows:
            out.extend(_expand_rrule(ev, since, since + timedelta(days=2)))
        return out
    finally:
        db.close()


def load_filed(owner: str) -> set[str]:
    from fork.athena import action_items as mai

    return set((mai.load_state(owner or "").get("filed") or {}).keys())


async def load_action_rows(owner: str) -> list[dict]:
    """Athena's open action items through the hub's link; raises when it can't answer."""
    from fork.athena import call_recorder as rec

    status, body = await rec.module_json("GET", "/action-items", params={"status": "open"})
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"Athena answered {status}")
    rows = body.get("action_items")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


async def _section(name: str, fn: Any, *args: Any) -> Any:
    """``fn(*args)`` in a worker thread; None (and a log line) when it fails."""
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception as e:  # noqa: BLE001 -- one section must not fail the page
        logger.warning("voice today: %s unavailable: %s", name, e)
        return None


async def _athena(owner: str) -> list[dict] | None:
    try:
        return await asyncio.wait_for(load_action_rows(owner), ATHENA_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("voice today: Athena didn't answer within %.0f s", ATHENA_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        logger.warning("voice today: Athena's action items unavailable: %s", e)
    return None


async def build_today(owner: str, now: datetime | None = None) -> dict:
    """The C1 answer for ``owner``; never raises for a section's failure."""
    now_local = await asyncio.to_thread(_now_local, owner, now)  # a database read
    insight, todos, events, filed, rows = await asyncio.gather(
        _section("insight", load_insight, owner, now_local),
        _section("to-dos", load_todos, owner, now_local),
        _section("calendar", load_events, owner, now_local),
        _section("filed items", load_filed, owner),
        _athena(owner),
    )
    return shape_today(now_local=now_local, insight=insight, occurrences=events,
                       todo_candidates=todos, action_rows=rows, filed=filed or ())


# --------------------------------------------------------------------------- route
def setup_voice_today_routes() -> APIRouter:
    from fork.voice.routes import PREFIX, voice_owner

    router = APIRouter(tags=["fork-voice"])

    @router.get(PREFIX + "/today")
    async def today(request: Request) -> dict:
        owner = voice_owner(request)
        return await build_today(owner)

    return router

