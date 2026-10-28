"""Fork: "Send Athena to a meeting" (src/meeting_bot.py, routes in routes/sound_module_routes.py).

Fictional meetings only: numbers, passcodes and hosts below are made up.
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import require_admin
from routes.sound_module_routes import setup_sound_module_routes
from src import call_recorder as rec
from src import meeting_bot as mb

TOKEN = "t" * 32
LINK = rec.Link(base="http://module:8001", token=TOKEN)
ROWS = [("srv1", "convo", f"http://module:8001/mcp?access_token={TOKEN}", True)]
MID = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"


def run(coro):
    return asyncio.run(coro)


def row(**kw):
    out = {"id": MID, "state": "recording", "reason": None, "active": True,
           "session_id": f"zoom-{MID}", "meeting_id": "85012345678", "title": None,
           "created_at": "2026-10-27T10:00:00+00:00",
           "started_at": "2026-10-27T10:00:20+00:00", "ended_at": None}
    out.update(kw)
    return out


class Module:
    """A fake Athena MCP HTTP app answering the meeting relay routes."""

    def __init__(self, status=200, body=None, text=None):
        self.status = status
        self.body = body
        self.text = text
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.body if self.body is not None else {})

    @property
    def transport(self):
        return httpx.MockTransport(self)


# ------------------------------------------------------------------ links
@pytest.mark.parametrize("text, want", [
    ("https://zoom.us/j/85012345678?pwd=AbC123.1", ("85012345678", "AbC123.1")),
    ("https://us06web.zoom.us/j/85012345678?pwd=xyz", ("85012345678", "xyz")),
    ("https://acme-corp.zoom.us/j/123456789", ("123456789", None)),
    ("http://zoom.us/j/1234567890?pwd=p", ("1234567890", "p")),
    ("zoom.us/j/85012345678?pwd=p", ("85012345678", "p")),
    ("us02web.zoom.us/j/85012345678", ("85012345678", None)),
    ("HTTPS://US06WEB.ZOOM.US/j/85012345678?pwd=p", ("85012345678", "p")),
    ("https://zoom.us/wc/join/85012345678?pwd=p", ("85012345678", "p")),
    ("https://zoom.us/wc/85012345678/join?pwd=p", ("85012345678", "p")),
    ("zoommtg://zoom.us/join?action=join&confno=85012345678&pwd=p", ("85012345678", "p")),
    ("zoomus://acme.zoom.us/join?confno=85012345678", ("85012345678", None)),
    ("<https://zoom.us/j/85012345678>", ("85012345678", None)),
    ("'https://zoom.us/j/85012345678?pwd=p'", ("85012345678", "p")),
    # BUG-25: path segments are case-insensitive, like the host and the scheme.
    ("https://zoom.us/J/85012345678", ("85012345678", None)),
    ("https://zoom.us/wc/JOIN/85012345678?pwd=p", ("85012345678", "p")),
    ("https://zoom.us/WC/85012345678/Join", ("85012345678", None)),
    ("zoommtg://zoom.us/JOIN?confno=85012345678", ("85012345678", None)),
])
def test_accepted_links(text, want):
    got = mb.check_link(text)
    assert (got.meeting_id, got.passcode) == want
    assert got.url.lower().startswith(("https://", "http://", "zoommtg://", "zoomus://"))


@pytest.mark.parametrize("text, words", [
    ("https://zoom.us/my/alice.example", "Personal links"),
    ("https://zoom.us/w/85012345678?tk=x", "Webinars"),
    ("https://zoom.us/s/85012345678?zak=x", "start link"),
    ("zoommtg://zoom.us/start?confno=85012345678", "start link"),
    ("zoommtg://zoom.us/join?action=join", "confno"),
    ("https://zoom.us/meeting/register/tJ0abc", "registration link"),
    ("https://zoom.us/webinar/register/WN_abc", "registration link"),
    ("https://zoomgov.com/j/1612345678", "Zoom for Government"),
    ("https://acme.zoomgov.com/j/1612345678", "Zoom for Government"),
    # BUG-25: a zoomgov link pasted without a scheme gets the same specific answer.
    ("zoomgov.com/j/1612345678", "Zoom for Government"),
    ("acme.ZOOMGOV.com/j/1612345678", "Zoom for Government"),
    ("zoommtg://zoom.us/START?confno=85012345678", "start link"),
    ("https://zoom.us.example.com/j/85012345678", "isn't a Zoom meeting link"),
    ("https://example.com/zoom.us/j/85012345678", "isn't a Zoom meeting link"),
    ("https://evilzoom.us/j/85012345678", "isn't a Zoom meeting link"),
    ("https://zoom.example/j/85012345678", "isn't a Zoom meeting link"),
    ("https://user:pw@zoom.us/j/85012345678", "isn't a Zoom meeting link"),
    ("https://alice@zoom.us/j/85012345678", "isn't a Zoom meeting link"),
    ("https://zoom.us:8443/j/85012345678", "isn't a Zoom meeting link"),
    ("https://zoom.us:99999/j/85012345678", "isn't a Zoom meeting link"),
    ("ftp://zoom.us/j/85012345678", "isn't a Zoom meeting link"),
    ("javascript:alert(1)", "isn't a Zoom meeting link"),
    ("https://zoom.us/j/12345", "9 to 11 digits"),
    ("https://zoom.us/j/123456789012", "9 to 11 digits"),
    ("https://zoom.us/j/85012345678/extra", "isn't a Zoom meeting link"),
    ("https://zoom.us/", "isn't a Zoom meeting link"),
    ("https://zoom.us/j/85012345678?pwd=a%20b", "passcode"),
    ("", "Paste a Zoom meeting link"),
])
def test_refused_links(text, words):
    with pytest.raises(mb.MeetingError) as e:
        mb.check_link(text)
    assert e.value.status == 400 and e.value.code == "bad_link"
    assert words in e.value.message


INVITE = """Alice Example is inviting you to a scheduled Zoom meeting.

