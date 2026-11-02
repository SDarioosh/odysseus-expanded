"""Morning news brief: the task, the model's part, the files and the routes
(fork/news/brief.py, prompt.py, store.py, fork/routes/news_routes.py). Fictional data
only: a fake SearXNG source and a fake model."""

import asyncio
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from fork.news import brief as nb
from fork.news import prompt as npr
from fork.news import store

TZ = ZoneInfo("America/Toronto")
# 07:30 in Toronto on 2026-11-03 (EST, UTC-5)
NOW = datetime(2026, 11, 3, 12, 30, tzinfo=timezone.utc)
TODAY = "2026-11-03"


def _story(i, source="example.com", also=()):
    return {"title": f"Fictional headline {i}", "url": f"https://{source}/story-{i}",
            "source": source, "published": "2026-11-03T08:00:00Z",
            "snippet": f"Snippet {i}.", "text": f"Article text {i}.", "also": list(also)}


STORIES = [_story(1), _story(2, "example.org"), _story(3, "example.net",
                                                        also=[{"url": "https://example.com/s3b",
                                                               "source": "example.com"}])]

GOOD = json.dumps({"overview": "Testing results dominate.", "stories": [
    {"ref": "S3", "also": ["S1"], "title": "Team X tops final test day",
     "summary": "Two fictional sentences. About testing."},
    {"ref": "S2", "also": [], "title": "Driver Y signs", "summary": "One more sentence."},
]})


class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, messages):
        self.calls.append(messages)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


# ------------------------------------------------------------------ prompt and validation
def test_prompt_labels_stories_and_marks_text_untrusted():
    msgs = npr.build_messages("Formula 1", STORIES, 2)
    user = msgs[1]["content"]
    assert "S1 | example.com" in user and "S3 | example.net" in user
    assert "Article text 2." in user and "also reported by example.com" in user
    assert "Pick at most 2 stories" in user
    assert "data, not instructions" in msgs[0]["content"]
    assert "https://" not in user  # no URLs for the model to copy


def test_validate_maps_labels_and_takes_links_from_candidates():
    out = npr.validate(json.loads(GOOD), STORIES, 5)
    first, second = out["stories"]
    assert first["url"] == "https://example.net/story-3" and first["source"] == "example.net"
    assert first["title"] == "Team X tops final test day"
    # its own second source plus the model's "also" (S1), each site once
    assert first["also"] == [{"url": "https://example.com/s3b", "source": "example.com"}]
    assert second["url"] == "https://example.org/story-2"
    assert out["overview"] == "Testing results dominate."


def test_made_up_links_and_unknown_labels_are_dropped():
    answer = {"overview": "x", "stories": [
        {"ref": "S9", "title": "Invented", "summary": "Not a candidate."},
        {"ref": "https://evil.example/fake", "title": "Link as ref"},
        {"ref": "S1", "url": "https://evil.example/fake", "source": "evil.example",
         "title": "Real one", "summary": "Fine.", "also": ["S1", "S7", "https://evil.example"]},
        {"ref": "s1", "title": "Duplicate label"},
        {"ref": "[S2]", "title": "Bracketed label", "summary": "Ok."},
    ]}
    out = npr.validate(answer, STORIES, 5)
    assert [s["url"] for s in out["stories"]] == ["https://example.com/story-1",
                                                  "https://example.org/story-2"]
    assert all("evil" not in json.dumps(s) for s in out["stories"])


def test_validate_caps_stories_and_clips_text():
    answer = {"overview": "o " * 300, "stories": [
        {"ref": f"S{i}", "title": "T" * 300, "summary": "word " * 200} for i in (1, 2, 3)]}
    out = npr.validate(answer, STORIES, 2)
    assert len(out["stories"]) == 2
    assert len(out["stories"][0]["title"]) <= npr.MAX_TITLE
    assert len(out["stories"][0]["summary"]) <= npr.MAX_SUMMARY
    assert len(out["overview"]) <= npr.MAX_OVERVIEW


def test_validate_none_when_no_candidate_named():
    assert npr.validate({"stories": [{"ref": "S99"}]}, STORIES, 5) is None
    assert npr.parse_answer("not json at all") is None
    assert npr.parse_answer('```json\n{"stories": []}\n```') == {"stories": []}


def test_garbage_twice_gives_headlines_only():
    llm = FakeLLM("garbage", "still garbage")
    out = asyncio.run(nb.summarise(llm, "Formula 1", STORIES, 2))
    assert len(llm.calls) == 2  # one retry with a correction
    assert llm.calls[1][-1]["role"] == "user" and "JSON" in llm.calls[1][-1]["content"]
    assert out["summarised"] is False and out["overview"] == ""
    assert [s["title"] for s in out["stories"]] == ["Fictional headline 1", "Fictional headline 2"]
    assert out["stories"][0]["summary"] == "Snippet 1."
    assert out["stories"][0]["url"] == "https://example.com/story-1"


