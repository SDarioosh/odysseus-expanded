"""Morning news brief, phase 2: RSS/Atom feeds, the "seen in the last 3 briefs" filter
and feed config (fork/news/gather.py, store.py, brief.py). Fictional data only: fake
feeds, a fake SearXNG source, no network."""

import asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from fork.news import brief as nb
from fork.news import gather as ng
from fork.news import store

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
 <channel>
  <title>Example Motorsport</title>
  <item>
   <title>Team X tops final test day in Valencia</title>
   <link>https://example.com/f1/test-day?utm_source=rss</link>
   <pubDate>Tue, 03 Nov 2026 08:10:00 GMT</pubDate>
   <description>&lt;p&gt;Fictional &lt;b&gt;report&lt;/b&gt; &amp;amp; notes.&lt;/p&gt;</description>
  </item>
  <item>
   <title>Driver Y signs a new contract</title>
   <link>/f1/driver-y</link>
   <dc:date>2026-11-03T09:30:00Z</dc:date>
  </item>
  <item>
   <title>Undated garage tour</title>
   <guid>https://example.com/f1/garage-tour</guid>
  </item>
  <item>
   <title>Last week's story</title>
   <link>https://example.com/f1/old</link>
   <pubDate>Tue, 27 Oct 2026 08:00:00 GMT</pubDate>
  </item>
 </channel>
</rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <title>Example City Desk</title>
 <entry>
  <title type="html">Council approves bike lanes &amp;amp; transit plan</title>
  <link rel="self" href="https://example.org/api/1"/>
  <link rel="alternate" href="https://example.org/city/bike-lanes"/>
  <updated>2026-11-03T07:00:00-05:00</updated>
  <summary>Fictional council vote.</summary>
 </entry>
 <entry>
  <title>Library extends weekend hours</title>
  <link href="https://example.org/city/library"/>
  <published>2026-11-03T10:00:00Z</published>
  <content type="html">&lt;p&gt;More hours.&lt;/p&gt;</content>
 </entry>
</feed>"""


# ------------------------------------------------------------------ parsing
def test_parse_rss():
    items = ng.parse_feed(RSS, base_url="https://example.com/rss.xml")
    assert [i["title"] for i in items] == ["Team X tops final test day in Valencia",
                                           "Driver Y signs a new contract",
                                           "Undated garage tour", "Last week's story"]
    first = items[0]
    assert first["url"] == "https://example.com/f1/test-day?utm_source=rss"
    assert first["snippet"] == "Fictional report & notes."
    assert ng.parse_published(first["published"]) == datetime(2026, 11, 3, 8, 10,
                                                              tzinfo=timezone.utc)
    assert items[1]["url"] == "https://example.com/f1/driver-y"  # relative link resolved
    assert items[1]["published"] == "2026-11-03T09:30:00Z"
    assert items[2]["url"] == "https://example.com/f1/garage-tour"
    assert items[2]["published"] is None


def test_parse_atom():
    items = ng.parse_feed(ATOM)
    assert [i["url"] for i in items] == ["https://example.org/city/bike-lanes",
                                         "https://example.org/city/library"]
    assert items[0]["title"] == "Council approves bike lanes & transit plan"
    assert ng.parse_published(items[0]["published"]) == datetime(2026, 11, 3, 12, 0,
                                                                 tzinfo=timezone.utc)
    assert items[1]["snippet"] == "More hours."


def test_clean_keeps_recent_and_undated_feed_items_dated_first():
    stories = ng.newest_first(ng.group(ng.clean(
        ng.parse_feed(RSS, base_url="https://example.com/rss.xml"), NOW)))
    assert [s["url"] for s in stories] == ["https://example.com/f1/driver-y",
                                           "https://example.com/f1/test-day",
                                           "https://example.com/f1/garage-tour"]


def test_since_the_previous_brief_widens_the_lookback_up_to_three_days():
    old = [{"title": "Two days old story", "url": "https://example.com/two",
            "published": "2026-11-01T13:00:00Z"},
           {"title": "Four days old story", "url": "https://example.com/four",
            "published": "2026-10-30T13:00:00Z"}]
    assert ng.clean(old, NOW) == []
    since = datetime(2026, 10, 25, tzinfo=timezone.utc)
    assert [c["url"] for c in ng.clean(old, NOW, since)] == ["https://example.com/two"]


@pytest.mark.parametrize("body", [
    b"<rss><channel><item><title>Broken",  # not closed
    b"not xml at all",
    b"<html><body>A page, not a feed</body></html>",
    b"",
])
def test_malformed_feeds_are_refused(body):
    with pytest.raises(ng.FeedError):
        ng.parse_feed(body)


@pytest.mark.parametrize("body", [
    b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2
    "&lol;&lol;&lol;&lol;">]><rss><channel><item><title>&lol2;</title></item></channel></rss>""",
    b"""<?xml version="1.0"?><!DOCTYPE rss SYSTEM "http://example.com/x.dtd"><rss/>""",
    """<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "b">]><rss/>"""
    .encode("utf-16"),
])
def test_doctype_and_entity_declarations_are_refused(body):
    with pytest.raises(ng.FeedError, match="DOCTYPE"):
        ng.parse_feed(body)


