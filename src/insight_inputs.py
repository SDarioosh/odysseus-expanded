"""Fork: open loops and this week's plans as Today insight candidates (Phase 3.4b, c).

``today_insight.gather`` adds :func:`extra_candidates` after the 30 rule-ordered to-dos
and events; the prompt labels them ``L1…`` (loops) and ``P1…`` (plans).

* **Open loops**: things the owner said they would do for someone, from Athena's
  open action items (``list_action_items`` JSON, already read by the insight pass): rows
  that are the owner's (``mine`` true, or no ``mine`` and an owner word as ``owner``),
  are for someone: one of the row's ``with`` people (module MCP ≥ 0.9.0; a row without
  ``with`` is never a loop) is named in its ``text`` or ``title``, by full or first name
  as a whole word (``with`` lists everyone in the conversation, so "buy milk" said while
  talking with Ana is not a loop for Ana), and aren't filed into Odysseus yet (the Module Action Items state's
  ``filed``: those are to-dos or events already). At most ``MAX_LOOPS``, by due date
  (undated last), then newest. Ref ``loop:<action item id>``.
* **Plans**: the owner's beliefs with attribute ``plans`` or ``decision``
  (``src/evidence_intake.py``; the attribute is set by the Belief Structure task),
  status tentative or confirmed, first seen in the last ``PLAN_DAYS`` days, not about
  another person, whose sentence says a plan or decision (:data:`PLAN_WORDS`: "plans to",
  "decided to", "wants to" …; "believes X is needed" or "has been thinking of" is not
  one). At most ``MAX_PLANS``, newest first. Ref ``belief:<belief id>``. The title is a
  short to-do phrase (:func:`plan_title`): the structured value ("Visit Alice in
  Lisbon"), else the sentence without its subject and plan verb, at most
  ``MAX_PLAN_TITLE`` characters.

Each part fails soft: an error leaves that part out.

Docs: ``docs/fork/today-insight.md#plans-and-open-loops``.
"""

from __future__ import annotations

import logging
import re
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

MAX_LOOPS = 5
MAX_PLANS = 5
PLAN_DAYS = 7
PLAN_ATTRIBUTES = ("plans", "decision")
PLAN_STATUSES = ("tentative", "confirmed")
MAX_TITLE = 200
MAX_PLAN_TITLE = 80
# the sentence of a plan or decision: "<subject> plans to …", "… decided to …"
PLAN_WORDS = re.compile(
    r"\b(?:plans?|planning|intends?|intending|wants?|wanted|decided|is\s+going|are\s+going|"
    r"will|aims?)\s+(?:to\s+|that\s+)?", re.IGNORECASE)


# ---------------------------------------------------------------------------- loops
def _is_owner_word(name: Any) -> bool:
    from src import evidence_intake as ledger

    return ledger._is_owner_name(name)


def is_mine(row: dict) -> bool:
    """The owner's item: ``mine`` true, or (an older module) no ``mine`` and the item's
    ``owner`` an owner word ("Me", "I", none)."""
    if "mine" in row and row.get("mine") is not None:
        return bool(row.get("mine"))
    return _is_owner_word(row.get("owner"))


def people(row: dict) -> list[str]:
    """The names in a row's ``with`` (module ≥ 0.9.0), in its order; [] when none."""
    out = []
    given = row.get("with")
    for p in given if isinstance(given, list) else []:
        name = p.get("name") if isinstance(p, dict) else p
        name = " ".join(str(name or "").split())
        if name and not _is_owner_word(name):
            out.append(name)
    return out


_TITLE_WORD = re.compile(r"^(?:dr|mr|mrs|ms|mx|prof|sir|dame)\.?$", re.IGNORECASE)


def _name_forms(name: str) -> tuple[str, ...]:
    """The forms a ``with`` name matches by: the full name and the first name, or, after a
    title ("Dr. Patel"), the name without the title; a title alone never matches."""
    words = name.split()
    rest = words[1:] if words and _TITLE_WORD.match(words[0]) else words
    if not rest:
        return (name,)
    short = " ".join(rest) if len(rest) < len(words) else rest[0]
    return tuple(dict.fromkeys((name, short)))


def short_name(name: str) -> str:
    """How a loop names its person: the first name, or the full name after a title."""
    words = name.split()
    if words and _TITLE_WORD.match(words[0]):
        return name
    return words[0] if words else name


def named_person(row: dict) -> str | None:
    """The first ``with`` person (in its order) whose full or first name appears as a
    whole word, case ignored, in the row's ``text`` or ``title``; None when none is.
    A leading title doesn't count as a first name: "Dr. Patel" matches "Patel"."""
    said = " ".join(str(row.get(k) or "") for k in ("text", "title"))
    for name in people(row):
        for form in _name_forms(name):
            if re.search(rf"(?<!\w){re.escape(form)}(?!\w)", said, re.IGNORECASE):
                return name
    return None