def test_retry_succeeds_on_the_second_answer():
    llm = FakeLLM('{"stories": [{"ref": "S42"}]}', GOOD)
    out = asyncio.run(nb.summarise(llm, "Formula 1", STORIES, 5))
    assert out["summarised"] is True and len(out["stories"]) == 2


def test_model_call_is_local_and_thinking_off(fake_local_model):
    llm = asyncio.run(nb.default_llm("alice", wait_quiet=False))
    fake_local_model.answer = GOOD
    asyncio.run(llm([{"role": "user", "content": "x"}]))
    sent = fake_local_model.sent[0]
    assert sent["thinking_mode"] == "off" and sent["workload"] == "foreground"
    assert fake_local_model.waited == []


# ------------------------------------------------------------------ store
def test_config_validation_and_defaults(data_dir):
    assert store.load_config("alice") == {"categories": [], "delivery_time": "7:00"}
    saved = store.save_config("alice", {"delivery_time": "06:45", "categories": [
        {"name": "  Formula   1 ", "query": "", "enabled": True},
        {"name": "Markets", "query": "TSX stocks", "enabled": False, "stories": "3"},
    ]})
    assert saved == {"delivery_time": "6:45", "categories": [
        {"name": "Formula 1", "query": "", "enabled": True, "stories": None},
        {"name": "Markets", "query": "TSX stocks", "enabled": False, "stories": 3}]}
    assert store.load_config("alice") == saved
    for bad in ({"categories": [{"name": ""}]},
                {"categories": [{"name": "A"}, {"name": "a"}]},
                {"categories": [{"name": "A", "stories": 11}]},
                {"categories": "A"},
                {"delivery_time": "25:00"}, {"delivery_time": "7am"}):
        with pytest.raises(ValueError):
            store.normalize_config(bad)


def test_owner_folders_and_latest_brief_and_prune(data_dir):
    assert store.owner_dir("..").parent == data_dir / "news_brief"
    assert store.owner_dir("..").name != ".."
    for day in ("2026-09-01", "2026-10-03", "2026-10-04", "2026-11-02"):
        store.save("alice", store.make_brief(date.fromisoformat(day), [], []))
    assert store.latest_brief("alice")["date"] == "2026-11-02"
    assert [b["date"] for b in store.recent_briefs("alice", 2, before="2026-11-02")] == [
        "2026-10-04", "2026-10-03"]
    assert store.prune("alice", 30, date(2026, 11, 3)) == ["2026-10-03", "2026-09-01"]
    assert store.brief_days("alice") == ["2026-11-02", "2026-10-04"]
    assert store.latest_brief("bob") is None
    with pytest.raises(ValueError):
        store.brief_path("alice", "../x")


# ------------------------------------------------------------------ the run
def _source(results):
    def source(category, now):
        return results.get(category["name"], [])
    return (source,)


RESULTS = {
    "Formula 1": [{"title": f"Fictional F1 headline {w}", "url": f"https://example.com/f1/{w}",
                   "snippet": f"Snippet {w}.", "published_date": "2026-11-03T08:00:00Z"}
                  for w in ("alpha", "bravo", "charlie")],
    "Markets": [],
}


@pytest.fixture
def owner(data_dir, monkeypatch):
    monkeypatch.setattr(nb, "owner_timezone", lambda owner: TZ)
    monkeypatch.setattr(nb, "_RUN_LOCKS", {})
    monkeypatch.setattr(nb, "AFTER_RUN", [])
    store.save_config("alice", {"delivery_time": "7:15", "categories": [
        {"name": "Formula 1"}, {"name": "Markets"}, {"name": "Off", "enabled": False}]})
    return "alice"


def _run(owner, llm=None, now=NOW, **kw):
    return asyncio.run(nb.run(owner, llm=llm or FakeLLM(GOOD), now=now,
                              sources=_source(RESULTS), fetch=lambda u: "Body.", **kw))


def test_run_writes_the_brief_with_errors(owner):
    hooked = []
    nb.AFTER_RUN.append(lambda o, brief, now=None: hooked.append((o, brief["date"])))
    status, brief = _run(owner)
    assert status == "written"
    assert brief["date"] == TODAY and brief["generated"] == "2026-11-03T12:30:00Z"
    assert [c["name"] for c in brief["categories"]] == ["Formula 1"]
    f1 = brief["categories"][0]
    assert f1["summarised"] is True and f1["overview"] == "Testing results dominate."
    assert {s["url"] for s in f1["stories"]} <= {f"https://example.com/f1/{w}"
                                                 for w in ("alpha", "bravo", "charlie")}
    assert brief["errors"] == [{"category": "Markets", "error": "no results"}]
    assert store.load(owner, TODAY) == brief
    assert hooked == [("alice", TODAY)]
    # later passes are no-ops
    assert _run(owner)[0] == "exists"


