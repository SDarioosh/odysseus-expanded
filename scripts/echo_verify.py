#!/usr/bin/env python3
"""Fork: check an Odysseus server the way an Echo uses it (docs/fork/voice-assistant.md#checking-a-server-for-the-echos).

Calls the routes an Echo calls, with an Echo's token (``/echo/link``, scopes ``voice`` and
``athena:ingest``), and prints PASS, FAIL or SKIP per check; the exit code is 1 when any
check failed. Read-only, except:

* a **test voice note**: 3 s of silence uploaded through ``/api/codex/athena/chunk``
  (``kind=note``, device ``echo-verify``, session ``echo-verify-<time>``), then ended.
  Silence has no speech, so Athena keeps no conversation and deletes the audio; its
  session row stays (Athena has no route that deletes an audio session). Skip it with
  ``--no-voice-note``.
* a **test announcement**, only with ``--admin-user`` and ``--password-file`` (the route
  is admin-only): kept for the feed of a made-up device ``echo-verify-<time>``, so no
  screen shows it and nothing is spoken. With ``--speak`` it goes to ``--device-id`` (a
  real Echo) instead, which shows it and says it aloud.
* ``satellite/hello``, only with ``--device-id``: sends that device's id alone, which keeps
  its stored name, room and version and only refreshes its last-seen time.

    python scripts/echo_verify.py --url https://odysseus.example.ts.net --token-file echo.token
    python scripts/echo_verify.py --url http://192.168.1.10:7000 --token-file echo.token \\
        --device-id <the Echo's id> --admin-user admin --password-file admin.txt

The token can come from ``--token``, ``--token-file`` or ``ODYSSEUS_ECHO_TOKEN``. It is
never printed, and nothing the server answers is printed beyond counts and versions.
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

VOICE = "/api/chat/voice/v1"
ATHENA = "/api/codex/athena"
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
TIMEOUT = 30.0
TEST_DEVICE = "echo-verify"
TEST_TEXT = "Odysseus verification test, please ignore"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ------------------------------------------------------------------------------ HTTP
@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None


class Http:
    """A tiny client over urllib: never raises for an HTTP status, keeps cookies (the admin
    sign-in). ``send`` is what the tests replace."""

    def __init__(self, base_url: str, timeout: float = TIMEOUT) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def send(self, method: str, path: str, *, headers: dict[str, str] | None = None,
             body: bytes | None = None) -> Response:
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers=headers or {})
        try:
            with self.opener.open(req, timeout=self.timeout) as res:
                return Response(res.status, _lower(res.headers), res.read())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, _lower(exc.headers), exc.read() or b"")


def _lower(headers: Any) -> dict[str, str]:
    return {k.lower(): v for k, v in (headers.items() if headers else [])}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]
              ) -> tuple[bytes, str]:
    """(body, content type) of a multipart/form-data request."""
    boundary = "echoverify" + uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                  f"{value}\r\n".encode())
    for name, (filename, data, kind) in files.items():
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                  f'filename="{filename}"\r\nContent-Type: {kind}\r\n\r\n'.encode())
        out.write(data + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def silent_wav(seconds: float = 3.0) -> bytes:
    """What an Echo records (16 kHz, mono, 16-bit PCM), with nothing in it."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


# ---------------------------------------------------------------------------- checks
@dataclass
class Result:
    name: str
    status: str
    detail: str = ""


@dataclass
class Options:
    token: str
    device_id: str = ""
    voice_note: bool = True
    admin_user: str = ""
    admin_password: str = ""
    speak: bool = False
    full_apk: bool = False
    stamp: str = field(default_factory=lambda: datetime.now(UTC).strftime("%Y%m%d%H%M%S"))


def _why(res: Response) -> str:
    body = res.json()
    if isinstance(body, dict):
        text = body.get("detail") or body.get("error") or body.get("message")
        if isinstance(text, str):
            return f"HTTP {res.status}: {text[:160]}"
    return f"HTTP {res.status}"


def check_auth(http: Http, opts: Options) -> Result:
    res = http.send("GET", VOICE + "/today")
    if res.status in (401, 403):
        return Result("no token is refused", PASS, f"HTTP {res.status}")
    return Result("no token is refused", FAIL,
                  f"HTTP {res.status} without a token; expected 401 or 403")


def check_models(http: Http, opts: Options) -> Result:
    res = http.send("GET", VOICE + "/models", headers=bearer(opts.token))
    body = res.json()
    if res.status == 200 and isinstance(body, dict) and any(
            isinstance(m, dict) and m.get("id") == "odysseus" for m in body.get("data") or []):
        return Result("token has the voice scope (models)", PASS)
    if res.status == 403:
        return Result("token has the voice scope (models)", FAIL,
                      _why(res) + " (make the token with Link this Echo)")
    return Result("token has the voice scope (models)", FAIL, _why(res))


