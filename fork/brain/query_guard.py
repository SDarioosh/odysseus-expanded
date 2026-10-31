"""Fork: the privacy guard for the brain's web-search queries.

Web queries leave the owner's network (SearXNG forwards them to search engines; the
DuckDuckGo fallback is a direct request), and the model may have just read private
data. :func:`guard_query` runs on every ``web_search`` query before it is sent and
removes:

- email addresses, phone numbers (``+49 30 1234567``, ``0171-1234567``,
  ``(555) 123-4567``, ``555-123-4567``; dates and plain numbers are kept) and street
  addresses (``12 Elm Street``, ``Hauptstraße 5``, ``Am Lindenweg 3``);
- the owner's contacts' names. A full display name (two or more words) goes wherever it
  appears, in any case. A single name token (``Wren`` from "Wren Hale") goes only when it
  is written capitalized in the query, is at least three letters, and is not a common
  word, month, given name that is also a word, or well-known place (:data:`KEEP_TOKENS`):
  "weather in Paris" stays even with a contact "Paris Hilton-Smith", whose full name and
  "Hilton-Smith" are still removed. A token with a digit, dot or underscore (a name made
  from an email address, ``jdoe42``) goes in any case. This trades a little recall for
  queries that still work: "gift for Jordan" keeps "Jordan" (a country), "Jordan Vale"
  is removed;
- the titles of the owner's events in the next 30 days that have two or more words
  ("Team sync"), as phrases in any case. One-word titles ("Dentist") are kept: they are
  ordinary words.

Contacts are read like the ``resolve_contact``/``manage_contact`` tools do
(``routes.contacts_routes._fetch_contacts(False, owner)``: CardDAV when configured,
else the local book filtered to the owner); events like ``manage_calendar`` does (the
calendars of ``owner``; no owner filter when ``owner`` is None, single-user mode).
Both are cached per owner for :data:`CACHE_SECONDS`.

It never raises: if the owner's data can't be read, only the regex patterns are
removed, and a warning is logged. Logs carry counts only, never the query or a removed
term.

Docs: ``docs/fork/brain.md#web-search`` ("Query privacy").
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
EVENT_DAYS = 30
MIN_TOKEN_LEN = 3

# ------------------------------------------------------------------------ patterns
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# phone shapes; each match is checked by _is_phone (digit count, not a date)
_PHONE_RES = (
    # international: +49 30 1234567, +1 (555) 123-4567, +44 20 7946 0958
    (re.compile(r"(?<![\w+])\+\d{1,3}(?:[\s.\-/]?\(?\d{1,5}\)?){1,6}(?!\w)"), 7),
    # 0049 30 1234567
    (re.compile(r"(?<![\w+])00\d{1,3}(?:[\s.\-/]?\d{1,5}){1,6}(?!\w)"), 9),
    # US: (555) 123-4567, 555-123-4567, 555.123.4567, 1-555-123-4567
    (re.compile(r"(?<![\w+])(?:1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\w)"), 10),
    # EU national: 0171-1234567, 030 1234567, 06 12 34 56 78, 030/1234567
    (re.compile(r"(?<![\w+.])0\d{1,4}(?:[\s.\-/]?\d{2,8}){1,4}(?!\w)"), 8),
)
_DATE_RE = re.compile(r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}")

_STREET_WORDS = (r"Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|"
                 r"Place|Pl|Way|Terrace|Close|Crescent|Square|Sq|Parkway|Pkwy|Highway|Hwy")
# "12 Elm Street", "221B Baker St." (capitalized name words, so "5 best way" stays)
_STREET_EN_RE = re.compile(
    r"\b\d{1,5}[A-Za-z]?\s+(?:[A-Z][\w'.-]*\s+){1,3}(?:" + _STREET_WORDS + r")\b\.?")
_DE_SUFFIX = r"(?:straße|strasse|str\.|weg|gasse|allee|platz)"
# "Hauptstraße 5", "Goethestr. 12a", "Lindenweg 4"
_STREET_DE_RE = re.compile(
    r"\b(?:(?:Am|An der|Im|In der|Auf dem|Zum|Zur)\s+)?[A-ZÄÖÜ][\wäöüß-]*" + _DE_SUFFIX
    + r"\s*\d{1,4}\s?[a-z]?\b")
# "Alte Bahnhof Straße 3", "Am Weg 5"
_STREET_DE_SPLIT_RE = re.compile(
    r"\b(?:[A-ZÄÖÜ][\wäöüß-]*\s+){1,2}(?:Straße|Strasse|Str\.|Weg|Gasse|Allee|Platz)"
    r"\s*\d{1,4}\s?[a-z]?\b")

_POSSESSIVE = r"(?:['’]s)?"

# Single name tokens that are kept: common words, months and days, given names that
# are also words, and well-known places. Lowercase.
KEEP_TOKENS = frozenset("""
the and for with from about what when where which who how why near best top new old
january february march april may june july august september october november december
monday tuesday wednesday thursday friday saturday sunday today tomorrow tonight
spring summer autumn fall winter dawn eve night day week month year
rose lily daisy iris ivy hazel holly violet jasmine poppy heather fern willow olive
ruby amber pearl crystal jade opal coral ginger pepper honey sky river brook stone
rain storm sunny star angel
grace hope faith joy glory charity patience mercy harmony melody liberty destiny
will bill mark pat sue rob jack frank grant hunter mason cook baker miller smith
taylor carter porter cooper turner walker fisher fletcher potter tanner archer
young king queen prince duke earl brown white black green gray grey gold silver
long rich price page bell love hall wood lane ray dean chase chance major sterling
penny max art gene guy don carol jean sandy rocky dusty pierce drew rich ash bob
august chuck chip buck rusty dash bear wolf fox hart moon sun
winter sommer koch becker wolf fuchs vogel fischer müller schneider
apple amazon google tesla ford dell jaguar mercedes porsche lincoln
paris london florence jordan sydney victoria georgia austin dallas houston phoenix
denver chelsea madison charlotte savannah orlando washington jackson berlin rome
vienna geneva sofia lima santiago adelaide alexandria brooklyn hamilton kent devon
florida carolina india china israel kenya chad troy regina dakota virginia ireland
cairo dublin milan venice naples lourdes munich hamburg cologne frankfurt dresden
leipzig zurich basel bern lyon nice marseille barcelona madrid valencia sevilla
lisbon porto york boston chicago seattle portland miami atlanta nashville memphis
cleveland detroit tokyo kyoto osaka beijing shanghai delhi mumbai moscow athens
oslo stockholm helsinki copenhagen amsterdam brussels prague warsaw budapest
hollywood vegas texas nevada arizona montana kansas indiana jersey
eden zion salem sharon carmel lorraine normandy brittany provence tuscany
""".split())

# words that alone don't make a query
_STOPWORDS = frozenset("""
a an the and or of for to in on at by with from about is are was were be what who
when where which how why do does did my me i you your his her their our it its this
that these those s
""".split())


# --------------------------------------------------------------------- owner data
_cache: dict[str, tuple[float, list[str], list[str]]] = {}
_cache_lock = threading.Lock()


def _load_contact_names(owner: str | None) -> list[str]:
    """Display names of the owner's contacts, read as the contact tools read them."""
    from routes import contacts_routes as cc

    rows = cc._fetch_contacts(False, owner) or []
    names = []
    for row in rows:
        name = str((row or {}).get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _load_event_titles(owner: str | None, now: datetime | None = None) -> list[str]:
    """Titles of the owner's events in the next ``EVENT_DAYS`` days (and recurring
    series, which may fall in them), read the way ``manage_calendar`` scopes them."""
    from core.database import CalendarCal, CalendarEvent, SessionLocal

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    # naive bounds a day wider than the window, for naive-local and UTC-stored rows
    start = now - timedelta(days=1)
    end = now + timedelta(days=EVENT_DAYS + 1)
    db = SessionLocal()
    try:
        q = db.query(CalendarEvent).join(CalendarCal, CalendarEvent.calendar_id == CalendarCal.id)
        if owner is not None:
            q = q.filter(CalendarCal.owner == owner)
        q = q.filter(CalendarEvent.status != "cancelled")
        titles = []
        for ev in q.all():
            recurring = bool(str(ev.rrule or "").strip())
            if ev.dtstart is None:
                continue
            in_window = start <= ev.dtstart < end or (
                ev.dtend is not None and ev.dtstart < start <= ev.dtend)
            if not (in_window or (recurring and ev.dtstart < end)):
                continue
            title = str(ev.summary or "").strip()
            if len(re.findall(r"\w+", title)) >= 2:
                titles.append(title)
        return titles
    finally:
        db.close()


def _private_terms(owner: str | None) -> tuple[list[str], list[str]]:
    """(contact names, event titles) for ``owner``, cached ``CACHE_SECONDS``."""
    key = str(owner or "")
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1], hit[2]
    names = _load_contact_names(owner)
    titles = _load_event_titles(owner)
    with _cache_lock:
        _cache[key] = (now, names, titles)
    return names, titles