Topic: Fictional planning sync
Time: Oct 28, 2026 10:00 AM Amsterdam

Join Zoom Meeting
https://us06web.zoom.us/j/85012345678?pwd=Zm9vYmFyLmV4YW1wbGU.1

Meeting ID: 850 1234 5678
Passcode: 424242

---

One tap mobile
+31201234567,,85012345678#,,,,*424242# Netherlands

Find your local number: https://us06web.zoom.us/u/kAbCdEf
"""


def test_invite_text_gives_its_first_join_link():
    got = mb.parse_join(INVITE)
    assert got.url == "https://us06web.zoom.us/j/85012345678?pwd=Zm9vYmFyLmV4YW1wbGU.1"
    assert got.payload() == {"url": got.url, "passcode": "Zm9vYmFyLmV4YW1wbGU.1"}


def test_invite_link_without_pwd_takes_the_invite_passcode():
    text = "Join: https://acme.zoom.us/j/85012345678.\nMeeting ID: 850 1234 5678\nPasscode: s3cret"
    got = mb.parse_join(text)
    assert got.url == "https://acme.zoom.us/j/85012345678"  # trailing "." dropped
    assert got.passcode == "s3cret"


def test_invite_skips_a_non_join_zoom_link_before_the_join_link():
    text = ("Find your local number: https://zoom.us/u/abc then join "
            "(https://zoom.us/j/123456789?pwd=x).")
    got = mb.parse_join(text)
    assert (got.meeting_id, got.passcode) == ("123456789", "x")


def test_invite_without_a_link_uses_its_meeting_id_and_passcode():
    got = mb.parse_join("Meeting ID: 850 1234 5678\nPasscode: 424242\n")
    assert got.payload("Sync") == {"meeting_id": "85012345678", "passcode": "424242",
                                   "title": "Sync"}


def test_pasted_text_with_only_a_refused_zoom_link_says_why():
    with pytest.raises(mb.MeetingError) as e:
        mb.parse_join("Join my room: https://zoom.us/my/alice.example see you")
    assert "Personal links" in e.value.message
    with pytest.raises(mb.MeetingError) as e:
        mb.parse_join("Webinar link https://zoom.us/w/85012345678?tk=abc today")
    assert "Webinars" in e.value.message
    with pytest.raises(mb.MeetingError) as e:
        mb.parse_join("go to https://zoom.us.example.com/j/85012345678 now")
    assert "isn't a Zoom meeting link" in e.value.message


def test_pasted_text_without_a_zoom_link_is_refused():
    for text in ("see https://meet.example.com/abc-defg-hij at 10", "hello there",
                 "https://example.com/j/85012345678 hello"):
        with pytest.raises(mb.MeetingError) as e:
            mb.parse_join(text)
        assert "No Zoom meeting link found" in e.value.message


@pytest.mark.parametrize("text, passcode, want", [
    ("850 1234 5678", "424242", {"meeting_id": "85012345678", "passcode": "424242"}),
    ("850-1234-5678", None, {"meeting_id": "85012345678"}),
    ("123456789", " x1 ", {"meeting_id": "123456789", "passcode": "x1"}),
    ("https://zoom.us/j/85012345678", "typed",
     {"url": "https://zoom.us/j/85012345678", "passcode": "typed"}),
    ("https://zoom.us/j/85012345678?pwd=inlink", "typed",
     {"url": "https://zoom.us/j/85012345678?pwd=inlink", "passcode": "inlink"}),
])
def test_meeting_id_and_passcode_typed_separately(text, passcode, want):
    assert mb.parse_join(text, passcode).payload() == want


@pytest.mark.parametrize("text, passcode, words", [
    ("1234 5678", None, "9 to 11 digits"),
    ("", None, "Paste a Zoom meeting link"),
    ("850 1234 5678", "has space", "passcode"),
    ("850 1234 5678", "é" * 3, "passcode"),
    ("x" * (mb.MAX_INPUT_CHARS + 1), None, "too long"),
    ("not-a-link", None, "isn't a Zoom meeting link"),
])
def test_bad_input(text, passcode, words):
    with pytest.raises(mb.MeetingError) as e:
        mb.parse_join(text, passcode)
    assert e.value.status == 400 and words in e.value.message


def test_title_and_meeting_id_checks():
    assert mb.check_title("  Fictional   sync ") == "Fictional sync"
    assert mb.check_title("  ") is None
    with pytest.raises(mb.MeetingError):
        mb.check_title("x" * 201)
    assert mb.check_meeting_id(MID) == MID
    for bad in ("", "current", MID.upper(), MID + "0", "../" + MID[:29]):
        with pytest.raises(mb.MeetingError) as e:
            mb.check_meeting_id(bad)
        assert e.value.status == 404


@pytest.mark.parametrize("state, reason, started, words", [
    ("starting", None, None, "Joining…"),
    ("starting", "waiting_for_owner", None,
     "Waiting for you to join the meeting — Athena joins as soon as you're in."),
    ("failed", "owner_not_joined", None,
     "You didn't join the meeting in time, so Athena gave up."),
    ("ended", "owner_left", "x", "Ended: you left the meeting, so Athena left too."),
    ("waiting_room", None, None, "the host has to admit Athena"),
    ("waiting_permission", None, None,
     "In the meeting — waiting for the host to allow recording (Zoom asked them once)."),
    ("waiting_permission", "denied", None,
     "The host declined recording. Athena stays in the meeting in case they allow it later."),
    ("recording", None, "x", "Recording."),
    ("recording", "no_audio", "x", "Zoom isn't sending audio"),
    ("ended", "meeting_ended", "x", "the meeting is over"),
    ("ended", "alone", "x", "only one left"),
    ("ended", "stopped", "x", "you stopped it"),
    ("ended", "something_new", "x", "Ended."),
    ("failed", "wrong_passcode", None, "Couldn't join: wrong passcode."),
    ("failed", "external_meeting_not_allowed", None, "published on the Zoom Marketplace"),
    ("failed", "outside_users_blocked", None, "only lets in people from certain Zoom"),
    ("failed", "blocked_by_account_admin", None, "admin blocks Athena's Zoom app"),
    ("failed", "zoom_signin_required", None, "Sign in to Zoom again"),
    ("failed", "waiting_room_timeout", None, "didn't admit Athena"),
    ("failed", "bot_crashed", "x", "Stopped: the meeting bot stopped unexpectedly."),
    ("failed", "new_reason", None, "Couldn't join: new reason."),
    ("odd", None, None, "State: odd."),
])
def test_describe(state, reason, started, words):
    assert words in mb.describe({"state": state, "reason": reason, "started_at": started})


# ------------------------------------------------------------------ module calls
def test_join_posts_json_with_the_mcp_token():
    module = Module(202, {"id": MID, "state": "starting", "session_id": f"zoom-{MID}"})
    req = mb.parse_join("https://zoom.us/j/85012345678?pwd=p")
    got = run(mb.join(LINK, req, "Fictional sync", transport=module.transport))
    assert got["id"] == MID and got["state"] == "starting" and got["active"] is True
    assert got["text"] == "Joining…" and got["title"] == "Fictional sync"
    assert got["meeting_id"] == "85012345678"
    sent = module.requests[0]
    assert sent.method == "POST" and sent.url.path == "/meetings"
    assert dict(sent.url.params) == {}  # the token goes in the header, never the URL
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["content-type"] == "application/json"
    assert json.loads(sent.content) == {"url": "https://zoom.us/j/85012345678?pwd=p",
                                        "passcode": "p", "title": "Fictional sync"}


@pytest.mark.parametrize("status, body, code, out_status, words", [
    (400, {"detail": "webinars are not supported yet"}, "bad_link", 400, "webinars"),
    (409, {"detail": "busy", "message": "Athena is already in a meeting (1 of 1 bot).",
           "active": OTHER}, "busy", 409, "busy in another meeting"),
    (501, {"detail": "meetings are off: CONVO_MEETING_BOT_PATH is not set"}, "off", 501,
     "Meetings aren't set up on the server"),
    (500, {"detail": "could not start the meeting bot: denied"}, "bot", 502,
     "couldn't start the meeting bot"),
    (502, {"detail": "Athena's ingest API is not reachable"}, "unreachable", 502,
     "isn't answering"),
    (401, {"detail": "unauthorized"}, "token", 502, "refused the token"),
    (503, {"detail": "zoom_signin_needed", "message": "Sign in to Zoom again."}, "signin",
     503, ("Athena's Zoom sign-in is missing or expired — sign in to Zoom on the "
           "Meetings page.")),
    (502, {"detail": "zoom_unavailable", "message": "Zoom ZAK/OBF request failed."}, "zoom",
     502, "Zoom didn't give Athena a join token; try again in a minute."),
    (404, {"detail": "Not Found"}, "update", 502, "Update Athena to use meetings"),
    (405, {"detail": "Method Not Allowed"}, "update", 502, "Update Athena"),
])
def test_join_errors(status, body, code, out_status, words):
    module = Module(status, body)
    with pytest.raises(mb.MeetingError) as e:
        run(mb.join(LINK, mb.parse_join("850 1234 5678"), transport=module.transport))
    assert (e.value.code, e.value.status) == (code, out_status)
    assert words in e.value.message
    assert TOKEN not in e.value.message
    if code == "busy":
        assert e.value.active == OTHER and e.value.body()["active"] == OTHER


def test_old_module_plain_404_says_update():
    module = Module(404, text="Not Found")
    with pytest.raises(mb.MeetingError) as e:
        run(mb.join(LINK, mb.parse_join("850 1234 5678"), transport=module.transport))
    assert e.value.code == "update" and "Update Athena to use meetings" in e.value.message
    with pytest.raises(mb.MeetingError) as e:
        run(mb.current(LINK, transport=module.transport))
    assert e.value.code == "update"


def test_busy_with_a_bad_active_id_is_dropped():
    module = Module(409, {"detail": "busy", "active": "../x"})
    with pytest.raises(mb.MeetingError) as e:
        run(mb.join(LINK, mb.parse_join("850 1234 5678"), transport=module.transport))
    assert e.value.code == "busy" and e.value.active is None


def test_unreachable_and_timeout():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(mb.MeetingError) as e:
        run(mb.get(LINK, MID, transport=httpx.MockTransport(down)))
    assert e.value.code == "unreachable" and e.value.status == 502
    with pytest.raises(mb.MeetingError) as e:
        run(mb.leave(LINK, MID, transport=httpx.MockTransport(slow)))
    assert e.value.code == "timeout" and e.value.status == 504


def test_get_and_leave():
    module = Module(200, row(state="waiting_room", started_at=None))
    got = run(mb.get(LINK, MID, transport=module.transport))
    assert got["state"] == "waiting_room" and "admit Athena" in got["text"]
    assert module.requests[0].url.path == f"/meetings/{MID}"
    gone = Module(200, row(state="ended", reason="stopped", active=False))
    got = run(mb.leave(LINK, MID, transport=gone.transport))
    assert gone.requests[0].method == "DELETE" and got["active"] is False
    assert got["text"] == "Ended: you stopped it."
    missing = Module(404, {"detail": "meeting not found"})
    with pytest.raises(mb.MeetingError) as e:
        run(mb.get(LINK, MID, transport=missing.transport))
    assert e.value.status == 404 and e.value.code == "not_found"


def test_bad_meeting_id_never_calls_the_module():
    module = Module(200, row())
    with pytest.raises(mb.MeetingError):
        run(mb.get(LINK, "../../health", transport=module.transport))
    with pytest.raises(mb.MeetingError):
        run(mb.leave(LINK, "x", transport=module.transport))
    assert module.requests == []


def test_current_picks_the_newest_active_meeting():
    module = Module(200, {"meetings": [row(id=OTHER, state="ended", active=False),
                                       row(state="starting", started_at=None)]})
    got = run(mb.current(LINK, transport=module.transport))
    assert got["id"] == MID and got["text"] == "Joining…"
    assert dict(module.requests[0].url.params) == {"limit": str(mb.LIST_LIMIT)}
    assert module.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    idle = Module(200, {"meetings": [row(state="failed", reason="wrong_passcode",
                                         active=False)]})
    assert run(mb.current(LINK, transport=idle.transport)) is None
    assert run(mb.current(LINK, transport=Module(200, {"meetings": []}).transport)) is None
    with pytest.raises(mb.MeetingError):
        run(mb.current(LINK, transport=Module(200, {"nope": 1}).transport))


def test_public_keeps_only_known_fields():
    got = mb.public({**row(), "upload_token_hash": "secret", "pid": 42})
    assert "upload_token_hash" not in got and "pid" not in got
    assert set(got) == set(mb._FIELDS) | {"text"}


def test_importing_starts_nothing():
    """No thread, timer or poll: the module only acts when a page request calls it."""
    code = ("import threading; n = threading.active_count(); import src.meeting_bot; "
            "assert threading.active_count() == n, threading.enumerate()")
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True, timeout=60)


def test_no_mcp_tool_is_registered():
    """The meeting routes are plain HTTP routes of the hub, never tools for the model."""
    import src.meeting_bot as module

    names = [n for n in dir(module) if "tool" in n.lower()]
    assert names == []
    paths = {getattr(r, "path", "") for r in setup_sound_module_routes().routes}
    assert "/api/modules/sound/meetings" in paths
    assert not any("mcp" in p for p in paths)
    root = Path(__file__).resolve().parents[1]
    for f in [root / "src/builtin_mcp.py", *sorted((root / "mcp_servers").rglob("*.py"))]:
        assert "meeting_bot" not in f.read_text(encoding="utf-8", errors="replace"), f


# ------------------------------------------------------------------ Zoom sign-in
AUTHORIZE = ("https://zoom.us/oauth/authorize?response_type=code&client_id=FictionalClient"
             "&redirect_uri=http%3A%2F%2Flocalhost%3A9%2Fzoom&state=FictionalState123")
PASTED = "http://localhost:9/zoom?code=FictionalCode987&state=FictionalState123"


def test_signin_status_reads_and_cleans_the_answer():
    module = Module(200, {"configured": False, "signed_in": False, "join_token": "weird",
                          "missing": ["CONVO_ZOOM_CLIENT_ID", "not a setting", 7,
                                      "CONVO_ZOOM_CLIENT_SECRET"]})
    got = run(mb.signin_status(LINK, transport=module.transport))
    assert got == {"configured": False, "signed_in": False, "join_token": "none",
                   "missing": ["CONVO_ZOOM_CLIENT_ID", "CONVO_ZOOM_CLIENT_SECRET"]}
    sent = module.requests[0]
    assert (sent.method, sent.url.path) == ("GET", "/zoom/signin")
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    module = Module(200, {"configured": True, "signed_in": True, "join_token": "obf",
                          "missing": []})
    got = run(mb.signin_status(LINK, transport=module.transport))
    assert got["signed_in"] is True and got["join_token"] == "obf"


def test_signin_start_returns_zoom_url():
    module = Module(200, {"url": AUTHORIZE, "expires_in": 600})
    got = run(mb.signin_start(LINK, transport=module.transport))
    assert got == {"url": AUTHORIZE, "expires_in": 600}
    assert (module.requests[0].method, module.requests[0].url.path) == (
        "POST", "/zoom/signin/start")


@pytest.mark.parametrize("url", [
    "http://zoom.us/oauth/authorize?x=1", "https://zoom.us.example.com/oauth/authorize",
    "https://evil.example/oauth", "javascript:alert(1)", "https://user@zoom.us/oauth",
    "https://zoom.us:8443/oauth", "", None,
])
def test_signin_start_refuses_a_non_zoom_url(url):
    module = Module(200, {"url": url, "expires_in": 600})
    with pytest.raises(mb.MeetingError) as e:
        run(mb.signin_start(LINK, transport=module.transport))
    assert e.value.status == 502 and "isn't Zoom's" in e.value.message


def test_signin_finish_sends_the_paste():
    module = Module(200, {"ok": True, "signed_in": True, "join_token": "zak"})
    got = run(mb.signin_finish(LINK, f"  {PASTED} ", transport=module.transport))
    assert got["ok"] is True and got["signed_in"] is True and got["join_token"] == "zak"
    sent = module.requests[0]
    assert (sent.method, sent.url.path) == ("POST", "/zoom/signin/finish")
    assert json.loads(sent.content) == {"pasted": PASTED}
    assert "FictionalCode987" not in str(sent.url)


@pytest.mark.parametrize("call", ["status", "start", "finish"])
@pytest.mark.parametrize("status, body, code, out_status, words", [
    (400, {"detail": "That sign-in attempt expired. Click Sign in to Zoom again."},
     "bad_paste", 400, "That sign-in attempt expired"),
    (501, {"detail": "CONVO_ZOOM_CLIENT_ID is not set"}, "not_configured", 501,
     "Athena's Zoom app isn't set up yet: CONVO_ZOOM_CLIENT_ID"),
    (502, {"detail": "Zoom is not reachable"}, "zoom", 502,
     "Athena couldn't reach Zoom: Zoom is not reachable"),
    (404, {"detail": "Not Found"}, "update", 502, "Update Athena to sign in to Zoom from here"),
    (405, {"detail": "Method Not Allowed"}, "update", 502, "Update Athena to sign in"),
    (404, None, "update", 502, "Update Athena to sign in"),
    (401, {"detail": "unauthorized"}, "token", 502, "refused the token"),
    (500, {"detail": "boom"}, "error", 502, "Athena HTTP 500: boom"),
])
def test_signin_errors(call, status, body, code, out_status, words):
    module = Module(status, body) if body is not None else Module(status, text="nope")
    fn = {"status": lambda: mb.signin_status(LINK, transport=module.transport),
          "start": lambda: mb.signin_start(LINK, transport=module.transport),
          "finish": lambda: mb.signin_finish(LINK, PASTED, transport=module.transport)}[call]
    with pytest.raises(mb.MeetingError) as e:
        run(fn())
    assert (e.value.code, e.value.status) == (code, out_status)
    assert words in e.value.message
    assert TOKEN not in e.value.message


def test_signin_finish_checks_the_paste_without_calling():
    module = Module(200, {"ok": True})
    for pasted in ("", "   ", None, "x" * (mb.MAX_PASTE_CHARS + 1)):
        with pytest.raises(mb.MeetingError) as e:
            run(mb.signin_finish(LINK, pasted, transport=module.transport))
        assert e.value.status == 400 and e.value.code == "bad_paste"
    assert module.requests == []


def test_signin_never_logs_the_paste_or_the_url_query(caplog):
    caplog.set_level("DEBUG")
    for status, body in ((200, {"ok": True, "signed_in": True, "join_token": "obf"}),
                         (400, {"detail": "Zoom refused the code."}),
                         (502, {"detail": "Zoom is not reachable"})):
        try:
            run(mb.signin_finish(LINK, PASTED, transport=Module(status, body).transport))
        except mb.MeetingError:
            pass
    run(mb.signin_start(LINK, transport=Module(200, {"url": AUTHORIZE}).transport))

    def boom(request):
        raise httpx.ConnectError("down")

    with pytest.raises(mb.MeetingError):
        run(mb.signin_finish(LINK, PASTED, transport=httpx.MockTransport(boom)))

    def slow(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(mb.MeetingError):
        run(mb.signin_finish(LINK, PASTED, transport=httpx.MockTransport(slow)))
    assert "FictionalCode987" not in caplog.text
    assert "FictionalState123" not in caplog.text
    assert "FictionalClient" not in caplog.text


def test_signin_needed_points_at_the_page():
    err = mb._error(503, {"detail": "zoom_signin_needed"})
    assert err.code == "signin"
    assert err.message == mb.SIGNIN_NEEDED
    assert "Meetings page" in err.message and "convo-server" not in err.message


# ------------------------------------------------------------------ routes
class Router:
    """Answers each relay route like Athena does."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.signin = (200, {"configured": True, "signed_in": False, "join_token": "none",
                             "missing": []})
        self.start = (200, {"url": AUTHORIZE, "expires_in": 600})
        self.finish = (200, {"ok": True, "signed_in": True, "join_token": "obf"})
        self.join = (202, {"id": MID, "state": "starting", "session_id": f"zoom-{MID}"})
        self.list = (200, {"meetings": [row()]})
        self.one = (200, row())
        self.leave = (200, row(state="ended", reason="stopped", active=False))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/zoom/signin":
            status, body = self.signin
        elif path == "/zoom/signin/start":
            status, body = self.start
        elif path == "/zoom/signin/finish":
            status, body = self.finish
        elif path == "/meetings" and method == "POST":
            status, body = self.join
        elif path == "/meetings":
            status, body = self.list
        elif method == "DELETE":
            status, body = self.leave
        else:
            status, body = self.one
        return httpx.Response(status, json=body)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv(rec.SERVER_ENV, raising=False)
    monkeypatch.setattr(rec, "_server_rows", lambda: ROWS)
    module = Router()
    real = httpx.AsyncClient

    def fake_client(**kw):
        kw["transport"] = httpx.MockTransport(module)
        return real(**kw)

    monkeypatch.setattr(mb.httpx, "AsyncClient", fake_client)
    app = FastAPI()
    app.include_router(setup_sound_module_routes())
    app.dependency_overrides[require_admin] = lambda: None
    with TestClient(app) as c:
        c.module = module
        yield c