def test_oversize_feed_is_refused(monkeypatch):
    monkeypatch.setattr(ng, "MAX_FEED_BYTES", 100)
    with pytest.raises(ng.FeedError, match="larger"):
        ng.parse_feed(RSS)


# ------------------------------------------------------------------ fetching
class _Response:
    def __init__(self, content=RSS, status=200, truncated=False):
        self.content, self.status_code, self.truncated = content, status, truncated

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.mark.parametrize("url", ["http://127.0.0.1/feed", "http://192.168.1.20/rss",
                                 "http://localhost:8080/rss", "http://router.lan/rss",
                                 "http://[::1]/rss", "file:///etc/passwd"])
def test_private_address_feeds_are_refused(url, monkeypatch):
    from services.search import content

    def never(*a, **kw):
        raise AssertionError("must not be fetched")

    monkeypatch.setattr(content, "_get_public_url", never)
    with pytest.raises(ng.FeedError, match="public"):
        ng.fetch_feed(url)
    items, error = ng.read_feed(url)
    assert items == [] and "public" in error


def test_fetch_feed_goes_through_the_guarded_fetch(monkeypatch):
    from services.search import content

    calls = []

    def get(url, headers, timeout, max_redirects=5, max_bytes=None):
        calls.append((url, timeout, max_bytes))
        return _Response()

    monkeypatch.setattr(content, "_public_http_url", lambda url: True)
    monkeypatch.setattr(content, "_get_public_url", get)
    assert ng.fetch_feed("https://example.com/rss.xml") == RSS
    assert calls == [("https://example.com/rss.xml", ng.FEED_TIMEOUT, ng.MAX_FEED_BYTES)]
    monkeypatch.setattr(content, "_get_public_url", lambda *a, **kw: _Response(truncated=True))
    with pytest.raises(ng.FeedError, match="larger"):
        ng.fetch_feed("https://example.com/rss.xml")
    monkeypatch.setattr(content, "_get_public_url", lambda *a, **kw: _Response(status=503))
    assert "HTTP 503" in ng.read_feed("https://example.com/rss.xml")[1]


# ------------------------------------------------------------------ feeds in the run
FEEDS = {"https://example.com/rss.xml": RSS, "https://example.org/atom.xml": ATOM}


@pytest.fixture
def fake_feeds(monkeypatch):
    fetched = []

    def fetch(url):
        fetched.append(url)
        if url not in FEEDS:
            raise ng.FeedError("couldn't be read (fictional outage)")
        return FEEDS[url]

    monkeypatch.setattr(ng, "fetch_feed", fetch)
    return fetched


def _search(results, calls=None):
    def searxng(category, now):
        if calls is not None:
            calls.append(category["name"])
        return results
    return searxng


def _gather(categories, sources, **kw):
    return asyncio.run(ng.gather(categories, now=NOW, sources=sources, fetch=lambda u: "",
                                 **kw))


