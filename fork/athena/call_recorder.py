"""Fork: add recordings to Athena through its connected MCP server.

Two callers send audio pieces here, and this file checks each piece and forwards it:

* the Meetings page (``static/call-recorder.html``): self-contained 30-second
  WebM/Opus pieces of a meeting captured in the browser (admin session);
* the phone relay paired through Odysseus (Athena's ``android-relay``): the
  watch's 30-second Ogg/Opus chunks (API token with scope ``sound:ingest``,
  ``routes/sound_relay_routes.py``).

Where the pieces go: Athena's MCP HTTP server (``convo-server mcp --http``) has two
plain HTTP routes next to ``/mcp``, behind its MCP token: ``POST /ingest/chunk`` and
``POST /sessions/{id}/end``. They are not MCP tools; the module forwards them to its own
ingest API with its ingest token, which never leaves the module. So the only settings
needed are the ones Odysseus already has: the URL and ``?access_token=`` of the MCP server
named ``convo`` (``SOUND_MODULE_SERVER``), read from Odysseus's MCP server list.

The module also forwards a few read routes (``GET /conversations`` …) the same way; the
relay's T5 sync reads them through :func:`forward` (``routes/sound_relay_routes.py``).

Odysseus never touches the module's storage (docs/fork/modules.md). Optional setting:
``SOUND_MODULE_CALL_DEVICE``, the device name call recordings get (default ``computer``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from fork.athena import client as athena
from fork.athena.client import (  # noqa: F401 -- their old home; callers import them here
    TIMEOUT,
    Link,
    _client,
    auth_headers,
    link_from_url,
)
from fork.common import settings

logger = logging.getLogger(__name__)

# The settings and their defaults are rows of fork/common/settings.py.
SERVER_ENV = settings.RECORDER_SERVER.name  # SOUND_MODULE_SERVER
LEGACY_SERVER_ENV = settings.RECORDER_SERVER.old[0]  # BARVIS_SOUND_SERVER: former name, still read
DEFAULT_SERVER = settings.RECORDER_SERVER.default
DEVICE_ENV = settings.CALL_DEVICE.name
DEFAULT_DEVICE = settings.CALL_DEVICE.default

# Same rule as Athena's ids (server/athena/validation.py _SAFE).
SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
KINDS = {"conversation", "note"}  # Athena's session kinds
MAX_CHUNK_BYTES = 5 * 1024 * 1024  # 30 s of Opus is ~60-250 KB; leaves room for 60 s at 64 kbit/s
MAX_SEQUENCE = 100_000  # 4 h sessions (the module's default cap) are 480 pieces
UPDATE_MODULE = ("Athena can't take uploads through its MCP server yet: update "
                 "Athena (scripts/fork_deploy.sh deploy).")


@dataclass(frozen=True)
class Source:
    """What a caller may send. A wrong clock would misplace the conversation, so pieces
    can't start in the future; how old they may be depends on the caller."""

    codecs: frozenset[str]
    max_future: timedelta
    max_age: timedelta


# MediaRecorder writes WebM in Chromium and Ogg in Firefox; a queued retry is hours old.
RECORDER = Source(frozenset({"webm", "ogg"}), timedelta(minutes=5), timedelta(days=2))
# The relay sends the watch's Ogg/Opus chunks (``codec=ogg``; ``opus`` is the module's name
# for Ogg/Opus or bare Opus packets). The watch keeps its queue across days offline and
# its clock comes from the phone, so the window is wide: a rejected chunk is lost audio.
RELAY = Source(frozenset({"ogg", "opus"}), timedelta(days=1), timedelta(days=60))


# A request the caller can be shown: ``status`` is the HTTP status to answer with. The one
# Athena error (fork/athena/client.py), under the name its callers know.
RecorderError = athena.AthenaError


def server_key() -> str:
    return athena.RECORDER.server_key()


def device() -> str:
    return settings.call_device(logger, valid=SAFE_ID.match)


def resolve() -> Link:
    """The module's upload link from the MCP server called ``SOUND_MODULE_SERVER`` (name or
    id, default ``convo``). Raises a 503 :class:`RecorderError` that says what to fix."""
    return athena.RECORDER.link()


def status() -> dict[str, Any]:
    """For the pages: ``{"configured": bool, "problem": str | None}`` (no URL, no token)."""
    try:
        resolve()
    except RecorderError as exc:
        return {"configured": False, "problem": exc.message}
    return {"configured": True, "problem": None}


def check_session_id(session_id: str) -> str:
    session_id = (session_id or "").strip()
    if not SAFE_ID.match(session_id):
        raise RecorderError(400, "invalid session id")
    return session_id


def check_chunk(*, data: bytes, session_id: str, sequence: int, timestamp: str, codec: str,
                source: Source = RECORDER, now: datetime | None = None) -> tuple[str, str, str]:
    """Validate one piece; returns (session_id, timestamp in ISO-8601 UTC, codec)."""
    session_id = check_session_id(session_id)
    if not 0 <= sequence <= MAX_SEQUENCE:
        raise RecorderError(400, "invalid sequence")
    codec = (codec or "").strip().lower()
    if codec not in source.codecs:
        raise RecorderError(400, f"codec must be one of {sorted(source.codecs)}")
    if not data:
        raise RecorderError(400, "empty audio")
    if len(data) > MAX_CHUNK_BYTES:
        raise RecorderError(413, "audio piece too large")
    started = _parse_time(timestamp)
    now = now or datetime.now(UTC)
    if started - now > source.max_future or now - started > source.max_age:
        raise RecorderError(400, "timestamp is too far from the server's clock")
    return session_id, started.astimezone(UTC).isoformat(), codec


