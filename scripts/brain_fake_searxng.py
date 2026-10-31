#!/usr/bin/env python3
"""Fork: a fake SearXNG for the brain eval (``scripts/brain_eval.py``).

Serves a small, fictional, stable web so every eval run sees the same search results:

* ``GET /search?q=...&format=json`` answers like SearXNG's JSON API (``results`` with
  ``url``, ``title``, ``content``, ``engines``, ``publishedDate``), which is what
  upstream's ``services/search/providers.py:searxng_search_api`` parses. Without
  ``format=json`` it answers SearXNG's HTML (``article.result``), what the provider's
  HTML fallback parses.
* ``GET /page/<slug>`` serves the saved page a result links to.
* ``GET /__eval/queries`` returns ``{"total": <count>, "queries": [...]}``, the queries
  after the first ``?since=<n>`` (JSON), and
  every query is also appended to ``--log`` (JSON lines). The eval's privacy check reads
  them: a seeded private term in any query fails it.
* ``GET /healthz`` answers ``ok``.

The web is ``tests/fixtures/brain_eval/``: ``results.json`` (entries: slug, title, snippet,
terms, topics) and ``pages/<slug>.html``. A query matches an entry when it contains one of
its ``terms``; matches are ranked by matched terms, then topics, then fixture order. A
query with no match gets the fixed ``no_results`` set (never empty: an empty answer
makes upstream retry and fall back to other providers). Dates in the fixtures are
written relative to the server's today (``{{date+1}}``, ``{{iso+0}}``, ``{{weekday+7}}``,
``{{year}}``), so the eval works on any day; ``--today`` pins it.

Point Odysseus at it with ``SEARXNG_INSTANCE=http://127.0.0.1:8890`` and
``ODYSSEUS_SEARCH_PROVIDER=searxng`` (an admin "search URL" setting wins over the env
var: leave it empty). Upstream's ``web_fetch`` refuses loopback and private addresses
(``src/outbound_fetch.py``), so the snippets carry each page's key facts and an answer
can cite the result URL without fetching it.

    python scripts/brain_fake_searxng.py --port 8890 --log /tmp/brain_searxng.jsonl

Fictional data only. Docs: ``docs/fork/brain.md#evaluation``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "brain_eval"
DEFAULT_PORT = 8890
MAX_RESULTS = 8
_PLACEHOLDER = re.compile(r"\{\{\s*(date|iso|weekday|year)\s*(?:([+-])\s*(\d+))?\s*\}\}")


def render(text: str, today: dt.date) -> str:
    """Fill the fixtures' date placeholders, counted in days from ``today``."""

    def one(m: re.Match) -> str:
        kind, sign, n = m.group(1), m.group(2), int(m.group(3) or 0)
        day = today + dt.timedelta(days=-n if sign == "-" else n)
        if kind == "iso":
            return day.isoformat()
        if kind == "weekday":
            return day.strftime("%A")
        if kind == "year":
            return str(day.year)
        return f"{day.strftime('%A')} {day.day} {day.strftime('%B %Y')}"

    return _PLACEHOLDER.sub(one, text)


def normalize(text: str) -> str:
    text = text.casefold().replace("’", "'")
    return " ".join(re.sub(r"[^\w'&.+-]+", " ", text).split())


def _mentions(norm_query: str, word: str) -> bool:
    """``word`` starts a word in the query ("marrowby" in "marrowby's", "open" in
    "opening")."""
    return re.search(r"(?<!\w)" + re.escape(normalize(word)), norm_query) is not None


@dataclass
class Web:
    """The fictional web: search entries and pages, rendered for ``today``."""

    entries: list[dict]
    no_results: list[dict]
    pages: dict[str, str]
    today: dt.date = field(default_factory=dt.date.today)
    base_url: str = f"http://127.0.0.1:{DEFAULT_PORT}"

    @classmethod
    def load(cls, fixtures: Path = FIXTURES, *, today: dt.date | None = None,
             base_url: str | None = None) -> Web:
        data = json.loads((fixtures / "results.json").read_text(encoding="utf-8"))
        pages = {p.stem: p.read_text(encoding="utf-8")
                 for p in sorted((fixtures / "pages").glob("*.html"))}
        web = cls(entries=list(data["entries"]), no_results=list(data["no_results"]),
                  pages=pages, today=today or dt.date.today())
        if base_url:
            web.base_url = base_url.rstrip("/")
        return web

    def url(self, slug: str) -> str:
        return f"{self.base_url}/page/{slug}"

    def _result(self, entry: dict, position: int) -> dict:
        url = self.url(entry["slug"])
        return {
            "url": url,
            "title": render(entry["title"], self.today),
            "content": render(entry["snippet"], self.today),
            "engine": "brain-eval",
            "engines": ["brain-eval"],
            "positions": [position],
            "score": round(1.0 / position, 3),
            "category": "general",
            "publishedDate": render(entry.get("published") or "", self.today) or None,
            "parsed_url": list(urlsplit(url)) + [""],
        }

    def search(self, query: str, limit: int = MAX_RESULTS) -> list[dict]:
        """SearXNG result dicts for ``query``, best first."""
        norm = normalize(query)
        ranked = []
        for order, entry in enumerate(self.entries):
            terms = sum(_mentions(norm, t) for t in entry.get("terms") or [])
            if not terms:
                continue
            topics = sum(_mentions(norm, t) for t in entry.get("topics") or [])
            ranked.append((-terms, -topics, order, entry))
        chosen = [e for *_, e in sorted(ranked, key=lambda r: r[:3])][:limit]
        chosen = chosen or self.no_results
        return [self._result(e, i) for i, e in enumerate(chosen, 1)]

    def page(self, slug: str) -> str | None:
        raw = self.pages.get(slug)
        return None if raw is None else render(raw, self.today)


