"""Fork: routines seen in the calendar, for the Today insight (Phase 3.4a).

A routine is a habit the calendar shows without saying so: the owner's single
(non-recurring, timed, not cancelled) events of the last ``WEEKS`` weeks, grouped by
normalized summary and weekday, whose starts lie within ``TOLERANCE_MINUTES`` of the
group's median start, in at least ``MIN_WEEKS`` distinct weeks. Recurring series are
skipped: they already say they repeat. :func:`detect` is the pure rule.

:func:`summary` (called by ``today_insight.gather`` on every pass) computes them at most
once a day, cached in ``<DATA_DIR>/today_insight/<owner>/routines.json``, and returns a
short text (≤ ``MAX_SUMMARY`` characters) for the prompt's ``ROUTINES`` block, e.g.
``- Gym: Tuesdays around 18:00 (4 of the last 6 weeks)``.

Each routine is submitted to the belief ledger (``src/evidence_intake.py``) once, when it
is first seen: source ``routines``, id ``<normalized summary>:<weekday>``, about the
owner, link ``/#event-<date of its last occurrence>``, one sighting on one day, never
source-confirmed. The ids are kept in ``routines.json`` (``submitted``), so a routine
seen again every day is not submitted again, and one sighting can't meet the ledger's
repetition rule: the owner confirms a routine on ``/beliefs``.

Docs: ``docs/fork/today-insight.md#routines``.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

SOURCE = "routines"
WEEKS = 6
MIN_WEEKS = 3
TOLERANCE_MINUTES = 60
MAX_SUMMARY = 400
MAX_TITLE = 60
CACHE_VERSION = 1
WEEKDAYS = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays",
            "Sundays")


# --------------------------------------------------------------------------- rule
def normalize(summary: Any) -> str:
    """The grouping key of an event summary: lower case, letters and digits only, single
    spaces ("Gym!", " gym ", "GYM" -> "gym")."""
    text = str(summary or "").casefold()
    text = re.sub(r"[^\w\s]|_", " ", text)
    return " ".join(text.split())


def _start(value: Any, tz: tzinfo) -> datetime | None:
    """A start as local time: aware values converted, naive ones taken as local."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)


def _round_quarter(minutes: int) -> str:
    m = int(round(minutes / 15.0)) * 15 % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def detect(events: Iterable[dict], tz: tzinfo, today: Date) -> list[dict]:
    """Routines in ``events`` (dicts with ``summary``, ``dtstart``; optional ``rrule``,
    ``all_day``, ``status``) from the ``WEEKS`` weeks before ``today``. Each routine:
    ``id`` (``<normalized summary>:<weekday 0-6>``), ``title`` (the latest summary as
    written), ``weekday``, ``days`` ("Tuesdays"), ``time`` (the median start, to the
    quarter hour), ``weeks`` (distinct weeks seen), ``of_weeks`` and ``last`` (the date
    of the latest occurrence). Most weeks first, then weekday and time."""
    first = today - timedelta(weeks=WEEKS)
    groups: dict[tuple[str, int], list[tuple[datetime, str]]] = {}
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        if str(ev.get("rrule") or "").strip() or ev.get("all_day"):
            continue
        if str(ev.get("status") or "") == "cancelled":
            continue
        key = normalize(ev.get("summary"))
        start = _start(ev.get("dtstart"), tz)
        if not key or start is None or not (first <= start.date() < today):
            continue
        groups.setdefault((key, start.weekday()), []).append(
            (start, " ".join(str(ev.get("summary") or "").split())))
    out = []
    for (key, weekday), rows in groups.items():
        minutes = [s.hour * 60 + s.minute for s, _ in rows]
        median = statistics.median_low(minutes)
        kept = [(s, t) for (s, t), m in zip(rows, minutes)
                if abs(m - median) <= TOLERANCE_MINUTES]
        weeks = {s.isocalendar()[:2] for s, _ in kept}
        if len(weeks) < MIN_WEEKS:
            continue
        kept.sort(key=lambda r: r[0])
        mid = statistics.median([s.hour * 60 + s.minute for s, _ in kept])
        out.append({"id": f"{key}:{weekday}", "title": kept[-1][1][:MAX_TITLE],
                    "weekday": weekday, "days": WEEKDAYS[weekday],
                    "time": _round_quarter(int(mid)), "weeks": len(weeks),
                    "of_weeks": WEEKS, "last": kept[-1][0].date().isoformat()})
    out.sort(key=lambda r: (-r["weeks"], r["weekday"], r["time"], r["id"]))
    return out


