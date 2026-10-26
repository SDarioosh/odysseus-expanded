"""Fork: rule weights for the Today insight, learned from the outcome log (Phase 3.2).

The rules (docs/fork/today-insight.md#weights):

* The candidates the model sees are put in a fixed rule order of five tiers (overdue and
  due today, due in the next 7 days, items of pinned notes, undated, due later). A weight
  only reorders items *within* a tier; it never moves an item to another tier.
* A weight belongs to a key: ``note_type:<type>`` and ``list:<note id>`` for to-do items,
  ``event_type:<type>`` and ``importance:<level>`` for events. A candidate's weight is
  the sum of its keys' weights, clamped to [-6, 6].
* A key learns from the outcome files (``<day>.outcome.json``, written by
  ``src/insight_outcomes.py``) of the finalized days in the last 28 days, only days after
  the key's reset. Each shown item of a day counts once; ``gone`` items count for nothing.
  To-do score: done + 2*promoted - 2*dismissed - moved - ignored/2. Event score:
  2*promoted - 2*dismissed - moved (a passed event says nothing).
  ``weight = clamp(round_half_away(3 * (score/shown - base)), -3, 3)`` where ``base`` is
  the same rate over all to-do items (or all events) of the window; a key shown fewer
  than 6 times keeps weight 0. Exact fractions, rounded half away from zero.
* Weights are recomputed at most once a day (the first time they are asked for on a new
  day) into ``<DATA_DIR>/today_insight/<owner>/weights.json``, so they are stable during
  the day. A reset records the day, so older outcomes no longer count for that key, and
  sets its weight to 0 at once.
* The T5 PDA gets one number per note (``t5_table``): the note's ``list:`` weight plus its
  ``note_type:`` weight.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

VERSION = 1
WINDOW_DAYS = 28
MIN_SHOWN = 6
KEY_LIMIT = 3
CANDIDATE_LIMIT = 6
T5_LIMIT = 32
COUNTS = ("shown", "done", "promoted", "dismissed", "moved", "ignored")
TODO_PREFIXES = ("note_type", "list")
EVENT_PREFIXES = ("event_type", "importance")
KEY_RE = re.compile(r"^(note_type|list|event_type|importance):[^\s]{1,200}$")

_LOCK = threading.Lock()


# ------------------------------------------------------------------------ helpers
def round_half_away(x: Fraction | float) -> int:
    """Round to the nearest integer, halves away from zero (2.5 → 3, -2.5 → -3)."""
    q = Fraction(x)
    n = int(abs(q) + Fraction(1, 2))  # floor(|x| + 1/2)
    return n if q >= 0 else -n


def _clamp(n: int, limit: int) -> int:
    return max(-limit, min(limit, int(n)))


def _owner_dir(owner: str | None) -> Path:
    from src import constants
    from src.today_insight import _slug

    return Path(constants.DATA_DIR) / "today_insight" / _slug(owner)


def weights_path(owner: str | None) -> Path:
    return _owner_dir(owner) / "weights.json"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(path)


def _outcome(owner: str | None, day: Date) -> dict | None:
    """One day's outcome file (format: docs/fork/today-insight.md#outcome-log)."""
    return _read_json(_owner_dir(owner) / f"{day.isoformat()}.outcome.json")