H = {"X-Odysseus-Recorder": "1"}


def test_route_join_state_leave(client):
    r = client.post("/api/modules/sound/meetings", headers=H,
                    json={"link": INVITE, "title": "Fictional sync"})
    assert r.status_code == 202, r.text
    assert r.json()["meeting"]["id"] == MID and r.json()["meeting"]["text"] == "Joining…"
    sent = json.loads(client.module.requests[-1].content)
    assert sent["url"].startswith("https://us06web.zoom.us/j/85012345678")
    assert sent["title"] == "Fictional sync"
    r = client.get(f"/api/modules/sound/meetings/{MID}")
    assert r.status_code == 200 and r.json()["meeting"]["text"] == "Recording."
    r = client.get("/api/modules/sound/meetings/current")
    assert r.status_code == 200 and r.json()["meeting"]["id"] == MID
    r = client.delete(f"/api/modules/sound/meetings/{MID}", headers=H)
    assert r.status_code == 200 and r.json()["meeting"]["state"] == "ended"


def test_route_bad_link_never_calls_the_module(client):
    r = client.post("/api/modules/sound/meetings", headers=H,
                    json={"link": "https://zoom.us/my/alice.example"})
    assert r.status_code == 400 and r.json()["code"] == "bad_link"
    assert "Personal links" in r.json()["detail"]
    r = client.post("/api/modules/sound/meetings", headers=H, json={"link": "hello"})
    assert r.status_code == 400
    assert client.module.requests == []


