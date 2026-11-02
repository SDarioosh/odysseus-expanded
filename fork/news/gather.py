"""Fork: gathering the news brief's stories, in code (``fork/news/brief.py``).

Per enabled category, in parallel with a time budget (``CATEGORY_BUDGET`` seconds):

1. **Candidates** from each source in :data:`SOURCES`. A source is
   ``source(category, now) -> list of {title, url, source, published, snippet}``; phase 1
   has one, :func:`searxng_source`: the self-hosted SearXNG only
   (``services/search/providers.searxng_search_api``, news category, last day), never
   ``comprehensive_web_search`` (its fallback chain can reach other search APIs). The
   query is the category's query field, else its name, and nothing else (a category with
   feeds and no query is not searched: feeds only). :func:`feeds_source` reads the
   category's RSS/Atom feeds (standard library ``xml.etree``; DOCTYPE and ENTITY
   declarations refused, at most ``MAX_FEED_BYTES``), fetched through the hub's
   public-address guard (``services/search/content._public_http_url`` and
   ``_get_public_url``, the same as page fetches) with ``FEED_TIMEOUT``. A feed that fails
   is reported in the brief's ``errors`` and leaves the other feeds and the search.
2. **Clean-up**: results without a title or an http(s) URL, and results older than
   ``LOOKBACK_HOURS`` or than the previous brief, whichever is earlier (at most
   ``MAX_SINCE_HOURS``; when the date is known), are dropped; URLs are made canonical
   (tracking parameters, fragments and AMP paths removed) and deduplicated; near-duplicate
   titles (token overlap) become one story with several sources (``also``).
3. **Seen** (:func:`seen_index`, :func:`already_seen`): a story whose canonical URL was in
   one of the last ``SEEN_BRIEFS`` briefs is dropped, unless it has a source that the
   earlier story didn't have. Across categories (:func:`gather`), a story stays in the
   first category that has it; at most ``MAX_PER_CATEGORY`` stories each, newest first.
4. **Read**: the first ``READ_PER_CATEGORY`` stories' pages through
   ``services/search/content.fetch_webpage_content`` (the hub's SSRF-guarded fetch), the
   text cut to ``TEXT_CHARS``; a page that fails keeps its search snippet.

Page and feed text is untrusted: it only goes into the model's prompt as data
(``prompt.py``).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.parsers import expat

logger = logging.getLogger(__name__)

SEARCH_COUNT = 15
LOOKBACK_HOURS = 24
MAX_SINCE_HOURS = 72
SEEN_BRIEFS = 3
MAX_PER_CATEGORY = 12
READ_PER_CATEGORY = 6
TEXT_CHARS = 1500
SNIPPET_CHARS = 400
CATEGORY_BUDGET = 60.0
READ_TIMEOUT = 8
FEED_TIMEOUT = 10
MAX_FEED_BYTES = 2_000_000
MAX_FEED_ITEMS = 50
FEED_WORKERS = 4

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
def cutoff_time(now: datetime, since: datetime | None = None) -> datetime:
    """The oldest date kept: ``LOOKBACK_HOURS`` ago, or the previous brief when that is
    earlier (at most ``MAX_SINCE_HOURS`` ago)."""
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    if since is not None and since < cutoff:
        cutoff = max(since, now - timedelta(hours=MAX_SINCE_HOURS))
    return cutoff


def clean(results: list[dict], now: datetime, since: datetime | None = None
          ) -> list[Candidate]:
    """Usable candidates in result order: title and http(s) URL present, canonical URL,
    not older than the lookback (:func:`cutoff_time`), deduplicated by URL."""
    cutoff = cutoff_time(now, since)
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


def story_sources(story: dict) -> set[str]:
    return {str(story.get("source") or ""),
            *(str(a.get("source") or "") for a in story.get("also") or [] if isinstance(a, dict))
            } - {""}


# ------------------------------------------------------------------ seen before
def seen_index(briefs: Iterable[dict]) -> dict[str, set[str]]:
    """``url_key`` of every story link (its own and its ``also``) in ``briefs`` -> the
    sources that story had."""
    index: dict[str, set[str]] = {}
    for brief in briefs or []:
        for cat in (brief or {}).get("categories") or []:
            for story in (cat or {}).get("stories") or []:
                if not isinstance(story, dict):
                    continue
                urls = [story.get("url"), *(a.get("url") for a in story.get("also") or []
                                            if isinstance(a, dict))]
                sources = story_sources(story)
                for url in urls:
                    canonical = canonical_url(url)
                    if canonical:
                        index.setdefault(url_key(canonical), set()).update(sources)
    return index


def already_seen(story: dict, index: dict[str, set[str]]) -> bool:
    """The story's link was in a recent brief and it brings no source that story lacked."""
    matched = [index[k] for k in story.get("_keys") or () if k in index]
    if not matched:
        return False
    before = set().union(*matched)
    return not (story_sources(story) - before)


