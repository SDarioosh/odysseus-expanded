"""Routines seen in the calendar (src/insight_routines.py). Fictional data only."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src import evidence_intake as ledger
from src import insight_routines as ir

TZ = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 10, 22)  # a Thursday
TUESDAYS = [TODAY - timedelta(days=2 + 7 * w) for w in range(6)]  # 20 Oct back to 15 Sep


def _ev(day, hour, minute=0, summary="Gym", **kw):
    return {"summary": summary, "dtstart": datetime(day.year, day.month, day.day, hour, minute),
            **kw}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    return tmp_path


# ------------------------------------------------------------------------ the rule
def test_three_distinct_weeks_make_a_routine():
    found = ir.detect([_ev(d, 18) for d in TUESDAYS[:3]], TZ, TODAY)
    assert found == [{"id": "gym:1", "title": "Gym", "weekday": 1, "days": "Tuesdays",
                      "time": "18:00", "weeks": 3, "of_weeks": 6, "last": "2026-10-20"}]
    assert ir.detect([_ev(d, 18) for d in TUESDAYS[:2]], TZ, TODAY) == []


def test_starts_within_an_hour_of_the_median():
    events = [_ev(TUESDAYS[0], 18), _ev(TUESDAYS[1], 19), _ev(TUESDAYS[2], 17, 0),
              _ev(TUESDAYS[3], 21)]  # 21:00 is three hours off
    [r] = ir.detect(events, TZ, TODAY)
    assert r["weeks"] == 3 and r["time"] == "18:00"


def test_more_than_an_hour_off_is_left_out():
    off = [_ev(TUESDAYS[0], 18), _ev(TUESDAYS[1], 19, 1), _ev(TUESDAYS[2], 16, 59)]
    assert ir.detect(off, TZ, TODAY) == []
    edge = [_ev(TUESDAYS[0], 18), _ev(TUESDAYS[1], 19, 0), _ev(TUESDAYS[2], 17, 0)]
    assert ir.detect(edge, TZ, TODAY)[0]["weeks"] == 3


def test_recurring_all_day_cancelled_and_old_events_are_skipped():
    base = [_ev(d, 18) for d in TUESDAYS[:2]]
    for extra in (_ev(TUESDAYS[2], 18, rrule="FREQ=WEEKLY"),
                  _ev(TUESDAYS[2], 18, all_day=True),
                  _ev(TUESDAYS[2], 18, status="cancelled"),
                  _ev(TODAY - timedelta(days=44), 18),  # older than six weeks
                  _ev(TODAY + timedelta(days=5), 18)):  # next Tuesday: not seen yet
        assert ir.detect([*base, extra], TZ, TODAY) == []


def test_weekdays_are_grouped_apart():
    events = [_ev(d, 18) for d in TUESDAYS[:2]] + [_ev(d + timedelta(days=1), 18)
                                                    for d in TUESDAYS[2:4]]
    assert ir.detect(events, TZ, TODAY) == []  # two Tuesdays and two Wednesdays
    events += [_ev(TUESDAYS[4] + timedelta(days=1), 18)]
    [r] = ir.detect(events, TZ, TODAY)
    assert r["id"] == "gym:2" and r["days"] == "Wednesdays"


def test_summaries_are_normalized():
    events = [_ev(TUESDAYS[0], 18, summary="Gym!"), _ev(TUESDAYS[1], 18, summary=" gym "),
              _ev(TUESDAYS[2], 18, summary="GYM")]
    [r] = ir.detect(events, TZ, TODAY)
    assert r["id"] == "gym:1" and r["title"] == "Gym!"  # the latest as written


def test_two_in_one_week_count_once_and_utc_starts_are_local():
    events = [_ev(TUESDAYS[0], 18), _ev(TUESDAYS[0], 18, 30), _ev(TUESDAYS[1], 18)]
    assert ir.detect(events, TZ, TODAY) == []
    utc = [{"summary": "Gym", "dtstart": datetime(d.year, d.month, d.day, 16, 0,
                                                  tzinfo=timezone.utc)} for d in TUESDAYS[:3]]
    assert ir.detect(utc, TZ, TODAY)[0]["time"] == "18:00"  # CEST


def test_summary_text_is_short_and_ordered():
    events = [_ev(d, 18) for d in TUESDAYS[:4]] + [
        _ev(d + timedelta(days=-1), 7, 30, summary="Swim with Alice") for d in TUESDAYS[:3]]
    found = ir.detect(events, TZ, TODAY)
    text = ir.summary_text(found)
    assert text.splitlines() == ["- Gym: Tuesdays around 18:00 (4 of the last 6 weeks)",
                                 "- Swim with Alice: Mondays around 07:30 (3 of the last 6 weeks)"]
    many = [dict(found[0], title=f"Habit number {i}") for i in range(20)]
    assert len(ir.summary_text(many)) <= ir.MAX_SUMMARY
    assert ir.summary_text([]) == ""


# -------------------------------------------------------------- cache and evidence
def test_cached_daily_and_submitted_once_never_confirmed(store):
    events = [_ev(d, 18) for d in TUESDAYS[:4]]
    now = datetime(2026, 10, 22, 7, tzinfo=timezone.utc)
    found = ir.routines("alice", TODAY, now=now, events=events, tz=TZ)
    assert [r["id"] for r in found] == ["gym:1"]
    cache = (store / "today_insight" / "alice" / "routines.json")
    assert cache.is_file()
    [belief] = ledger.list_beliefs("alice")
    assert belief["status"] == "tentative"
    assert belief["about"] == "owner"
    assert belief["source"] == "routines"
    assert belief["link"] == "/#event-2026-10-20"
    assert belief["text"] == "Usually has Gym on Tuesdays around 18:00"
    # the same day: from the cache (events aren't even read)
    assert ir.routines("alice", TODAY, now=now, events=[], tz=TZ) == found
    # every later day for weeks: seen again, but never submitted again
    for i in range(1, 15):
        day = TODAY + timedelta(days=i)
        later = events + [_ev(d, 18) for d in TUESDAYS if d < day] + [
            _ev(TODAY + timedelta(days=5 + 7 * w), 18) for w in range(2)]
        ir.routines("alice", day, now=now + timedelta(days=i), events=later, tz=TZ)
    [belief] = ledger.list_beliefs("alice")
    assert belief["status"] == "tentative"
    assert len(belief["evidence"]) == 1
    assert belief["evidence"][0]["times"] == 1


def test_repeated_submission_alone_cannot_confirm(store):
    r = ir.detect([_ev(d, 18) for d in TUESDAYS[:4]], TZ, TODAY)
    for i in range(5):  # even if the same routine were submitted again and again
        ir.submit_new("alice", [dict(r[0], last=(TODAY + timedelta(days=7 * i)).isoformat())],
                      set(), now=datetime(2026, 10, 22 + i, tzinfo=timezone.utc))
    [belief] = ledger.list_beliefs("alice")
    assert belief["status"] == "tentative"


def test_owner_confirms_on_the_review_page(store):
    ir.routines("alice", TODAY, events=[_ev(d, 18) for d in TUESDAYS[:3]], tz=TZ)
    [belief] = ledger.list_beliefs("alice")
    assert ledger.confirm("alice", belief["id"])["status"] == "confirmed"
    assert ledger.apply_structure("alice", belief["id"], "routine", "Gym on Tuesdays 18:00")
    assert ledger.list_beliefs("alice", attribute="routine")[0]["value"] == \
        "Gym on Tuesdays 18:00"


def test_summary_reads_the_calendar_of_the_owner(store, monkeypatch):
    import core.database as cdb
    from tests.helpers.sqlite_db import make_temp_sqlite

    factory, engine, _tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr("src.today_insight.owner_timezone", lambda owner: TZ)
    try:
        with factory() as db:
            db.add(cdb.CalendarCal(id="cal-a", owner="alice", name="Personal"))
            db.add(cdb.CalendarCal(id="cal-b", owner="bob", name="Bob's"))
            for i, d in enumerate(TUESDAYS[:3]):
                db.add(cdb.CalendarEvent(uid=f"g{i}", calendar_id="cal-a", summary="Gym",
                                         dtstart=datetime(d.year, d.month, d.day, 16),
                                         dtend=datetime(d.year, d.month, d.day, 17),
                                         is_utc=True))
                db.add(cdb.CalendarEvent(uid=f"r{i}", calendar_id="cal-a", summary="Standup",
                                         dtstart=datetime(d.year, d.month, d.day, 9),
                                         dtend=datetime(d.year, d.month, d.day, 9, 15),
                                         rrule="FREQ=WEEKLY"))
                db.add(cdb.CalendarEvent(uid=f"b{i}", calendar_id="cal-b", summary="Bob's run",
                                         dtstart=datetime(d.year, d.month, d.day, 7),
                                         dtend=datetime(d.year, d.month, d.day, 8)))
            db.commit()
        text = ir.summary("alice", TODAY, now=datetime(2026, 10, 22, 7, tzinfo=timezone.utc))
        assert text == "- Gym: Tuesdays around 18:00 (3 of the last 6 weeks)"
    finally:
        engine.dispose()