def line(r: dict) -> str:
    return (f"- {r['title']}: {r['days']} around {r['time']} "
            f"({r['weeks']} of the last {r['of_weeks']} weeks)")


def summary_text(routines: Iterable[dict], limit: int = MAX_SUMMARY) -> str:
    """The prompt lines, whole lines only, at most ``limit`` characters."""
    text = ""
    for r in routines or []:
        part = line(r)
        joined = f"{text}\n{part}" if text else part
        if len(joined) > limit:
            break
        text = joined
    return text


# ------------------------------------------------------------------------- loading
def load_events(owner: str | None, today: Date, tz: tzinfo) -> list[dict]:
    """The owner's single (non-recurring) timed events starting in the ``WEEKS`` weeks
    before today, starts as local datetimes."""
    from core.database import CalendarCal, CalendarEvent, SessionLocal

    # naive bounds a day wider than the window, for naive-local and UTC-stored rows
    start = datetime.combine(today - timedelta(weeks=WEEKS, days=1), time.min)
    end = datetime.combine(today + timedelta(days=1), time.min)
    db = SessionLocal()
    try:
        rows = (db.query(CalendarEvent)
                .join(CalendarCal, CalendarEvent.calendar_id == CalendarCal.id)
                .filter(CalendarCal.owner == owner, CalendarEvent.status != "cancelled",
                        CalendarEvent.dtstart >= start, CalendarEvent.dtstart < end)
                .all())
        out = []
        for ev in rows:
            if str(ev.rrule or "").strip() or ev.all_day or ev.dtstart is None:
                continue
            dt = ev.dtstart
            dt = dt.replace(tzinfo=timezone.utc) if ev.is_utc else dt.replace(tzinfo=tz)
            out.append({"uid": ev.uid, "summary": ev.summary or "", "dtstart": dt.astimezone(tz)})
        return out
    finally:
        db.close()


# ---------------------------------------------------------------------------- cache
def cache_path(owner: str | None) -> Path:
    from src.today_insight import insight_path

    return insight_path(owner, "2000-01-01").parent / "routines.json"


def _load_cache(owner: str | None) -> dict:
    try:
        data = json.loads(cache_path(owner).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(owner: str | None, data: dict) -> None:
    path = cache_path(owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------------- evidence
def evidence_text(r: dict) -> str:
    return f"Usually has {r['title']} on {r['days']} around {r['time']}"


def submit_new(owner: str | None, routines: list[dict], submitted: set[str], *,
               now: datetime | None = None) -> set[str]:
    """Submit routines not submitted before, once each; returns the ids submitted now."""
    from src import evidence_intake as ledger

    new = [r for r in routines if r["id"] not in submitted]
    if not new:
        return set()
    ledger.submit(owner, [ledger.Evidence(
        source=SOURCE, source_id=r["id"], subject="", about="owner",
        text=evidence_text(r), link=f"/#event-{r['last']}", seen_first=r["last"],
        seen_last=r["last"], times=1, days=1, source_confirmed=False, category="fact",
        payload={"weekday": r["weekday"], "time": r["time"], "weeks": r["weeks"]})
        for r in new], now=now)
    return {r["id"] for r in new}


def routines(owner: str | None, today: Date, *, now: datetime | None = None,
             events: list[dict] | None = None, tz: tzinfo | None = None) -> list[dict]:
    """Today's routines: from the cache when computed today, else detected now (and new
    ones submitted as evidence, once each)."""
    cache = _load_cache(owner)
    if cache.get("version") == CACHE_VERSION and cache.get("date") == today.isoformat():
        return [r for r in cache.get("routines") or [] if isinstance(r, dict)]
    if tz is None:
        from src.today_insight import owner_timezone

        tz = owner_timezone(owner)
    found = detect(load_events(owner, today, tz) if events is None else events, tz, today)
    submitted = {str(i) for i in cache.get("submitted") or []}
    try:
        submitted |= submit_new(owner, found, submitted, now=now)
    except Exception as e:  # noqa: BLE001 -- tried again with tomorrow's pass
        logger.warning("routines: evidence not submitted: %s", e)
    _save_cache(owner, {"version": CACHE_VERSION, "date": today.isoformat(),
                        "routines": found, "submitted": sorted(submitted)})
    return found


def summary(owner: str | None, today: Date, *, now: datetime | None = None) -> str:
    """The prompt's routines text ("" when none)."""
    return summary_text(routines(owner, today, now=now))