def test_route_busy_returns_the_active_id(client):
    client.module.join = (409, {"detail": "busy", "message": "…", "active": OTHER})
    r = client.post("/api/modules/sound/meetings", headers=H, json={"link": "850 1234 5678",
                                                                    "passcode": "424242"})
    assert r.status_code == 409
    assert r.json() == {"detail": mb._error(409, {}).message, "code": "busy", "active": OTHER}


def test_route_meetings_off_and_old_module(client):
    client.module.join = (501, {"detail": "meetings are off: CONVO_MEETING_BOT_PATH is not set"})
    r = client.post("/api/modules/sound/meetings", headers=H, json={"link": "850 1234 5678"})
    assert r.status_code == 501 and r.json()["code"] == "off"
    client.module.list = (404, {"detail": "Not Found"})
    r = client.get("/api/modules/sound/meetings/current")
    assert r.status_code == 502 and r.json()["code"] == "update"


def test_route_writes_need_the_recorder_header(client):
    assert client.post("/api/modules/sound/meetings",
                       json={"link": "850 1234 5678"}).status_code == 400
    assert client.delete(f"/api/modules/sound/meetings/{MID}").status_code == 400
    assert client.module.requests == []


def test_route_unknown_id_is_404_without_a_call(client):
    r = client.get("/api/modules/sound/meetings/not-an-id")
    assert r.status_code == 404
    assert client.delete("/api/modules/sound/meetings/not-an-id", headers=H).status_code == 404
    assert client.module.requests == []