def search_json(web: Web, query: str) -> dict:
    results = web.search(query)
    return {"query": query, "number_of_results": len(results), "results": results,
            "answers": [], "corrections": [], "infoboxes": [], "suggestions": [],
            "unresponsive_engines": []}


def search_html(web: Web, query: str) -> str:
    items = "\n".join(
        f'<article class="result"><h3><a href="{html.escape(r["url"])}">'
        f'{html.escape(r["title"])}</a></h3><p class="content">{html.escape(r["content"])}'
        "</p></article>" for r in web.search(query))
    return (f"<!DOCTYPE html><html><head><title>{html.escape(query)} - SearXNG</title>"
            f"</head><body><main id=\"results\">{items}</main></body></html>")


class QueryLog:
    """Every search query received, in memory and appended to ``path`` (JSON lines)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.entries: list[dict] = []
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, query: str, params: dict) -> None:
        row = {"t": round(time.time(), 3), "q": query, "params": params}
        with self._lock:
            self.entries.append(row)
            if self.path:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")

    def since(self, n: int = 0) -> list[dict]:
        return self.count_and_since(n)[1]

    def count_and_since(self, n: int = 0) -> tuple[int, list[dict]]:
        """How many queries so far, and those after the first ``n``."""
        with self._lock:
            return len(self.entries), list(self.entries[max(0, n):])


def make_handler(web: Web, log: QueryLog) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "brain-fake-searxng/1"

        def log_message(self, fmt, *args):  # quiet: the query log is the record
            pass

        def _send(self, status: int, body: str, ctype: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parts = urlsplit(self.path)
            qs = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
            path = parts.path.rstrip("/") or "/"
            if path == "/search":
                query = qs.get("q", "")
                log.add(query, {k: v for k, v in qs.items() if k != "q"})
                if qs.get("format") == "json":
                    self._send(200, json.dumps(search_json(web, query)), "application/json")
                else:
                    self._send(200, search_html(web, query), "text/html")
            elif path.startswith("/page/"):
                page = web.page(path.removeprefix("/page/"))
                if page is None:
                    self._send(404, "<h1>Not found</h1>", "text/html")
                else:
                    self._send(200, page, "text/html")
            elif path == "/__eval/queries":
                try:
                    since = int(qs.get("since") or 0)
                except ValueError:
                    since = 0
                total, rows = log.count_and_since(since)
                self._send(200, json.dumps({"total": total, "queries": rows}),
                           "application/json")
            elif path in ("/healthz", "/"):
                self._send(200, "ok", "text/plain")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            parts = urlsplit(self.path)
            self.path = f"{parts.path}?{body or parts.query}"
            self.do_GET()

    return Handler


def make_server(web: Web, log: QueryLog, host: str = "127.0.0.1",
                port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(web, log))
    server.daemon_threads = True
    return server


@contextmanager
def running(port: int = 0, *, fixtures: Path = FIXTURES, log_path: Path | None = None,
            today: dt.date | None = None) -> Iterator[tuple[str, Web, QueryLog]]:
    """Serve the fake web in a thread (``port=0``: a free one); yields its base URL."""
    web = Web.load(fixtures, today=today)
    log = QueryLog(log_path)
    server = make_server(web, log, "127.0.0.1", port)
    web.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield web.base_url, web, log
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--fixtures", type=Path, default=FIXTURES)
    ap.add_argument("--log", type=Path, default=REPO / "data" / "results" / "brain_searxng.jsonl",
                    help="query log, JSON lines (appended)")
    ap.add_argument("--base-url", help="URL the result links use (default: http://HOST:PORT)")
    ap.add_argument("--today", type=dt.date.fromisoformat, help="pin the date (YYYY-MM-DD)")
    args = ap.parse_args(argv)
    web = Web.load(args.fixtures, today=args.today,
                   base_url=args.base_url or f"http://{args.host}:{args.port}")
    log = QueryLog(args.log)
    server = make_server(web, log, args.host, args.port)
    print(f"fake SearXNG on http://{args.host}:{server.server_address[1]} "
          f"({len(web.entries)} entries, today {web.today}, log {args.log})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
