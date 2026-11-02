"""Fork: gathering the news brief's stories, in code (``fork/news/brief.py``).

Per enabled category, in parallel with a time budget (``CATEGORY_BUDGET`` seconds):

1. **Candidates** from each source in :data:`SOURCES`. A source is
   ``source(category, now) -> list of {title, url, source, published, snippet}``; phase 1
   has one, :func:`searxng_source`: the self-hosted SearXNG only
   (``services/search/providers.searxng_search_api``, news category, last day), never
   ``comprehensive_web_search`` (its fallback chain can reach other search APIs). The
   query is the category's query field, else its name, and nothing else. Phase 2 adds an
   RSS source to the tuple.
2. **Clean-up**: results without a title or an http(s) URL, and results older than
   ``LOOKBACK_HOURS`` (when the date is known), are dropped; URLs are made canonical
   (tracking parameters, fragments and AMP paths removed) and deduplicated; near-duplicate
   titles (token overlap) become one story with several sources (``also``).
3. Across categories (:func:`gather`), a story stays in the first category that has it;
   at most ``MAX_PER_CATEGORY`` stories each, newest first.
4. **Read**: the first ``READ_PER_CATEGORY`` stories' pages through
   ``services/search/content.fetch_webpage_content`` (the hub's SSRF-guarded fetch), the
   text cut to ``TEXT_CHARS``; a page that fails keeps its search snippet.

Page text is untrusted: it only goes into the model's prompt as data (``prompt.py``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

SEARCH_COUNT = 15
LOOKBACK_HOURS = 24
MAX_PER_CATEGORY = 12
READ_PER_CATEGORY = 6
TEXT_CHARS = 1500
SNIPPET_CHARS = 400
CATEGORY_BUDGET = 60.0
READ_TIMEOUT = 8

Candidate = dict[str, Any]
Source = Callable[[dict, datetime], list[Candidate]]

# ------------------------------------------------------------------ canonical URLs
TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|yclid|"
                      r"_hsenc|_hsmi|cmpid|ocid|smid|ito|at_medium|at_campaign|"
                      r"outputtype|amp)$", re.IGNORECASE)


def canonical_url(url: Any) -> str | None:
    """The URL without tracking parameters, fragment and AMP path; None when it is not
    http(s) or has no host."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.netloc.lower()
    path = parts.path or "/"
    path = re.sub(r"/amp(?:\.html)?/?$", "/", path, flags=re.IGNORECASE)
    path = re.sub(r"^/amp/", "/", path, flags=re.IGNORECASE)
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not TRACKING.match(k)])
    return urlunsplit((scheme, host, path, query, ""))


def url_key(url: str) -> str:
    """Dedupe key: canonical URL without scheme and ``www.``."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    return f"{host}{parts.path}?{parts.query}"


def source_name(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for prefix in ("www.", "amp.", "m."):
        host = host.removeprefix(prefix)
    return host


# ------------------------------------------------------------------------ dates
def parse_published(value: Any) -> datetime | None:
    """A result's date as aware UTC, or None when missing or unreadable."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError):
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


# ------------------------------------------------------------------ titles
SUFFIX = re.compile(r"\s+[-–—|]\s+[^-–—|]{2,40}$")
WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset("""a an and are as at be but by for from has have he her his in into is it
its new news of on or our over says say said she than that the their they this to up was
were what when who will with after before about amid more most""".split())
SIMILAR = 0.6
MIN_SHARED = 3


def clean_title(title: Any) -> str:
    """Whitespace collapsed and a trailing " - Site Name" removed."""
    text = " ".join(str(title or "").split())
    stripped = SUFFIX.sub("", text)
    return stripped if len(stripped) >= 15 else text


def title_tokens(title: str) -> frozenset[str]:
    return frozenset(w for w in WORD.findall(title.lower()) if len(w) > 2 and w not in STOP)


def similar_titles(a: frozenset[str], b: frozenset[str]) -> bool:
    """Near-duplicates: at least ``MIN_SHARED`` shared tokens and an overlap of at least
    ``SIMILAR`` of the shorter title."""
    if not a or not b:
        return False
    shared = len(a & b)
    return shared >= min(MIN_SHARED, len(a), len(b)) and shared / min(len(a), len(b)) >= SIMILAR


# ------------------------------------------------------------------ clean-up
def clean(results: list[dict], now: datetime) -> list[Candidate]:
    """Usable candidates in result order: title and http(s) URL present, canonical URL,
    not older than the lookback, deduplicated by URL."""
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    out, seen = [], set()
    for r in results or []:
        if not isinstance(r, dict):
            continue
        title = clean_title(r.get("title"))
        url = canonical_url(r.get("url"))
        if not title or not url:
            continue
        published = parse_published(r.get("published") or r.get("published_date")
                                    or r.get("publishedDate"))
        if published and published < cutoff:
            continue
        key = url_key(url)
        if key in seen:
            continue
        seen.add(key)
        snippet = " ".join(str(r.get("snippet") or r.get("content") or "").split())
        out.append({"title": title, "url": url,
                    "source": str(r.get("source") or "").strip() or source_name(url),
                    "published": iso(published), "snippet": snippet[:SNIPPET_CHARS]})
    return out