def test_route_not_configured(client, monkeypatch):
    monkeypatch.setattr(rec, "_server_rows", list)
    r = client.get("/api/modules/sound/meetings/current")
    assert r.status_code == 503 and "Settings → MCP" in r.json()["detail"]
    assert r.json()["code"] == "setup"


def test_page_has_the_meeting_box(client):
    r = client.get("/modules/sound/record")
    assert r.status_code == 200
    assert "Send Athena to a meeting" in r.text and "meetingBot.js" in r.text


def test_routes_are_admin_only(monkeypatch):
    app = FastAPI()
    app.include_router(setup_sound_module_routes())
    monkeypatch.setattr("core.middleware.auth_disabled", lambda: False)
    with TestClient(app) as c:
        assert c.get("/api/modules/sound/meetings/current").status_code == 403
        assert c.get(f"/api/modules/sound/meetings/{MID}").status_code == 403
        assert c.post("/api/modules/sound/meetings", headers=H,
                      json={"link": "850 1234 5678"}).status_code == 403
        assert c.delete(f"/api/modules/sound/meetings/{MID}", headers=H).status_code == 403
        assert c.get("/api/modules/sound/zoom-signin").status_code == 403
        assert c.post("/api/modules/sound/zoom-signin/start", headers=H).status_code == 403
        assert c.post("/api/modules/sound/zoom-signin/finish", headers=H,
                      json={"pasted": PASTED}).status_code == 403