def test_feed_items_merge_and_dedupe_with_search_results(fake_feeds):
    search = [{"title": "Team X tops the final test day at Valencia", "url":
               "https://example.net/f1/x-valencia", "published_date": "2026-11-03T09:00:00Z"},
              {"title": "Driver Y signs a new contract",
               "url": "https://example.com/f1/driver-y?utm_medium=search"}]
    cat = {"name": "Formula 1", "query": "", "feeds": ["https://example.com/rss.xml"]}
    rows = _gather([{**cat, "query": "F1"}], (_search(search), ng.feeds_source))
    stories = rows[0]["stories"]
    assert rows[0]["error"] is None
    by_url = {s["url"]: s for s in stories}
    # the search result and the feed item of the same story are one story
    assert by_url["https://example.net/f1/x-valencia"]["also"] == [
        {"url": "https://example.com/f1/test-day", "source": "example.com"}]
    # the same URL from both is one candidate
    assert sum(1 for s in stories if s["url"].endswith("/driver-y")) == 1
    assert "https://example.com/f1/garage-tour" in by_url  # undated, kept
    assert stories[-1]["url"] == "https://example.com/f1/garage-tour"  # after dated ones
    assert "https://example.com/f1/old" not in by_url


def test_feeds_only_category_is_not_searched(fake_feeds, monkeypatch):
    searched = []
    monkeypatch.setattr(ng, "searxng_search", lambda q: searched.append(q) or [])
    cat = {"name": "City", "query": "", "feeds": ["https://example.org/atom.xml"]}
    rows = _gather([cat], None)  # the real SOURCES
    assert searched == []
    assert [s["url"] for s in rows[0]["stories"]] == ["https://example.org/city/bike-lanes",
                                                      "https://example.org/city/library"]
    # with a query, both are used
    _gather([{**cat, "query": "city council"}], None)
    assert searched == ["city council"]


def test_a_failing_feed_is_recorded_and_the_category_goes_on(fake_feeds):
    cat = {"name": "City", "query": "", "feeds": ["https://example.invalid/down.xml",
                                                  "https://example.org/atom.xml"]}
    rows = _gather([cat], (ng.feeds_source,))
    assert len(rows[0]["stories"]) == 2
    assert rows[0]["error"] == ("feed example.invalid/down.xml couldn't be read "
                                "(fictional outage)")
    rows = _gather([{**cat, "feeds": cat["feeds"][:1]}], (ng.feeds_source,))
    assert rows[0]["stories"] == [] and "example.invalid/down.xml" in rows[0]["error"]


def test_feed_errors_reach_the_brief(fake_feeds, data_dir, monkeypatch):
    monkeypatch.setattr(nb, "owner_timezone", lambda owner: ZoneInfo("UTC"))
    monkeypatch.setattr(nb, "_RUN_LOCKS", {})
    monkeypatch.setattr(nb, "AFTER_RUN", [])
    store.save_config("alice", {"categories": [
        {"name": "City", "feeds": ["https://example.org/atom.xml",
                                   "https://example.invalid/down.xml"]}]})
    status, brief = asyncio.run(nb.run("alice", force=True, llm=None, now=NOW,
                                       sources=(ng.feeds_source,), fetch=lambda u: ""))
    assert status == "written"
    assert len(brief["categories"][0]["stories"]) == 2
    assert brief["errors"] == [{"category": "City", "error":
                                "feed example.invalid/down.xml couldn't be read "
                                "(fictional outage)"}]


# ------------------------------------------------------------------ seen filter
def _brief(day, stories):
    return store.make_brief(date.fromisoformat(day), [{"name": "F1", "stories": stories}], [])


SEEN = [{"title": "Team X tops final test day in Valencia",
         "url": "https://example.com/f1/test-day", "source": "example.com",
         "also": [{"url": "https://example.org/f1/x", "source": "example.org"}]}]


def test_stories_from_the_last_briefs_are_dropped():
    index = ng.seen_index([_brief("2026-11-02", SEEN)])
    results = [{"title": "Team X tops final test day in Valencia",
                "url": "https://www.example.com/f1/test-day/?utm_source=x"},
               {"title": "Driver Y signs a new contract", "url": "https://example.com/f1/y"}]
    rows = _gather([{"name": "F1"}], (_search(results),), seen=index)
    assert [s["url"] for s in rows[0]["stories"]] == ["https://example.com/f1/y"]
    # an also-link of an earlier story counts too
    rows = _gather([{"name": "F1"}], (_search([{"title": "Team X story",
                                                "url": "https://example.org/f1/x"}]),),
                   seen=index)
    assert rows[0]["stories"] == [] and "last 3 briefs" in rows[0]["error"]