def clear_cache() -> None:
    """Forget the cached names and titles (tests, or after the owner edits them)."""
    with _cache_lock:
        _cache.clear()


# ------------------------------------------------------------------------ removal
def _phone_check(min_digits: int):
    def check(text: str) -> bool:
        digits = re.sub(r"\D", "", text)
        if not min_digits <= len(digits) <= 15:
            return False
        return not _DATE_RE.fullmatch(text.strip())
    return check


def _sub_count(pattern: re.Pattern, text: str, keep=None) -> tuple[str, int]:
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        if keep is not None and not keep(m.group(0)):
            return m.group(0)
        count += 1
        return " "

    return pattern.sub(repl, text), count


def _remove_patterns(text: str) -> tuple[str, int]:
    total = 0
    text, n = _sub_count(_EMAIL_RE, text)
    total += n
    for street in (_STREET_EN_RE, _STREET_DE_RE, _STREET_DE_SPLIT_RE):
        text, n = _sub_count(street, text)
        total += n
    for phone, min_digits in _PHONE_RES:
        text, n = _sub_count(phone, text, keep=_phone_check(min_digits))
        total += n
    return text, total


def _phrase_re(phrase: str) -> re.Pattern:
    words = re.split(r"[\s\-]+", phrase.strip())
    body = r"[\s\-]+".join(re.escape(w) for w in words if w)
    return re.compile(r"(?<![\w])" + body + _POSSESSIVE + r"(?![\w])", re.IGNORECASE)


