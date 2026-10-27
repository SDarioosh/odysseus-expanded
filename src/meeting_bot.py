"""Fork: "Send Athena to a meeting": start and stop Athena's Zoom bot from the hub.

The "Record a call" page (``static/call-recorder.html``, ``static/js/meetingBot.js``) has a
box where the owner pastes a Zoom link (or an invite, or a meeting ID and passcode).
Odysseus checks it here, then calls the meeting routes of Athena's MCP HTTP server, next
to ``/mcp`` and behind the same token, exactly like the call recorder's upload routes
(``src/call_recorder.py``: same server lookup, URL and ``?access_token=``):

    POST   /meetings          {url} or {meeting_id, passcode}, optional title -> 202
    GET    /meetings?limit=N  {meetings: [...]} newest first
    GET    /meetings/{id}     one meeting's state
    DELETE /meetings/{id}     leave (answers once the bot has exited)

They are plain HTTP routes, not MCP tools: the model never sees them. The module forwards
them to its own ingest API (``/v1/meetings``) with its ingest token, which never leaves the
module. So the MCP token can send the bot into a meeting, as it can already add
recordings.

Nothing here runs on its own: no thread, timer or poll. The page asks for the state while
it is open and a meeting is on (docs/fork/modules/odysseus-sound.md).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from src import call_recorder as recorder

logger = logging.getLogger(__name__)

# The module's relay waits 15 s for a join and 30 s for a leave (the bot gets SIGTERM, then
# SIGKILL after 10 s); give each a little more so its own answer arrives first.
JOIN_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
READ_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
LEAVE_TIMEOUT = httpx.Timeout(40.0, connect=5.0)

MAX_INPUT_CHARS = 20_000  # an invite pasted whole, with its dial-in numbers
MAX_TITLE_CHARS = 200
MAX_PASSCODE_CHARS = 128
LIST_LIMIT = 5  # the page looks for an active meeting among the newest few
ACTIVE_STATES = ("starting", "waiting_room", "recording")
FINAL_STATES = ("ended", "failed")

UPDATE_MODULE = ("Update Athena to use meetings: its MCP server has no meeting routes yet "
                 "(scripts/fork_deploy.sh deploy).")

# The same rules as Athena's launcher (convo_server/meetings.py parse_zoom_link).
_ZOOM_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*zoom\.us$")
_NO_SCHEME = re.compile(r"^([a-z0-9-]+\.)*zoom\.us/", re.IGNORECASE)
_MEETING_NUMBER = re.compile(r"^\d{9,11}$")
_PASSCODE = re.compile(r"^[!-~]{1,128}$")  # printable ASCII, no spaces
_MEETING_ID = re.compile(r"^[0-9a-f]{32}$")  # the launcher's ids (uuid4 hex)
# Link-like words in pasted text: anything with a scheme, or a host name ending in zoom.us
# or zoomgov.com (look-alikes such as zoom.us.example.com are caught and refused too).
_LINK_WORD = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://\S+|(?<![\w@.-])(?:[a-z0-9-]+\.)*zoom(?:gov)?\.(?:us|com)\S*)",
    re.IGNORECASE)
_TRAILING = ".,;:!?)]}>'\"»”’"
# An invite's own lines, for an invite pasted without a link ("Meeting ID: 850 1234 5678").
_INVITE_ID = re.compile(r"meeting\s*id\s*[:#]?\s*(\d[\d \t-]{7,16}\d)", re.IGNORECASE)
_INVITE_PASSCODE = re.compile(r"\b(?:passcode|password|pass\s*code)\s*[:#]?\s*(\S+)",
                              re.IGNORECASE)
_ONLY_NUMBER = re.compile(r"^[\d \t-]+$")


class MeetingError(Exception):
    """A request the page can be shown: ``status`` is the HTTP status to answer with,
    ``code`` a word the page acts on (``busy`` shows a Leave button for ``active``)."""

    def __init__(self, status: int, message: str, code: str = "error",
                 active: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.active = active

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"detail": self.message, "code": self.code}
        if self.active:
            out["active"] = self.active
        return out


# ------------------------------------------------------------------ Zoom links
@dataclass(frozen=True)
class JoinRequest:
    """What goes to the module: a checked link, or a meeting number; and a passcode."""

    url: str | None = None
    meeting_id: str | None = None
    passcode: str | None = None

    def payload(self, title: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = ({"url": self.url} if self.url
                                else {"meeting_id": self.meeting_id})
        if self.passcode:
            body["passcode"] = self.passcode
        if title:
            body["title"] = title
        return body


def _bad(message: str) -> MeetingError:
    return MeetingError(400, message, "bad_link")


def meeting_number(value: str) -> str:
    number = re.sub(r"[\s-]", "", value or "")
    if not _MEETING_NUMBER.match(number):
        raise _bad("A Zoom meeting ID has 9 to 11 digits.")
    return number


def passcode(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not _PASSCODE.match(value):
        raise _bad(f"The passcode can have at most {MAX_PASSCODE_CHARS} characters, "
                   "without spaces.")
    return value


def check_link(text: str) -> JoinRequest:
    """One Zoom join link -> its number and passcode. Accepts ``https://zoom.us/j/<n>?pwd=…``
    on zoom.us or any subdomain (``us06web.zoom.us``, a company's ``acme.zoom.us``),
    ``http`` or no scheme, the web client's ``/wc/join/<n>`` and ``/wc/<n>/join``, and
    ``zoommtg://`` / ``zoomus://…/join?confno=<n>&pwd=…``. Everything else raises a
    400 :class:`MeetingError` that says why."""
    raw = (text or "").strip().strip("<>\"'").strip()
    if not raw:
        raise _bad("Paste a Zoom meeting link.")
    if "://" not in raw and _NO_SCHEME.match(raw):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise _bad("That isn't a Zoom meeting link.") from None
    scheme = parts.scheme.lower()
    if host == "zoomgov.com" or host.endswith(".zoomgov.com"):
        raise _bad("Zoom for Government links (zoomgov.com) aren't supported.")
    if (scheme not in ("https", "http", "zoommtg", "zoomus") or not _ZOOM_HOST.match(host)
            or port is not None or parts.username or parts.password):
        raise _bad("That isn't a Zoom meeting link: it should look like "
                   "https://zoom.us/j/<meeting ID>?pwd=…")
    query = parse_qs(parts.query)
    pwd = (query.get("pwd") or [None])[0] or None
    segs = [s for s in parts.path.split("/") if s]
    if scheme in ("zoommtg", "zoomus"):
        if segs[:1] == ["start"]:
            raise _bad("This is the host's start link; paste the join link instead.")
        number = (query.get("confno") or [""])[0]
        if segs[:1] != ["join"] or not number:
            raise _bad("That Zoom app link has no meeting ID (confno); paste the "
                       "https:// join link instead.")
    else:
        first = segs[0].lower() if segs else ""
        if first == "my":
            raise _bad("Personal links (/my/<name>) carry no meeting ID: paste the invite "
                       "link with /j/<number>, or type the meeting ID and passcode.")
        if first == "w":
            raise _bad("Webinars aren't supported yet.")
        if first == "s":
            raise _bad("This is the host's start link; paste the join link instead.")
        if first in ("meeting", "webinar") and "register" in [s.lower() for s in segs]:
            raise _bad("This is a registration link: register first, then paste the join "
                       "link from the confirmation.")
        if first == "j" and len(segs) == 2:
            number = segs[1]
        elif first == "wc" and len(segs) == 3 and segs[1] == "join":
            number = segs[2]
        elif first == "wc" and len(segs) == 3 and segs[2] == "join":
            number = segs[1]
        else:
            raise _bad("That isn't a Zoom meeting link: it should look like "
                       "https://zoom.us/j/<meeting ID>?pwd=…")
    return JoinRequest(url=raw, meeting_id=meeting_number(number), passcode=passcode(pwd))


def _link_words(text: str) -> list[str]:
    return [w.rstrip(_TRAILING) for w in _LINK_WORD.findall(text)]


def _invite_passcode(text: str) -> str | None:
    """The invite's "Passcode: …" (the first one: the meeting's, not the dial-in one)."""
    found = _INVITE_PASSCODE.search(text)
    value = found.group(1).rstrip(_TRAILING) if found else ""
    return value if _PASSCODE.match(value) else None


def parse_join(text: str, typed_passcode: str | None = None) -> JoinRequest:
    """What the owner typed into the box -> a :class:`JoinRequest`, without calling the
    module. ``text`` is one of:

    * a Zoom join link (see :func:`check_link`);
    * an invite pasted whole: its first Zoom join link wins; an invite without a link is
      read from its "Meeting ID:" and "Passcode:" lines;
    * a meeting ID ("850 1234 5678"), with the passcode in ``typed_passcode``.

    A passcode typed separately is used when the link has none. Raises a 400
    :class:`MeetingError` with a message the page shows.
    """
    text = (text or "").strip()
    if len(text) > MAX_INPUT_CHARS:
        raise _bad("That's too long: paste the Zoom link, or the invite only.")
    extra = passcode(typed_passcode)
    if not text:
        raise _bad("Paste a Zoom meeting link, or type the meeting ID.")
    if _ONLY_NUMBER.match(text):
        return JoinRequest(meeting_id=meeting_number(text), passcode=extra)
    if not re.search(r"\s", text):  # one word: it must be the link itself
        found = check_link(text)
        return JoinRequest(found.url, found.meeting_id, found.passcode or extra)
    first_error: MeetingError | None = None
    for word in _link_words(text):
        try:
            found = check_link(word)
        except MeetingError as exc:
            if first_error is None and re.search(r"zoom(?:gov)?\.|zoommtg:|zoomus:", word,
                                                 re.IGNORECASE):
                first_error = exc
            continue
        pwd = found.passcode or extra or _invite_passcode(text)
        return JoinRequest(found.url, found.meeting_id, pwd)
    number = _INVITE_ID.search(text)
    if number:
        return JoinRequest(meeting_id=meeting_number(number.group(1)),
                           passcode=extra or _invite_passcode(text))
    if first_error is not None:
        raise first_error
    raise _bad("No Zoom meeting link found. Paste a link like https://zoom.us/j/<meeting ID>, "
               "or type the meeting ID and passcode.")


def check_title(title: str | None) -> str | None:
    title = " ".join((title or "").split())
    if len(title) > MAX_TITLE_CHARS:
        raise MeetingError(400, f"The title can have at most {MAX_TITLE_CHARS} characters.",
                           "bad_title")
    return title or None


def check_meeting_id(meeting_id: str) -> str:
    if not _MEETING_ID.match(meeting_id or ""):
        raise MeetingError(404, "No such meeting.", "not_found")
    return meeting_id


# ------------------------------------------------------------------ plain words
_ENDED = {
    "meeting_ended": "Ended: the meeting is over.",
    "alone": "Ended: Athena was the only one left in the meeting.",
    "removed": "Ended: Athena was removed from the meeting.",
    "stopped": "Ended: you stopped it.",
    "max_duration": "Ended: the meeting reached the maximum recording length.",
    "server_stop": "Ended: Athena's server stopped.",
    "exited": "Ended.",
}
_FAILED = {
    "wrong_passcode": "wrong passcode",
    "meeting_not_started": "the meeting hasn't started yet",
    "waiting_room_timeout": "the host didn't admit Athena from the waiting room",
    "join_failed": "Zoom didn't let Athena join",
    "auth_failed": "Zoom refused the server's Meeting SDK credentials",
    "sdk_error": "the Zoom SDK reported an error",
    "bot_not_started": "the meeting bot couldn't start on the server",
    "bot_crashed": "the meeting bot stopped unexpectedly",
    "server_restarted": "Athena's server restarted",
}


def describe(meeting: dict[str, Any]) -> str:
    """A meeting's state in plain words for the page."""
    state = meeting.get("state")
    reason = meeting.get("reason")
    if state == "starting":
        return "Joining…"
    if state == "waiting_room":
        return "Waiting in the waiting room — the host has to admit Athena."
    if state == "recording":
        if reason == "no_audio":
            return "Recording, but Zoom isn't sending audio."
        return "Recording."
    if state == "ended":
        return _ENDED.get(reason or "", "Ended.")
    if state == "failed":
        why = _FAILED.get(reason or "") or (str(reason).replace("_", " ") if reason
                                            else "unknown error")
        if meeting.get("started_at"):  # it had recorded: it didn't fail to join
            return f"Stopped: {why}."
        return f"Couldn't join: {why}."
    return f"State: {state or 'unknown'}."


