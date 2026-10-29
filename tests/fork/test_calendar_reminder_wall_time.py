"""A calendar reminder names the event's wall time, not its UTC instant."""

import asyncio
import json

import core.database as cdb
from tests.helpers.sqlite_db import make_temp_sqlite


def _reminder_text(monkeypatch, args):
    import routes.calendar_routes as cr
    from src.tools.calendar import do_manage_calendar
    from src.user_time import clear_user_time_context

    factory, engine, _ = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(cr, "SessionLocal", factory, raising=False)
    clear_user_time_context()
    try:
        result = asyncio.run(do_manage_calendar(json.dumps(args), owner="admin"))
        assert result.get("exit_code", 0) == 0, result
        with factory() as db:
            note = db.query(cdb.Note).filter(cdb.Note.label == "calendar").one()
            return json.loads(note.items)[0]["text"]
    finally:
        engine.dispose()


def test_reminder_uses_the_stated_zone(monkeypatch):
    text = _reminder_text(monkeypatch, {
        "action": "create_event", "summary": "Meet Jason",
        "local_start": {"date": "2099-10-21", "time": "12:00"},
        "timezone": "Europe/Berlin", "reminder_minutes": 30})
    assert text == "Meet Jason — Wed Oct 21 12:00"


def test_reminder_for_an_all_day_event_is_unchanged(monkeypatch):
    text = _reminder_text(monkeypatch, {
        "action": "create_event", "summary": "Lunch", "dtstart": "2099-10-22",
        "all_day": True, "reminder_minutes": 60})
    assert text == "Lunch — Thu Oct 22"