def _parse_time(value: str) -> datetime:
    """ISO-8601 with a zone (``Z`` too), or epoch seconds like the module accepts."""
    text = (value or "").strip()
    if re.fullmatch(r"\d{9,11}(\.\d+)?", text):
        return datetime.fromtimestamp(float(text), UTC)
    try:
        started = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z")
                                         else text)
    except ValueError:
        raise RecorderError(400, "invalid timestamp") from None
    if started.tzinfo is None:
        raise RecorderError(400, "timestamp needs a time zone")
    return started


def check_device(name: str) -> str:
    name = (name or "").strip()
    if not SAFE_ID.match(name):
        raise RecorderError(400, "invalid device")
    return name


def check_kind(kind: str | None) -> str:
    kind = (kind or "conversation").strip().lower()
    if kind not in KINDS:
        raise RecorderError(400, f"kind must be one of {sorted(KINDS)}")
    return kind


def _detail(res: httpx.Response) -> str:
    try:
        body = res.json()
    except ValueError:
        return ""
    return str(body.get("detail") or "") if isinstance(body, dict) else ""


def _module_error(res: httpx.Response) -> RecorderError:
    detail = _detail(res)
    if res.status_code in (401, 403):
        # Odysseus's MCP token for the module is wrong: not the caller's fault.
        return athena.RECORDER.refused_token()
    if res.status_code in (400, 413, 415, 422):
        return RecorderError(res.status_code, detail or f"Athena HTTP {res.status_code}")
    if res.status_code == 502 and detail:  # the module's ingest API is down
        return RecorderError(502, detail)
    return RecorderError(502, f"Athena HTTP {res.status_code}")


async def _post(link: Link, path: str, transport: httpx.AsyncBaseTransport | None,
                **kwargs: Any) -> httpx.Response:
    return await athena.request(link, "POST", path, transport=transport, **kwargs)


async def forward_chunk(link: Link, *, data: bytes, session_id: str, sequence: int,
                        timestamp: str, codec: str, device_name: str,
                        kind: str = "conversation", source: Source = RECORDER,
                        transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Send one checked piece to the module; returns the module's answer (``ok``, ``stop``,
    ``reason``, ``duplicate`` …)."""
    session_id, timestamp, codec = check_chunk(data=data, session_id=session_id,
                                               sequence=sequence, timestamp=timestamp,
                                               codec=codec, source=source)
    form = {"device": check_device(device_name), "session_id": session_id,
            "sequence": str(sequence), "timestamp": timestamp, "codec": codec,
            "kind": check_kind(kind)}
    files = {"audio": (f"{sequence:06d}.{codec}", data, f"audio/{codec}")}
    res = await _post(link, "/ingest/chunk", transport, data=form, files=files)
    if res.status_code == 404:
        raise RecorderError(502, UPDATE_MODULE)
    if res.status_code != 200:
        raise _module_error(res)
    try:
        body = res.json()
    except ValueError:
        raise RecorderError(502, "Athena sent an unreadable answer") from None
    if not isinstance(body, dict):
        raise RecorderError(502, "Athena sent an unreadable answer")
    return {**body, "ok": bool(body.get("ok", True)), "stop": bool(body.get("stop")),
            "reason": body.get("reason")}


async def end_session(link: Link, session_id: str,
                      transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Tell the module the recording is over, so it processes it now instead of after the
    idle timeout. A session the module never saw (Stop before the first piece) is fine."""
    session_id = check_session_id(session_id)
    res = await _post(link, f"/sessions/{session_id}/end", transport)
    if res.status_code == 404:
        if _detail(res) == "session not found":
            return {"ok": True, "found": False}
        raise RecorderError(502, UPDATE_MODULE)  # the route itself is missing
    if res.status_code != 200:
        raise _module_error(res)
    return {"ok": True, "found": True}


async def forward(link: Link, method: str, path: str, *, params: dict[str, Any] | None = None,
                  json: Any = None, transport: httpx.AsyncBaseTransport | None = None
                  ) -> tuple[int, Any]:
    """A read (or small write) route of the module's MCP HTTP server, as (status, JSON body).

    The module's own status and ``detail`` come back as they are, except that a refused
    Odysseus token becomes 502 (it is not the caller's token) and a missing route says to
    update Athena.
    """
    query = {k: v for k, v in (params or {}).items() if v is not None}
    res = await athena.request(link, method, path, params=query, json=json,
                               transport=transport)
    if res.status_code in (401, 403):
        raise _module_error(res)
    try:
        body = res.json()
    except ValueError:
        body = None
    if res.status_code in (404, 405) and not (isinstance(body, dict) and body.get("detail")
                                              and body.get("detail") != "Not Found"):
        raise RecorderError(502, UPDATE_MODULE)
    if body is None:
        raise RecorderError(502, f"Athena HTTP {res.status_code} without JSON")
    return res.status_code, body


async def module_json(method: str, path: str, *, params: dict[str, Any] | None = None,
                      json: Any = None, old_module: str | None = None) -> tuple[int, Any]:
    """:func:`forward` to the link of the ``SOUND_MODULE_SERVER`` server. With
    ``old_module``, a module without the route says that instead of :data:`UPDATE_MODULE`
    (the settings cards' own words)."""
    try:
        return await forward(resolve(), method, path, params=params, json=json)
    except RecorderError as exc:
        if old_module is None:
            raise
        message = old_module if exc.message == UPDATE_MODULE else exc.message
        raise RecorderError(exc.status, message) from None


def path_part(value: str) -> str:
    """One URL path segment from a caller's value (ids, dates)."""
    return quote(str(value), safe="")
