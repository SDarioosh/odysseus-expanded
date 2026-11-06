"""Fork: the Echo app's releases hosted by the hub (fork/voice/app_release.py,
scripts/echo_release.py). Fictional bytes, never a real APK."""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fork.voice import app_release
from fork.voice.routes import PREFIX

LATEST = PREFIX + "/app/latest"
APK = PREFIX + "/app/apk"
VOICE = {"x-test-scopes": "voice"}
BYTES = b"PK\x03\x04" + b"fictional echo build " * 500


@pytest.fixture
def apk(tmp_path):
    path = tmp_path / "build" / "echo.apk"
    path.parent.mkdir()
    path.write_bytes(BYTES)
    return path


@pytest.fixture
def api(data_dir):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        request.state.api_token_owner = "alice" if scopes is not None else None
        request.state.api_token_scopes = scopes.split(",") if scopes else []
        return await call_next(request)

    app.include_router(app_release.setup_app_release_routes())
    with TestClient(app) as client:
        yield client


def publish(apk, code=1400030, **kw):
    return app_release.publish(apk, version_name=kw.pop("name", f"0.14.0+odysseus.{code % 1000}"),
                               version_code=code, **kw)


# ----------------------------------------------------------------------------- publishing
def test_publish_writes_the_apk_and_its_release(data_dir, apk):
    release = publish(apk, notes="Kiosk settings; Today page.")
    folder = data_dir / "echo_app"
    assert (folder / "odysseus-echo.apk").read_bytes() == BYTES
    assert json.loads((folder / "release.json").read_text()) == release == {
        "app": "odysseus-echo", "version_name": "0.14.0+odysseus.30",
        "version_code": 1400030, "size": len(BYTES),
        "sha256": hashlib.sha256(BYTES).hexdigest(), "notes": "Kiosk settings; Today page."}
    assert sorted(p.name for p in folder.iterdir()) == [".publish.lock", "odysseus-echo.apk",
                                                        "release.json"]
    assert app_release.current() == release


def test_a_lower_version_code_is_refused_unless_forced(data_dir, apk):
    publish(apk, 1400030)
    with pytest.raises(app_release.ReleaseError, match="lower"):
        publish(apk, 1400029)
    assert app_release.current()["version_code"] == 1400030
    assert publish(apk, 1400029, force=True)["version_code"] == 1400029
    assert publish(apk, 1400031)["version_code"] == 1400031


@pytest.mark.parametrize("kw", [{"name": ""}, {"name": "has space"}, {"code": 0},
                                {"code": -5}, {"notes": "x" * 3000}])
def test_bad_versions_are_refused(data_dir, apk, kw):
    with pytest.raises(app_release.ReleaseError):
        publish(apk, kw.pop("code", 1400030), **kw)
    assert not (data_dir / "echo_app").exists()


def test_only_an_apk_is_published(data_dir, tmp_path):
    not_zip = tmp_path / "x.apk"
    not_zip.write_bytes(b"fictional, not a zip")
    with pytest.raises(app_release.ReleaseError, match="not an APK"):
        publish(not_zip)
    with pytest.raises(app_release.ReleaseError, match="no such file"):
        publish(tmp_path / "missing.apk")
    assert app_release.current() is None


def test_a_broken_or_mismatched_release_is_no_release(data_dir, apk):
    publish(apk)
    (data_dir / "echo_app" / "odysseus-echo.apk").write_bytes(b"PK\x03\x04short")
    assert app_release.current() is None
    (data_dir / "echo_app" / "release.json").write_text("{not json")
    assert app_release.current() is None


# --------------------------------------------------------------------------------- routes
def test_only_a_voice_token_may_ask(api, apk):
    publish(apk)
    for url in (LATEST, APK):
        assert api.get(url).status_code == 403
        assert api.get(url, headers={"x-test-scopes": "chat"}).status_code == 403


def test_no_release_is_404(api):
    for url in (LATEST, APK):
        res = api.get(url, headers=VOICE)
        assert res.status_code == 404 and res.json() == {"error": "no release"}


def test_latest_is_contract_c2(api, apk):
    release = publish(apk, notes="Today page.")
    res = api.get(LATEST, headers=VOICE)
    assert res.status_code == 200 and res.json() == release
    assert set(res.json()) == {"app", "version_name", "version_code", "size", "sha256", "notes"}


