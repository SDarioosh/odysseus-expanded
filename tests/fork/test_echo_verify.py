"""Fork: scripts/echo_verify.py, the owner's check of a server the way an Echo uses it.
A fake hub answers in memory (no network); fictional data only."""
from __future__ import annotations

import hashlib
import io
import json
import wave
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import echo_verify as ev

APK = b"PK\x03\x04" + b"fake apk body" * 10
SHA = hashlib.sha256(APK).hexdigest()
TOKEN = "ody_test_token_not_real"
TODAY = {"date": "2026-11-06", "generated": "2026-11-06T07:45:00-05:00", "headline": None,
         "focus": [], "events": [{"uid": "e1", "title": "Dentist", "start": "2026-11-06T15:00",
                                  "end": None, "all_day": False, "location": None}],
         "todos": [{"ref": "todo:41:2", "title": "Call the plumber", "due": None,
                    "list": "Home"}],
         "action_items": [{"id": "17", "title": "Send Sam the photos", "source": "athena"}],
         "counts": {"todos_open": 3, "action_items_open": 1}}


class FakeHub(ev.Http):
    """Answers like the hub; ``override[(method, path)]`` replaces one answer."""

    def __init__(self, *, release: bool = True, owner_feed: bool = True) -> None:
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.release = release
        self.override: dict[tuple[str, str], ev.Response] = {}
        self.signed_in = False
        self.feed: list[dict] = []
        self.owner_feed = owner_feed

    def send(self, method, path, *, headers=None, body=None):
        headers = headers or {}
        self.calls.append((method, path, headers, body))
        url = urlsplit(path)
        key = (method, url.path)
        if key in self.override:
            return self.override[key]
        authed = headers.get("Authorization") == f"Bearer {TOKEN}"
        if url.path.startswith(ev.VOICE) or url.path.startswith(ev.ATHENA):
            if not authed:
                return _json(401, {"detail": "Not authenticated"})
        if key == ("GET", ev.VOICE + "/models"):
            return _json(200, {"object": "list", "data": [{"id": "odysseus"}]})
        if key == ("GET", ev.VOICE + "/today"):
            return _json(200, TODAY)
        if key == ("GET", ev.VOICE + "/app/latest"):
            if not self.release:
                return _json(404, {"error": "no release"})
            return _json(200, {"app": "odysseus-echo", "version_name": "0.14.0+odysseus.30",
                               "version_code": 1400030, "size": len(APK), "sha256": SHA,
                               "notes": ""})
        if key == ("GET", ev.VOICE + "/app/apk"):
            etag = {"etag": f'"{SHA}"'}
            if headers.get("Range") == "bytes=0-3":
                return ev.Response(206, {**etag, "content-range": f"bytes 0-3/{len(APK)}"},
                                   APK[:4])
            return ev.Response(200, etag, APK)
        if key == ("POST", ev.VOICE + "/satellite/hello"):
            return _json(200, {"ok": True})
        if key == ("GET", ev.VOICE + "/announcements"):
            since = int(parse_qs(url.query)["since"][0])
            device = parse_qs(url.query)["device_id"][0]
            rows = [r for r in self.feed if r["at"] > since
                    and r["target"].get("device_id") == device] if self.owner_feed else []
            return _json(200, {"announcements": rows})
        if key == ("GET", ev.ATHENA + "/health"):
            return _json(200, {"ok": True})
        if key == ("POST", ev.ATHENA + "/chunk"):
            return _json(200, {"ok": True, "stop": False})
        if method == "POST" and url.path.startswith(ev.ATHENA + "/sessions/"):
            return _json(200, {"ok": True, "stop": True})
        if key == ("POST", "/api/auth/login"):
            self.signed_in = json.loads(body)["password"] == "pw"
            return _json(200 if self.signed_in else 401, {"ok": self.signed_in})
        if key == ("POST", "/api/fork/voice/announce"):
            if not self.signed_in:
                return _json(403, {"detail": "admin only"})
            sent = json.loads(body)
            row = {"id": "an_0123456789abcdef", "at": 10 ** 13, "expires_at": 10 ** 13 + 1,
                   "kind": "announcement", "title": "Test announcement",
                   "text": sent["message"], "target": {"device_id": sent["device_id"]},
                   "ref": None}
            self.feed.append(row)
            return _json(200, {"ok": True, "problem": None, "id": row["id"],
                               "spoken": bool(sent["speak"])})
        return _json(404, {"detail": "Not Found"})


def _json(status, body):
    return ev.Response(status, {"content-type": "application/json"}, json.dumps(body).encode())


def by_name(results):
    return {r.name: r for r in results}


def opts(**kw):
    return ev.Options(token=TOKEN, stamp="20261106120000", **kw)