def test_route_zoom_signin_flow(client):
    r = client.get("/api/modules/sound/zoom-signin")
    assert r.status_code == 200
    assert r.json() == {"configured": True, "signed_in": False, "join_token": "none",
                        "missing": []}
    r = client.post("/api/modules/sound/zoom-signin/start", headers=H)
    assert r.status_code == 200 and r.json() == {"url": AUTHORIZE, "expires_in": 600}
    r = client.post("/api/modules/sound/zoom-signin/finish", headers=H, json={"pasted": PASTED})
    assert r.status_code == 200, r.text
    assert r.json()["signed_in"] is True and r.json()["join_token"] == "obf"
    assert json.loads(client.module.requests[-1].content) == {"pasted": PASTED}


def test_route_zoom_signin_errors(client):
    client.module.finish = (400, {"detail": "That address has no Zoom code in it."})
    r = client.post("/api/modules/sound/zoom-signin/finish", headers=H, json={"pasted": "hi"})
    assert r.status_code == 400
    assert r.json() == {"detail": "That address has no Zoom code in it.", "code": "bad_paste"}
    client.module.start = (501, {"detail": "CONVO_ZOOM_CLIENT_ID is not set"})
    r = client.post("/api/modules/sound/zoom-signin/start", headers=H)
    assert r.status_code == 501 and r.json()["code"] == "not_configured"
    client.module.signin = (404, {"detail": "Not Found"})
    r = client.get("/api/modules/sound/zoom-signin")
    assert r.status_code == 502 and r.json()["code"] == "update"
    assert "Update Athena to sign in to Zoom from here" in r.json()["detail"]