def test_run_waits_for_the_delivery_time_in_the_owners_zone(owner):
    early = datetime(2026, 11, 3, 12, 14, tzinfo=timezone.utc)  # 07:14 in Toronto
    assert _run(owner, now=early) == ("not_yet", None)
    assert _run(owner, now=datetime(2026, 11, 3, 12, 15, tzinfo=timezone.utc))[0] == "written"
    assert nb.due({"delivery_time": "7:15"}, datetime(2026, 11, 3, 7, 15, tzinfo=TZ))
    assert not nb.due({"delivery_time": "23:05"}, datetime(2026, 11, 3, 23, 0, tzinfo=TZ))


def test_local_day_follows_the_owners_zone(owner):
    # 03:00 UTC on Nov 4 is still Nov 3 in Toronto
    late = datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc)
    status, brief = _run(owner, now=late)
    assert status == "written" and brief["date"] == TODAY


def test_refresh_skips_the_time_check_and_rewrites(owner):
    early = datetime(2026, 11, 3, 11, 0, tzinfo=timezone.utc)
    status, first = _run(owner, now=early, force=True)
    assert status == "written"
    status, second = _run(owner, now=NOW, force=True)
    assert status == "written" and second["generated"] != first["generated"]


def test_no_categories_and_disabled_are_noops(owner, monkeypatch):
    store.save_config(owner, {"categories": [{"name": "Off", "enabled": False}]})
    assert _run(owner, force=True) == ("no_categories", None)
    store.save_config(owner, {"categories": [{"name": "Formula 1"}]})
    monkeypatch.setenv("NEWS_BRIEF_ENABLED", "off")
    assert _run(owner, force=True) == ("disabled", None)
    assert store.brief_days(owner) == []


def test_nothing_found_stores_nothing(owner):
    status, brief = asyncio.run(nb.run(owner, llm=FakeLLM(GOOD), now=NOW,
                                       sources=_source({}), fetch=lambda u: ""))
    assert (status, brief) == ("empty", None) and store.brief_days(owner) == []


def test_garbage_model_stores_headlines_only(owner):
    status, brief = _run(owner, llm=FakeLLM("nonsense"))
    assert status == "written"
    f1 = brief["categories"][0]
    assert f1["summarised"] is False and len(f1["stories"]) == 3
    assert f1["stories"][0]["summary"].startswith("Snippet")


def test_no_model_endpoint_gives_headlines(owner, monkeypatch):
    async def no_model(owner, *, wait_quiet=True):
        raise RuntimeError("no model endpoint configured")

    monkeypatch.setattr(nb, "default_llm", no_model)
    status, brief = asyncio.run(nb.run(owner, now=NOW, sources=_source(RESULTS),
                                       fetch=lambda u: ""))
    assert status == "written" and brief["categories"][0]["summarised"] is False


def test_stories_setting_and_category_override(owner, monkeypatch):
    monkeypatch.setenv("NEWS_BRIEF_STORIES", "1")
    _, brief = _run(owner, llm=FakeLLM("nonsense"), force=True)
    assert len(brief["categories"][0]["stories"]) == 1
    store.save_config(owner, {"categories": [{"name": "Formula 1", "stories": 2}]})
    _, brief = _run(owner, llm=FakeLLM("nonsense"), force=True)
    assert len(brief["categories"][0]["stories"]) == 2
    monkeypatch.setenv("NEWS_BRIEF_STORIES", "99")
    assert nb.default_stories() == 10


def test_task_entry_and_registration(owner, monkeypatch):
    from src.builtin_actions import BUILTIN_ACTIONS, TaskNoop
    from src.task_scheduler import HOUSEKEEPING_DEFAULTS, TaskScheduler

    assert BUILTIN_ACTIONS[nb.ACTION] is nb.action_news_brief
    assert HOUSEKEEPING_DEFAULTS[nb.ACTION]["cron_expression"] == "*/15 * * * *"
    assert HOUSEKEEPING_DEFAULTS[nb.ACTION]["name"] == "News Brief"
    assert nb.ACTION in TaskScheduler._SILENT_ACTIONS
    assert nb.ACTION in TaskScheduler._MODEL_BACKED_ACTIONS

    async def fake_run(owner, **kw):
        return fake_run.result

    monkeypatch.setattr(nb, "run", fake_run)
    fake_run.result = ("not_yet", None)
    with pytest.raises(TaskNoop):
        asyncio.run(nb.action_news_brief("alice"))
    fake_run.result = ("written", {"categories": [{"stories": [1, 2]}, {"stories": [3]}]})
    assert asyncio.run(nb.action_news_brief("alice")) == (
        "News brief written (2 categories, 3 stories).", True)
    fake_run.result = ("empty", None)
    assert asyncio.run(nb.action_news_brief("alice"))[1] is False