def test_the_apk_is_served_with_its_headers(api, apk):
    release = publish(apk)
    res = api.get(APK, headers=VOICE)
    assert res.status_code == 200 and res.content == BYTES
    assert res.headers["content-type"] == "application/vnd.android.package-archive"
    assert res.headers["content-length"] == str(len(BYTES))
    assert res.headers["etag"] == f'"{release["sha256"]}"'
    part = api.get(APK, headers={**VOICE, "range": "bytes=4-13"})
    assert part.status_code == 206 and part.content == BYTES[4:14]


def test_the_apk_is_not_gzipped_by_the_app(data_dir, apk):
    # app.py adds GZipMiddleware; the APK must reach the device byte for byte, with its size.
    from starlette.middleware.gzip import GZipMiddleware
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.api_token = True
        request.state.api_token_owner = "alice"
        request.state.api_token_scopes = ["voice"]
        return await call_next(request)

    app.include_router(app_release.setup_app_release_routes())
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    big = apk.parent / "big.apk"
    big.write_bytes(b"PK\x03\x04" + b"a" * 50_000)
    publish(big)
    with TestClient(app) as client:
        res = client.get(APK, headers={"accept-encoding": "gzip"})
    assert res.status_code == 200
    assert res.headers.get("content-encoding") in (None, "identity")
    assert res.headers["content-length"] == str(big.stat().st_size)
    assert res.content == big.read_bytes()


# --------------------------------------------------------------------------------- script
def test_the_script_publishes_and_checks_the_version(data_dir, apk, capsys):
    from scripts import echo_release

    args = [str(apk), "--version-name", "0.14.0+odysseus.30", "--version-code", "1400030",
            "--data-dir", str(data_dir)]
    assert echo_release.main(args) == 0
    assert "published: 0.14.0+odysseus.30 (1400030)" in capsys.readouterr().out
    lower = [str(apk), "--version-name", "0.14.0+odysseus.29", "--version-code", "1400029",
             "--data-dir", str(data_dir)]
    assert echo_release.main(lower) == 2
    assert "lower than the published 1400030" in capsys.readouterr().err
    assert echo_release.main([*lower, "--force"]) == 0
    assert echo_release.main(["--show", "--data-dir", str(data_dir)]) == 0
    assert '"version_code": 1400029' in capsys.readouterr().out


def test_the_script_without_a_release_says_so(data_dir, capsys):
    from scripts import echo_release

    assert echo_release.main(["--show"]) == 1
    assert "No release published" in capsys.readouterr().out


# ------------------------------------------------------------------- edge cases
def test_two_publishes_at_once_leave_one_whole_release(data_dir, tmp_path, monkeypatch):
    """The APK and release.json always come from the same publish."""
    import threading

    small, big = tmp_path / "small.apk", tmp_path / "big.apk"
    small.write_bytes(b"PK\x03\x04" + b"s" * 100)
    big.write_bytes(b"PK\x03\x04" + b"b" * 5000)
    import os

    real_replace = os.replace
    gate = threading.Barrier(2, timeout=0.5)

    def replace(src, dst):
        real_replace(src, dst)
        if str(dst).endswith(app_release.APK_NAME):
            try:
                gate.wait()  # both APKs renamed before either release.json
            except threading.BrokenBarrierError:
                pass

    monkeypatch.setattr(os, "replace", replace)
    errors = []

    def run(path, code):
        try:
            publish(path, code, force=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(small, 1400031)),
               threading.Thread(target=run, args=(big, 1400032))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    release = app_release.current()
    assert release is not None
    folder = app_release.release_dir()
    data = (folder / app_release.APK_NAME).read_bytes()
    assert hashlib.sha256(data).hexdigest() == release["sha256"]
    assert sorted(p.name for p in folder.iterdir() if p.name.startswith(".incoming")) == []


@pytest.mark.parametrize("header, status, length", [
    ("bytes=0-9", 206, 10), ("bytes=-10", 206, 10), ("bytes=100-", 206, len(BYTES) - 100),
    ("bytes=0-99999999", 206, len(BYTES)), (f"bytes={len(BYTES)}-", 416, 0)])
def test_range_requests(api, apk, header, status, length):
    publish(apk)
    res = api.get(APK, headers={**VOICE, "range": header})
    assert res.status_code == status and len(res.content) == length
    if status == 206:
        assert res.headers["etag"] == f'"{hashlib.sha256(BYTES).hexdigest()}"'
        assert res.headers["content-range"].endswith(f"/{len(BYTES)}")


def test_a_resumed_download_puts_the_file_back_together(api, apk):
    publish(apk)
    first = api.get(APK, headers={**VOICE, "range": "bytes=0-999"}).content
    rest = api.get(APK, headers={**VOICE, "range": "bytes=1000-"}).content
    assert first + rest == BYTES
