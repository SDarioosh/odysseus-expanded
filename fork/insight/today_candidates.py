"""Fork: the Today insight's candidates and memory (``fork/insight/today.py``).

The open to-do items and calendar events of the next days, each bucketed and given a
ref, in the published rule order (:func:`order_candidates`); the memory lines for the
prompt (:func:`select_memory`); and the hash of everything the insight is built from.
Pure functions over what ``today.py`` loaded. ``MEMORY_SOURCE`` (Athena's memory
source) is defined here only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from datetime import date as Date
from datetime import datetime, timedelta, tzinfo
from typing import Any

from fork.common import settings

logger = logging.getLogger("fork.insight.today")  # the insight's one logger


TODO_TYPES = ("checklist", "todo", "goal")
MAX_CANDIDATES = 30
MAX_MEMORY = 40
MEMORY_CATEGORIES = frozenset({"preference", "contact", "fact"})
MEMORY_SOURCE = "odysseus-sound"
DEFAULT_SKIP_LISTS = settings.SKIP_LISTS.default  # "Shopping,Groceries,Grocery"


def skip_lists() -> set[str]:
    raw = settings.today_insight_skip_lists()
    return {k for k in (_list_key(p) for p in raw.split(",")) if k}


def _list_key(title: Any) -> str:
    text = re.sub(r"\s+", " ", str(title or "").strip().lower())
    return re.sub(r"\s*\blist$", "", text).strip()


def is_skipped_list(title: Any, due_date: Any = None, names: set[str] | None = None) -> bool:
    """A shopping-type list (by title) without a due date."""
    if str(due_date or "").strip():
        return False
    key = _list_key(title)
    return bool(key) and key in (skip_lists() if names is None else names)


def local_date(value: Any, tz: tzinfo) -> Date | None:
    """A note's ``due_date`` (``YYYY-MM-DD``, naive or offset ISO datetime) as a local date."""
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return Date.fromisoformat(text)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
        try:
            return Date.fromisoformat(m.group(1)) if m else None
        except ValueError:
            return None
    return dt.astimezone(tz).date() if dt.tzinfo else dt.date()


def bucket_for(day: Date | None, today: Date) -> str:
    if day is None:
        return "undated"
    if day < today:
        return "overdue"
    if day == today:
        return "due_today"
    if day <= today + timedelta(days=7):
        return "due_soon"
    return "later"


def _items(raw: Any) -> list:
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _item_done(item: Any) -> bool:
    return isinstance(item, dict) and bool(item.get("done") or item.get("checked")
                                           or item.get("complete"))


def _item_text(item: Any) -> str:
    text = item.get("text") if isinstance(item, dict) else item
    return re.sub(r"\s+", " ", str(text or "")).strip() if isinstance(text, (str, int, float)) else ""


def todo_candidates(notes: Iterable[Any], today: Date, tz: tzinfo,
                    names: set[str] | None = None) -> list[dict]:
    """Open items of to-do notes, in the given note order, each with its bucket."""
    names = skip_lists() if names is None else names
    out = []
    for n in notes:
        if (getattr(n, "note_type", None) or "note") not in TODO_TYPES:
            continue
        if getattr(n, "archived", False):
            continue
        if getattr(n, "source", None) == "calendar" and getattr(n, "label", None) == "calendar":
            continue  # an event's reminder to-do; the event itself is a candidate
        title = str(getattr(n, "title", None) or "").strip()
        due_raw = getattr(n, "due_date", None)
        if is_skipped_list(title, due_raw, names):
            continue
        day = local_date(due_raw, tz)
        bucket = bucket_for(day, today)
        for index, item in enumerate(_items(getattr(n, "items", None))):
            text = _item_text(item)
            if not text or _item_done(item):
                continue
            out.append({"ref": f"todo:{n.id}:{index}", "kind": "todo", "bucket": bucket,
                        "title": text[:200], "date": day.isoformat() if day else None,
                        "time": None, "list": title[:80] or None,
                        "pinned": bool(getattr(n, "pinned", False)),
                        "note_id": str(n.id),
                        "note_type": getattr(n, "note_type", None) or "checklist"})
    return out