def test_route_zoom_signin_writes_need_the_recorder_header(client):
    assert client.post("/api/modules/sound/zoom-signin/start").status_code == 400
    assert client.post("/api/modules/sound/zoom-signin/finish",
                       json={"pasted": PASTED}).status_code == 400
    assert client.module.requests == []


def test_route_zoom_signin_finish_body(client):
    big = {"pasted": "x" * 9000}
    r = client.post("/api/modules/sound/zoom-signin/finish", headers=H, json=big)
    assert r.status_code == 413
    r = client.post("/api/modules/sound/zoom-signin/finish", headers=H,
                    json={"pasted": "x" * (mb.MAX_PASTE_CHARS + 1)})
    assert r.status_code == 400 and r.json()["code"] == "bad_paste"
    r = client.post("/api/modules/sound/zoom-signin/finish", headers=H,
                    content=b"not json {FictionalCode987")
    assert r.status_code == 400 and "FictionalCode987" not in r.text
    r = client.post("/api/modules/sound/zoom-signin/finish", headers=H, json={"pasted": 5})
    assert r.status_code == 400
    assert client.module.requests == []


def test_route_zoom_signin_logs_no_paste(client, caplog):
    caplog.set_level("DEBUG")
    client.module.finish = (400, {"detail": "Zoom refused the code."})
    client.post("/api/modules/sound/zoom-signin/finish", headers=H, json={"pasted": PASTED})
    client.module.finish = (200, {"ok": True, "signed_in": True, "join_token": "obf"})
    client.post("/api/modules/sound/zoom-signin/finish", headers=H, json={"pasted": PASTED})
    assert "FictionalCode987" not in caplog.text


