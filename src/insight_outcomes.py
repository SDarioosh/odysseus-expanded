"""Fork: the Today insight's outcome log (Phase 3.1).

What became of each item the insight showed, per owner and day, in
``<DATA_DIR>/today_insight/<owner>/<day>.outcome.json`` next to the insight. Ids and
counts only: no titles, no text (the insight file has the titles). Written by the
15-minute pass (``observe``, called by ``today_insight._after_run``) and by the owner's
actions on ``/today`` (``record``, ``routes/insight_outcome_routes.py``). Read by the
weights (``src/insight_weights.py``), the push (``src/insight_push.py``) and the trends.

Outcomes: ``open``, ``done``, ``moved``, ``dismissed`` ("not today"), ``promoted``
("↑ first"), ``ignored`` (still open when the day was finalized), ``gone``. ``via``:
``today`` (the page), ``observed`` (the pass), ``final`` (finalize).

Docs: docs/fork/today-insight.md#outcome-log.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date as Date
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable, Iterator

logger = logging.getLogger(__name__)

VERSION = 1
OUTCOMES = ("open", "done", "moved", "dismissed", "promoted", "ignored", "gone")
OWNER_MARKS = ("dismissed", "promoted")
ACTIONS = ("done", "open", "dismissed", "promoted", "clear")
SECTIONS = ("focus", "coming_up", "also")
_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})\.outcome\.json$")
_TODO = re.compile(r"^todo:(.+):(\d+)(?:#\d+)?$")  # ``#n``: a spare key (``_retire_renamed``)
TITLE_CHARS = 200  # candidate titles are a line's first 200 characters
FINALIZE_DAYS = 28  # finalize_pending looks this far back (the weights' window)

_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
# Titles of shown items, in memory only (never on disk): per key the line its record
# follows. The 15-min pass re-finds a ticked line by its title when the lines moved, even
# after the insight was rewritten without it.
_TITLES: dict[tuple[str, str], dict[str, str]] = {}
# Days known to be final, per outcome folder, so finalize_pending doesn't read them again.
_FINAL: dict[str, set[str]] = {}


def _ti():
    from src import today_insight

    return today_insight


def _lock(owner: str | None) -> threading.RLock:
    """One writer of an owner's outcome files at a time; a thread lock, because the pass
    calls in synchronously and the routes are async (both only hold it for file I/O)."""
    key = _ti()._slug(owner)
    with _GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def outcome_path(owner: str | None, day: str) -> Path:
    return _ti().insight_path(owner, day).with_name(f"{day}.outcome.json")


def _now(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _iso(now: datetime | None) -> str:
    return _ti()._utc_iso(_now(now))


def blank(day: str) -> dict:
    return {"version": VERSION, "date": day, "final": False, "finalized": None,
            "pushed": None, "push_tries": 0, "votes": {"up": 0, "down": 0}, "items": {}}


def load(owner: str | None, day: str) -> dict | None:
    try:
        data = json.loads(outcome_path(owner, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("items"), dict):
        data["items"] = {}
    return data


def save(owner: str | None, data: dict) -> None:
    path = outcome_path(owner, data["date"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------------ helpers
def _kind(ref: str) -> str:
    prefix = ref.split(":", 1)[0]
    return {"todo": "todo", "event": "event", "loop": "loop", "belief": "plan"}.get(prefix, "other")


def _rows(insight: dict) -> Iterator[tuple[str, int, dict]]:
    for section in SECTIONS:
        rows = insight.get(section) if isinstance(insight, dict) else None
        for position, row in enumerate(rows if isinstance(rows, list) else [], 1):
            if isinstance(row, dict) and isinstance(row.get("ref"), str) and row["ref"]:
                yield section, position, row


def _new_item(ref: str, section: str, position: int, candidate: dict | None,
              now: datetime | None) -> dict:
    c = candidate or {}
    kind = c.get("kind") or _kind(ref)
    item = {"kind": kind, "section": section, "position": position,
            "first_shown": _iso(now), "bucket": c.get("bucket"), "date": c.get("date")}
    if kind == "todo":
        m = _TODO.match(ref)
        item["note_id"] = c.get("note_id") or (m.group(1) if m else None)
        item["note_type"] = c.get("note_type")
    elif kind == "event":
        item["event_type"] = c.get("event_type")
        item["importance"] = c.get("importance") or "normal"
    item.update({"outcome": "open", "at": None, "via": None})
    return item


def _set(item: dict, outcome: str, via: str, now: datetime | None) -> bool:
    if item.get("outcome") == outcome:
        return False
    item.update({"outcome": outcome, "at": _iso(now), "via": via})
    return True


def _line_key(s: Any) -> str:
    """A to-do line's text as candidates title it: whitespace collapsed, its first
    ``TITLE_CHARS`` characters (``todo_candidates`` cuts there), lower case."""
    return re.sub(r"\s+", " ", str(s or "")).strip()[:TITLE_CHARS].strip().lower()


def _same(a: Any, b: Any) -> bool:
    return _line_key(a) == _line_key(b)


def _retire_renamed(items: dict, titles: dict, insight: dict) -> None:
    """A to-do ref (``todo:<note>:<index>``) whose row title differs from the title its
    record was shown with names another line now (a line was added, removed or moved
    above it). Its record keeps following its own line by title under a spare key
    ``<ref>#<n>`` (never shown, so the page can't act on it), and the ref is free for a
    fresh record of the new line."""
    for _, _, row in _rows(insight):
        ref, title = row["ref"], row.get("title")
        if (ref in items and ref.startswith("todo:") and title and ref in titles
                and not _same(titles[ref], title)):
            n = 1
            while f"{ref}#{n}" in items:
                n += 1
            items[f"{ref}#{n}"] = items.pop(ref)
            titles[f"{ref}#{n}"] = titles.pop(ref)


def _adopt(items: dict, titles: dict, ref: str, title: Any, shown: set[str]) -> bool:
    """Move the record of the same line (same note, same title) kept under a key the
    insight doesn't show (an older ref or a spare key) to ``ref``, the line's ref now, so
    one line has one record. True when one was moved."""
    m = _TODO.match(ref)
    if not m or not title or "#" in ref:
        return False
    for key in list(items):
        if (key != ref and key not in shown and items[key].get("kind") == "todo"
                and str(items[key].get("note_id") or "") == m.group(1)
                and key in titles and _same(titles[key], title)):
            items[ref] = items.pop(key)
            titles.pop(key, None)
            return True
    return False


def _vote_counts(insight: dict) -> dict[str, int]:
    counts = {"up": 0, "down": 0}
    for v in insight.get("votes") or []:
        if isinstance(v, dict) and v.get("vote") in counts:
            counts[v["vote"]] += 1
    return counts


def _load_notes_by_id(owner: str | None, note_ids: Iterable[str]) -> dict[str, dict]:
    """Notes the pass didn't load (archived, e.g. a finished checklist archived by
    ``src/checklist_auto_archive.py``), as ``today_insight.notes_state`` entries."""
    ids = [i for i in note_ids if i]
    if not ids:
        return {}
    try:
        from core.database import Note, SessionLocal

        db = SessionLocal()
        try:
            q = db.query(Note).filter(Note.id.in_(ids))
            if owner:
                q = q.filter(Note.owner == owner)
            return _ti().notes_state(q.all())
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 -- then they count as gone
        logger.debug("insight outcomes: note lookup failed: %s", e)
        return {}


def _todo_outcome(ref: str, item: dict, note: dict | None, title: str | None,
                  tz: tzinfo) -> str:
    """``done``, ``open``, ``moved`` or ``gone`` from the note as it is now: the line at
    the ref's index when its text is the title, else the line with that title (as
    ``/today`` finds it); without a known title, the line at the index."""
    m = _TODO.match(ref)
    if note is None or m is None:
        return "gone"
    items = note.get("items") or []
    index = int(m.group(2))
    at = None
    if index < len(items) and (title is None or _same(items[index][0], title)):
        at = index
    elif title is not None:
        at = next((i for i, (text, _) in enumerate(items) if _same(text, title)), None)
    if at is None:
        return "gone"
    if items[at][1]:
        return "done"
    shown = item.get("date")
    due = _ti().local_date(note.get("due_date"), tz)
    if shown and due is not None and due.isoformat() > shown:
        return "moved"
    return "open"


def _event_outcome(ref: str, item: dict, occurrences: list, tz: tzinfo) -> str:
    """``moved`` when the occurrence now starts on a later day than when it was shown,
    ``gone`` when it was cancelled or isn't in the window any more, else ``open`` (a
    passed event stays open until the day is finalized; events are never ``done``)."""
    uid = ref.split(":", 1)[1]
    ev = next((o for o in occurrences if isinstance(o, dict) and str(o.get("uid") or "") == uid),
              None)
    if ev is None or (ev.get("status") or "") == "cancelled":
        return "gone"
    start = _ti()._occurrence_time(ev.get("dtstart"), tz)
    shown = item.get("date")
    if start is not None and shown and start.date().isoformat() > shown:
        return "moved"
    return "open"


# ------------------------------------------------------------------------ observe
def observe(owner: str | None, insight: dict, inputs: dict, *,
            now: datetime | None = None) -> dict:
    """Add every ref the insight shows (first section and position kept), update the
    outcomes from the inputs' notes, occurrences and action items, copy the vote counts,
    then finalize earlier days. Returns the day's outcome dict ({} without an insight)."""
    day = str((insight or {}).get("date") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return {}
    inputs = inputs or {}
    local_now = inputs.get("now")
    tz = local_now.tzinfo if isinstance(local_now, datetime) and local_now.tzinfo else timezone.utc
    pool = [c for c in [*(inputs.get("candidates") or []), *(inputs.get("extra") or [])]
            if isinstance(c, dict) and c.get("ref")]
    by_ref = {c["ref"]: c for c in pool}
    with _lock(owner):
        data = load(owner, day) or blank(day)
        items = data["items"]
        titles = _TITLES.setdefault((_ti()._slug(owner), day), {})
        _retire_renamed(items, titles, insight)
        shown = {row["ref"] for _, _, row in _rows(insight)}
        for section, position, row in _rows(insight):
            ref = row["ref"]
            if ref not in items and not _adopt(items, titles, ref, row.get("title"), shown):
                items[ref] = _new_item(ref, section, position, by_ref.get(ref), now)
            if row.get("title"):
                titles[ref] = str(row["title"])  # the line the ref names now
        for ref in items:
            if ref not in titles and by_ref.get(ref, {}).get("title"):
                titles[ref] = str(by_ref[ref]["title"])
        if not data.get("final"):
            _observe_items(owner, items, titles, inputs, tz, now)
        data["votes"] = _vote_counts(insight)
        save(owner, data)
    today = inputs.get("today") if isinstance(inputs.get("today"), Date) else Date.fromisoformat(day)
    finalize_pending(owner, today, now=now)
    return data


def _observe_items(owner: str | None, items: dict, titles: dict, inputs: dict,
                   tz: tzinfo, now: datetime | None) -> None:
    notes = dict(inputs.get("notes_state") or {})
    occurrences = list(inputs.get("occurrences") or [])
    rows = [r for r in inputs.get("action_rows") or [] if isinstance(r, dict)]
    done_rows = inputs.get("action_rows_done")  # None: not read, or the module failed
    open_rows = {str(r.get("id")): str(r.get("status") or "open") for r in rows}
    closed = {str(r.get("id")) for r in done_rows or [] if isinstance(r, dict)}
    reachable = bool(rows) or isinstance(done_rows, list)
    gathered = str(inputs.get("gathered_at") or "")
    missing = {it.get("note_id") for it in items.values()
               if it.get("kind") == "todo" and it.get("note_id") not in notes}
    if missing:
        notes.update(_load_notes_by_id(owner, missing))
    for ref, item in items.items():
        current = item.get("outcome")
        if current in ("dismissed", "ignored"):
            continue  # owner mark; ignored only after finalize
        if gathered and item.get("via") == "today" and str(item.get("at") or "") >= gathered:
            continue  # changed on the page after these inputs were read: they are older
        kind = item.get("kind")
        if kind == "todo":
            if "#" in ref and ref not in titles:
                continue  # a spare key without its title (after a restart): can't tell
            seen = _todo_outcome(ref, item, notes.get(item.get("note_id")), titles.get(ref), tz)
        elif kind == "event":
            seen = _event_outcome(ref, item, occurrences, tz)
        elif kind == "loop":
            if not reachable:
                continue  # module unreachable, or nothing read: can't tell
            loop_id = ref.split(":", 1)[1]
            status = open_rows.get(loop_id)
            if loop_id in closed:
                seen = "done"
            elif status is None:
                seen = "gone"
            else:
                seen = "open" if status == "open" else "done"
        else:
            continue  # plans: only open, ignored and the owner's marks
        if current == "promoted" and seen != "done":
            continue  # a promotion stays until it is done
        if kind == "loop" and current == "done" and item.get("via") == "today":
            continue  # closed on the page; the module may not know yet
        _set(item, seen, "observed", now)


def open_loop_ids(owner: str | None, today: Date) -> set[str]:
    """Action item ids of the open loops today's outcome log (or today's insight, for
    rows not observed yet) still has open, so the pass knows to read the done items."""
    day = today.isoformat()
    items = _items_of(load(owner, day))
    ids = {r.split(":", 1)[1] for r, it in items.items()
           if r.startswith("loop:") and it.get("outcome") in ("open", "promoted")}
    for _, _, row in _rows(_ti().load(owner, day) or {}):
        if row["ref"].startswith("loop:") and row["ref"] not in items:
            ids.add(row["ref"].split(":", 1)[1])
    return ids


# ------------------------------------------------------------------- owner actions
def record(owner: str | None, ref: str, outcome: str, *, not_task: bool = False,
           now: datetime | None = None) -> dict:
    """The owner's action on today's insight: ``done``, ``open`` (un-tick), ``dismissed``,
    ``promoted`` or ``clear`` (undo a mark). Returns the item. LookupError when there is no
    insight today or the ref isn't in it; ValueError for an unknown action, or ``done``/
    ``open`` on an event or a plan (they are never done).

    ``not_task`` with ``dismissed`` on an open loop: "Not a task", the module item was
    never a to-do (the page dismisses it in the module first). Kept on the item as
    ``not_task`` so the log shows which extractions were wrong; ``clear`` drops it."""
    if outcome not in ACTIONS:
        raise ValueError(f"outcome must be one of {', '.join(ACTIONS)}")
    if not_task and (outcome != "dismissed" or not ref.startswith("loop:")):
        raise ValueError("only an open loop can be marked not a task")
    ti = _ti()
    day = ti.today_str(owner, now)
    insight = ti.load(owner, day)
    if not insight:
        raise LookupError("no insight yet")
    shown = {row["ref"]: (section, position) for section, position, row in
             reversed(list(_rows(insight)))}
    if "#" in ref:
        raise LookupError("not in today's insight")  # a spare key is never shown
    with _lock(owner):
        data = load(owner, day) or blank(day)
        items = data["items"]
        titles = _TITLES.setdefault((ti._slug(owner), day), {})
        _retire_renamed(items, titles, insight)  # the insight may be newer than the pass
        title = next((row.get("title") for _, _, row in _rows(insight) if row["ref"] == ref),
                     None)
        if ref not in items and not _adopt(items, titles, ref, title, set(shown)):
            if ref not in shown:
                raise LookupError("not in today's insight")
            items[ref] = _new_item(ref, *shown[ref], None, now)
        if title:
            titles[ref] = str(title)
        item = items[ref]
        if outcome in ("done", "open") and item.get("kind") not in ("todo", "loop"):
            raise ValueError("only to-dos and open loops can be done")
        if outcome == "clear":
            if item.get("outcome") in OWNER_MARKS:
                _set(item, "open", "today", now)
        elif outcome == "open":
            if item.get("outcome") == "done":
                _set(item, "open", "today", now)
        elif outcome in OWNER_MARKS and item.get("outcome") == "done":
            pass  # a done item stays done; a mark on it changes nothing
        else:
            _set(item, outcome, "today", now)
        if outcome == "dismissed" and not_task and item.get("outcome") == "dismissed":
            item["not_task"] = True
        elif outcome == "clear":
            item.pop("not_task", None)
        save(owner, data)
        return dict(item)


# ---------------------------------------------------------------------- finalize
def _final_days(owner: str | None) -> set[str]:
    return _FINAL.setdefault(str(outcome_path(owner, "2000-01-01").parent), set())


def _closed(owner: str | None, day: str) -> None:
    """A final day: remembered as such, and its titles leave memory."""
    _final_days(owner).add(day)
    _TITLES.pop((_ti()._slug(owner), day), None)


def finalize(owner: str | None, day: str, *, now: datetime | None = None) -> dict:
    """Close a day: items still ``open`` become ``ignored``, and the vote counts are
    copied from the insight once more (votes cast after the last pass count).
    Idempotent; {} without a file."""
    with _lock(owner):
        data = load(owner, day)
        if data is None:
            return {}
        if not data.get("final"):
            for item in data["items"].values():
                if item.get("outcome") == "open":
                    _set(item, "ignored", "final", now)
            insight = _ti().load(owner, day)
            if insight:
                data["votes"] = _vote_counts(insight)
            data["final"], data["finalized"] = True, _iso(now)
            save(owner, data)
        _closed(owner, day)
        return data


def _files(owner: str | None) -> list[tuple[str, Path]]:
    folder = outcome_path(owner, "2000-01-01").parent
    try:
        names = sorted(p.name for p in folder.iterdir())
    except OSError:
        return []
    return [(m.group(1), folder / name) for name in names if (m := _DAY.match(name))]


def finalize_pending(owner: str | None, today: Date, *, now: datetime | None = None) -> int:
    """Finalize every day of the ``FINALIZE_DAYS`` before ``today`` that isn't final yet
    (the first pass after local midnight closes yesterday). Older days are left alone (the
    weights don't read them), and a day known final in this process isn't read again.
    Returns how many were finalized."""
    lo, hi = (today - timedelta(days=FINALIZE_DAYS)).isoformat(), today.isoformat()
    final = _final_days(owner)
    final.difference_update({d for d in final if d < lo})
    count = 0
    for day, _ in _files(owner):
        if not lo <= day < hi or day in final:
            continue
        data = load(owner, day)
        if data is None:
            continue
        if data.get("final"):
            _closed(owner, day)
        else:
            finalize(owner, day, now=now)
            count += 1
    return count


def days(owner: str | None, since: Date, until: Date) -> Iterator[dict]:
    """The outcome dicts of the days from ``since`` to ``until`` (inclusive), oldest first."""
    lo, hi = since.isoformat(), until.isoformat()
    for day, _ in _files(owner):
        if lo <= day <= hi:
            data = load(owner, day)
            if data is not None:
                yield data


# -------------------------------------------------------------------- owner order
def _items_of(outcome: dict | None) -> dict:
    items = (outcome or {}).get("items") if isinstance(outcome, dict) else None
    return items if isinstance(items, dict) else {}


def _promoted(items: dict) -> list[str]:
    """Promoted refs, the latest promotion first."""
    refs = [r for r, it in items.items() if it.get("outcome") == "promoted"]
    return sorted(refs, key=lambda r: str(items[r].get("at") or ""), reverse=True)


def owner_marks(owner: str | None, today: Date) -> dict:
    """The owner's marks on today's insight: ``dismissed`` refs and ``promoted`` refs
    (the latest promotion first)."""
    items = _items_of(load(owner, today.isoformat()))
    return {"dismissed": {r for r, it in items.items() if it.get("outcome") == "dismissed"},
            "promoted": _promoted(items)}


def apply_owner_order(insight: dict, outcome: dict | None, *,
                      rows: dict[str, dict] | None = None) -> dict:
    """A copy of the insight with the owner's order: promoted refs first in ``focus``
    (moved there from any section, the latest promotion first), dismissed refs removed.
    ``rows``: rows for promoted refs a rewrite of the insight left out (see ``ordered``)."""
    out = dict(insight or {})
    items = _items_of(outcome)
    if not items:
        return out
    dismissed = {r for r, it in items.items() if it.get("outcome") == "dismissed"}
    by_ref = {**(rows or {}), **{row["ref"]: row for _, _, row in _rows(insight)}}
    first = [by_ref[r] for r in _promoted(items) if r in by_ref]
    lifted = {r["ref"] for r in first}
    for section in SECTIONS:
        if section not in out and section != "focus":
            continue
        kept = [row for row in (out.get(section) or [])
                if not (isinstance(row, dict) and row.get("ref") in dismissed | lifted)]
        out[section] = [*first, *kept] if section == "focus" else kept
    return out


def _resolve_titles(owner: str | None, day: str, refs: Iterable[str]) -> dict[str, str]:
    """Titles of refs the insight no longer shows: from this process's memory of earlier
    insights, else (to-dos) from the note itself. Never stored."""
    cache = _TITLES.get((_ti()._slug(owner), day), {})
    out = {r: cache[r] for r in refs if r in cache}
    todo = {r: m for r in refs if r not in out and (m := re.match(r"^todo:(.+):(\d+)$", r))}
    if todo:
        notes = _load_notes_by_id(owner, {m.group(1) for m in todo.values()})
        for ref, m in todo.items():
            lines = (notes.get(m.group(1)) or {}).get("items") or []
            index = int(m.group(2))
            if index < len(lines) and lines[index][0]:
                out[ref] = lines[index][0]
    return out


def _missing_rows(owner: str | None, insight: dict, outcome: dict | None,
                  mark: str) -> dict[str, dict]:
    items = _items_of(outcome)
    shown = {row["ref"] for _, _, row in _rows(insight)}
    refs = [r for r, it in items.items()
            if it.get("outcome") == mark and r not in shown and "#" not in r]
    if not refs:
        return {}
    titles = _resolve_titles(owner, str(insight.get("date") or ""), refs)
    return {r: {"ref": r, "why": "", "title": titles.get(r)} for r in refs
            if titles.get(r) or mark == "dismissed"}


def ordered(owner: str | None, insight: dict, outcome: dict | None = None) -> dict:
    """``apply_owner_order`` with the insight's own day's outcome file (or ``outcome``);
    a promoted item the model left out of a rewritten insight still comes first."""
    if not insight or not insight.get("date"):
        return dict(insight or {})
    outcome = load(owner, str(insight["date"])) if outcome is None else outcome
    return apply_owner_order(insight, outcome,
                             rows=_missing_rows(owner, insight, outcome, "promoted"))


def dismissed_rows(insight: dict, outcome: dict | None,
                   owner: str | None = None) -> list[dict]:
    """The rows the owner marked "not today", each with its ``section``: those in the
    insight, then (with ``owner``) those a rewrite left out (``title`` may be None).
    ``not_task`` is true for an open loop marked "Not a task"."""
    items = _items_of(outcome)
    out = [{**row, "section": section} for section, _, row in _rows(insight)
           if items.get(row["ref"], {}).get("outcome") == "dismissed"]
    if owner is not None:
        for ref, row in _missing_rows(owner, insight, outcome, "dismissed").items():
            out.append({**row, "section": items[ref].get("section")})
    for row in out:
        row["not_task"] = bool(items.get(row["ref"], {}).get("not_task"))
    return out


def outcome_map(outcome: dict | None) -> dict[str, str]:
    return {r: str(it.get("outcome") or "open") for r, it in _items_of(outcome).items()}


# -------------------------------------------------------------------------- push
def pushed(owner: str | None, day: str) -> str | None:
    return (load(owner, day) or {}).get("pushed")


def mark_pushed(owner: str | None, day: str, *, now: datetime | None = None) -> None:
    with _lock(owner):
        data = load(owner, day) or blank(day)
        data["pushed"] = _iso(now)
        save(owner, data)


def push_tries(owner: str | None, day: str) -> int:
    try:
        return int((load(owner, day) or {}).get("push_tries") or 0)
    except (TypeError, ValueError):
        return 0


def note_push_try(owner: str | None, day: str) -> int:
    """Count one push attempt for the day; returns the new count."""
    with _lock(owner):
        data = load(owner, day) or blank(day)
        try:
            tries = int(data.get("push_tries") or 0) + 1
        except (TypeError, ValueError):
            tries = 1
        data["push_tries"] = tries
        save(owner, data)
        return tries