def test_everything_passes_against_a_good_hub():
    hub = FakeHub()
    results = ev.run(hub, opts(device_id="echo-kitchen", admin_user="admin",
                               admin_password="pw"))
    assert [r.status for r in results] == [ev.PASS] * 10, results
    out = io.StringIO()
    assert ev.report(results, out) == 0
    assert "10 passed, 0 failed, 0 skipped" in out.getvalue()
    assert TOKEN not in out.getvalue()
    assert "Call the plumber" not in out.getvalue()  # counts only, never content


def test_without_extras_the_writes_are_skipped():
    hub = FakeHub()
    results = by_name(ev.run(hub, opts(voice_note=False)))
    assert results["satellite/hello"].status == ev.SKIP
    assert results["test voice note (WAV, kind=note)"].status == ev.SKIP
    assert results["test announcement (feed)"].status == ev.SKIP
    assert not [c for c in hub.calls if c[0] == "POST"]  # read-only


def test_the_voice_note_is_a_silent_echo_wav_marked_as_a_note():
    hub = FakeHub()
    ev.check_voice_note(hub, opts())
    method, path, headers, body = hub.calls[0]
    assert (method, path) == ("POST", ev.ATHENA + "/chunk")
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    for field in (b'name="kind"\r\n\r\nnote', b'name="codec"\r\n\r\nwav',
                  b'name="device"\r\n\r\necho-verify',
                  b'name="session_id"\r\n\r\necho-verify-20261106120000'):
        assert field in body
    start = body.index(b"RIFF")
    with wave.open(io.BytesIO(body[start:]), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        assert w.getnframes() == 48000
        assert set(w.readframes(48000)) == {0}
    assert hub.calls[1][:2] == ("POST", ev.ATHENA + "/sessions/echo-verify-20261106120000/end")


def test_the_announcement_goes_to_a_made_up_device_unless_spoken():
    hub = FakeHub()
    r = ev.check_announcement(hub, opts(admin_user="admin", admin_password="pw"))
    assert r.status == ev.PASS
    sent = json.loads([c for c in hub.calls if c[1] == "/api/fork/voice/announce"][0][3])
    assert sent["device_id"] == "echo-verify-20261106120000" and sent["speak"] is False
    assert sent["message"].startswith(ev.TEST_TEXT)

    hub = FakeHub()
    r = ev.check_announcement(hub, opts(admin_user="admin", admin_password="pw", speak=True))
    assert r.status == ev.FAIL and "--device-id" in r.detail
    r = ev.check_announcement(hub, opts(admin_user="admin", admin_password="pw", speak=True,
                                        device_id="echo-kitchen"))
    assert r.status == ev.PASS and "spoken" in r.detail


def test_announcement_failures():
    hub = FakeHub()
    r = ev.check_announcement(hub, opts(admin_user="admin", admin_password="wrong"))
    assert r.status == ev.FAIL and "sign-in" in r.detail
    hub = FakeHub(owner_feed=False)  # the token belongs to another owner
    r = ev.check_announcement(hub, opts(admin_user="admin", admin_password="pw"))
    assert r.status == ev.FAIL and "same owner" in r.detail
    hub = FakeHub()
    hub.override[("POST", "/api/fork/voice/announce")] = _json(200, {"ok": False,
                                                                     "problem": "x"})
    r = ev.check_announcement(hub, opts(admin_user="admin", admin_password="pw"))
    assert r.status == ev.FAIL and "older hub" in r.detail


def test_no_release_is_a_pass_and_skips_the_apk():
    results = by_name(ev.run(FakeHub(release=False), opts(voice_note=False)))
    assert results["app/latest"].status == ev.PASS
    assert "no release" in results["app/latest"].detail
    assert results["app/apk"].status == ev.SKIP


def test_a_wrong_apk_fails():
    hub = FakeHub()
    hub.override[("GET", ev.VOICE + "/app/apk")] = ev.Response(
        206, {"etag": '"nope"', "content-range": f"bytes 0-3/{len(APK)}"}, b"PK\x03\x04")
    assert ev.run(hub, opts())[4].status == ev.FAIL
    hub.override[("GET", ev.VOICE + "/app/apk")] = ev.Response(200, {}, APK)
    r = by_name(ev.run(hub, opts()))["app/apk"]
    assert r.status == ev.FAIL and "206" in r.detail
    hub = FakeHub()
    hub.override[("GET", ev.VOICE + "/app/apk")] = ev.Response(200, {}, APK + b"x")
    r = ev.check_app(hub, opts(full_apk=True))[1]
    assert r.status == ev.FAIL and "sha256" in r.detail


@pytest.mark.parametrize(("change", "problem"), [
    ({"date": None}, "no date"),
    ({"todos": None}, "todos isn't a list"),
    ({"todos": [{"ref": "41", "title": "x"}]}, "a to-do without a todo: ref"),
    ({"events": [{"title": "x"}]}, "an event without start or all_day"),
    ({"action_items": [{"title": "x"}]}, "an action item without id or title"),
    ({"counts": {"todos_open": 1}}, "counts missing"),
    ({"headline": 5}, "headline isn't text or null"),
])
def test_today_problems(change, problem):
    assert ev.today_problem({**TODAY, **change}) == problem
    assert ev.today_problem(TODAY) is None


def test_feed_and_release_problems():
    good = {"id": "an_1", "at": 1, "expires_at": 2, "kind": "reminder", "target": {"all": True}}
    assert ev.feed_problem({"announcements": [good]}) is None
    assert ev.feed_problem({"announcements": [{**good, "kind": "x"}]}) == "unknown kind 'x'"
    assert ev.feed_problem({"announcements": [{**good, "target": "all"}]})
    assert ev.feed_problem({"announcements": [{**good, "at": "1"}]})
    assert ev.feed_problem({}) == "no announcements list"
    rel = {"app": "odysseus-echo", "version_name": "a", "version_code": 1, "size": 1,
           "sha256": SHA}
    assert ev.release_problem(rel) is None
    assert ev.release_problem({**rel, "sha256": "abc"}) == "sha256 isn't 64 hex digits"
    assert ev.release_problem({**rel, "version_code": "1"}) == "no version_code"


def test_scope_and_auth_failures_say_what_to_do():
    hub = FakeHub()
    hub.override[("GET", ev.VOICE + "/today")] = _json(200, TODAY)  # no auth at all
    assert ev.check_auth(hub, opts()).status == ev.FAIL
    hub.override[("GET", ev.VOICE + "/models")] = _json(403, {"detail": "missing scope"})
    r = ev.check_models(hub, opts())
    assert r.status == ev.FAIL and "Link this Echo" in r.detail
    hub.override[("GET", ev.ATHENA + "/health")] = _json(403, {"detail": "missing scope"})
    r = ev.check_athena(hub, opts())
    assert r.status == ev.FAIL and "athena:ingest" in r.detail


def test_a_server_that_doesnt_answer_fails_each_check():
    class Down(ev.Http):
        def __init__(self):
            pass

        def send(self, *a, **k):
            raise ConnectionRefusedError("refused")

    results = ev.run(Down(), opts())
    assert results and all(r.status in (ev.FAIL, ev.SKIP) for r in results)
    assert sum(r.status == ev.FAIL for r in results) >= 7
    assert ev.report(results, io.StringIO()) == 1


def test_main_reads_the_token_and_checks_arguments(tmp_path, monkeypatch, capsys):
    token = tmp_path / "t"
    token.write_text(TOKEN + "\n")
    assert ev.main(["--url", "http://x", "--token-file", str(token), "--no-voice-note"],
                   http=FakeHub()) == 0
    assert TOKEN not in capsys.readouterr().out
    monkeypatch.delenv("ODYSSEUS_ECHO_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        ev.main(["--url", "http://x"], http=FakeHub())
    with pytest.raises(SystemExit):
        ev.main(["--url", "http://x", "--token", TOKEN, "--admin-user", "admin"],
                http=FakeHub())
    monkeypatch.setenv("ODYSSEUS_ECHO_TOKEN", TOKEN)
    assert ev.main(["--url", "http://x", "--no-voice-note"], http=FakeHub()) == 0


# ------------------------------------------------------------------- edge cases
def test_today_focus_may_name_events_loops_and_beliefs():
    body = {**TODAY, "focus": [{"ref": "event:dentist", "title": "Dentist", "why": "At 3"},
                               {"ref": "loop:7", "title": "Reply to Sam", "why": ""},
                               {"ref": "todo:41:2", "title": "Call the plumber", "why": ""}]}
    assert ev.today_problem(body) is None
    assert ev.today_problem({**TODAY, "focus": [{"ref": "", "title": "x"}]}) is not None
    assert ev.today_problem({**TODAY, "todos": [{"ref": "event:x", "title": "x"}]}) \
        is not None


def test_a_settings_item_in_the_feed_is_fine():
    row = {"id": "an_1", "at": 1, "expires_at": 2, "kind": "settings",
           "target": {"device_id": "echo-1"}, "title": "Settings",
           "text": "Settings changed in Odysseus.", "ref": "settings:3"}
    assert ev.feed_problem({"announcements": [row]}) is None
    assert ev.feed_problem({"announcements": [{**row, "kind": "spam"}]}) is not None