TODAY_LISTS = ("focus", "events", "todos", "action_items")


def today_problem(body: Any) -> str | None:
    """What is wrong with a ``/today`` answer, or None."""
    if not isinstance(body, dict):
        return "not a JSON object"
    if not isinstance(body.get("date"), str) or not re.match(r"^\d{4}-\d{2}-\d{2}$",
                                                             body["date"]):
        return "no date"
    if body.get("headline") is not None and not isinstance(body["headline"], str):
        return "headline isn't text or null"
    for key in TODAY_LISTS:
        if not isinstance(body.get(key), list):
            return f"{key} isn't a list"
    for row in body["todos"]:
        if not isinstance(row, dict) or not str(row.get("ref", "")).startswith("todo:"):
            return "a to-do without a todo: ref"
    for row in body["focus"]:  # a to-do, an event, a loop or a plan: todo:, event:, …
        if not isinstance(row, dict) or not isinstance(row.get("ref"), str) or not row["ref"]:
            return "a focus row without a ref"
    for row in body["events"]:
        if not isinstance(row, dict) or not row.get("start") or "all_day" not in row:
            return "an event without start or all_day"
    for row in body["action_items"]:
        if not isinstance(row, dict) or not row.get("id") or not row.get("title"):
            return "an action item without id or title"
    counts = body.get("counts")
    if not isinstance(counts, dict) or not all(
            isinstance(counts.get(k), int) for k in ("todos_open", "action_items_open")):
        return "counts missing"
    return None


def check_today(http: Http, opts: Options) -> Result:
    res = http.send("GET", VOICE + "/today", headers=bearer(opts.token))
    if res.status != 200:
        return Result("today", FAIL, _why(res))
    body = res.json()
    problem = today_problem(body)
    if problem:
        return Result("today", FAIL, problem)
    detail = (f"{body['date']}: headline {'yes' if body['headline'] else 'none'}, "
              f"{len(body['events'])} events, {len(body['todos'])} to-dos "
              f"({body['counts']['todos_open']} open), {len(body['action_items'])} action "
              f"items ({body['counts']['action_items_open']} open)")
    if not body["action_items"] and not body["counts"]["action_items_open"]:
        detail += "; no action items: fine if Athena has none open, else check Athena"
    return Result("today", PASS, detail)


def release_problem(body: Any) -> str | None:
    if not isinstance(body, dict):
        return "not a JSON object"
    if body.get("app") != "odysseus-echo":
        return "app isn't odysseus-echo"
    if not isinstance(body.get("version_code"), int) or body["version_code"] <= 0:
        return "no version_code"
    if not isinstance(body.get("version_name"), str) or not body["version_name"]:
        return "no version_name"
    if not isinstance(body.get("size"), int) or body["size"] <= 0:
        return "no size"
    if not isinstance(body.get("sha256"), str) or not _HEX64.match(body["sha256"]):
        return "sha256 isn't 64 hex digits"
    return None


def check_app(http: Http, opts: Options) -> list[Result]:
    res = http.send("GET", VOICE + "/app/latest", headers=bearer(opts.token))
    body = res.json()
    if res.status == 404 and isinstance(body, dict) and body.get("error") == "no release":
        return [Result("app/latest", PASS, "no release published (scripts/echo_release.py)"),
                Result("app/apk", SKIP, "no release published")]
    if res.status != 200:
        return [Result("app/latest", FAIL, _why(res)), Result("app/apk", SKIP, "no release")]
    problem = release_problem(body)
    if problem:
        return [Result("app/latest", FAIL, problem), Result("app/apk", SKIP, "bad release")]
    latest = Result("app/latest", PASS, f"{body['version_name']} ({body['version_code']}), "
                                        f"{body['size']} bytes")
    return [latest, check_apk(http, opts, body)]


def check_apk(http: Http, opts: Options, release: dict) -> Result:
    size, sha = release["size"], release["sha256"]
    if opts.full_apk:
        res = http.send("GET", VOICE + "/app/apk", headers=bearer(opts.token))
        if res.status != 200:
            return Result("app/apk", FAIL, _why(res))
        if len(res.body) != size or hashlib.sha256(res.body).hexdigest() != sha:
            return Result("app/apk", FAIL, "the download's size or sha256 isn't the release's")
        return Result("app/apk", PASS, f"downloaded {size} bytes, sha256 matches")
    res = http.send("GET", VOICE + "/app/apk", headers={**bearer(opts.token),
                                                       "Range": "bytes=0-3"})
    if res.status != 206:
        return Result("app/apk", FAIL, _why(res) + " to a Range request; expected 206")
    if res.headers.get("content-range") != f"bytes 0-3/{size}":
        return Result("app/apk", FAIL, f"Content-Range {res.headers.get('content-range')!r}, "
                                       f"expected 'bytes 0-3/{size}'")
    if res.body != b"PK\x03\x04":
        return Result("app/apk", FAIL, "the file doesn't start like an APK (a zip)")
    if res.headers.get("etag", "").strip('"') != sha:
        return Result("app/apk", FAIL, "ETag isn't the release's sha256")
    return Result("app/apk", PASS, "Range answered, zip header, ETag = sha256 "
                                   "(--full-apk downloads it all)")


