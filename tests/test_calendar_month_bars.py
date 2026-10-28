"""Month view: which days an event's bar covers (static/js/calendar/utils.js)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _last_days(events: list[dict], tz: str = "America/Los_Angeles") -> list[str]:
    source = (
        "import { _lastDayOf } from './static/js/calendar/utils.js';"
        f"console.log(JSON.stringify({json.dumps(events)}.map(_lastDayOf)));"
    )
    out = subprocess.run(["node", "--input-type=module", "-e", source], cwd=ROOT,
                         check=True, capture_output=True, text=True,
                         env={**os.environ, "TZ": tz})
    return json.loads(out.stdout)


def test_timed_event_ends_on_its_own_day():
    assert _last_days([
        {"dtstart": "2026-10-28T11:00:00", "dtend": "2026-10-28T12:00:00", "all_day": False},
        # ends at midnight: the 29th is not covered
        {"dtstart": "2026-10-28T22:00:00", "dtend": "2026-10-29T00:00:00", "all_day": False},
        # UTC times read in the viewer's zone (UTC-7 here)
        {"dtstart": "2026-10-28T18:00:00Z", "dtend": "2026-10-29T02:00:00Z", "all_day": False},
    ]) == ["2026-10-28", "2026-10-28", "2026-10-28"]


def test_overnight_and_all_day_events():
    assert _last_days([
        {"dtstart": "2026-10-28T20:00:00", "dtend": "2026-10-29T05:00:00", "all_day": False},
        # all-day end dates are exclusive
        {"dtstart": "2026-11-01", "dtend": "2026-11-02", "all_day": True},
        {"dtstart": "2026-11-01", "dtend": "2026-11-04", "all_day": True},
        {"dtstart": "2026-11-01", "dtend": "2026-11-01", "all_day": True},
        # bad end: the start day
        {"dtstart": "2026-10-28T11:00:00", "dtend": "2026-10-28T10:00:00", "all_day": False},
    ]) == ["2026-10-29", "2026-11-01", "2026-11-03", "2026-11-01", "2026-10-28"]
