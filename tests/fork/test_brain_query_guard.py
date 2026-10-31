"""The brain's web-query privacy guard (``fork/brain/query_guard.py``).

Fictional contacts and events for two owners, seeded through the real stores: the local
contacts book (``routes.contacts_routes._create_contact``) and the calendar tables in a
temp SQLite database.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from fork.brain import query_guard as qg


@pytest.fixture
def owners(data_dir, monkeypatch):
    import core.database as cdb
    from routes import contacts_routes as cc
    from tests.helpers.sqlite_db import make_temp_sqlite

    monkeypatch.setattr(cc, "DATA_DIR", data_dir)
    monkeypatch.setattr(cc, "LOCAL_CONTACTS_FILE", data_dir / "contacts.json")
    monkeypatch.setattr(cc, "_get_carddav_config", lambda: {"url": ""})
    monkeypatch.setattr(cc, "_contact_cache", {"contacts": [], "fetched_at": None,
                                               "failed_at": None, "by_owner": {}})
    for name, email, owner in [("Wren Hale", "wren.hale@example.org", "alice"),
                               ("Paris Hilton-Smith", "paris@example.org", "alice"),
                               ("Jordan Vale", "jordan@example.org", "alice"),
                               ("jdoe42", "jdoe42@example.org", "alice"),
                               ("Arlo Vance", "arlo@example.org", "bob")]:
        assert cc._create_contact(name, email, "", [], owner)

    factory, engine, _tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with factory() as db:
        db.add(cdb.CalendarCal(id="cal-a", owner="alice", name="Personal"))
        db.add(cdb.CalendarCal(id="cal-b", owner="bob", name="Bob's"))
        for uid, cal, title, days, extra in [
            ("e1", "cal-a", "Quarterly Garden Review", 3, {}),
            ("e2", "cal-a", "Dentist", 2, {}),
            ("e3", "cal-a", "Lantern Festival Planning", 60, {}),          # too far ahead
            ("e4", "cal-a", "Harbor Picnic Prep", 5, {"status": "cancelled"}),
            ("e5", "cal-a", "Copper Choir Practice", -40, {"rrule": "FREQ=WEEKLY"}),
            ("e6", "cal-b", "Moonlight Launch Party", 4, {}),
        ]:
            start = now + timedelta(days=days)
            db.add(cdb.CalendarEvent(uid=uid, calendar_id=cal, summary=title, dtstart=start,
                                     dtend=start + timedelta(hours=1), **extra))
        db.commit()
    qg.clear_cache()
    yield
    qg.clear_cache()
    engine.dispose()


def test_emails_phones_and_addresses_are_removed():
    cases = {
        "reviews of shop jdoe@example.org": "reviews of shop",
        "who calls from +49 30 1234567": "who calls from",
        "spam number 0171-1234567": "spam number",
        "spam number (555) 123-4567": "spam number",
        "spam number 555.123.4567": "spam number",
        "parking near 12 Elm Street Springfield": "parking near Springfield",
        "bakery Hauptstraße 5 Berlin": "bakery Berlin",
    }
    for query, want in cases.items():
        assert qg._regex_only(query) == (want, 1), query


def test_numbers_dates_and_ordinary_words_are_kept():
    for query in ["events 01.11.2026 in Berlin", "news 2026-10-31", "top 10 phones 2026",
                  "iPhone 15 Pro price", "price range 100-1000 euro", "5 best way to cook rice",
                  "Spring 2026 fashion", "Route 66 diner"]:
        assert qg._regex_only(query) == (query, 0), query


def test_full_contact_names_are_removed_in_any_case(owners):
    assert qg.guard_query("birthday gift ideas for Wren Hale", "alice") == ("birthday gift ideas for", 1)
    assert qg.guard_query("wren hale's favourite band", "alice") == ("favourite band", 1)
    assert qg.guard_query("Jordan Vale linkedin", "alice") == ("linkedin", 1)


def test_single_name_tokens_need_a_capital_and_an_uncommon_word(owners):
    # a capitalized token of a contact's name goes
    assert qg.guard_query("what does Wren like to eat", "alice") == ("what does like to eat", 1)
    assert qg.guard_query("Hilton-Smith family history", "alice") == ("family history", 1)
    # lowercase, it is an ordinary word
    assert qg.guard_query("wren bird song", "alice") == ("wren bird song", 0)
    # places and words stay even when a contact is named after them
    assert qg.guard_query("weather in Paris", "alice") == ("weather in Paris", 0)
    assert qg.guard_query("flights to Jordan in May", "alice") == ("flights to Jordan in May", 0)
    # a name made from an email address goes in any case
    assert qg.guard_query("jdoe42 profile", "alice") == ("profile", 1)


def test_paris_hilton_smith_full_name_still_removed(owners):
    assert qg.guard_query("Paris Hilton-Smith in Paris", "alice") == ("in Paris", 1)


def test_another_owners_contacts_and_events_are_not_used(owners):
    assert qg.guard_query("Arlo Vance Moonlight Launch Party", "alice") == (
        "Arlo Vance Moonlight Launch Party", 0)
    assert qg.guard_query("Arlo Vance Moonlight Launch Party", "bob") == ("", 2)
    assert qg.guard_query("Wren Hale", "bob") == ("Wren Hale", 0)


def test_upcoming_event_titles_of_two_or_more_words_are_removed(owners):
    assert qg.guard_query("how to run a quarterly garden review", "alice") == ("how to run a", 1)
    assert qg.guard_query("songs for Copper Choir Practice", "alice") == ("songs for", 1)  # series
    # one-word titles, far-away and cancelled events stay
    assert qg.guard_query("Dentist open on Sunday", "alice") == ("Dentist open on Sunday", 0)
    assert qg.guard_query("Lantern Festival Planning tips", "alice")[1] == 0
    assert qg.guard_query("Harbor Picnic Prep list", "alice")[1] == 0


def test_nothing_meaningful_left_is_empty(owners):
    assert qg.guard_query("Wren Hale wren.hale@example.org", "alice") == ("", 2)
    assert qg.guard_query("about Wren Hale?", "alice") == ("", 1)
    assert qg.guard_query("   ", "alice") == ("", 0)


def test_names_are_cached_per_owner(owners, monkeypatch):
    calls = []
    real = qg._load_contact_names
    monkeypatch.setattr(qg, "_load_contact_names", lambda o: calls.append(o) or real(o))
    qg.clear_cache()
    for _ in range(3):
        qg.guard_query("Wren Hale", "alice")
    assert calls == ["alice"]
    qg.guard_query("Wren Hale", "bob")
    assert calls == ["alice", "bob"]


def test_error_path_returns_regex_cleaned_query_and_logs_no_text(owners, monkeypatch, caplog):
    def broken(owner):
        raise RuntimeError("store down")

    monkeypatch.setattr(qg, "_load_contact_names", broken)
    qg.clear_cache()
    caplog.set_level(logging.DEBUG, logger="fork.brain.query_guard")
    got = qg.guard_query("Wren Hale at wren.hale@example.org", "alice")
    assert got == ("Wren Hale at", 1)
    assert "owner data unavailable" in caplog.text
    assert "Wren" not in caplog.text and "example.org" not in caplog.text


def test_never_raises_on_odd_input(owners):
    assert qg.guard_query(None, "alice") == ("", 0)  # type: ignore[arg-type]
    assert qg.guard_query("Wren Hale", None)[1] >= 0


def test_logs_counts_only(owners, caplog):
    caplog.set_level(logging.DEBUG)
    cleaned, removed = qg.guard_query(
        "dinner with Wren Hale at 12 Elm Street, call +49 30 1234567", "alice")
    assert removed == 3
    assert "removed 3 term" in caplog.text
    for secret in ("Wren", "Hale", "Elm", "1234567", "dinner"):
        assert secret not in caplog.text