_FIELDS = ("id", "state", "reason", "active", "session_id", "meeting_id", "title",
           "created_at", "started_at", "ended_at")


def public(meeting: dict[str, Any]) -> dict[str, Any]:
    """The fields the page needs from a module meeting row, plus ``text``."""
    out = {k: meeting.get(k) for k in _FIELDS}
    out["active"] = bool(meeting.get("active", meeting.get("state") in ACTIVE_STATES))
    out["text"] = describe(meeting)
    return out


# ------------------------------------------------------------------ module calls
def _detail(body: Any) -> str:
    return str(body.get("detail") or "") if isinstance(body, dict) else ""


def _route_missing(status: int, body: Any) -> bool:
    """A module without meeting routes: Starlette's plain 404/405, not the API's own."""
    detail = _detail(body)
    return status in (404, 405) and (not detail or detail in ("Not Found",
                                                              "Method Not Allowed"))


async def _call(link: recorder.Link, method: str, path: str, timeout: httpx.Timeout, *,
                params: dict[str, Any] | None = None, json: Any = None,
                transport: httpx.AsyncBaseTransport | None = None) -> tuple[int, Any]:
    query = {**(params or {}), "access_token": link.token}
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport,
                                     follow_redirects=False) as client:
            res = await client.request(method, f"{link.base}{path}", params=query, json=json)
    except httpx.TimeoutException:
        logger.warning("Athena timed out at %s %s", method, path)
        raise MeetingError(504, "Athena didn't answer in time; check again in a moment.",
                           "timeout") from None
    except httpx.HTTPError as exc:
        # The URL carries the token: log only the base and the error's type.
        logger.warning("Athena unreachable at %s: %s", link.base, type(exc).__name__)
        raise MeetingError(502, "Athena is not reachable.", "unreachable") from None
    try:
        body = res.json()
    except ValueError:
        body = None
    if res.status_code in (401, 403):
        raise MeetingError(502, f"Athena refused the token of the {recorder.server_key()!r} "
                                "MCP server (Settings → MCP).", "token")
    if _route_missing(res.status_code, body):
        raise MeetingError(502, UPDATE_MODULE, "update")
    return res.status_code, body


