"""Fork: "Send Athena" on calendar events: Athena's Zoom bot joins toggled meetings itself.

Each calendar event with a Zoom link gets a **Send Athena** toggle in the event form
(``static/js/forkMeetingAutoJoin.js``), off by default (the owner's decision). A toggled
event gets exactly one one-shot task in the existing ``TaskScheduler``
(``scheduled_tasks``, action ``meeting_auto_join``), due one minute before the start. When
it runs it sends the bot in through ``src/meeting_bot.py`` (the same join call, link rules
and error words as the "Send Athena to a meeting" box). Athena joins on the owner's behalf
(Zoom's OBF token) and waits for the owner to be in the meeting, so the owner joins too.

Nothing new runs between meetings: no thread, timer or poll. The tasks follow the calendar
whenever it changes (:func:`after_calendar_change`, called after the CalDAV pull in
``src/caldav_sync.py`` and after an edit or delete in ``routes/calendar_routes.py``), when
the toggle changes, and when a task runs:

* a moved event moves its task (same task id); a deleted event, a removed Zoom link or a
  toggle turned off cancels it (the row is deleted);
* a repeating event gets a task for its next occurrence; after each run the next one is
  scheduled;
* toggled meetings that overlap: the earlier one wins; the later one gets no task and one
  notification that Athena is busy then (the hub's task notifications).

The toggles live in the hub, never in Athena: ``<DATA_DIR>/meeting_auto_join/<owner>.json``
``{"events": {uid: {"on", "task_id", "start", "joined", "busy"}}}``, keyed by the series
uid (the part before ``::``). ``task_id`` makes every reconcile idempotent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src import meeting_bot

logger = logging.getLogger(__name__)

ACTION = "meeting_auto_join"
INFO = ("Send Athena's Zoom bot into a calendar meeting one minute before it starts "
        "(created by the event's Send Athena toggle; one task per toggled event).")
LEAD = timedelta(minutes=1)
TASK_PREFIX = "Send Athena: "
MAX_SUMMARY = 120
MAX_OCCURRENCES = 1000  # rrule steps looked at to find the next occurrence

# Tests set a fake httpx transport here; None means the real network.
TRANSPORT = None

_LOCK = threading.RLock()
_RUNNING: set[str] = set()  # task ids whose join is in progress (never deleted then)


# ------------------------------------------------------------------ storage
def _slug(owner: str | None) -> str:
    slug = "".join(c if (c.isalnum() or c in "-_@") else "_" for c in (owner or "default"))
    return slug or "default"


def state_path(owner: str | None) -> Path:
    from src import constants

    return Path(constants.DATA_DIR) / "meeting_auto_join" / f"{_slug(owner)}.json"


def load(owner: str | None) -> dict[str, Any]:
    try:
        data = json.loads(state_path(owner).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    events = data.get("events") if isinstance(data, dict) else None
    return {"events": {str(k): v for k, v in (events or {}).items() if isinstance(v, dict)}}


def save(owner: str | None, state: dict[str, Any]) -> None:
    path = state_path(owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def base_uid(uid: str) -> str:
    """A recurring occurrence's uid (``<series>::<date>``) -> the series uid."""
    return (uid or "").split("::", 1)[0].strip()


# ------------------------------------------------------------------ events
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def zoom_join(ev: Any) -> tuple[meeting_bot.JoinRequest | None, str | None]:
    """The event's Zoom join link (location first, then the description), read with the
    "Send Athena to a meeting" box's rules (``meeting_bot.parse_join``). Returns
    ``(request, None)``, ``(None, why)`` for a Zoom link Athena can't join (a webinar, a
    personal link…), or ``(None, None)`` when the event has no Zoom link at all. A bare
    "Meeting ID:" line without a link doesn't count."""
    text = "\n".join(p for p in (getattr(ev, "location", "") or "",
                                 getattr(ev, "description", "") or "") if p.strip())
    text = text[:meeting_bot.MAX_INPUT_CHARS].strip()
    if not text or not re.search(r"zoom", text, re.IGNORECASE):
        return None, None
    try:
        request = meeting_bot.parse_join(text)
    except meeting_bot.MeetingError as exc:
        if re.search(r"zoom(?:gov)?\.|zoommtg:|zoomus:", text, re.IGNORECASE):
            return None, exc.message
        return None, None
    if not request.url:
        return None, None
    return request, None