def test_a_seen_story_with_a_new_source_is_kept():
    index = ng.seen_index([_brief("2026-11-02", SEEN)])
    results = [{"title": "Team X tops final test day in Valencia",
                "url": "https://example.com/f1/test-day"},
               {"title": "Team X tops the final test day at Valencia again",
                "url": "https://example.net/f1/x-again"}]
    rows = _gather([{"name": "F1"}], (_search(results),), seen=index)
    story = rows[0]["stories"][0]
    assert story["url"] == "https://example.com/f1/test-day"
    assert story["also"] == [{"url": "https://example.net/f1/x-again", "source": "example.net"}]


def test_run_reads_the_last_three_briefs_before_today(data_dir, monkeypatch):
    monkeypatch.setattr(nb, "owner_timezone", lambda owner: ZoneInfo("UTC"))
    monkeypatch.setattr(nb, "_RUN_LOCKS", {})
    monkeypatch.setattr(nb, "AFTER_RUN", [])
    store.save_config("alice", {"categories": [{"name": "F1"}]})
    titles = {"2026-10-29": "Harbour ferry schedule changes announced",
              "2026-10-30": "Museum opens fictional dinosaur exhibit",
              "2026-10-31": "Marathon route closes downtown streets",
              "2026-11-01": "Bakery wins regional bread award",
              "today": "Observatory reports clear skies tonight"}
    for day in ("2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01", "2026-11-03"):
        key = "today" if day == "2026-11-03" else day
        store.save("alice", _brief(day, [{"title": titles[key],
                                          "url": f"https://example.com/{key}",
                                          "source": "example.com"}]))
    results = [{"title": title, "url": f"https://example.com/{key}"}
               for key, title in titles.items()]
    status, brief = asyncio.run(nb.run("alice", force=True, llm=None, now=NOW,
                                       sources=(_search(results),), fetch=lambda u: ""))
    assert status == "written"
    urls = {s["url"] for s in brief["categories"][0]["stories"]}
    # the three briefs before today are "seen"; the fourth and today's own (Refresh) are not
    assert urls == {"https://example.com/2026-10-29", "https://example.com/today"}


# ------------------------------------------------------------------ config
def test_feeds_config_is_validated(data_dir):
    saved = store.save_config("alice", {"categories": [
        {"name": "City", "feeds": [" https://example.org/atom.xml ", "",
                                   "https://example.org/atom.xml", "http://example.com/rss"]}]})
    assert saved["categories"][0]["feeds"] == ["https://example.org/atom.xml",
                                               "http://example.com/rss"]
    assert store.load_config("alice")["categories"][0]["feeds"] == saved["categories"][0]["feeds"]
    too_many = [f"https://example.com/feed/{i}" for i in range(store.MAX_FEEDS + 1)]
    for feeds, reason in ((["ftp://example.com/rss"], "http(s)"),
                          (["javascript:alert(1)"], "http(s)"),
                          (["https://"], "http(s)"),
                          (["https://user:secret@example.com/rss"], "http(s)"),
                          (["https://example.com/a feed"], "http(s)"),
                          (["https://example.com/" + "x" * 600], "longer"),
                          ("https://example.com/rss", "list"),
                          ([42], "list"),
                          (too_many, "at most 10 feeds")):
        with pytest.raises(ValueError, match=reason.replace("(", r"\(").replace(")", r"\)")):
            store.normalize_config({"categories": [{"name": "City", "feeds": feeds}]})


def test_config_route_answers_400_for_a_bad_feed(data_dir, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.middleware import require_admin
    from fork.routes import news_routes as nr

    monkeypatch.setattr(nr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(nb, "owner_timezone", lambda owner: ZoneInfo("UTC"))
    app = FastAPI()
    app.dependency_overrides[require_admin] = lambda: None
    app.include_router(nr.setup_news_routes())
    with TestClient(app) as client:
        r = client.put("/api/news/config", json={"categories": [
            {"name": "City", "feeds": ["http://example.org/atom.xml"]}]})
        assert r.status_code == 200
        assert r.json()["categories"][0]["feeds"] == ["http://example.org/atom.xml"]
        r = client.put("/api/news/config", json={"categories": [
            {"name": "City", "feeds": ["file:///etc/passwd"]}]})
        assert r.status_code == 400 and "http(s) URL" in r.json()["detail"]
        assert store.load_config("alice")["categories"][0]["feeds"] == [
            "http://example.org/atom.xml"]