def _error(status: int, body: Any) -> MeetingError:
    """The module's refusal, in words for the page."""
    detail = _detail(body)
    if status == 400:
        return MeetingError(400, f"Athena refused the link: {detail}" if detail
                            else "Athena refused the link.", "bad_link")
    if status == 404:
        return MeetingError(404, "No such meeting.", "not_found")
    if status == 409:
        active = body.get("active") if isinstance(body, dict) else None
        return MeetingError(409, "Athena is busy in another meeting. Leave it first, or wait "
                                 "until it ends.", "busy",
                            active=active if _MEETING_ID.match(str(active or "")) else None)
    if status == 501:
        return MeetingError(501, "Meetings aren't set up on the server"
                                 + (f" ({detail})." if detail else "."), "off")
    if status == 500:
        return MeetingError(502, "Athena couldn't start the meeting bot"
                                 + (f": {detail}" if detail else "."), "bot")
    if status == 502 and detail:  # the module's ingest API is down
        return MeetingError(502, f"Athena's API isn't answering: {detail}", "unreachable")
    return MeetingError(502, f"Athena HTTP {status}" + (f": {detail}" if detail else ""))


def _meeting(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or not body.get("id"):
        raise MeetingError(502, "Athena sent an unreadable answer.")
    return body


async def join(link: recorder.Link, request: JoinRequest, title: str | None = None, *,
               transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Send the bot into the meeting; returns ``{id, state, session_id, text, …}``."""
    status, body = await _call(link, "POST", "/meetings", JOIN_TIMEOUT,
                               json=request.payload(title), transport=transport)
    if status not in (200, 201, 202):
        raise _error(status, body)
    meeting = _meeting(body)
    return public({"state": "starting", "active": True, "title": title,
                   "meeting_id": request.meeting_id, **meeting})


async def get(link: recorder.Link, meeting_id: str, *,
              transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    meeting_id = check_meeting_id(meeting_id)
    status, body = await _call(link, "GET", f"/meetings/{meeting_id}", READ_TIMEOUT,
                               transport=transport)
    if status != 200:
        raise _error(status, body)
    return public(_meeting(body))


async def current(link: recorder.Link, *,
                  transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any] | None:
    """The meeting the bot is in now (the newest active one), or None."""
    status, body = await _call(link, "GET", "/meetings", READ_TIMEOUT,
                               params={"limit": LIST_LIMIT}, transport=transport)
    if status != 200:
        raise _error(status, body)
    rows = body.get("meetings") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise MeetingError(502, "Athena sent an unreadable answer.")
    for row in rows:
        if isinstance(row, dict) and row.get("id") and (
                row.get("active") or row.get("state") in ACTIVE_STATES):
            return public(row)
    return None


async def leave(link: recorder.Link, meeting_id: str, *,
                transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Make the bot leave; answers with the meeting's final state. Idempotent."""
    meeting_id = check_meeting_id(meeting_id)
    status, body = await _call(link, "DELETE", f"/meetings/{meeting_id}", LEAVE_TIMEOUT,
                               transport=transport)
    if status != 200:
        raise _error(status, body)
    return public(_meeting(body))


def resolve() -> recorder.Link:
    """The module's link from the ``convo`` MCP server (``src/call_recorder.py``)."""
    try:
        return recorder.resolve()
    except recorder.RecorderError as exc:
        raise MeetingError(exc.status, exc.message, "setup") from None