def _event(db: Any, owner: str, uid: str) -> Any:
    from core.database import CalendarCal, CalendarEvent

    ev = (db.query(CalendarEvent).join(CalendarCal)
          .filter(CalendarEvent.uid == uid).first())
    if ev is None or ev.calendar is None:
        return None
    if owner and ev.calendar.owner != owner:
        return None
    return ev


def _to_utc(ev: Any, dt: datetime) -> datetime:
    """A stored start -> naive UTC. ``is_utc`` rows are UTC already; others are the
    user's wall time (``src/user_time.py``)."""
    if getattr(ev, "is_utc", False):
        return dt
    from src.user_time import user_timezone

    return dt.replace(tzinfo=user_timezone()).astimezone(timezone.utc).replace(tzinfo=None)


def _exdates(ev: Any) -> set[str]:
    try:
        values = json.loads(getattr(ev, "recurrence_exdates", "") or "[]")
    except ValueError:
        return set()
    return {str(v) for v in values} if isinstance(values, list) else set()


def next_occurrence(ev: Any, now: datetime, joined: datetime | None = None
                    ) -> tuple[datetime, datetime] | None:
    """The first occurrence (start, end in naive UTC) not over yet and not already joined;
    None for all-day events and series that have ended."""
    if getattr(ev, "all_day", False) or not ev.dtstart or not ev.dtend:
        return None
    duration = ev.dtend - ev.dtstart
    rule_text = (getattr(ev, "rrule", "") or "").strip()
    if not rule_text:
        start = _to_utc(ev, ev.dtstart)
        end = start + duration
        if end <= now or (joined is not None and start == joined):
            return None
        return start, end
    from dateutil.rrule import rrulestr

    # Same tweak as routes/calendar_routes.py _expand_rrule: a naive DTSTART with UNTIL=…Z.
    rule_text = re.sub(r"(UNTIL=\d{8}(?:T\d{6})?)Z", r"\1", rule_text, flags=re.IGNORECASE)
    try:
        rule = rrulestr(rule_text, dtstart=ev.dtstart)
    except (ValueError, TypeError):
        return None
    skip = _exdates(ev)
    # Look from a day back in the stored frame; the UTC checks below decide.
    for count, occ in enumerate(rule.xafter(now - duration - timedelta(days=1), inc=True)):
        if count >= MAX_OCCURRENCES:
            return None
        if occ.strftime("%Y-%m-%dT%H:%M") in skip:
            continue
        start = _to_utc(ev, occ)
        end = start + duration
        if end <= now or (joined is not None and start == joined):
            continue
        return start, end
    return None


def _summary(ev: Any) -> str:
    text = " ".join((getattr(ev, "summary", "") or "").split()) or "Zoom meeting"
    return text[:MAX_SUMMARY]


# ------------------------------------------------------------------ tasks
def _notify(title: str, status: str, owner: str, body: str,
            task_id: str | None = None) -> None:
    """The hub's task notifications (toast, browser notification, notification log)."""
    try:
        from src.event_bus import get_task_scheduler

        scheduler = get_task_scheduler()
        if scheduler is not None:
            scheduler.add_notification(title, status, task_id, owner=owner, body=body)
    except Exception:
        logger.warning("Send Athena: could not add a notification", exc_info=True)


def _delete_task(db: Any, task_id: str | None) -> None:
    if not task_id or task_id in _RUNNING:
        return
    from core.database import ScheduledTask, TaskRun

    task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
    if task is None or task.action != ACTION:
        return
    db.query(TaskRun).filter(TaskRun.task_id == task_id).delete(synchronize_session=False)
    db.delete(task)


def _ensure_task(db: Any, owner: str, uid: str, entry: dict, summary: str,
                 due: datetime) -> None:
    """Create the event's task, or move the one it has. A paused task is the owner's
    choice and is left alone."""
    from core.database import ScheduledTask

    name = TASK_PREFIX + summary
    task = None
    if entry.get("task_id"):
        task = db.query(ScheduledTask).filter(ScheduledTask.id == entry["task_id"]).first()
        if task is not None and (task.owner != owner or task.action != ACTION
                                 or task.prompt != uid):
            task = None
    if task is not None and task.id not in _RUNNING:
        if task.status == "paused":
            return
        if task.status == "active":
            task.scheduled_date = due
            task.next_run = due
            task.name = name
            return
        _delete_task(db, task.id)  # completed: the next occurrence gets a fresh row
    from src.agent_runtime.authority import seal_task_authority

    task = ScheduledTask(
        id=uuid.uuid4().hex[:8],
        owner=owner,
        name=name,
        task_type="action",
        action=ACTION,
        prompt=uid,
        request_authority_json=seal_task_authority(uid, "action", ACTION, owner=owner),
        trigger_type="schedule",
        schedule="once",
        scheduled_date=due,
        next_run=due,
        status="active",
        output_target="none",
        notifications_enabled=False,
    )
    db.add(task)
    entry["task_id"] = task.id


