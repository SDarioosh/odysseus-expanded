"""Calendar times reach the model in the owner's wall time, not UTC.

``list_events`` printed ``2026-11-05T15:00:00Z`` for a 16:00 Berlin meeting and the
model said 3 pm; a voice turn had no zone and used the server's (UTC in Docker).
fork/common/local_time.py.
"""

import json
import sys
import tempfile
import uuid
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb  # noqa: E402

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(f"sqlite:///{_TMPDB.name}", connect_args={"check_same_thread": False},
                        poolclass=NullPool)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def _bind_temp_db(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    parent = sys.modules.get("core")
    if parent is not None:
        monkeypatch.setattr(parent, "database", cdb, raising=False)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    from src.user_time import clear_user_time_context
    clear_user_time_context()
    yield
    clear_user_time_context()


async def test_list_events_shows_the_wall_time():
    from src.tool_implementations import do_manage_calendar
    from src.user_time import set_user_tz_name

    owner = "cal-local-" + uuid.uuid4().hex[:8]
    created = await do_manage_calendar(json.dumps({
        "action": "create_event", "summary": "Team sync",
        "dtstart": "2126-11-05T16:00:00", "dtend": "2126-11-05T17:00:00",
        "timezone": "Europe/Berlin"}), owner=owner)
    assert created.get("exit_code") == 0, created

    set_user_tz_name("Europe/Berlin")
    listed = await do_manage_calendar(json.dumps({
        "action": "list_events", "start": "2126-11-05T00:00:00",
        "end": "2126-11-06T00:00:00"}), owner=owner)
    assert listed["exit_code"] == 0, listed
    text = listed["response"]
    assert "2126-11-05 Tue 16:00 -> 17:00" in text
    assert "15:00:00Z" not in text
    assert "Europe/Berlin" in text
    ev = listed["events"][0]
    assert ev["local_start"] == "2126-11-05 Tue 16:00"
    assert ev["local_end"] == "17:00"
    assert ev["dtstart"].endswith("Z")  # the stored time is unchanged


def test_local_times_all_day_and_naive():
    from fork.common.local_time import event_line_when, local_times

    zone = ZoneInfo("America/New_York")
    assert event_line_when({"dtstart": "2126-03-02", "dtend": "2126-03-03",
                            "all_day": True}, zone) == "2126-03-02 Sat (all day)"
    # A legacy naive time is a wall time already.
    assert local_times({"dtstart": "2126-03-02T09:30:00", "dtend": "2126-03-02T10:00:00"},
                       zone) == ("2126-03-02 Sat 09:30", "10:00")
    # An end on another day keeps its date.
    assert local_times({"dtstart": "2126-03-02T23:00:00Z", "dtend": "2126-03-03T06:00:00Z"},
                       zone) == ("2126-03-02 Sat 18:00", "2126-03-03 Sun 01:00")


def test_owner_timezone_fills_in_only_when_no_client_sent_one():
    from fork.common.local_time import use_owner_timezone
    from src.user_time import get_user_tz_name, set_user_tz_name

    owner = "tz-owner-" + uuid.uuid4().hex[:8]
    db = _TS()
    db.add(cdb.CrewMember(id=uuid.uuid4().hex, owner=owner, name="Helper",
                          timezone="Asia/Tokyo"))
    db.add(cdb.CrewMember(id=uuid.uuid4().hex, owner=owner, name="Assistant",
                          is_default_assistant=True, timezone="Europe/Berlin"))
    db.add(cdb.CrewMember(id=uuid.uuid4().hex, owner="someone-else", name="X",
                          is_default_assistant=True, timezone="America/Denver"))
    db.commit()
    db.close()

    assert use_owner_timezone(owner) == "Europe/Berlin"
    assert get_user_tz_name() == "Europe/Berlin"

    set_user_tz_name("Australia/Brisbane")  # a client's zone wins
    assert use_owner_timezone(owner) is None
    assert get_user_tz_name() == "Australia/Brisbane"


def test_owner_without_a_zone_changes_nothing():
    from fork.common.local_time import use_owner_timezone
    from src.user_time import get_user_tz_name

    assert use_owner_timezone("nobody-" + uuid.uuid4().hex[:8]) is None
    assert get_user_tz_name() is None
