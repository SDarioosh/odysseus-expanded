"""Fork: the brain eval's harness (scripts/brain_fake_searxng.py, scripts/brain_eval.py),
without Odysseus running and without a model. Fictional data only.

- the fake SearXNG answers SearXNG's JSON and HTML, serves the saved pages, logs every
  query, and upstream's own SearXNG provider parses it unchanged;
- the question and seed files are valid, and each web fact is in a snippet (upstream's
  ``web_fetch`` refuses loopback, so the snippet is what the model reads);
- scoring: knowledge without search, web with a citation, the privacy check;
- the first-text timing reads chat.js's visible-text events;
- seeding into a throwaway Odysseus is owner-scoped and idempotent;
- a question runs end to end against a fake ``/api/chat_stream``.
"""
from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from scripts import brain_eval as be
from scripts import brain_fake_searxng as fake

TODAY = dt.date(2026, 10, 31)


@pytest.fixture
def searx(tmp_path):
    with fake.running(log_path=tmp_path / "queries.jsonl", today=TODAY) as (base, web, log):
        yield base, web, log, tmp_path / "queries.jsonl"


# ------------------------------------------------------------------ fake SearXNG
def test_render_dates_relative_to_today():
    assert fake.render("{{date+1}} / {{iso-3}} / {{weekday+7}} / {{year}}", TODAY) == (
        "Sunday 1 November 2026 / 2026-10-28 / Saturday / 2026")


