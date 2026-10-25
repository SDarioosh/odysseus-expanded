"""Web lookups when the web toggle is on (src/web_turn_scope.py)."""
import json

import pytest

from src.agent_runtime.authority import ExactOperation, create_request_authority
from src.agent_runtime.remote_resources import bind_backend_for_operation
from src.web_turn_scope import (
    with_unclassified_web_search,
)

SEARCH = ExactOperation.normalize("web_search", json.dumps({"query": "mistral 7b vs qwen3.5-9b"}))
FETCH = ExactOperation.normalize("web_fetch", json.dumps({"url": "https://example.com"}))


def tools(message):
    return {g.tool for g in create_request_authority(message, owner="alice", session_id="s").grants}


def authority(message):
    return create_request_authority(message, owner="alice", session_id="s")


# 1. Model comparisons and explicit web lookups route to web search.

@pytest.mark.parametrize("message", [
    "search mistral 7b vs qwen3.5-9b",
    "search the web for mistral 7b vs qwen3.5-9b",
    "search qwen 9b versus llama 8b benchmarks",
])
def test_model_comparison_gets_web_search(message):
    assert "web_search" in tools(message)


@pytest.mark.parametrize("message", [
    "find a small qwen model I can run locally",
    "search hugging face for a 7b mistral model",
])
def test_model_discovery_still_uses_hugging_face(message):
    granted = tools(message)
    assert "search_hf_models" in granted
    assert "web_search" not in granted


# 2. Web toggle on + no tool in the words → web_search only.

def test_unclassified_turn_gets_web_search_when_enabled():
    base = authority("mistral 7b vs qwen3.5-9b")
    assert not base.permits(SEARCH)
    auth = with_unclassified_web_search(base, web_search_enabled=True)
    assert auth.permits(SEARCH)
    assert not auth.permits(FETCH)
    assert bind_backend_for_operation(auth, SEARCH).resource.tool_id == "web_search"


def test_web_toggle_off_grants_nothing():
    base = authority("mistral 7b vs qwen3.5-9b")
    assert with_unclassified_web_search(base, web_search_enabled=False) is base


def test_turn_whose_words_chose_tools_is_unchanged():
    base = authority("what are my tasks for today?")
    assert with_unclassified_web_search(base, web_search_enabled=True) is base


def test_disables_still_win():
    auth = with_unclassified_web_search(authority("mistral 7b vs qwen3.5-9b"), web_search_enabled=True)
    assert not auth.restrict(disabled_tools={"web_search"}).permits(SEARCH)
    assert with_unclassified_web_search(None, web_search_enabled=True) is None


# 3. A turn that may search may fetch the pages its search returned.

from src.tool_capabilities import ToolRunSecurityContext  # noqa: E402
from src.web_turn_scope import with_search_hit_fetch  # noqa: E402

HITS = [
    {"url": "https://fanttik.com/products/fanttik-e1-max-precision-electric-screwdriver", "title": "E1 Max"},
    {"url": "https://www.amazon.com/Fanttik-Screwdriver-Precision-E1-PRO/dp/B097BJ8WP7?th=1", "title": "E1 Pro"},
]


def fetch(url, **extra):
    return ExactOperation.normalize("web_fetch", json.dumps({"url": url, **extra}))


def searched(hits=HITS):
    ctx = ToolRunSecurityContext()
    ctx.observe_tool_result("web_search", {
        "output": "results\n\n<!-- SOURCES:" + json.dumps(hits) + " -->", "exit_code": 0})
    return ctx


def search_turn():
    return with_unclassified_web_search(
        authority("Compare the fanatik nex e1 pro and e1 max"), web_search_enabled=True)