def _filed(owner: str | None) -> set[str]:
    from src import module_action_items as mai

    try:
        return {str(k) for k in (mai.load_state(owner or "").get("filed") or {})}
    except Exception:  # noqa: BLE001
        return set()


def is_loop(row: Any, filed: set[str]) -> bool:
    return (isinstance(row, dict) and row.get("id") is not None
            and str(row.get("status") or "open") == "open" and is_mine(row)
            and named_person(row) is not None and str(row["id"]) not in filed
            and bool(_title(row)))


def _title(row: dict) -> str:
    text = row.get("title") or row.get("text") or ""
    return re.sub(r"\s+", " ", str(text)).strip()[:MAX_TITLE]


def loop_candidates(owner: str | None, today: Date, rows: Iterable[dict], *,
                    filed: set[str] | None = None, limit: int = MAX_LOOPS) -> list[dict]:
    from src.today_insight import bucket_for, local_date

    filed = _filed(owner) if filed is None else filed
    loops = [r for r in rows or [] if is_loop(r, filed)]
    loops.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)  # newest first
    loops.sort(key=lambda r: (not r.get("due_date"), str(r.get("due_date") or "")))
    out = []
    for r in loops[:limit]:
        day = local_date(r.get("due_date"), timezone.utc)
        out.append({"ref": f"loop:{r['id']}", "kind": "loop",
                    "bucket": bucket_for(day, today), "title": _title(r),
                    "date": day.isoformat() if day else None,
                    "time": (str(r.get("due_time") or "").strip()[:5] or None) if day else None,
                    "person": short_name(str(named_person(r))),
                    "link": str(r.get("link") or "") or None, "weight": 0})
    return out


# ---------------------------------------------------------------------------- plans
def _clip_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"


def plan_title(text: str, value: Any = None) -> str:
    """A plan belief as a short to-do phrase: its structured value ("visit Alice in
    Lisbon" -> "Visit Alice in Lisbon"), else its sentence from the plan verb on ("The
    owner plans to slow down …" -> "Slow down …"), at most ``MAX_PLAN_TITLE``
    characters (cut at a word, with "…")."""
    phrase = re.sub(r"\s+", " ", str(value or "")).strip()
    if not phrase:
        phrase = text
        m = PLAN_WORDS.search(text)
        if m and m.start() <= 40:  # the subject comes first: "The owner", "Shahyah"
            phrase = text[m.end():]
    phrase = phrase.strip().rstrip(".!")
    if not phrase:
        return _clip_words(text, MAX_PLAN_TITLE)
    return _clip_words(phrase[:1].upper() + phrase[1:], MAX_PLAN_TITLE)


def plan_candidates(owner: str | None, today: Date, *, beliefs: list[dict] | None = None,
                    limit: int = MAX_PLANS) -> list[dict]:
    if beliefs is None:
        from src import evidence_intake as ledger

        beliefs = [b for a in PLAN_ATTRIBUTES
                   for b in ledger.list_beliefs(owner, status=PLAN_STATUSES, attribute=a)]
    since = (today - timedelta(days=PLAN_DAYS)).isoformat()
    picked = []
    for b in beliefs:
        first = str(b.get("first_seen") or "")[:10]
        if (b.get("attribute") not in PLAN_ATTRIBUTES or b.get("status") not in PLAN_STATUSES
                or b.get("about") == "person" or not first or first < since):
            continue
        text = re.sub(r"\s+", " ", str(b.get("shown_text") or b.get("owner_text")
                                       or b.get("text") or "")).strip()
        if text and PLAN_WORDS.search(text):
            picked.append((first, int(b.get("id") or 0), b, text))
    picked.sort(key=lambda p: (p[0], p[1]), reverse=True)
    return [{"ref": f"belief:{b['id']}", "kind": "plan", "bucket": "undated",
             "title": plan_title(text, None if b.get("owner_text") else b.get("value")),
             "text": text[:MAX_TITLE], "date": None, "time": None,
             "attribute": b["attribute"], "weight": 0}
            for _, _, b, text in picked[:limit]]


# ---------------------------------------------------------------------------- entry
def extra_candidates(owner: str | None, today: Date, action_rows: list[dict], *,
                     now: datetime | None = None) -> list[dict]:
    """Open loops, then plans (≤ ``MAX_LOOPS`` + ``MAX_PLANS``); each part fails soft."""
    out: list[dict] = []
    try:
        out += loop_candidates(owner, today, action_rows or [])
    except Exception as e:  # noqa: BLE001
        logger.warning("today insight: open loops unavailable: %s", e)
    try:
        out += plan_candidates(owner, today)
    except Exception as e:  # noqa: BLE001
        logger.warning("today insight: plans unavailable: %s", e)
    return out
