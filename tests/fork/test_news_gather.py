"""Morning news brief: gathering and clean-up (fork/news/gather.py). Fictional data only."""

import asyncio
from datetime import datetime, timezone

import pytest

from fork.news import gather as ng

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def _r(title, url, published="2026-11-03T08:00:00Z", snippet="A snippet."):
    return {"title": title, "url": url, "snippet": snippet, "published_date": published}


# ------------------------------------------------------------------ canonical URLs
@pytest.mark.parametrize(("url", "want"), [
    ("https://Example.com/f1/test-day?utm_source=x&utm_medium=y&id=7#top",
     "https://example.com/f1/test-day?id=7"),
    ("https://example.com/f1/test-day/?fbclid=abc", "https://example.com/f1/test-day"),
    ("https://example.com/f1/test-day/amp", "https://example.com/f1/test-day"),
    ("https://example.com/amp/f1/test-day", "https://example.com/f1/test-day"),
    ("http://example.org/", "http://example.org/"),
])
def test_canonical_url(url, want):
    assert ng.canonical_url(url) == want


@pytest.mark.parametrize("url", ["", "ftp://example.com/x", "javascript:alert(1)",
                                 "mailto:a@example.com", "/relative/path", "https://"])
def test_non_http_urls_are_dropped(url):
    assert ng.canonical_url(url) is None


def test_source_name_and_title_suffix():
    assert ng.source_name("https://www.example.com/x") == "example.com"
    assert ng.clean_title("Team X tops final test day - Example News") == "Team X tops final test day"
    assert ng.clean_title("Short - Site") == "Short - Site"  # too short once cut: kept


# ------------------------------------------------------------------ clean-up
def test_clean_drops_bad_old_and_duplicate_results():
    out = ng.clean([
        _r("Team X tops final test day", "https://example.com/f1/test-day?utm_source=feed"),
        _r("Team X tops final test day", "https://www.example.com/f1/test-day"),  # same URL
        _r("", "https://example.com/no-title"),
        _r("No URL here", ""),
        _r("Not the web", "ftp://example.com/file"),
        _r("Last week's story", "https://example.com/old", published="2026-10-27T08:00:00Z"),
        _r("Undated story is kept", "https://example.net/undated", published=None),
        "not a dict",
    ], NOW)
    assert [c["url"] for c in out] == ["https://example.com/f1/test-day",
                                       "https://example.net/undated"]
    assert out[0] == {"title": "Team X tops final test day",
                      "url": "https://example.com/f1/test-day", "source": "example.com",
                      "published": "2026-11-03T08:00:00Z", "snippet": "A snippet."}
    assert out[1]["published"] is None


def test_near_duplicate_titles_become_one_story_with_sources():
    stories = ng.group(ng.clean([
        _r("Team X tops final test day in Valencia", "https://example.com/a"),
        _r("Team X tops the final test day at Valencia - Example Org", "https://example.org/b",
           published="2026-11-03T09:30:00Z"),
        _r("Team X tops final test day in Valencia", "https://example.com/c"),  # same site
        _r("Driver Y signs a new contract", "https://example.net/d"),
    ], NOW))
    assert len(stories) == 2
    first = stories[0]
    assert first["url"] == "https://example.com/a"
    assert first["also"] == [{"url": "https://example.org/b", "source": "example.org"}]
    assert first["published"] == "2026-11-03T09:30:00Z"  # the newest of the group
    assert stories[1]["title"] == "Driver Y signs a new contract"


def test_different_stories_stay_apart():
    a = ng.title_tokens("City council approves new bike lanes downtown")
    b = ng.title_tokens("City council rejects budget for transit downtown")
    assert not ng.similar_titles(a, b)


# ------------------------------------------------------------------ sources and gather
def _fake_search(results_by_query, seen):
    def search(query):
        seen.append(query)
        return results_by_query.get(query, [])
    return search


def test_searxng_source_uses_only_the_category_query(monkeypatch):
    calls = []

    def fake_api(query, count=None, categories="general", time_filter=None, **kw):
        calls.append((query, count, categories, time_filter, kw))
        return []

    monkeypatch.setattr("services.search.providers.searxng_search_api", fake_api)
    ng.searxng_source({"name": "Formula 1", "query": ""}, NOW)
    ng.searxng_source({"name": "Markets", "query": "TSX stocks"}, NOW)
    assert calls == [("Formula 1", 15, "news", "day", {}),
                     ("TSX stocks", 15, "news", "day", {})]