def test_page_has_the_zoom_signin_line(client):
    r = client.get("/modules/sound/record")
    text = " ".join(r.text.split())
    assert "Sign in to Zoom" in text and 'id="mb-signin-finish"' in text
    assert ("In the new tab, sign in to Zoom and click Allow. Zoom then opens a page that "
            "can't load — that's expected. Copy that page's address from the address bar "
            "and paste it here.") in text


def test_waiting_for_owner_still_counts_as_active():
    got = mb.public(row(state="starting", reason="waiting_for_owner", started_at=None))
    assert got["active"] is True and got["text"].startswith("Waiting for you to join")


def test_route_signin_needed(client):
    client.module.join = (503, {"detail": "zoom_signin_needed", "message": "…"})
    r = client.post("/api/modules/sound/meetings", headers=H, json={"link": "850 1234 5678"})
    assert r.status_code == 503 and r.json()["code"] == "signin"
    assert "sign in to Zoom on the Meetings page" in r.json()["detail"]
    assert "convo-server" not in r.json()["detail"]


def test_page_has_the_join_yourself_hint(client):
    r = client.get("/modules/sound/record")
    assert "Join the meeting yourself too — Athena records while you're in it." in r.text


def test_waiting_permission_is_active():
    for reason in (None, "denied"):
        got = mb.public({"id": MID, "state": "waiting_permission", "reason": reason})
        assert got["active"] is True
    module = Module(200, {"meetings": [row(state="waiting_permission", reason="denied",
                                           active=True)]})
    assert run(mb.current(LINK, transport=module.transport))["id"] == MID