def _occurrence_time(value: Any, tz: tzinfo) -> datetime | None:
    """An occurrence's ``dtstart``/``dtend`` as local time. The calendar route's dicts
    hold ``YYYY-MM-DD`` (all-day), ISO with ``Z`` (stored as UTC) or naive ISO (local)."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)


def event_candidates(occurrences: Iterable[dict], now_local: datetime) -> list[dict]:
    """Event occurrences (the calendar route's ``_expand_rrule`` dicts) from today to
    +7 days that haven't ended, each with its bucket. The ref is the occurrence uid:
    ``{series uid}::{date or datetime}`` for a recurring event, the plain uid otherwise,
    as in the calendar route's answer (and the T5 feed built from it)."""
    tz = now_local.tzinfo
    today = now_local.date()
    last = today + timedelta(days=7)
    out = []
    for ev in occurrences:
        if not isinstance(ev, dict) or (ev.get("status") or "") == "cancelled":
            continue
        uid = str(ev.get("uid") or "").strip()
        start = _occurrence_time(ev.get("dtstart"), tz)
        if not uid or start is None:
            continue
        end = _occurrence_time(ev.get("dtend"), tz) or start
        all_day = bool(ev.get("all_day"))
        if all_day:
            # all-day ends are dates (often the next midnight); over once its days are past
            last_day = max(start.date(), (end - timedelta(seconds=1)).date())
            if last_day < today:
                continue
        elif end <= now_local and start <= now_local:
            continue  # already ended
        if start.date() > last:
            continue
        day = max(start.date(), today)
        out.append({"ref": f"event:{uid}", "kind": "event",
                    "bucket": "due_today" if day == today else "due_soon",
                    "title": (str(ev.get("summary") or "").strip() or "(no title)")[:200],
                    "date": day.isoformat(),
                    "time": None if all_day else start.strftime("%H:%M"),
                    "all_day": all_day,
                    "location": (str(ev.get("location") or "").strip() or None),
                    "importance": (ev.get("importance") or "normal"),
                    "event_type": ev.get("event_type") or None})
    return out


def order_candidates(todos: list[dict], events: list[dict],
                     limit: int = MAX_CANDIDATES, *, weights: dict | None = None,
                     dismissed: Iterable[str] = ()) -> list[dict]:
    """The published rule order (docs/fork/today-insight.md#rule-order), at most ``limit``:
    tier 0 overdue and due today, 1 due soon, 2 items of pinned notes, 3 undated, 4 later;
    within a tier higher weight first (``src/insight_weights.py``; never across tiers),
    then date and time, then input order. Each candidate gets its ``weight``. Refs in
    ``dismissed`` (the owner's "not today") are left out."""
    from fork.insight import weights as insight_weights

    def tier(c):
        b = c["bucket"]
        if b in ("overdue", "due_today"):
            return 0
        if b == "due_soon":
            return 1
        if c.get("pinned"):
            return 2
        return 3 if b == "undated" else 4

    def key(pair):
        seq, c = pair
        t = tier(c)
        dated = t in (0, 1, 4)
        return (t, -c["weight"], (c.get("date") or "") if dated else "",
                (c.get("time") or "") if dated else "", seq)

    gone = set(dismissed or ())
    pool = [c for c in [*todos, *events] if c["ref"] not in gone]
    for c in pool:
        c["weight"] = insight_weights.candidate_weight(c, weights) if weights else 0
    ordered = [c for _, c in sorted(enumerate(pool), key=key)]
    return ordered[:limit]


_SOURCE_SUFFIX = re.compile(r"\s*\(source: https?://[^\s()]*\)\s*$")


def _memory_text(text: Any) -> str:
    """One line, without the trailing "(source: <link>)" of module copies: the link
    is for chat answers; in the insight prompt it only costs tokens."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return _SOURCE_SUFFIX.sub("", text).strip()


def _by_use(entries: Iterable[dict], limit: int) -> tuple[list[str], list[dict]]:
    picked = []
    for m in entries or []:
        if not isinstance(m, dict):
            continue
        text = re.sub(r"\s+", " ", str(m.get("text") or "")).strip()
        if not text:
            continue
        category = str(m.get("category") or "fact").strip().lower()
        if category in MEMORY_CATEGORIES or m.get("source") == MEMORY_SOURCE:
            picked.append(m)

    def num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    picked.sort(key=lambda m: (num(m.get("uses")), num(m.get("timestamp"))), reverse=True)
    rows = [(_memory_text(m["text"])[:300], m) for m in picked]
    rows = [(t, m) for t, m in rows if t]
    return [t for t, _ in rows][:limit], [m for _, m in rows]


def memory_query(candidates: Iterable[dict]) -> str:
    """The query the belief ranker gets for the day: candidate titles, list names and
    locations (people and projects are usually named there), one per line."""
    parts: list[str] = []
    for c in candidates or []:
        for key in ("title", "list", "location"):
            value = re.sub(r"\s+", " ", str(c.get(key) or "")).strip()
            if value and value not in parts:
                parts.append(value)
    return "\n".join(parts)


def select_memory(entries: Iterable[dict], limit: int = MAX_MEMORY, *,
                  owner: str | None = None, query: str | None = None) -> list[str]:
    """Memory texts for the insight prompt (source links stripped).

    With ``query`` (``memory_query`` of the day's candidates): the confirmed beliefs the
    ledger's ranker (``src/belief_search.py``) puts first for it, then the other
    preference/contact/fact entries and Athena facts, most used first, up to
    ``limit``. Without a query, or when the ledger has nothing for it: most used first."""
    fallback, rows = _by_use(entries, limit)
    if query is None:
        return fallback
    try:
        from fork.beliefs import search as belief_search

        hits = belief_search.search_beliefs(owner, query, k=limit)
    except Exception as e:  # noqa: BLE001 -- the insight is written without it
        logger.info("today insight: belief ranking unavailable: %s", e)
        hits = []
    if not hits:
        return fallback
    ids = {h["id"] for h in hits}
    out: list[str] = []
    for text in (_memory_text(h.get("shown_text") or h.get("text"))[:300] for h in hits):
        if text and text not in out:
            out.append(text)
    for m in rows:
        text = _memory_text(m["text"])[:300]
        if m.get("belief_id") in ids or text in out:
            continue
        out.append(text)
    return out[:limit]


def input_hash(candidates: list[dict], memory: list[str], digest: str | None,
               extra: Any = None) -> str:
    # memory sorted: chats bump use counts, which reorder the list without changing it
    blob = json.dumps({"candidates": candidates, "memory": sorted(memory or []),
                       "digest": digest or "",
                       "extra": extra or []}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def notes_state(notes: Iterable[Any]) -> dict[str, dict]:
    """The to-do notes as the outcome log needs them: per note id its type, due date,
    archived flag and items as ``(text, done)``."""
    out = {}
    for n in notes:
        out[str(n.id)] = {
            "note_type": getattr(n, "note_type", None) or "note",
            "due_date": getattr(n, "due_date", None),
            "archived": bool(getattr(n, "archived", False)),
            "items": [(_item_text(i), _item_done(i)) for i in _items(getattr(n, "items", None))]}
    return out