def test_gather_never_calls_the_comprehensive_search(monkeypatch):
    import services.search as search_pkg

    def boom(*a, **k):
        raise AssertionError("comprehensive_web_search must not be used")

    monkeypatch.setattr(search_pkg, "comprehensive_web_search", boom, raising=False)
    monkeypatch.setattr("services.search.providers.searxng_search_api",
                        lambda q, **kw: [_r("Rain expected all week", "https://example.com/w")])
    rows = asyncio.run(ng.gather([{"name": "Weather"}], now=NOW, fetch=lambda u: "Text."))
    assert rows[0]["stories"][0]["url"] == "https://example.com/w"


def test_gather_assigns_a_story_to_the_first_category_and_caps(monkeypatch):
    shared = _r("Central bank holds rates steady", "https://example.com/rates")
    words = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
             "november oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee "
             "zulu amber basil cedar dahlia ember fern ginger hazel iris jasper koala lotus "
             "maple nutmeg olive pepper quartz raven sage thyme umber violet willow yarrow "
             "zinnia acorn birch clover daisy elm").split()
    many = [_r(" ".join(words[3 * i:3 * i + 3]).title() + " report", f"https://example.com/m{i}",
               published=f"2026-11-03T{i % 10:02d}:00:00Z") for i in range(20)]
    seen = []
    monkeypatch.setattr(ng, "searxng_search", _fake_search({
        "Economy": [shared],
        "Markets": [shared, *many],
        "Empty": [],
    }, seen))
    cats = [{"name": "Economy"}, {"name": "Off", "enabled": False}, {"name": "Markets"},
            {"name": "Empty"}]
    rows = asyncio.run(ng.gather(cats, now=NOW, fetch=lambda url: f"Body of {url}"))
    assert seen == ["Economy", "Markets", "Empty"]  # the disabled one is not searched
    assert [r["category"]["name"] for r in rows] == ["Economy", "Markets", "Empty"]
    economy, markets, empty = rows
    assert [s["url"] for s in economy["stories"]] == ["https://example.com/rates"]
    assert "https://example.com/rates" not in [s["url"] for s in markets["stories"]]
    assert len(markets["stories"]) == ng.MAX_PER_CATEGORY
    dates = [s["published"] for s in markets["stories"]]
    assert dates == sorted(dates, reverse=True)  # newest first
    assert empty == {"category": {"name": "Empty"}, "stories": [], "error": "no results"}
    # the first six are read, the rest keep only their snippet
    assert all(s["text"].startswith("Body of ") for s in markets["stories"][:6])
    assert all("text" not in s for s in markets["stories"][6:])
    assert all(not k.startswith("_") for s in markets["stories"] for k in s)


def test_read_texts_cut_and_fall_back_to_the_snippet():
    stories = [{"url": "https://example.com/1", "snippet": "Snippet one."},
               {"url": "https://example.com/2", "snippet": "Snippet two."},
               {"url": "https://example.com/3", "snippet": "Snippet three."}]

    def fetch(url):
        if url.endswith("/1"):
            return "word " * 1000
        if url.endswith("/2"):
            raise OSError("paywall")
        return ""

    asyncio.run(ng.read_texts(stories, fetch=fetch))
    assert len(stories[0]["text"]) == ng.TEXT_CHARS
    assert stories[1]["text"] == "Snippet two."
    assert stories[2]["text"] == "Snippet three."


def test_fetch_text_uses_the_guarded_fetch(monkeypatch):
    calls = []

    def fake_fetch(url, timeout=5, **kw):
        calls.append(url)
        return {"success": url.endswith("ok"), "content": "Article body."}

    monkeypatch.setattr("services.search.content.fetch_webpage_content", fake_fetch)
    assert ng.fetch_text("https://example.com/ok") == "Article body."
    assert ng.fetch_text("https://example.com/fail") == ""
    assert calls == ["https://example.com/ok", "https://example.com/fail"]


def test_a_failing_source_is_reported_and_others_still_count():
    def broken(category, now):
        raise RuntimeError("searx down")

    def works(category, now):
        return [_r("Local library extends hours", "https://example.com/lib")]

    rows = asyncio.run(ng.gather([{"name": "Toronto"}], now=NOW, sources=(broken, works),
                                 fetch=lambda u: ""))
    assert rows[0]["error"] is None and len(rows[0]["stories"]) == 1
    rows = asyncio.run(ng.gather([{"name": "Toronto"}], now=NOW, sources=(broken,),
                                 fetch=lambda u: ""))
    assert rows[0]["stories"] == [] and rows[0]["error"] == "searx down"


def test_a_slow_category_times_out():
    import time

    def slow(category, now):
        time.sleep(0.5)
        return []

    rows = asyncio.run(ng.gather([{"name": "Slow"}], now=NOW, sources=(slow,), budget=0.05,
                                 fetch=lambda u: ""))
    assert rows[0]["error"] == "timed out"
