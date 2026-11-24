"""Fork: requests a voice device makes on its own are not "the owner is using Odysseus".

Upstream treats every request that is not on its passive list (``src/interactive_gate.py``:
the heartbeat, ``/api/health``, ...) as foreground activity, and on each one it cancels
every running background task ("Paused because Odysseus became active"), holds model-backed
tasks while one is recent, and pushes due tasks back 15 minutes.

An Echo polls its feed every 30 s all day. A model-backed task that needs more than 30 s
(the morning News Brief, the Today insight) was cancelled by the next poll, started again
from nothing 15 minutes later, and so never finished. Reads and housekeeping pings the
device sends by itself are passive here. Everything that does work for the owner stays
foreground: a spoken turn (``/satellite/turn``, ``/chat/completions``), an interrupt, an
announcement being sent, marking something done, and every request from the owner's
browser.

Called from ``should_track_interactive_request`` in ``src/interactive_gate.py`` (FORK.md).
"""

from __future__ import annotations

PREFIX = "/api/chat/voice/v1"

# (method, path) a device sends by itself.
_EXACT = frozenset({
    ("GET", PREFIX + "/announcements"),       # the device's feed, every 30 s
    ("GET", PREFIX + "/turns/latest"),        # the screen's feed
    ("GET", PREFIX + "/alarms"),
    ("GET", PREFIX + "/models"),
    ("GET", PREFIX + "/app/latest"),          # update check
    ("POST", PREFIX + "/satellite/hello"),    # hourly
    ("POST", PREFIX + "/satellite/timing"),   # latency report after a turn
})
_SETTINGS_PREFIX = PREFIX + "/devices/"       # GET .../{device_id}/settings


def is_passive(path: str, method: str = "GET") -> bool:
    """True for a voice device's own reads and pings (not a turn, not an owner's action)."""
    method = (method or "GET").upper()
    path = (path or "").rstrip("/") or "/"
    if (method, path) in _EXACT:
        return True
    return (method == "GET" and path.startswith(_SETTINGS_PREFIX)
            and path.endswith("/settings") and path.count("/") == PREFIX.count("/") + 3)