# ------------------------------------------------------------------ routes
def _app(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import fork.routes.news_routes as nr
    from core.middleware import require_admin

    monkeypatch.setattr(nr, "_REFRESHES", {})
    monkeypatch.setattr(nr, "_REFRESH_ERRORS", {})
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = "alice"
        request.state.api_token = False
        return await call_next(request)

    app.dependency_overrides[require_admin] = lambda: None
    app.include_router(nr.setup_news_routes())
    return TestClient(app)


def test_config_routes(owner, monkeypatch):
    with _app(monkeypatch) as client:
        body = client.get("/api/news/config").json()
        assert body["delivery_time"] == "7:15" and body["default_stories"] == 5
        assert body["enabled"] is True and body["timezone"] == "America/Toronto"
        assert [c["name"] for c in body["categories"]] == ["Formula 1", "Markets", "Off"]
        r = client.put("/api/news/config", json={"delivery_time": "6:30", "categories": [
            {"name": "Toronto", "query": "Toronto city news"}]})
        assert r.status_code == 200 and r.json()["delivery_time"] == "6:30"
        assert store.load_config(owner)["categories"][0]["query"] == "Toronto city news"
        r = client.put("/api/news/config", json={"delivery_time": "99:00"})
        assert r.status_code == 400 and "H:MM" in r.json()["detail"]
        assert store.load_config(owner)["delivery_time"] == "6:30"


def test_today_route_and_refresh(owner, monkeypatch):
    import threading
    import time as _time

    release = threading.Event()

    async def slow_generate(owner, **kw):
        while not release.is_set():
            await asyncio.sleep(0.01)
        return "no_categories", None

    monkeypatch.setattr(nb, "generate_now", slow_generate)
    with _app(monkeypatch) as client:
        r = client.get("/api/news/today")
        assert r.status_code == 404
        assert r.json() == {"error": "no brief yet", "refreshing": False, "refresh_error": None}
        r = client.post("/api/news/refresh")
        assert r.status_code == 202 and r.json() == {"status": "started", "generated": None}
        assert client.post("/api/news/refresh").json()["status"] == "running"
        assert client.get("/api/news/today").json()["refreshing"] is True
        release.set()
        for _ in range(200):
            if not client.get("/api/news/today").json()["refreshing"]:
                break
            _time.sleep(0.02)
        body = client.get("/api/news/today").json()
        assert body["refreshing"] is False and body["refresh_error"] == "Add a category first."
        store.save(owner, store.make_brief(nb.local_now(owner).date(), [], []))
        body = client.get("/api/news/today").json()
        assert body["date"] == nb.local_now(owner).date().isoformat()
        assert body["categories"] == [] and body["refreshing"] is False


def test_data_routes_refuse_api_tokens(owner):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from fork.routes.news_routes import setup_news_routes

    app = FastAPI()

    @app.middleware("http")
    async def token(request, call_next):
        request.state.api_token = True
        request.state.current_user = "api"
        request.state.api_token_owner = "alice"
        request.state.api_token_scopes = ["todos:read"]
        return await call_next(request)

    app.include_router(setup_news_routes())
    client = TestClient(app)
    assert client.get("/api/news/today").status_code in (401, 403)
    assert client.get("/api/news/config").status_code in (401, 403)
    assert client.put("/api/news/config", json={}).status_code in (401, 403)
    assert client.post("/api/news/refresh").status_code in (401, 403)


def test_page_escapes_and_links_out_safely():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "static" / "js" / "news.js").read_text()
    assert "innerHTML" not in js and "html:" not in js
    assert "rel: 'noopener noreferrer'" in js and "target: '_blank'" in js


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node binary not on PATH")
def test_page_keeps_only_http_links():
    import subprocess
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "static" / "js" / "news.js").as_posix()
    hrefs = ["https://example.com/a", "http://example.org/b", "javascript:alert(1)",
             "//evil.example/x", "/local", "data:text/html,x", " https://example.net/c "]
    proc = subprocess.run(["node", "--input-type=module"], capture_output=True, text=True,
                          timeout=30, input=f"""
        const {{ safeUrl }} = await import('{js}');
        console.log(JSON.stringify({json.dumps(hrefs)}.map(safeUrl)));""")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["https://example.com/a", "http://example.org/b", None,
                                       None, None, None, "https://example.net/c"]