@pytest.mark.parametrize("url", [
    "https://fanttik.com/products/fanttik-e1-max-precision-electric-screwdriver",
    "https://www.fanttik.com/products/fanttik-e1-max-precision-electric-screwdriver/",
    "https://www.amazon.com/Fanttik-Screwdriver-Precision-E1-PRO/dp/B097BJ8WP7?th=1",
    # The clean product page on a hit's site.
    "https://www.amazon.com/Fanttik-Screwdriver-Precision-E1-PRO/dp/B097BJ8WP7",
    "https://fanttik.com/products/fanttik-e2-ultra",
])
def test_search_turn_may_fetch_search_hits(url):
    op = fetch(url)
    auth, admitted = with_search_hit_fetch(search_turn(), op, searched())
    assert admitted and auth.permits(op)
    assert bind_backend_for_operation(auth, op).resource.tool_id == "web_fetch"
    # The grant covers that exact call only.
    assert not auth.permits(fetch("https://fanttik.com/products/other"))


@pytest.mark.parametrize("url", [
    "https://attacker.example/collect",                 # a site no search returned
    "https://fanttik.com/products/x?secret=data",       # query string on a hit's site
    "https://fanttik.com/" + "a" * 300,                 # long path on a hit's site
    "http://127.0.0.1/products",                        # not a public host
])
def test_search_turn_may_not_fetch_other_urls(url):
    base = search_turn()
    auth, admitted = with_search_hit_fetch(base, fetch(url), searched())
    assert not admitted and auth is base


def test_batch_needs_every_url_to_be_a_hit():
    good = HITS[0]["url"]
    ok = ExactOperation.normalize("web_fetch", json.dumps({"urls": [good, HITS[1]["url"]]}))
    bad = ExactOperation.normalize("web_fetch", json.dumps({"urls": [good, "https://evil.example/x"]}))
    assert with_search_hit_fetch(search_turn(), ok, searched())[1]
    assert not with_search_hit_fetch(search_turn(), bad, searched())[1]


def test_links_inside_results_dont_count():
    ctx = ToolRunSecurityContext()
    ctx.observe_tool_result("web_fetch", {"output": "see https://evil.example/page", "exit_code": 0})
    ctx.observe_tool_result("web_search", {"output": "text https://evil.example/x", "exit_code": 0})
    assert not with_search_hit_fetch(search_turn(), fetch("https://evil.example/page"), ctx)[1]


def test_no_search_grant_or_disabled_fetch_keeps_denial():
    op = fetch(HITS[0]["url"])
    no_search = authority("what are my tasks for today?")
    assert not with_search_hit_fetch(no_search, op, searched())[1]
    fetch_off = search_turn().restrict(disabled_tools={"web_fetch"})
    assert not with_search_hit_fetch(fetch_off, op, searched())[1]
    search_off = search_turn().restrict(disabled_tools={"web_search"})
    assert not with_search_hit_fetch(search_off, op, searched())[1]
    assert not with_search_hit_fetch(search_turn(), op, None)[1]
    assert not with_search_hit_fetch(search_turn(), SEARCH, searched())[1]


def test_execute_tool_block_runs_fetch_of_search_hit(monkeypatch):
    import asyncio

    from src import tool_execution
    from src.tool_types import ToolBlock

    calls = []

    async def fake_impl(block, **kwargs):
        calls.append(block.content)
        return "web_fetch", {"output": "page", "exit_code": 0}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", fake_impl)
    from src import auto_mode
    monkeypatch.setattr(auto_mode, "enabled", lambda: True)

    def run(url, ctx):
        auto_mode.bind_run(ctx, owner="alice", session_id="s")
        return asyncio.run(tool_execution.execute_tool_block(
            ToolBlock("web_fetch", json.dumps({"url": url})),
            owner="alice", session_id="s", security_context=ctx,
            request_authority=search_turn()))

    _, result = run(HITS[0]["url"], searched())
    assert result.get("exit_code") == 0, result
    assert len(calls) == 1
    _, result = run("https://attacker.example/collect", searched())
    assert result.get("failure_kind") == "request_authority_denied"
    assert len(calls) == 1