def check_hello(http: Http, opts: Options) -> Result:
    if not opts.device_id:
        return Result("satellite/hello", SKIP, "pass --device-id (scripts/odysseus/"
                                               "verify_device.sh in odysseus-echo prints it)")
    body, kind = multipart({"device_id": opts.device_id}, {})
    res = http.send("POST", VOICE + "/satellite/hello",
                    headers={**bearer(opts.token), "Content-Type": kind}, body=body)
    if res.status == 200 and res.json() == {"ok": True}:
        return Result("satellite/hello", PASS, "id only: name, room and version kept")
    return Result("satellite/hello", FAIL, _why(res))


# "settings": the device's hub-managed settings changed (no card; fork/voice/device_settings.py)
FEED_KINDS = ("reminder", "announcement", "settings")


def feed_problem(body: Any) -> str | None:
    if not isinstance(body, dict) or not isinstance(body.get("announcements"), list):
        return "no announcements list"
    for row in body["announcements"]:
        if not isinstance(row, dict):
            return "an announcement isn't an object"
        if not str(row.get("id", "")).startswith("an_"):
            return "an announcement without an an_ id"
        if row.get("kind") not in FEED_KINDS:
            return f"unknown kind {row.get('kind')!r}"
        if not isinstance(row.get("target"), dict):
            return "an announcement whose target isn't an object"
        if not isinstance(row.get("at"), int) or not isinstance(row.get("expires_at"), int):
            return "at / expires_at aren't epoch ms"
    return None


def get_feed(http: Http, opts: Options, device_id: str, since_ms: int
             ) -> tuple[Response, Any]:
    query = urllib.parse.urlencode({"device_id": device_id, "since": since_ms})
    res = http.send("GET", f"{VOICE}/announcements?{query}", headers=bearer(opts.token))
    return res, res.json()


def check_feed(http: Http, opts: Options) -> Result:
    device_id = opts.device_id or f"{TEST_DEVICE}-{opts.stamp}"
    res, body = get_feed(http, opts, device_id, int((time.time() - 3600) * 1000))
    if res.status != 200:
        return Result("announcements feed", FAIL, _why(res))
    problem = feed_problem(body)
    if problem:
        return Result("announcements feed", FAIL, problem)
    who = "this device" if opts.device_id else "a made-up device"
    return Result("announcements feed", PASS,
                  f"{len(body['announcements'])} in the last hour for {who}")


def check_athena(http: Http, opts: Options) -> Result:
    res = http.send("GET", ATHENA + "/health", headers=bearer(opts.token))
    if res.status == 200:
        return Result("Athena through the hub (health)", PASS)
    if res.status == 403:
        return Result("Athena through the hub (health)", FAIL,
                      _why(res) + " (no athena:ingest: link the Echo again for a new token)")
    return Result("Athena through the hub (health)", FAIL, _why(res))


def check_voice_note(http: Http, opts: Options) -> Result:
    name = "test voice note (WAV, kind=note)"
    if not opts.voice_note:
        return Result(name, SKIP, "--no-voice-note")
    session = f"{TEST_DEVICE}-{opts.stamp}"
    body, kind = multipart(
        {"device": TEST_DEVICE, "session_id": session, "sequence": "0",
         "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "codec": "wav",
         "kind": "note"},
        {"audio": ("000000.wav", silent_wav(), "audio/wav")})
    res = http.send("POST", ATHENA + "/chunk",
                    headers={**bearer(opts.token), "Content-Type": kind}, body=body)
    answer = res.json()
    if res.status != 200 or not isinstance(answer, dict) or answer.get("ok") is not True:
        return Result(name, FAIL, "upload: " + _why(res))
    res = http.send("POST", f"{ATHENA}/sessions/{session}/end", headers=bearer(opts.token))
    if res.status != 200 or (res.json() or {}).get("ok") is not True:
        return Result(name, FAIL, "end: " + _why(res))
    return Result(name, PASS, f"session {session} uploaded and ended; silent, so Athena "
                              "keeps no conversation (its session row stays: Athena "
                              "can't delete one)")