def group(candidates: list[Candidate]) -> list[dict]:
    """Near-duplicate titles become one story: the first candidate is the story, later
    ones from other sites are its ``also``. The newest date of the group counts."""
    stories: list[dict] = []
    for c in candidates:
        tokens = title_tokens(c["title"])
        for s in stories:
            if similar_titles(tokens, s["_tokens"]):
                sources = {s["source"], *(a["source"] for a in s["also"])}
                if c["source"] not in sources:
                    s["also"].append({"url": c["url"], "source": c["source"]})
                s["_keys"].add(url_key(c["url"]))
                if c["published"] and (not s["published"] or c["published"] > s["published"]):
                    s["published"] = c["published"]
                if not s["snippet"] and c["snippet"]:
                    s["snippet"] = c["snippet"]
                break
        else:
            stories.append({**c, "also": [], "_tokens": tokens, "_keys": {url_key(c["url"])}})
    return stories


def newest_first(stories: list[dict]) -> list[dict]:
    """By date, newest first; undated stories after the dated ones, in result order."""
    dated = sorted((s for s in stories if s.get("published")),
                   key=lambda s: s["published"], reverse=True)
    return dated + [s for s in stories if not s.get("published")]


def same_story(a: dict, b: dict) -> bool:
    return bool(a["_keys"] & b["_keys"]) or similar_titles(a["_tokens"], b["_tokens"])


def public_story(story: dict) -> dict:
    return {k: v for k, v in story.items() if not k.startswith("_")}


# ------------------------------------------------------------------ sources
def searxng_search(query: str) -> list[dict]:
    """SearXNG's news category for the last day (the hub's own SearXNG, nothing else)."""
    from services.search.providers import searxng_search_api

    return searxng_search_api(query, count=SEARCH_COUNT, categories="news", time_filter="day")


def category_query(category: dict) -> str:
    """Only what the owner typed: the query field, else the category name."""
    return (category.get("query") or "").strip() or (category.get("name") or "").strip()


def searxng_source(category: dict, now: datetime) -> list[Candidate]:
    query = category_query(category)
    return searxng_search(query) if query else []


# Phase 2 appends an RSS source here (docs/fork/news-brief.md).
SOURCES: tuple[Source, ...] = (searxng_source,)


async def _from_sources(category: dict, now: datetime,
                        sources: tuple[Source, ...]) -> tuple[list[Candidate], list[str]]:
    raw, errors = [], []
    for source in sources:
        try:
            raw.extend(await asyncio.to_thread(source, category, now) or [])
        except Exception as e:  # noqa: BLE001 -- one source failing leaves the others
            logger.warning("news brief: %s failed for %r: %s",
                           getattr(source, "__name__", "source"), category.get("name"), e)
            errors.append(str(e) or type(e).__name__)
    return raw, errors


async def collect(category: dict, now: datetime,
                  sources: tuple[Source, ...] | None = None,
                  budget: float = CATEGORY_BUDGET) -> tuple[list[dict], str | None]:
    """One category's grouped stories (not yet capped) and its error, if any."""
    try:
        raw, errors = await asyncio.wait_for(
            _from_sources(category, now, SOURCES if sources is None else sources), budget)
    except asyncio.TimeoutError:
        return [], "timed out"
    stories = group(clean(raw, now))
    if not stories:
        return [], "; ".join(errors) or "no results"
    return stories, None


# ------------------------------------------------------------------ reading
def fetch_text(url: str) -> str:
    """The article's text (empty when it couldn't be read), through the hub's guarded
    fetch."""
    from services.search.content import fetch_webpage_content

    result = fetch_webpage_content(url, timeout=READ_TIMEOUT) or {}
    if not result.get("success", bool(result.get("content"))):
        return ""
    return str(result.get("content") or "")


async def read_texts(stories: list[dict], count: int = READ_PER_CATEGORY,
                     fetch: Callable[[str], str] | None = None) -> None:
    """Set ``text`` on the first ``count`` stories (their snippet when the page can't be
    read), cut to ``TEXT_CHARS``."""
    fetch = fetch or fetch_text

    async def one(story):
        try:
            text = await asyncio.to_thread(fetch, story["url"])
        except Exception as e:  # noqa: BLE001
            logger.info("news brief: couldn't read %s: %s", story["url"], e)
            text = ""
        text = " ".join(str(text or "").split())
        story["text"] = (text or story.get("snippet") or "")[:TEXT_CHARS]

    await asyncio.gather(*(one(s) for s in stories[:count]))


# ------------------------------------------------------------------ all categories
async def gather(categories: list[dict], *, now: datetime | None = None,
                 sources: tuple[Source, ...] | None = None,
                 fetch: Callable[[str], str] | None = None,
                 budget: float = CATEGORY_BUDGET) -> list[dict]:
    """``[{"category": <config row>, "stories": [...], "error": str | None}]`` for the
    enabled categories, in their order. A story in more than one category stays in the
    first; at most ``MAX_PER_CATEGORY`` each, newest first; the first
    ``READ_PER_CATEGORY`` have ``text``."""
    now = now or datetime.now(timezone.utc)
    enabled = [c for c in categories if c.get("enabled", True)]
    found = await asyncio.gather(*(collect(c, now, sources, budget) for c in enabled))
    out, kept = [], []
    for category, (stories, error) in zip(enabled, found):
        mine = [s for s in stories if not any(same_story(s, k) for k in kept)]
        kept.extend(mine)
        mine = newest_first(mine)[:MAX_PER_CATEGORY]
        if stories and not mine:
            error = "every story was already in an earlier category"
        out.append({"category": category, "stories": mine, "error": error})
    await asyncio.gather(*(read_texts(row["stories"], fetch=fetch) for row in out))
    for row in out:
        row["stories"] = [public_story(s) for s in row["stories"]]
    return out