def _utc_iso(now: datetime | None) -> str:
    now = (now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today(owner: str | None, now: datetime | None) -> Date:
    from src import today_insight as ti

    return Date.fromisoformat(ti.today_str(owner, now))


def _parse_date(value: Any) -> Date | None:
    try:
        return Date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _value(v: Any) -> str | None:
    text = str(v).strip() if v is not None else ""
    return text if text and not re.search(r"\s", text) else None


def _todo_note_id(c: dict) -> str | None:
    nid = _value(c.get("note_id"))
    if nid:
        return nid
    m = re.match(r"^todo:(.+):\d+$", str(c.get("ref") or ""))
    return m.group(1) if m else None


def keys_for(c: dict) -> list[str]:
    """The weight keys of a candidate or an outcome item (to-dos and events only)."""
    kind = c.get("kind")
    if not kind:
        ref = str(c.get("ref") or "")
        kind = "todo" if ref.startswith("todo:") else "event" if ref.startswith("event:") else None
    out = []
    if kind == "todo":
        note_type, note_id = _value(c.get("note_type")), _todo_note_id(c)
        if note_type:
            out.append(f"note_type:{note_type}")
        if note_id:
            out.append(f"list:{note_id}")
    elif kind == "event":
        for prefix in EVENT_PREFIXES:
            v = _value(c.get(prefix))
            if v:
                out.append(f"{prefix}:{v}")
    return out


def is_event_key(key: str) -> bool:
    return key.split(":", 1)[0] in EVENT_PREFIXES


def _score(counts: dict, event: bool) -> Fraction:
    s = Fraction(2 * counts["promoted"] - 2 * counts["dismissed"] - counts["moved"])
    if not event:
        s += counts["done"] - Fraction(counts["ignored"], 2)
    return s


def _zero() -> dict:
    return {c: 0 for c in COUNTS}


def _count(counts: dict, outcome: str) -> None:
    counts["shown"] += 1
    if outcome == "open":  # a final day has no open items; treat a stray one as ignored
        outcome = "ignored"
    if outcome in counts:
        counts[outcome] += 1


# ------------------------------------------------------------------------ compute
def _finalize_pending(owner: str | None, today: Date, now: datetime | None) -> None:
    """Let the outcome log finalize earlier days first (its nightly pass), so the first
    computation of a day already counts yesterday. Optional: missing or failing is fine."""
    try:
        from src import insight_outcomes

        fn = getattr(insight_outcomes, "finalize_pending", None)
        if fn is not None:
            fn(owner, today, now=now)
    except Exception as e:  # noqa: BLE001
        logger.warning("insight weights: finalize_pending failed: %s", e)


def compute(owner: str | None, today: Date, *, now: datetime | None = None,
            resets: dict[str, str] | None = None) -> dict:
    """Weights for ``today`` from the finalized outcome days of the last 28 days (not
    today). ``resets`` (key → reset day) defaults to the stored file's."""
    if resets is None:
        resets = (_read_json(weights_path(owner)) or {}).get("resets") or {}
    first = today - timedelta(days=WINDOW_DAYS)
    # drop resets the window has passed: every window day is after them anyway
    resets = {k: str(v) for k, v in resets.items()
              if isinstance(k, str) and KEY_RE.match(k)
              and (_parse_date(v) or first) >= first}
    reset_days = {k: _parse_date(v) for k, v in resets.items()}

    keys: dict[str, dict] = {}
    base = {"todo": _zero(), "event": _zero()}
    for i in range(WINDOW_DAYS):
        day = first + timedelta(days=i)
        data = _outcome(owner, day)
        if not data or data.get("final") is not True:
            continue
        items = data.get("items")
        if not isinstance(items, dict):
            continue
        for ref, item in items.items():
            if not isinstance(item, dict):
                continue
            outcome = str(item.get("outcome") or "")
            if outcome == "gone" or not outcome:
                continue
            item = {**item, "ref": ref}
            kind = item.get("kind") or ("todo" if str(ref).startswith("todo:") else
                                        "event" if str(ref).startswith("event:") else None)
            if kind not in ("todo", "event"):
                continue
            item["kind"] = kind
            _count(base[kind], outcome)
            for key in keys_for(item):
                after = reset_days.get(key)
                if after is not None and day <= after:
                    continue
                _count(keys.setdefault(key, _zero()), outcome)

    rates = {kind: (_score(c, kind == "event") / c["shown"]) if c["shown"] else Fraction(0)
             for kind, c in base.items()}
    out_keys = {}
    for key in sorted(set(keys) | set(resets)):
        counts = keys.get(key) or _zero()
        event = is_event_key(key)
        weight = 0
        if counts["shown"] >= MIN_SHOWN:
            rate = _score(counts, event) / counts["shown"]
            weight = _clamp(round_half_away(3 * (rate - rates["event" if event else "todo"])),
                            KEY_LIMIT)
        out_keys[key] = {"weight": weight, **counts, "reset_at": resets.get(key)}
    return {"version": VERSION, "date": today.isoformat(), "computed": _utc_iso(now),
            "window_days": WINDOW_DAYS, "min_shown": MIN_SHOWN,
            "base": {k: round(float(v), 4) for k, v in rates.items()},
            "keys": out_keys, "resets": resets}


def _valid(data: dict | None) -> bool:
    return (isinstance(data, dict) and isinstance(data.get("keys"), dict)
            and isinstance(data.get("date"), str))


def current(owner: str | None, today: Date, *, now: datetime | None = None) -> dict:
    """Today's weights: the stored file when it is today's, else computed now and stored
    (so at most once a day). A missing or broken file is recomputed."""
    path = weights_path(owner)
    with _LOCK:
        data = _read_json(path)
        if _valid(data) and data["date"] == today.isoformat():
            return data
        _finalize_pending(owner, today, now)
        resets = (data or {}).get("resets") if isinstance((data or {}).get("resets"), dict) else {}
        fresh = compute(owner, today, now=now, resets=resets)
        try:
            _write_json(path, fresh)
        except OSError as e:
            logger.warning("insight weights: couldn't store %s: %s", path, e)
        return fresh


def reset(owner: str | None, key: str | None, *, now: datetime | None = None) -> dict:
    """Forget what a key learnt (``None``: every key): outcomes up to today no longer
    count for it, and its weight is 0 from now on. Returns today's weights."""
    if key is not None and not KEY_RE.match(key):
        raise ValueError(f"not a weight key: {key!r}")
    today = _today(owner, now)
    path = weights_path(owner)
    data = current(owner, today, now=now)
    with _LOCK:
        resets = dict(data.get("resets") or {})
        if key is not None:
            targets = {key}
        else:
            targets = set(data.get("keys") or {}) | set(resets)
            # also keys of days not finalized yet (they would count later)
            for i in range(WINDOW_DAYS + 1):
                day = today - timedelta(days=i)
                items = (_outcome(owner, day) or {}).get("items")
                if isinstance(items, dict):
                    for ref, item in items.items():
                        if isinstance(item, dict):
                            targets.update(keys_for({**item, "ref": ref}))
        for k in targets:
            resets[k] = today.isoformat()
        fresh = compute(owner, today, now=now, resets=resets)
        try:
            _write_json(path, fresh)
        except OSError as e:
            logger.warning("insight weights: couldn't store %s: %s", path, e)
        return fresh


# ------------------------------------------------------------------- candidates
def _key_row(weights: dict | None, key: str) -> dict | None:
    rows = (weights or {}).get("keys") if isinstance(weights, dict) else None
    row = rows.get(key) if isinstance(rows, dict) else None
    return row if isinstance(row, dict) else None


def _key_weight(weights: dict | None, key: str) -> int:
    row = _key_row(weights, key)
    try:
        return _clamp(int((row or {}).get("weight") or 0), KEY_LIMIT)
    except (TypeError, ValueError):
        return 0


def candidate_weight(candidate: dict, weights: dict | None) -> int:
    """The sum of the weights of the candidate's keys, clamped to [-6, 6]."""
    if not isinstance(candidate, dict):
        return 0
    return _clamp(sum(_key_weight(weights, k) for k in keys_for(candidate)), CANDIDATE_LIMIT)


def contributions(candidate: dict, weights: dict | None) -> list[dict[str, Any]]:
    """One row per key that applies to the candidate: its weight and counts."""
    out = []
    for key in keys_for(candidate if isinstance(candidate, dict) else {}):
        row = _key_row(weights, key) or {}
        entry = {"key": key, "weight": _key_weight(weights, key)}
        for c in COUNTS:
            try:
                entry[c] = int(row.get(c) or 0)
            except (TypeError, ValueError):
                entry[c] = 0
        entry["reset_at"] = row.get("reset_at")
        out.append(entry)
    return out


# --------------------------------------------------------------------------- T5
def t5_table(owner: str | None, today: Date, *, notes: Iterable[Any] | None = None,
             now: datetime | None = None) -> dict:
    """One weight per to-do note for the T5 PDA: ``list:<id>`` + ``note_type:<type>``,
    clamped to [-6, 6]; only non-zero, at most 32 (largest |weight| first, ties by key)."""
    weights = current(owner, today, now=now)
    if notes is None:
        try:
            from src import today_insight as ti

            notes = ti._load_notes(owner)
        except Exception as e:  # noqa: BLE001 -- serve what can be served
            logger.warning("insight weights: couldn't load notes: %s", e)
            notes = []
    table: dict[str, int] = {}
    for n in notes or ():
        nid = _value(getattr(n, "id", None))
        if not nid:
            continue
        note_type = _value(getattr(n, "note_type", None)) or "checklist"
        w = _clamp(_key_weight(weights, f"list:{nid}")
                   + _key_weight(weights, f"note_type:{note_type}"), CANDIDATE_LIMIT)
        if w:
            table[f"list:{nid}"] = w
    top = sorted(table.items(), key=lambda kv: (-abs(kv[1]), kv[0]))[:T5_LIMIT]
    return {"version": VERSION, "date": today.isoformat(), "weights": dict(top)}