def admin_login(http: Http, opts: Options) -> str | None:
    """Sign in as the admin; returns a problem or None."""
    body = json.dumps({"username": opts.admin_user, "password": opts.admin_password,
                       "remember": False}).encode()
    res = http.send("POST", "/api/auth/login", headers={"Content-Type": "application/json"},
                    body=body)
    if res.status != 200 or not (res.json() or {}).get("ok"):
        return "admin sign-in: " + _why(res)
    return None


def check_announcement(http: Http, opts: Options) -> Result:
    name = "test announcement (feed)"
    if not (opts.admin_user and opts.admin_password):
        return Result(name, SKIP, "pass --admin-user and --password-file (an admin route)")
    if opts.speak and not opts.device_id:
        return Result(name, FAIL, "--speak needs --device-id (the Echo that says it)")
    problem = admin_login(http, opts)
    if problem:
        return Result(name, FAIL, problem)
    device_id = opts.device_id if opts.speak else f"{TEST_DEVICE}-{opts.stamp}"
    since = int(time.time() * 1000) - 1000
    payload = {"message": f"{TEST_TEXT} ({opts.stamp}).", "device_id": device_id,
               "speak": opts.speak}
    res = http.send("POST", "/api/fork/voice/announce",
                    headers={"Content-Type": "application/json"},
                    body=json.dumps(payload).encode())
    answer = res.json()
    if res.status != 200 or not isinstance(answer, dict):
        return Result(name, FAIL, _why(res))
    if not answer.get("id"):
        return Result(name, FAIL, "not kept for the feed (an older hub answers without an "
                                  f"id): {answer.get('problem')}")
    res, body = get_feed(http, opts, device_id, since)
    ids = [row.get("id") for row in (body or {}).get("announcements") or []
           if isinstance(row, dict)]
    if res.status != 200 or answer["id"] not in ids:
        return Result(name, FAIL, "the device's feed doesn't show it (the token and the admin "
                                  "must be the same owner)")
    if opts.speak:
        if not answer.get("spoken"):
            return Result(name, FAIL, f"in the feed, not spoken: {answer.get('problem')}")
        return Result(name, PASS, "in the device's feed and spoken; the Echo shows it")
    return Result(name, PASS, f"in the feed of {device_id} only; not spoken, no screen "
                              "shows it")


CHECKS: list[Callable[[Http, Options], Result | list[Result]]] = [
    check_auth, check_models, check_today, check_app, check_hello, check_feed,
    check_athena, check_voice_note, check_announcement]


def run(http: Http, opts: Options) -> list[Result]:
    results: list[Result] = []
    for check in CHECKS:
        try:
            out = check(http, opts)
        except (OSError, urllib.error.URLError) as exc:
            out = Result(check.__name__.removeprefix("check_"), FAIL,
                         f"no answer: {type(exc).__name__}: {exc}")
        results.extend(out if isinstance(out, list) else [out])
    return results


def report(results: list[Result], out: Any = sys.stdout) -> int:
    width = max(len(r.name) for r in results)
    for r in results:
        line = f"{r.status:4}  {r.name.ljust(width)}"
        print(line + (f"  {r.detail}" if r.detail else ""), file=out)
    failed = sum(r.status == FAIL for r in results)
    passed = sum(r.status == PASS for r in results)
    skipped = sum(r.status == SKIP for r in results)
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped", file=out)
    return 1 if failed else 0


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def main(argv: list[str] | None = None, http: Http | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True, help="the hub, e.g. http://192.168.1.10:7000")
    ap.add_argument("--token", help="an Echo's token (prefer --token-file)")
    ap.add_argument("--token-file", help="a file holding the Echo's token")
    ap.add_argument("--device-id", default="", help="a real Echo's id: hello and its feed")
    ap.add_argument("--no-voice-note", action="store_true", help="don't upload the test note")
    ap.add_argument("--admin-user", default="", help="admin, for the test announcement")
    ap.add_argument("--password-file", help="a file holding that admin's password")
    ap.add_argument("--speak", action="store_true",
                    help="send the test announcement to --device-id and say it aloud")
    ap.add_argument("--full-apk", action="store_true", help="download the whole APK")
    args = ap.parse_args(argv)
    token = (args.token or (_read(args.token_file) if args.token_file else "")
             or os.environ.get("ODYSSEUS_ECHO_TOKEN", "")).strip()
    if not token:
        ap.error("give the Echo's token: --token, --token-file or ODYSSEUS_ECHO_TOKEN")
    if bool(args.admin_user) != bool(args.password_file):
        ap.error("--admin-user and --password-file go together")
    opts = Options(token=token, device_id=args.device_id.strip(),
                   voice_note=not args.no_voice_note, admin_user=args.admin_user,
                   admin_password=_read(args.password_file) if args.password_file else "",
                   speak=args.speak, full_apk=args.full_apk)
    return report(run(http or Http(args.url), opts))


if __name__ == "__main__":
    sys.exit(main())
