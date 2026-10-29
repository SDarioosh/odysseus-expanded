"""Fork: the belief ledger's vocabulary and evidence (``fork/beliefs/evidence_intake.py``).

The ledger's constants, errors and settings, the :class:`Evidence` a source submits, the
matching key (:func:`normalize`), the memory category (:func:`category_for`), the stored
shape of one evidence item (``_clean``) and the subject-name helpers. No database here.
The source value of Athena's evidence, ``MODULE_SOURCE`` (``athena``; a ledger written
before the rename holds ``MODULE_SOURCE_OLD``, ``odysseus-sound``), is defined here only:
``MODULE_SOURCES`` and :func:`is_module_source` take either (``fork/common/athena_names.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from fork.common import athena_names

MODULE_SOURCE = athena_names.SOURCE
MODULE_SOURCE_OLD = athena_names.OLD_SOURCE
MODULE_SOURCES = (MODULE_SOURCE, MODULE_SOURCE_OLD)


def is_module_source(source: Any) -> bool:
    """Whether evidence of this source is Athena's (under the new or the old name)."""
    return isinstance(source, str) and source in MODULE_SOURCES


ABOUT = ("owner", "person", "other")
CATEGORIES = ("fact", "preference", "contact")
STATUSES = ("tentative", "confirmed", "stale", "rejected")
CONFIRMED_BY = ("source", "owner", "rule")
EVENT_KINDS = ("added", "evidence", "confirmed", "stale", "rejected", "corrected",
               "restored", "retired_upstream", "unconfirmed", "merged", "erased", "moved")
# An erased belief keeps only a hash of its matching keys, so the same claim coming back
# from any source still finds it (and stays out of memory) without its words being kept.
ERASED_PREFIX = "erased:"
# Per-evidence erase state (``payload.erase``): the module erased it (``done``), only
# retired it (an older module: ``retired``), not reached yet (``pending``); evidence of
# another source keeps its link so the page can say where the words still are (``source``).
ERASE_STATES = ("done", "retired", "pending", "source")
SUBJECT_KINDS = ("person", "project", "thing", "owner")
# The controlled vocabulary of a belief's ``attribute`` (stage 2.2); anything else: other.
ATTRIBUTES = ("lives_in", "works_at", "role", "birthday", "allergy", "diet", "likes",
              "dislikes", "relationship", "owns", "pet", "plans", "decision",
              "routine", "contact_info", "health", "project_status", "deadline", "other")
PROJECT_ATTRIBUTES = ("project_status", "deadline")
PROJECT_WORDS = re.compile(r"\b(project|launch|release|migration|plan)\b", re.IGNORECASE)
GENERIC_SUBJECT = "General"  # the subject of evidence that names none
OWNER_DISPLAY = "Owner"
_OWNER_NAMES = frozenset({"", "me", "i", "myself", "owner", "the owner", "user", "the user"})
MAX_VALUE_CHARS = 120
EVENTS_KEPT = 50  # per belief: the history stays small
PREFERENCE_WORDS = frozenset({
    "like", "likes", "prefer", "prefers", "love", "loves", "hate", "hates", "enjoy",
    "enjoys", "favorite", "favourite", "want", "wants", "dislike",
})
# Memory fields only a belief with Athena evidence carries (the Phase 0 shape).
MODULE_FIELDS = ("module_fact_id", "times_said", "days", "first_said", "last_said")
# Memory fields a mirrored entry carries only sometimes (removed when they no longer apply).
OPTIONAL_FIELDS = (*MODULE_FIELDS, "contact_id")
_RESERVED_PAYLOAD = ("gone", "gone_at")


class BeliefNotFound(LookupError):
    """No belief with that id for that owner."""


class SubjectNotFound(LookupError):
    """No subject with that id for that owner."""


class BeliefErased(ValueError):
    """The owner erased this belief: it can't be confirmed, corrected or restored."""


# ----------------------------------------------------------------------- settings
@dataclass(frozen=True)
class Settings:
    confirm_sources: int = 2
    confirm_times: int = 3
    confirm_days: int = 2
    stale_days: int = 180


def settings() -> Settings:
    """The confirmation and decay thresholds from the environment."""
    from fork.common import settings as env

    return Settings(
        confirm_sources=env.belief_confirm_sources(),
        confirm_times=env.belief_confirm_times(),
        confirm_days=env.belief_confirm_days(),
        stale_days=env.belief_stale_days(),
    )


_read_settings = settings  # confirmation_for's ``settings`` argument shadows the name


# ------------------------------------------------------------------------ evidence
@dataclass
class Evidence:
    """One thing a source observed. ``source_id`` is the source's own stable id for it
    (one source id maps to one belief per owner); ``link`` points to where it came from
    (required: evidence without a link is skipped); ``seen_first``/``seen_last`` are ISO
    dates or datetimes; ``times``/``days`` are the source's own totals (distinct
    sightings, distinct days); ``source_confirmed`` is the source's own verdict.
    ``valid_until`` (ISO date, optional) ends the belief on that day. ``person_id``
    (optional) is the source's stable id of the person the evidence is about (e.g.
    Athena's ``list_people`` id): it files the belief under that person's subject
    whatever name the source uses."""

    source: str
    source_id: str
    subject: str
    about: str
    text: str
    link: str
    seen_first: str
    seen_last: str
    times: int = 1
    days: int = 1
    source_confirmed: bool = False
    category: str | None = None
    payload: dict | None = None
    valid_until: str | None = None
    person_id: str | None = None