def _name_tokens(names: list[str]) -> set[str]:
    """Single tokens of the contacts' names, lowercase: each word, and for a
    hyphenated word also its parts ("Hilton-Smith", "hilton", "smith")."""
    tokens: set[str] = set()
    for name in names:
        for word in name.split():
            word = word.strip(".,;:()\"'’")
            if not word:
                continue
            tokens.add(word.lower())
            if "-" in word:
                tokens.update(p.lower() for p in word.split("-") if p)
    return tokens


def _odd_token(token: str) -> bool:
    """A token no ordinary word looks like (from an email address: ``jdoe42``)."""
    return bool(re.search(r"[\d_.]", token)) and bool(re.search(r"[^\W\d_]", token))


def _remove_owner_terms(text: str, names: list[str], titles: list[str]) -> tuple[str, int]:
    total = 0
    full = [n for n in names if len(n.split()) >= 2]
    singles = [n for n in names if len(n.split()) == 1]
    # longest first, so "Team sync review" goes before "Team sync"
    for phrase in sorted(set(titles) | set(full), key=len, reverse=True):
        text, n = _sub_count(_phrase_re(phrase), text)
        total += n

    tokens = _name_tokens(full + singles)
    if not tokens:
        return text, total
    token_re = re.compile(r"(?<![\w@.])[^\W\d_][\w.'’-]*[\w]" + r"|(?<![\w@.])[\w.]*\d[\w.]*")

    def repl(m: re.Match) -> str:
        nonlocal total
        word = m.group(0)
        bare = re.sub(r"['’]s$", "", word)
        low = bare.lower()
        if low not in tokens:
            return word
        if _odd_token(bare):
            total += 1
            return " "
        if len(bare) < MIN_TOKEN_LEN or low in KEEP_TOKENS or not bare[0].isupper():
            return word
        total += 1
        return " "

    return token_re.sub(repl, text), total


def _tidy(text: str) -> str:
    text = re.sub(r"\(\s*[,;:]?\s*\)|\[\s*\]|\"\s*\"", " ", text)
    text = re.sub(r"\s+([,;:.!?])", r"\1", text)
    text = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[\s,;:.\-–—&/]+|[\s,;:\-–—&/]+$", "", text)
    words = [w.lower() for w in re.findall(r"[^\W_]+", text)]
    if not words or all(w in _STOPWORDS for w in words):
        return ""
    return text


def _regex_only(query: str) -> tuple[str, int]:
    try:
        text, n = _remove_patterns(query)
        return _tidy(text), n
    except Exception:  # fail closed: send nothing rather than the raw query
        logger.warning("query guard: regex cleaning failed; query dropped")
        return "", 0


def guard_query(query: str, owner: str | None) -> tuple[str, int]:
    """Return (cleaned_query, number_of_removed_terms)."""
    query = query if isinstance(query, str) else str(query or "")
    try:
        text, removed = _remove_patterns(query)
    except Exception:
        return _regex_only(query)
    try:
        names, titles = _private_terms(owner)
        text, n = _remove_owner_terms(text, names, titles)
        removed += n
        cleaned = _tidy(text)
    except Exception as exc:
        logger.warning("query guard: owner data unavailable (%s); regex cleaning only",
                       type(exc).__name__)
        return _regex_only(query)
    if removed:
        logger.info("query guard: removed %d term(s)", removed)
    return cleaned, removed
