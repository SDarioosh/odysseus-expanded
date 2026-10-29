"""Fork: what the Today insight reads (``fork/insight/today.py``).

The owner's to-do notes, calendar occurrences and memory from Odysseus, and today's
digest and open action items from Athena over its MCP tools (left out when Athena isn't
connected). ``today.gather`` calls these through ``today.py``, where tests and the eval
replace them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import date as Date
from datetime import datetime, time, timedelta

from fork.insight.today_candidates import TODO_TYPES, select_memory

logger = logging.getLogger("fork.insight.today")  # the insight's one logger


MAX_DIGEST = 1500
MAX_ACTION_ITEMS = 10
CallTool = Callable[[str, dict], Awaitable[str]]


def _load_notes(owner: str | None) -> list:
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(Note).filter(Note.archived == False,  # noqa: E712
                                  Note.note_type.in_(TODO_TYPES))
        if owner:
            q = q.filter(Note.owner == owner)
        notes = q.all()
        for n in notes:
            db.expunge(n)
    finally:
        db.close()

    def order(n):
        stamp = n.updated_at.timestamp() if isinstance(getattr(n, "updated_at", None), datetime) else 0
        return (0 if n.pinned else 1, -stamp)

    return sorted(notes, key=order)


def _load_events(owner: str | None, now_local: datetime) -> list[dict]:
    """The owner's event occurrences around the window, expanded by the calendar route's
    own ``_expand_rrule`` (recurring series whose first date is long past included)."""
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    from fork.upstream import checkin_calendar_events as _checkin_calendar_events
    from fork.upstream import expand_rrule as _expand_rrule

    midnight = datetime.combine(now_local.date(), time.min)
    # naive bounds wide enough for both naive-local and UTC-stored rows; filtered later
    start, end = midnight - timedelta(days=1), midnight + timedelta(days=9)
    db = SessionLocal()
    try:
        rows = {ev.uid: ev for ev in _checkin_calendar_events(db, owner, start, end)}
        series = (db.query(CalendarEvent).join(CalendarCal,
                                               CalendarEvent.calendar_id == CalendarCal.id)
                  .filter(CalendarCal.owner == owner, CalendarEvent.status != "cancelled",
                          CalendarEvent.rrule.isnot(None), CalendarEvent.rrule != "",
                          CalendarEvent.dtstart < end).all())
        rows.update((ev.uid, ev) for ev in series)
        out = []
        for ev in rows.values():
            try:
                out.extend(_expand_rrule(ev, start, end))
            except Exception as e:  # noqa: BLE001 -- one bad row must not hide the rest
                logger.warning("today insight: couldn't expand event %s: %s", ev.uid, e)
        return out
    finally:
        db.close()


def _load_memory(owner: str | None, query: str | None = None) -> list[str]:
    from src.constants import DATA_DIR
    from src.memory import MemoryManager

    return select_memory(MemoryManager(DATA_DIR).load(owner), owner=owner, query=query)


def _digest_text(result: str) -> str | None:
    lines = [ln.rstrip() for ln in (result or "").splitlines() if ln.strip()]
    if not lines or lines[0].startswith("No digest"):
        return None
    lines = [ln for ln in lines if not ln.startswith("When answering")]
    return "\n".join(lines)[:MAX_DIGEST] or None


def _action_item_lines(owner: str | None, result: str | list,
                       skip: Iterable[str] = ()) -> list[str]:
    """Prompt lines for the open action items (``result``: the tool's JSON text or its
    rows); ids in ``skip`` (the open loops, which are candidates) are left out."""
    from fork.athena import action_items as mai

    rows = result if isinstance(result, list) else (mai.parse_items_json(result) or [])
    skip = {str(i) for i in skip}
    try:
        filed = set((mai.load_state(owner or "").get("filed") or {}).keys())
    except Exception:  # noqa: BLE001
        filed = set()
    lines = []
    for row in rows:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        if str(row.get("status") or "open") != "open" or str(row["id"]) in filed \
                or str(row["id"]) in skip:
            continue  # filed items are already to-dos or events; loops are candidates
        text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
        if not text:
            continue
        due = " ".join(str(row.get(k) or "") for k in ("due_date", "due_time")).strip() \
            or str(row.get("due") or "")
        lines.append(f"{text[:160]}{f' (due {due})' if due else ''}")
        if len(lines) >= MAX_ACTION_ITEMS:
            break
    return lines


def _module_caller() -> CallTool | None:
    try:
        from fork.athena.client import ATHENA

        return ATHENA.caller()
    except Exception as e:  # noqa: BLE001
        logger.debug("today insight: module not reachable: %s", e)
        return None


async def _module_inputs(owner: str | None, today: Date, call: CallTool | None,
                         loops: Iterable[str] = ()
                         ) -> tuple[str | None, list[str], list[dict], list[dict] | None]:
    """(digest text, open action-item lines for the prompt, the raw JSON rows, the done
    rows). ``loops``: ids of open loops the outcome log still has open; when one of them
    isn't among the open rows, the done items are read once (else None), so the log can
    tell a closed loop (``done``) from a deleted one (``gone``)."""
    if call is None:
        return None, [], [], None
    digest, items, rows, done = None, [], [], None
    try:
        digest = _digest_text(await call("get_daily_digest", {"date": today.isoformat()}))
    except Exception as e:  # noqa: BLE001 -- the insight is written without it
        logger.info("today insight: get_daily_digest failed: %s", e)
    try:
        raw = await call("list_action_items", {"status": "open", "format": "json"})
        from fork.athena import action_items as mai

        rows = [r for r in (mai.parse_items_json(raw) or []) if isinstance(r, dict)]
        items = _action_item_lines(owner, raw)
    except Exception as e:  # noqa: BLE001
        logger.info("today insight: list_action_items failed: %s", e)
        return digest, items, rows, None  # unreachable: closed loops can't be told
    if set(map(str, loops)) - {str(r.get("id")) for r in rows}:
        try:
            raw = await call("list_action_items", {"status": "done", "format": "json"})
            from fork.athena import action_items as mai

            done = [r for r in (mai.parse_items_json(raw) or []) if isinstance(r, dict)]
        except Exception as e:  # noqa: BLE001
            logger.info("today insight: list_action_items (done) failed: %s", e)
    return digest, items, rows, done