_OWNER_LEADS = ("the owner", "owner", "i am", "i'm", "i've", "me", "my", "i")
_OWNER_VERBS = {"am": "is", "have": "has"}


def normalize(text: str, subject: str | None = None, about: str | None = None) -> str:
    """Matching key: lowercase, whitespace collapsed, trailing punctuation dropped.

    For a fact about the owner (``about="owner"``) a leading mention of the owner (``the
    owner``, ``owner``, ``me``, ``i``, ``i'm``, ``i am``, ``my`` or the evidence's own
    ``subject``, e.g. the owner's name) becomes the token ``owner``, and ``am``/``have``
    right after it become ``is``/``has``: "I am vegetarian", "Me is vegetarian" and "The
    owner is vegetarian." share one key. A possessive (``my``, ``the owner's``,
    ``<name>'s``) becomes ``owner's``. Facts about people and things keep the plain key."""
    norm = " ".join(str(text or "").lower().split()).rstrip(" .,;:!?…")
    if str(about or "").strip().lower() != "owner":
        return norm
    return _owner_key(norm.replace("\u2019", "'"), subject)


def _owner_key(norm: str, subject: str | None) -> str:
    name = " ".join(str(subject or "").lower().split()).replace("\u2019", "'")
    leads = sorted({*_OWNER_LEADS, *([name] if name else [])}, key=len, reverse=True)
    for lead in leads:
        if not norm.startswith(lead):
            continue
        rest = norm[len(lead):]
        if lead == "my":
            if rest.startswith(" "):
                return "owner's" + rest
            continue
        if rest.startswith("'s ") or rest == "'s":
            return "owner" + rest
        if rest and not rest.startswith(" "):
            continue  # "meals …", "ice …": not a subject mention
        if lead == "i'm":
            rest = " is" + rest
        elif lead == "i've":
            rest = " has" + rest
        elif lead == "i am":
            rest = " is" + rest
        words = rest.split()
        if words and words[0] in _OWNER_VERBS:
            words[0] = _OWNER_VERBS[words[0]]
        return " ".join(["owner", *words])
    return norm


# Kinds a source can give a fact (Athena MCP 0.11.0 ``kind``); they decide the category
# instead of preference words (:func:`category_for`).
FACT_KINDS = ("preference", "plan", "decision", "relationship", "personal", "other")


def category_for(about: str, text: str, kind: str | None = None) -> str:
    """The memory category (fact|preference|contact). A person -> contact. Else a source's
    fact ``kind`` (:data:`FACT_KINDS`) decides: preference, or fact; without one, about the
    owner -> preference when the text has a preference word, else fact."""
    about = str(about or "").strip().lower()
    kind = str(kind or "").strip().lower()
    if kind in FACT_KINDS and about != "person":
        return "preference" if kind == "preference" else "fact"
    if about == "owner":
        words = set(re.findall(r"[a-z]+", str(text or "").lower()))
        return "preference" if words & PREFERENCE_WORDS else "fact"
    if about == "person":
        return "contact"
    return "fact"


def _day(value: Any) -> str | None:
    """An ISO date (YYYY-MM-DD) from a date, datetime, epoch or ISO string."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo else value).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        return None


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _stamp(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean(item: Evidence, today: str) -> dict | None:
    """The stored shape of one evidence item, or None when it can't be filed."""
    source = str(item.source or "").strip()
    source_id = str(item.source_id if item.source_id is not None else "").strip()
    text = " ".join(str(item.text or "").split())
    link = str(item.link or "").strip()
    if not (source and source_id and text and link):
        return None
    about = str(item.about or "").strip().lower()
    about = about if about in ABOUT else "other"
    cat = str(item.category or "").strip().lower()
    cat = cat if cat in CATEGORIES else category_for(about, text)
    first, last = _day(item.seen_first), _day(item.seen_last)
    first = first or last or today
    last = last or first
    first, last = min(first, last), max(first, last)
    payload = {k: v for k, v in (item.payload or {}).items() if k not in _RESERVED_PAYLOAD}
    valid_until = _day(item.valid_until)
    if valid_until:
        payload["valid_until"] = valid_until
    person_id = str(item.person_id if item.person_id is not None else "").strip()
    return {
        "person_id": person_id or None,
        "source": source, "source_id": source_id, "subject": str(item.subject or "").strip(),
        "about": about, "text": text, "norm": normalize(text, item.subject, about),
        "category": cat,
        "link": link, "seen_first": first, "seen_last": last,
        "times": max(1, _int(item.times, 1)), "days": max(1, _int(item.days, 1)),
        "source_confirmed": 1 if item.source_confirmed else 0,
        "payload": json.dumps(payload, sort_keys=True, ensure_ascii=False),
    }


# ----------------------------------------------------------------------- subjects
def subject_key(name: Any) -> str:
    """A subject's matching key: lowercase, whitespace collapsed, surrounding quotes and
    trailing punctuation dropped. Names and aliases match on it."""
    key = " ".join(str(name or "").casefold().replace("\u2019", "'").split())
    return key.strip(" .,;:!?…\"'")


def _clean_name(name: Any) -> str:
    return " ".join(str(name or "").split())[:120]


def _aliases(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(a) for a in value if str(a).strip()] if isinstance(value, list) else []


def _is_owner_name(name: Any) -> bool:
    return subject_key(name) in _OWNER_NAMES


def _payload(raw: Any) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