# ------------------------------------------------------------------ sources
def searxng_search(query: str) -> list[dict]:
    """SearXNG's news category for the last day (the hub's own SearXNG, nothing else)."""
    from services.search.providers import searxng_search_api

    return searxng_search_api(query, count=SEARCH_COUNT, categories="news", time_filter="day")


def category_query(category: dict) -> str:
    """Only what the owner typed: the query field, else the category name."""
    return (category.get("query") or "").strip() or (category.get("name") or "").strip()


def searxng_source(category: dict, now: datetime) -> list[Candidate]:
    if category.get("feeds") and not (category.get("query") or "").strip():
        return []  # feeds only
    query = category_query(category)
    return searxng_search(query) if query else []


# ------------------------------------------------------------------ RSS / Atom feeds
class FeedError(Exception):
    """A feed that couldn't be used; the message is shown in the brief's errors."""


class Found(list):
    """A source's candidates plus the problems it met without failing (a feed that
    couldn't be read); those are reported even when the category has stories."""

    def __init__(self, items: Iterable[Candidate] = (), errors: Iterable[str] = ()):
        super().__init__(items)
        self.errors = list(errors)


FEED_ROOTS = frozenset({"rss", "feed", "rdf"})
TAG = re.compile(r"<[^>]*>")


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _child(element: ET.Element, *names: str) -> ET.Element | None:
    """The first direct child with one of ``names`` (local names, in that order)."""
    for name in names:
        for child in element:
            if _local(child.tag) == name:
                return child
    return None


def _text(element: ET.Element | None) -> str:
    return "".join(element.itertext()) if element is not None else ""


def plain(text: str) -> str:
    """Feed text (often escaped HTML) as plain text."""
    return " ".join(html.unescape(TAG.sub(" ", html.unescape(text or ""))).split())


def _no_declarations(body: bytes) -> None:
    """Refuse DOCTYPE and ENTITY declarations before ElementTree sees the body (entity
    expansion attacks). Expat reads the encoding itself, so UTF-16 can't hide one."""
    def refuse(*_args):
        raise FeedError("has a DOCTYPE or ENTITY declaration (refused)")

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(body, True)
    except expat.ExpatError as e:
        raise FeedError(f"is not valid XML ({e})") from None


def _atom_link(entry: ET.Element) -> str:
    links = [c for c in entry if _local(c.tag) == "link" and c.get("href")]
    for link in links:
        if (link.get("rel") or "alternate") == "alternate":
            return link.get("href") or ""
    return (links[0].get("href") or "") if links else ""


def _rss_link(item: ET.Element) -> str:
    link = _text(_child(item, "link")).strip()
    if link:
        return link
    guid = _child(item, "guid")
    if guid is not None and (guid.get("isPermaLink") or "true").lower() != "false":
        return _text(guid).strip()
    return ""


def parse_feed(body: bytes, base_url: str = "") -> list[Candidate]:
    """RSS 2.0, RSS 1.0 or Atom -> ``[{title, url, published, snippet}]`` (at most
    ``MAX_FEED_ITEMS``; ``published`` None when the item has no date). FeedError when the
    body is too big, declares a DOCTYPE or ENTITY, isn't XML or isn't a feed."""
    if len(body) > MAX_FEED_BYTES:
        raise FeedError(f"is larger than {MAX_FEED_BYTES // 1_000_000} MB")
    _no_declarations(body)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise FeedError(f"is not valid XML ({e})") from None
    if _local(root.tag) not in FEED_ROOTS:
        raise FeedError("is not an RSS or Atom feed")
    items: list[Candidate] = []
    for element in root.iter():
        kind = _local(element.tag)
        if kind not in ("item", "entry"):
            continue
        if kind == "entry":
            link = _atom_link(element)
            published = _text(_child(element, "published", "updated", "issued")).strip()
            summary = _text(_child(element, "summary", "content"))
        else:
            link = _rss_link(element)
            published = _text(_child(element, "pubdate", "date", "published")).strip()
            summary = _text(_child(element, "description", "encoded", "summary"))
        items.append({"title": plain(_text(_child(element, "title"))),
                      "url": urljoin(base_url, link.strip()) if link else "",
                      "published": published or None,
                      "snippet": plain(summary)[:SNIPPET_CHARS]})
        if len(items) >= MAX_FEED_ITEMS:
            break
    return items


FEED_HEADERS = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
                          "text/xml;q=0.9, */*;q=0.5"}