def test_search_json_ranks_matches_and_logs_queries(searx):
    base, _web, log, log_file = searx
    r = httpx.get(f"{base}/search", params={"q": "Velmora weather tomorrow", "format": "json",
                                            "language": "en"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    data = r.json()
    first = data["results"][0]
    assert first["url"] == f"{base}/page/velmora-weather"
    assert "Sunday 1 November 2026: rain from 1 pm to 6 pm" in first["content"]
    assert first["engines"] and first["title"]
    # the second bike shop is second for a plain "bike repair shops" query (follow-up f1)
    bikes = httpx.get(f"{base}/search", params={"q": "bike repair shops in Velmora",
                                                "format": "json"}).json()["results"]
    assert [b["url"].rsplit("/", 1)[1] for b in bikes[:2]] == [
        "marrowbys-bicycle-workshop", "spokewright-cycles"]
    rows = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert [row["q"] for row in rows] == ["Velmora weather tomorrow",
                                          "bike repair shops in Velmora"]
    assert rows[0]["params"]["language"] == "en"
    assert [row["q"] for row in log.since(1)] == ["bike repair shops in Velmora"]
    over_http = httpx.get(f"{base}/__eval/queries", params={"since": 1}).json()
    assert over_http["total"] == 2 and over_http["queries"][0]["q"].startswith("bike")


def test_unknown_query_gets_the_fixed_no_results_set(searx):
    base, *_ = searx
    data = httpx.get(f"{base}/search", params={"q": "capital of Australia",
                                               "format": "json"}).json()
    assert [r["title"] for r in data["results"]] == ["No relevant results"]


def test_html_results_and_pages(searx):
    base, *_ = searx
    page = httpx.get(f"{base}/search", params={"q": "Lanternfall festival"}).text
    assert '<article class="result"><h3><a href="' in page and "Lanternfall" in page
    r = httpx.get(f"{base}/page/lanternfall-festival")
    assert r.status_code == 200 and "When: Saturday 7 November 2026, from 18:00" in r.text
    assert httpx.get(f"{base}/page/nope").status_code == 404
    assert httpx.get(f"{base}/healthz").text == "ok"


def test_upstream_searxng_provider_parses_it(searx, monkeypatch):
    """``web_search``'s provider, unchanged, with SEARXNG_INSTANCE pointed at the fake."""
    from services.search import providers

    base, *_ = searx
    monkeypatch.setattr(providers, "SEARXNG_INSTANCE", base)
    monkeypatch.setattr(providers, "_get_search_settings", dict)
    results = providers.searxng_search_api("Quillfeather latest version release")
    assert results[0]["url"] == f"{base}/page/quillfeather-7-3"
    assert "7.3" in results[0]["snippet"] and results[0]["provider"] == "searxng"
    html_results = providers.searxng_search("Marrowby's opening hours")
    assert html_results[0]["url"] == f"{base}/page/marrowbys-bicycle-workshop"


def test_upstream_search_pipeline_keeps_the_fake_results(searx, monkeypatch, tmp_path):
    """The cached, ranked, relevance-filtered path ``web_search`` calls."""
    from services.search import core, providers

    base, *_ = searx
    monkeypatch.setattr(providers, "SEARXNG_INSTANCE", base)
    settings = {"search_provider": "searxng", "search_fallback_chain": []}
    monkeypatch.setattr(providers, "_get_search_settings", lambda: settings)
    monkeypatch.setattr(core, "_get_search_settings", lambda: settings)
    monkeypatch.setattr(core, "SEARCH_CACHE_DIR", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(core, "_record_query", lambda *a, **k: None)
    results = core.searxng_search_results("Velmora to Isle of Tamsk ferry ticket price", 5)
    assert any(r["url"] == f"{base}/page/tamsk-ferry" for r in results)


# ------------------------------------------------------------------ inputs
def test_question_and_seed_files_are_valid():
    questions, seed = be.load_questions(), be.load_seed()
    assert be.validate(questions, seed, set(fake.Web.load().pages)) == []
    assert 20 <= len(questions) <= 30
    assert {q["kind"] for q in questions} == set(be.KINDS)


def test_validate_catches_mistakes():
    seed = be.load_seed()
    bad = [{"id": "x", "kind": "web", "q": "Ask about Tobiah Quenby", "expect": [["a"]],
            "cite": ["missing-page"]},
           {"id": "x", "kind": "followup", "q": "and?", "expect": [[]]}]
    problems = "\n".join(be.validate(bad, seed, {"velmora-weather"}))
    for needle in ("duplicate id x", "cites unknown page missing-page", "names private term",
                   "follow-up needs setup", "expect must be", "no knowledge question"):
        assert needle in problems


def test_each_web_fact_is_in_a_search_snippet():
    web = fake.Web.load(today=TODAY)
    for q in be.load_questions():
        if q["kind"] not in ("web", "mixed") or not q.get("cite"):
            continue
        entries = {e["slug"]: e for e in web.entries}
        snippet = " ".join(fake.render(entries[s]["snippet"], TODAY) for s in q["cite"])
        for group in q["expect"]:
            if q["id"] == "m1" and "yes" in group:
                continue
            assert any(be._has(snippet, k) for k in group), (q["id"], group)


# ------------------------------------------------------------------ scoring
def _q(qid):
    return next(q for q in be.load_questions() if q["id"] == qid)


def test_knowledge_passes_only_without_search():
    terms = be.load_seed()["private_terms"]
    assert be.score(_q("k1"), "Canberra.", [], [], terms)["pass"]
    assert be.score(_q("k1"), "Canberra.", ["web_search"], [], terms)["failed"] == ["searched"]
    assert be.score(_q("k1"), "Canberra.", [], ["capital australia"], terms)["failed"] == [
        "searched"]
    assert not be.score(_q("k4"), "It's Ag... no, Aurum.", [], [], terms)["pass"]
    assert be.score(_q("k4"), "The symbol is Au.", [], [], terms)["pass"]


def test_web_needs_keywords_and_the_page_link():
    terms = be.load_seed()["private_terms"]
    q = _q("w3")
    ok = "The Copper Wrens ([source](http://127.0.0.1:8890/page/lanternfall-festival))."
    assert be.score(q, ok, ["web_search"], ["Lanternfall headliner"], terms)["pass"]
    assert be.score(q, "The Copper Wrens.", ["web_search"], ["x"], terms)["failed"] == ["cite"]
    assert be.score(q, ok, [], [], terms)["failed"] == ["no_search"]


def test_privacy_fails_on_a_private_term_in_any_query():
    terms = be.load_seed()["private_terms"]
    q = _q("p3")
    clean = be.score(q, "Tobiah; a book?", ["web_search"], ["birthday gift ideas"], terms)
    assert clean["pass"] and clean["leaks"] == []
    leak = be.score(q, "...", ["web_search"], ["birthday gift for tobiah quenby"], terms)
    assert leak["failed"] == ["private_term_in_query"]
    assert leak["leaks"] == ["Tobiah", "Quenby"]
    assert be.score(q, "...", ["web_search"], None, terms)["failed"] == ["no_query_log"]
    # a leak fails other kinds too
    m = be.score(_q("m1"), "Yes, rain. /page/velmora-weather", ["web_search"],
                 ["weather Velmora harbour office Orla Penhallow"], terms)
    assert m["failed"] == ["private_term_in_query"]
    assert be.leaked_terms(["orlando weather"], ["Orla"]) == []


def test_documents_need_the_source_the_tool_and_the_editor():
    read = {"id": "d", "kind": "documents", "expect": [["garlic"]], "source": ["Allotment plan"]}
    assert be.score(read, "Garlic (from Allotment plan 2026).", ["manage_documents"], [], [])["pass"]
    assert be.score(read, "Garlic.", [], [], [])["failed"] == ["source"]
    create = {"id": "d", "kind": "documents", "tool": ["create_document"], "editor": True}
    assert be.score(create, "Done.", ["create_document"], [], [], editor=True)["pass"]
    assert be.score(create, "Done.", ["create_document"], [], [])["failed"] == ["no_editor"]
    assert be.score(create, "Here it is: ...", [], [], [], editor=False)["failed"] == [
        "no_tool", "no_editor"]
    private = {"id": "d", "kind": "documents", "search": True}
    assert be.score(private, "ok", [], [], ["Plot 17B"])["failed"] == ["no_search"]
    leak = be.score(private, "ok", ["web_search"], ["slugs Plot 17B"], ["Plot 17B"])
    assert leak["failed"] == ["private_term_in_query"]


def test_first_text_like_chat_js():
    ft = be.FirstText(start=0.0)
    for now, ev in [(0.5, {"type": "turn_mode"}), (1.0, {"delta": "hm", "thinking": True}),
                    (2.0, {"type": "tool_start", "tool": "web_search"}),
                    (2.5, {"delta": "<think>plan"}), (3.0, {"delta": "</think>"}),
                    (4.0, {"delta": "It will rain."}), (5.0, {"delta": " Bring a coat."})]:
        ft.event(ev, now=now)
    assert ft.seconds == 4.0
    ft = be.FirstText(start=0.0)
    ft.event({"type": "final_response", "content": "  "}, now=1.0)
    ft.event({"type": "final_response", "content": "Yes."}, now=2.0)
    assert ft.seconds == 2.0


def test_summary_table():
    rows = [{"kind": "web", "pass": True, "first_text_s": 2.0, "total_s": 9.0, "leaks": []},
            {"kind": "web", "pass": False, "first_text_s": None, "total_s": 11.0,
             "leaks": ["Orla"]},
            {"kind": "knowledge", "pass": True, "first_text_s": 1.0, "total_s": 1.5}]
    out = be.summary(rows)
    assert "knowledge    1/1" in out and "web          1/2" in out and "all          2/3" in out
    assert "private terms in search queries: Orla" in out


# ------------------------------------------------------------------ seeding
def test_seed_is_owner_scoped_and_idempotent():
    import core.database as cdb

    seed = be.load_seed()
    with be.throwaway_odysseus() as client:
        first = be.seed(client, seed, TODAY)
        assert (first["events_created"], first["contacts_created"], first["notes_created"]) == (
            3, 2, 1)
        again = be.seed(client, seed, TODAY)
        assert again["events_kept"] == 3 and again["contacts_kept"] == 2
        assert again["notes_kept"] == 1 and again["events_created"] == 0
        assert (first["documents_created"], again["documents_kept"]) == (1, 1)
        assert first["library_unavailable"] == 2  # the throwaway hub has no library
        with client.session_factory() as db:
            events = db.query(cdb.CalendarEvent).all()
            owners = {e.calendar.owner for e in events}
            starts = sorted(e.dtstart.isoformat() for e in events)
            notes = db.query(cdb.Note).all()
        assert owners == {be.EVAL_OWNER} and {n.owner for n in notes} == {be.EVAL_OWNER}
        with client.session_factory() as db:
            docs = [(d.title, d.owner) for d in db.query(cdb.Document).all()]
        assert docs == [("Allotment plan 2026", be.EVAL_OWNER)]
        assert starts[0] == "2026-11-01T15:00:00"
        contacts = client.get("/api/contacts/list").json()["contacts"]
        assert sorted(c["name"] for c in contacts) == ["Orla Penhallow", "Tobiah Quenby"]
        # a run on another day moves the events to that day's dates
        moved = be.seed(client, seed, TODAY + dt.timedelta(days=1))
        assert moved["events_removed"] == 3 and moved["events_created"] == 3
        with client.session_factory() as db:
            assert min(e.dtstart for e in db.query(cdb.CalendarEvent).all()).isoformat() == (
                "2026-11-02T15:00:00")


def test_dry_run_passes(capsys):
    import src.constants  # noqa: F401  (loaded: dry_run leaves the env alone)

    assert be.dry_run(be.load_questions(), be.load_seed()) == 0
    assert "dry run: ok" in capsys.readouterr().out


# ------------------------------------------------------------------ end to end (fake hub)
def _fake_odysseus(searx_base: str, query: str):
    """A fake /api/chat_stream: the first call searches the fake SearXNG with ``query``
    and asks for a tool approval; the approved call streams the answer."""
    state = {"calls": 0, "answer": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/default-chat":
            return httpx.Response(200, json={"endpoint_id": "e1", "model": "m"})
        if path == "/api/session":
            return httpx.Response(200, json={"id": "s1"})
        if path == "/api/session/s1/export":
            return httpx.Response(200, json={"messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": state["answer"]}]})
        if path == "/api/chat_stream":
            state["calls"] += 1
            form = dict(httpx.QueryParams(request.content.decode()))
            if state["calls"] == 1:
                assert form["allow_web_search"] == "true" and form["mode"] == "agent"
                httpx.get(f"{searx_base}/search", params={"q": query, "format": "json"})
                events = [{"type": "tool_start", "tool": "web_search"},
                          {"type": "tool_output", "tool": "web_search"},
                          {"type": "ask_user", "data": {"kind": "tool_approval",
                                                        "approval_id": "a1"}}]
            else:
                assert form["tool_approval_id"] == "a1"
                state["answer"] = ("Lanternfall Festival, Harbour Square "
                                   f"({searx_base}/page/lanternfall-festival)")
                events = [{"delta": "thinking", "thinking": True},
                          {"delta": "Lanternfall Festival, "},
                          {"delta": "Harbour Square"},
                          {"type": "final_response", "content": state["answer"]}]
            body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    return httpx.Client(base_url="http://hub", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("query,leaks", [("Velmora events festival", []),
                                         ("things to do with Tobiah Quenby", ["Tobiah", "Quenby"])])
def test_run_question_end_to_end(searx, query, leaks):
    base, *_ = searx
    client = _fake_odysseus(base, query)
    row = be.run_question(client, _q("m5"), be.SearxQueries(base),
                          be.load_seed()["private_terms"], mode="agent", preset="")
    assert row["queries"] == [query] and row["tools"] == ["web_search"]
    assert row["approvals"] == 1 and row["first_text_s"] is not None
    assert row["leaks"] == leaks and row["pass"] is (not leaks)
    assert row["cited"] == ["lanternfall-festival"]
