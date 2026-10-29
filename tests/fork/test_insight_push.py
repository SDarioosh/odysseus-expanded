"""Today insight push (src/insight_push.py). Fictional data only."""

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src import insight_outcomes, insight_push
from src import today_insight as ti

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 22, 7, 0, tzinfo=timezone.utc)  # 09:00 in Berlin
DAY = "2026-10-22"


def _insight(**kw):
    base = {"date": DAY, "headline": "A busy Thursday.",
            "focus": [{"ref": "todo:n1:0", "why": "due today", "title": "Send the slides"}],
            "coming_up": [], "also": []}
    return {**base, **kw}


class Sender:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    async def __call__(self, owner, title, body, key):
        self.calls.append((owner, title, body, key))
        if isinstance(self.ok, Exception):
            raise self.ok
        return self.ok


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ti, "owner_timezone", lambda owner: TZ)
    monkeypatch.setenv("TODAY_INSIGHT_PUSH", "on")
    monkeypatch.setenv("MODULE_NOTIFY_CHANNEL", "ntfy")
    monkeypatch.setenv("MODULE_DIGEST_HOUR", "8")
    # the outcome log's push state, faked
    state = {"pushed": {}, "tries": {}}
    monkeypatch.setattr(insight_outcomes, "pushed",
                        lambda owner, day: state["pushed"].get((owner, day)), raising=False)

    def mark(owner, day, *, now=None):
        state["pushed"][(owner, day)] = "2026-10-22T07:00:00Z"

    def note(owner, day):
        state["tries"][(owner, day)] = state["tries"].get((owner, day), 0) + 1
        return state["tries"][(owner, day)]

    monkeypatch.setattr(insight_outcomes, "mark_pushed", mark, raising=False)
    monkeypatch.setattr(insight_outcomes, "push_tries",
                        lambda owner, day: state["tries"].get((owner, day), 0), raising=False)
    monkeypatch.setattr(insight_outcomes, "note_push_try", note, raising=False)
    monkeypatch.setattr(insight_outcomes, "load", lambda owner, day: None, raising=False)
    return state


def _run(insight, now=NOW, sender=None):
    return asyncio.run(insight_push.maybe_push("alice", insight, now=now, push=sender))


def test_pushes_once_with_headline_and_first(env):
    sender = Sender()
    assert _run(_insight(), sender=sender) is True
    assert sender.calls == [("alice", "Today", "A busy Thursday.\nFirst: Send the slides",
                             f"today-insight:{DAY}")]
    assert _run(_insight(), sender=sender) is False  # never twice
    assert len(sender.calls) == 1


def test_off_by_default(env, monkeypatch):
    monkeypatch.delenv("TODAY_INSIGHT_PUSH")
    sender = Sender()
    assert _run(_insight(), sender=sender) is False
    assert sender.calls == []


def test_channel_off_and_digest_hour_off(env, monkeypatch):
    sender = Sender()
    monkeypatch.setenv("MODULE_NOTIFY_CHANNEL", "off")
    assert _run(_insight(), sender=sender) is False
    monkeypatch.setenv("MODULE_NOTIFY_CHANNEL", "ntfy")
    monkeypatch.setenv("MODULE_DIGEST_HOUR", "off")
    assert _run(_insight(), sender=sender) is False
    assert sender.calls == []


def test_waits_for_the_digest_hour(env, monkeypatch):
    sender = Sender()
    monkeypatch.setenv("MODULE_DIGEST_HOUR", "10")
    assert _run(_insight(), sender=sender) is False  # 09:00 local
    assert _run(_insight(), now=datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc),
                sender=sender) is True  # 10:00 local


def test_not_the_empty_insight_nor_another_day(env):
    sender = Sender()
    empty = {"date": DAY, "headline": ti.EMPTY_HEADLINE, "focus": [], "coming_up": [],
             "also": []}
    assert _run(empty, sender=sender) is False
    assert _run(_insight(date="2026-10-21"), sender=sender) is False
    assert sender.calls == []


def test_no_first_line_without_focus(env):
    sender = Sender()
    assert _run(_insight(focus=[]), sender=sender) is True
    assert sender.calls[0][2] == "A busy Thursday."


def test_owner_order_picks_the_first(env, monkeypatch):
    def order(insight, outcome):
        out = dict(insight)
        out["focus"] = [{"ref": "event:e1", "title": "Dentist"}, *insight["focus"]]
        return out

    monkeypatch.setattr(insight_outcomes, "apply_owner_order", order)
    sender = Sender()
    _run(_insight(), sender=sender)
    assert sender.calls[0][2].endswith("First: Dentist")


def test_failed_send_retries_at_most_three_times(env):
    sender = Sender(ok=False)
    for _ in range(5):
        assert _run(_insight(), sender=sender) is False
    assert len(sender.calls) == 3
    assert not env["pushed"]


def test_send_error_counts_as_a_try_and_success_marks(env):
    sender = Sender(ok=RuntimeError("ntfy down"))
    assert _run(_insight(), sender=sender) is False
    sender.ok = True
    assert _run(_insight(), sender=sender) is True
    assert env["pushed"][("alice", DAY)]
    assert _run(_insight(), sender=sender) is False
    assert len(sender.calls) == 2


def test_state_lives_in_the_real_outcome_log(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ti, "owner_timezone", lambda owner: TZ)
    monkeypatch.setenv("TODAY_INSIGHT_PUSH", "on")
    monkeypatch.setenv("MODULE_NOTIFY_CHANNEL", "ntfy")
    monkeypatch.setenv("MODULE_DIGEST_HOUR", "8")
    sender = Sender(ok=False)
    for _ in range(4):
        _run(_insight(), sender=sender)
    assert len(sender.calls) == 3  # at most 3 tries a day
    assert insight_outcomes.push_tries("alice", DAY) == 3
    sender.ok = True
    tomorrow = datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)
    assert _run(_insight(date="2026-10-23"), now=tomorrow, sender=sender) is True
    assert insight_outcomes.pushed("alice", "2026-10-23")
    assert _run(_insight(date="2026-10-23"), now=tomorrow, sender=sender) is False
    assert not (tmp_path / "today_insight" / "alice" / "push.json").exists()


def test_after_run_calls_the_push(env, monkeypatch):
    seen = []

    async def fake(owner, insight, *, now=None, push=None):
        seen.append((owner, insight["date"]))
        return True

    monkeypatch.setattr(insight_push, "maybe_push", fake)
    asyncio.run(ti._after_run("alice", None, _insight(), NOW))
    assert seen == [("alice", DAY)]