def fetch_feed(url: str) -> bytes:
    """The feed's body, through the hub's public-address guard (the same as page fetches:
    no LAN, loopback or metadata address, DNS-pinned redirects), at most
    ``MAX_FEED_BYTES``."""
    from services.search import content
    from src.constants import WEB_FETCH_USER_AGENT

    if not content._public_http_url(url):
        raise FeedError("is not a public http(s) address")
    try:
        response = content._get_public_url(
            url, headers={"User-Agent": WEB_FETCH_USER_AGENT, **FEED_HEADERS},
            timeout=FEED_TIMEOUT, max_bytes=MAX_FEED_BYTES)
    except content.BodyTooLargeError:
        raise FeedError(f"is larger than {MAX_FEED_BYTES // 1_000_000} MB") from None
    response.raise_for_status()
    if getattr(response, "truncated", False):
        raise FeedError(f"is larger than {MAX_FEED_BYTES // 1_000_000} MB")
    return bytes(response.content)


def _feed_label(url: str) -> str:
    return (f"{source_name(url)}{urlsplit(url).path}" or url)[:80]


def read_feed(url: str) -> tuple[list[Candidate], str | None]:
    """One feed's items, or ``([], "feed <site/path> <problem>")``."""
    try:
        return parse_feed(fetch_feed(url), base_url=url), None
    except FeedError as e:
        problem = str(e)
    except Exception as e:  # noqa: BLE001 -- network errors, HTTP status
        problem = f"couldn't be read ({(str(e) or type(e).__name__)[:160]})"
    logger.info("news brief: feed %s %s", url, problem)
    return [], f"feed {_feed_label(url)} {problem}"


def feeds_source(category: dict, now: datetime) -> Found:
    """The category's feeds, read in parallel; a feed that fails is reported in
    ``Found.errors`` and leaves the others."""
    feeds = [u for u in category.get("feeds") or [] if isinstance(u, str) and u.strip()]
    found = Found()
    if not feeds:
        return found
    with ThreadPoolExecutor(max_workers=min(FEED_WORKERS, len(feeds))) as pool:
        for items, error in pool.map(read_feed, feeds):
            found.extend(items)
            if error:
                found.errors.append(error)
    return found


SOURCES: tuple[Source, ...] = (searxng_source, feeds_source)


async def _from_sources(category: dict, now: datetime, sources: tuple[Source, ...]
                        ) -> tuple[list[Candidate], list[str], list[str]]:
    """(candidates, errors of sources that failed, problems of sources that didn't)."""
    raw, errors, problems = [], [], []
    for source in sources:
        try:
            result = await asyncio.to_thread(source, category, now)
        except Exception as e:  # noqa: BLE001 -- one source failing leaves the others
            logger.warning("news brief: %s failed for %r: %s",
                           getattr(source, "__name__", "source"), category.get("name"), e)
            errors.append(str(e) or type(e).__name__)
            continue
        raw.extend(result or [])
        problems.extend(getattr(result, "errors", None) or [])
    return raw, errors, problems


async def collect(category: dict, now: datetime,
                  sources: tuple[Source, ...] | None = None,
                  budget: float = CATEGORY_BUDGET, since: datetime | None = None,
                  ) -> tuple[list[dict], str | None]:
    """One category's grouped stories (not yet capped) and its error, if any: why it has
    no story, else the feeds that couldn't be read."""
    try:
        raw, errors, problems = await asyncio.wait_for(
            _from_sources(category, now, SOURCES if sources is None else sources), budget)
    except asyncio.TimeoutError:
        return [], "timed out"
    stories = group(clean(raw, now, since))
    if not stories:
        return [], "; ".join(errors + problems) or "no results"
    return stories, "; ".join(problems) or None


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
                 budget: float = CATEGORY_BUDGET, since: datetime | None = None,
                 seen: dict[str, set[str]] | None = None) -> list[dict]:
    """``[{"category": <config row>, "stories": [...], "error": str | None}]`` for the
    enabled categories, in their order. ``since``: the previous brief's time (older
    results are kept back to it, :func:`cutoff_time`); ``seen``: :func:`seen_index` of
    the last briefs (stories already shown are dropped, :func:`already_seen`). A story in
    more than one category stays in the first; at most ``MAX_PER_CATEGORY`` each, newest
    first; the first ``READ_PER_CATEGORY`` have ``text``."""
    now = now or datetime.now(timezone.utc)
    enabled = [c for c in categories if c.get("enabled", True)]
    found = await asyncio.gather(*(collect(c, now, sources, budget, since) for c in enabled))
    out, kept = [], []
    for category, (stories, error) in zip(enabled, found):
        if seen:
            fresh = [s for s in stories if not already_seen(s, seen)]
            if stories and not fresh:
                error = error or f"every story was in one of the last {SEEN_BRIEFS} briefs"
            stories = fresh
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