def _busy_text(summary: str, start: datetime, winner: str) -> str:
    return (f"Athena is busy in “{winner}” then, so it won't join “{summary}” "
            f"({_iso(start)}). Turn Send Athena off on one of them, or join this one "
            "from the Record a call page.")


def reconcile(owner: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Bring the owner's Send Athena tasks in line with the calendar and the toggles.
    Idempotent; returns ``{"scheduled": n, "busy": n, "cancelled": n}``."""
    now = now or _utcnow()
    counts = {"scheduled": 0, "busy": 0, "cancelled": 0}
    import core.database as cdb

    with _LOCK:
        state = load(owner)
        events = state["events"]
        if not events:
            return counts
        notes: list[tuple[str, str]] = []
        db = cdb.SessionLocal()
        try:
            plan = []  # (start, end, uid, summary)
            for uid, entry in list(events.items()):
                ev = _event(db, owner, uid)
                if ev is None or not entry.get("on"):
                    _delete_task(db, entry.get("task_id"))
                    events.pop(uid)
                    counts["cancelled"] += 1
                    continue
                request, _ = zoom_join(ev)
                occ = next_occurrence(ev, now, _parse_iso(entry.get("joined")))
                if request is None or occ is None:
                    if entry.get("task_id"):
                        counts["cancelled"] += 1
                    _delete_task(db, entry.get("task_id"))
                    entry["task_id"] = None
                    entry["start"] = None
                    if (not (ev.rrule or "").strip() and ev.dtend
                            and _to_utc(ev, ev.dtend) <= now):
                        events.pop(uid)  # a one-off meeting that is over
                    continue
                plan.append((occ[0], occ[1], uid, _summary(ev)))
            plan.sort()
            winners: list[tuple[datetime, datetime, str]] = []
            for start, end, uid, summary in plan:
                entry = events[uid]
                clash = next((w for w in winners if w[0] < end and start < w[1]), None)
                if clash is not None:
                    _delete_task(db, entry.get("task_id"))
                    entry["task_id"] = None
                    entry["start"] = None
                    if entry.get("busy") != _iso(start):
                        entry["busy"] = _iso(start)
                        notes.append((uid, _busy_text(summary, start, clash[2])))
                    counts["busy"] += 1
                    continue
                winners.append((start, end, summary))
                entry["busy"] = None
                entry["start"] = _iso(start)
                _ensure_task(db, owner, uid, entry, summary, max(start - LEAD, now))
                counts["scheduled"] += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        save(owner, state)
    for _uid, text in notes:
        _notify("Send Athena", "error", owner, text)
    return counts


async def after_calendar_change(owner: str | None) -> None:
    """Hook target after a calendar pull, edit or delete. Never raises; does no work
    for an owner without toggles."""
    if not owner:
        return
    try:
        if not state_path(owner).exists():
            return
        await asyncio.to_thread(reconcile, owner)
    except Exception:
        logger.warning("Send Athena: reconcile after a calendar change failed", exc_info=True)


# ------------------------------------------------------------------ toggle (routes)
def describe(owner: str, uid: str) -> dict[str, Any]:
    """What the event form shows: ``eligible`` (a Zoom link Athena can join), ``on``,
    ``reason`` (why a Zoom link can't be used), and the scheduled ``start``/``due`` or
    ``busy`` occurrence. Raises ``LookupError`` for an unknown event."""
    import core.database as cdb

    uid = base_uid(uid)
    db = cdb.SessionLocal()
    try:
        ev = _event(db, owner, uid)
        if ev is None:
            raise LookupError(uid)
        request, reason = zoom_join(ev)
        all_day = bool(getattr(ev, "all_day", False))
    finally:
        db.close()
    entry = load(owner)["events"].get(uid) or {}
    start = _parse_iso(entry.get("start"))
    return {
        "uid": uid,
        "eligible": request is not None and not all_day,
        "zoom": request is not None or reason is not None,
        "reason": reason or ("All-day events have no start time." if request and all_day
                             else None),
        "on": bool(entry.get("on")),
        "start": entry.get("start"),
        "due": _iso(start - LEAD) if start else None,
        "busy": entry.get("busy"),
        "task_id": entry.get("task_id"),
    }


def set_toggle(owner: str, uid: str, on: bool) -> dict[str, Any]:
    """Turn Send Athena on or off for an event, then reconcile. Raises ``LookupError``
    for an unknown event and ``ValueError`` when it has no Zoom link Athena can join."""
    uid = base_uid(uid)
    info = describe(owner, uid)
    with _LOCK:
        state = load(owner)
        entry = state["events"].get(uid)
        if on:
            if not info["eligible"]:
                raise ValueError(info["reason"] or "This event has no Zoom meeting link.")
            if entry is None:
                state["events"][uid] = {"on": True, "task_id": None, "start": None,
                                        "joined": None, "busy": None}
            else:
                entry["on"] = True
        elif entry is not None:
            entry["on"] = False  # reconcile cancels the task and drops the entry
        save(owner, state)
        reconcile(owner)
    return describe(owner, uid)


# ------------------------------------------------------------------ the task's run
def is_time_critical(task: Any) -> bool:
    """Hook target in ``TaskScheduler._check_due_tasks``: a join can't wait 15 minutes
    for the owner to stop using Odysseus, so it runs on time, outside the model queue."""
    return (getattr(task, "task_type", None) == "action"
            and getattr(task, "action", None) == ACTION)


def _fail_text(summary: str, exc: meeting_bot.MeetingError) -> str:
    if exc.code == "busy":
        return (f"Athena was busy in another meeting, so it didn't join “{summary}”. "
                "Leave the other meeting from the Record a call page, then join this one "
                "there.")
    return f"Athena couldn't join “{summary}”: {exc.message}"


async def _join(owner: str, summary: str, request: meeting_bot.JoinRequest
                ) -> tuple[str, bool]:
    try:
        meeting = await meeting_bot.join(meeting_bot.resolve(), request,
                                         meeting_bot.check_title(summary),
                                         transport=TRANSPORT)
    except meeting_bot.MeetingError as exc:
        return _fail_text(summary, exc), False
    return (f"Athena is joining “{summary}”: {meeting.get('text') or 'joining…'} "
            "Join the meeting too: Athena waits for you before it goes in."), True


async def action_meeting_auto_join(owner: str, prompt: str = "", **_: Any
                                   ) -> tuple[str, bool]:
    """The scheduled task: check the event still starts now, then send the bot in."""
    import core.database as cdb
    from src.builtin_actions import TaskNoop

    uid = base_uid(prompt)
    task_id = None
    now = _utcnow()
    with _LOCK:
        state = load(owner)
        entry = state["events"].get(uid)
        if not entry or not entry.get("on"):
            raise TaskNoop("Send Athena is off for this event")
        task_id = entry.get("task_id")
        db = cdb.SessionLocal()
        try:
            ev = _event(db, owner, uid)
            request = zoom_join(ev)[0] if ev is not None else None
            occ = (next_occurrence(ev, now, _parse_iso(entry.get("joined")))
                   if ev is not None else None)
            summary = _summary(ev) if ev is not None else ""
        finally:
            db.close()
        # Moved later, deleted or without its link now: put the task right, don't join.
        if request is None or occ is None or occ[0] - LEAD > now + timedelta(seconds=90):
            reconcile(owner, now=now)
            raise TaskNoop("the event changed; its Send Athena task was updated")
        entry["joined"] = _iso(occ[0])
        save(owner, state)
    if task_id:
        _RUNNING.add(task_id)
    try:
        # A repeating meeting gets its next task now (this row stays: it's running).
        await asyncio.to_thread(reconcile, owner)
    except Exception:
        logger.warning("Send Athena: scheduling the next occurrence failed", exc_info=True)
    # The join runs shielded: if the scheduler cancels this run (the owner opened
    # Odysseus), the join still finishes and its outcome still reaches the owner.
    inner = asyncio.ensure_future(_join(owner, summary, request))

    def _after(fut: asyncio.Future, cancelled: bool) -> None:
        if task_id:
            _RUNNING.discard(task_id)
        if fut.cancelled() or fut.exception() is not None:
            return
        text, ok = fut.result()
        if ok or cancelled:
            _notify(TASK_PREFIX + summary, "success" if ok else "error", owner, text,
                    task_id)

    try:
        text, ok = await asyncio.shield(inner)
    except asyncio.CancelledError:
        inner.add_done_callback(lambda f: _after(f, True))
        raise
    _after(inner, False)  # a failure is notified by the scheduler (error run)
    return text, ok


def register_builtin_action(actions: dict, info: dict) -> None:
    actions[ACTION] = action_meeting_auto_join
    info[ACTION] = INFO
