"""Morning news brief push (fork/news/push.py, the hooks in fork/news/brief.py).
Fictional data only; the notify channel is faked."""

import asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from fork.news import brief as nb
from fork.news import push as npush
from fork.news import store

TZ = ZoneInfo("America/Toronto")
NOW = datetime(2026, 11, 3, 12, 30, tzinfo=timezone.utc)  # 07:30 in Toronto
DAY = "2026-11-03"


def _brief(day=DAY, categories=None):
    if categories is None:
        categories = [
            {"name": "Formula 1", "stories": [
                {"title": "Team X tops  final test day", "url": "https://example.com/a"},
                {"title": "Driver Y signs", "url": "https://example.com/b"}]},
            {"name": "Markets", "stories": []},
            {"name": "City", "stories": [{"title": "Council approves bike lanes",
                                          "url": "https://example.org/c"}]},
        ]
    return store.make_brief(date.fromisoformat(day), categories, [], now=NOW)


class Sender:
    def __init__(self, *results):
        self.results, self.calls = list(results) or [True], []

    async def __call__(self, owner, title, body, key):
        self.calls.append((owner, title, body, key))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def env(data_dir, monkeypatch):
    monkeypatch.setattr(nb, "owner_timezone", lambda owner: TZ)
    monkeypatch.setenv("NEWS_BRIEF_PUSH", "on")
    monkeypatch.setenv("MODULE_NOTIFY_CHANNEL", "ntfy")
    monkeypatch.setenv("MODULE_DIGEST_HOUR", "off")  # not waited for
    return "alice"


def _push(owner, brief, sender, now=NOW):
    return asyncio.run(npush.maybe_push(owner, brief, now=now, push=sender))


def test_message_is_the_top_headline_per_category():
    assert npush.message(_brief()) == ("Formula 1: Team X tops final test day\n"
                                       "City: Council approves bike lanes")
    long = _brief(categories=[{"name": f"Cat {i}", "stories": [{"title": "x" * 150}]}
                              for i in range(8)])
    body = npush.message(long)
    assert len(body) <= npush.MAX_BODY and body.count("\n") == 2  # three whole lines
    assert npush.message(_brief(categories=[{"name": "A", "stories": [{"title": "y" * 900}]}]
                                )) == ("A: " + "y" * 900)[:npush.MAX_BODY]


def test_off_by_default(env, monkeypatch):
    monkeypatch.delenv("NEWS_BRIEF_PUSH")
    sender = Sender()
    assert _push(env, _brief(), sender) is False and sender.calls == []


def test_channel_off_sends_nothing(env, monkeypatch):
    monkeypatch.setenv("MODULE_NOTIFY_CHANNEL", "off")
    sender = Sender()
    assert _push(env, _brief(), sender) is False and sender.calls == []


def test_sent_once_a_day(env):
    sender = Sender()
    assert _push(env, _brief(), sender) is True
    assert sender.calls == [("alice", "News", "Formula 1: Team X tops final test day\n"
                             "City: Council approves bike lanes", "news-brief:2026-11-03")]
    assert store.load_push_state(env, DAY) == {"pushed": "2026-11-03T12:30:00Z", "tries": 1}
    assert _push(env, _brief(), sender) is False
    assert len(sender.calls) == 1


def test_failed_send_is_retried_then_stops_at_max_tries(env):
    sender = Sender(False, RuntimeError("ntfy down"), True)
    assert _push(env, _brief(), sender) is False
    assert store.load_push_state(env, DAY) == {"pushed": None, "tries": 1}
    assert _push(env, _brief(), sender) is False  # an exception counts as a failed try
    assert _push(env, _brief(), sender) is True
    assert store.load_push_state(env, DAY)["tries"] == 3
    never = Sender(False)
    store.save_push_state(env, "2026-11-04", {"pushed": None, "tries": 0})
    tomorrow = datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc)
    for _ in range(5):
        _push(env, _brief("2026-11-04"), never, now=tomorrow)
    assert len(never.calls) == npush.MAX_TRIES
    assert store.load_push_state(env, "2026-11-04") == {"pushed": None, "tries": 3}


def test_empty_or_old_brief_is_not_pushed(env):
    sender = Sender()
    empty = _brief(categories=[{"name": "Markets", "stories": []}])
    assert _push(env, empty, sender) is False
    assert _push(env, _brief("2026-11-02"), sender) is False
    assert sender.calls == []
    assert store.load_push_state(env, DAY) == {"pushed": None, "tries": 0}


def test_the_run_pushes_after_writing_and_retries_on_later_passes(env, monkeypatch):
    sent = []
    results = [False, True]

    async def fake_push(owner, title, body, key):
        sent.append(key)
        return results.pop(0)

    from fork.athena import action_items as mai

    monkeypatch.setattr(mai, "push", fake_push)
    monkeypatch.setattr(nb, "_RUN_LOCKS", {})
    store.save_config(env, {"delivery_time": "7:15", "categories": [{"name": "City"}]})

    def source(category, now):
        return [{"title": "Council approves bike lanes downtown",
                 "url": "https://example.org/city/bike-lanes",
                 "published_date": "2026-11-03T08:00:00Z"}]

    def run():
        return asyncio.run(nb.run(env, llm=None, now=NOW, sources=(source,),
                                  fetch=lambda u: ""))

    assert run()[0] == "written"
    assert sent == ["news-brief:2026-11-03"]  # failed
    assert run()[0] == "exists"
    assert sent == ["news-brief:2026-11-03"] * 2  # retried, sent
    assert run()[0] == "exists"
    assert len(sent) == 2
    assert store.load_push_state(env, DAY)["pushed"] == "2026-11-03T12:30:00Z"


def test_push_files_are_pruned_with_the_briefs(data_dir):
    store.save("alice", _brief("2026-09-01"))
    store.save_push_state("alice", "2026-09-01", {"pushed": None, "tries": 1})
    store.save_push_state("alice", "2026-11-02", {"pushed": None, "tries": 1})
    assert store.prune("alice", 30, date(2026, 11, 3)) == ["2026-09-01"]
    assert not store.push_path("alice", "2026-09-01").exists()
    assert store.push_path("alice", "2026-11-02").exists()
    assert store.brief_days("alice") == []  # push files are not briefs
