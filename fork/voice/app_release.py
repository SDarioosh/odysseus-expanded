"""Fork: the Echo app's releases, hosted by the hub (docs/fork/voice-assistant.md#app-updates).

The Echo app (``SDarioosh/odysseus-echo``) updates itself from its hub, over the voice API:

* ``GET /api/chat/voice/v1/app/latest`` (API token with the ``voice`` scope): the release
  on offer, ``{"app", "version_name", "version_code", "size", "sha256", "notes"}``, or 404
  ``{"error": "no release"}`` when none has been published.
* ``GET /api/chat/voice/v1/app/apk`` (the same): the APK itself, streamed from disk
  (``application/vnd.android.package-archive``, ``Content-Length``, ``ETag`` = the quoted
  sha256, Range requests answered), or the same 404.

A device compares ``version_code``, never the name (semver ignores ``+odysseus.N``).

Storage is ``<DATA_DIR>/echo_app/``: ``odysseus-echo.apk`` and ``release.json`` (the fields
above). :func:`publish` puts a new release there, the APK first and then its
``release.json``, each written to a temp file and renamed into place, one publish at a
time (``.publish.lock``); the owner runs it
through ``scripts/echo_release.py`` on the server. Nothing here reads the APK's manifest:
the owner passes the version the Echo build printed.
"""
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

APP = "odysseus-echo"
DIR_NAME = "echo_app"
APK_NAME = "odysseus-echo.apk"
RELEASE_NAME = "release.json"
LOCK_NAME = ".publish.lock"
APK_MEDIA_TYPE = "application/vnd.android.package-archive"
NO_RELEASE = {"error": "no release"}
FIELDS = ("app", "version_name", "version_code", "size", "sha256", "notes")
MAX_NAME = 64
MAX_NOTES = 2000
CHUNK = 1024 * 1024


class ReleaseError(ValueError):
    """A release that can't be published (bad version, lower version code, not an APK)."""


def release_dir(data_dir: str | os.PathLike | None = None) -> Path:
    """``<DATA_DIR>/echo_app`` (``DATA_DIR`` read at call time, so tests can move it)."""
    if data_dir is None:
        from src.constants import DATA_DIR

        data_dir = DATA_DIR
    return Path(data_dir) / DIR_NAME


def _clean(raw: Any) -> dict | None:
    """``release.json``'s fields, checked; ``None`` if it isn't a usable release."""
    if not isinstance(raw, dict):
        return None
    code, size, sha = raw.get("version_code"), raw.get("size"), raw.get("sha256")
    name = raw.get("version_name")
    if (not isinstance(code, int) or isinstance(code, bool) or code <= 0
            or not isinstance(size, int) or isinstance(size, bool) or size <= 0
            or not isinstance(sha, str) or len(sha) != 64
            or any(c not in "0123456789abcdef" for c in sha)
            or not isinstance(name, str) or not name):
        return None
    notes = raw.get("notes")
    return {"app": APP, "version_name": name, "version_code": code, "size": size,
            "sha256": sha, "notes": notes if isinstance(notes, str) else ""}


def current(data_dir: str | os.PathLike | None = None) -> dict | None:
    """The published release (contract C2's fields), or ``None``: no ``release.json``, a
    broken one, or its APK missing or not the size it names."""
    folder = release_dir(data_dir)
    try:
        release = _clean(json.loads((folder / RELEASE_NAME).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if release is None:
        return None
    try:
        if (folder / APK_NAME).stat().st_size != release["size"]:
            return None
    except OSError:
        return None
    return release


def _check_version(version_name: str, version_code: int, notes: str) -> None:
    if not isinstance(version_name, str) or not version_name.strip():
        raise ReleaseError("--version-name is required")
    if len(version_name) > MAX_NAME or any(c.isspace() for c in version_name.strip()):
        raise ReleaseError(f"--version-name must be one word of at most {MAX_NAME} characters")
    if isinstance(version_code, bool) or not isinstance(version_code, int) or version_code <= 0:
        raise ReleaseError("--version-code must be a whole number above 0")
    if len(notes) > MAX_NOTES:
        raise ReleaseError(f"--notes is longer than {MAX_NOTES} characters")


def _copy_hashing(src: BinaryIO, dst: BinaryIO) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    while True:
        block = src.read(CHUNK)
        if not block:
            return size, digest.hexdigest()
        digest.update(block)
        dst.write(block)
        size += len(block)


def _temp_in(folder: Path, suffix: str) -> tuple[int, str]:
    return tempfile.mkstemp(prefix=".incoming-", suffix=suffix, dir=folder)


def publish(apk: str | os.PathLike, *, version_name: str, version_code: int,
            notes: str = "", force: bool = False,
            data_dir: str | os.PathLike | None = None) -> dict:
    """Publish ``apk`` as the release on offer and return its ``release.json`` fields.

    Refuses (:class:`ReleaseError`, nothing changed) a ``version_code`` lower than the
    published one unless ``force``, a file that isn't a zip (every APK is one), an empty
    file and a bad version. The APK is copied to a temp file in the release folder while
    its sha256 is worked out, synced, and renamed over ``odysseus-echo.apk``; then
    ``release.json`` the same way. A download already running keeps the file it opened.
    """
    version_name = (version_name or "").strip() if isinstance(version_name, str) else version_name
    notes = (notes or "").strip()
    _check_version(version_name, version_code, notes)
    source = Path(apk)
    if not source.is_file():
        raise ReleaseError(f"{source}: no such file")
    with source.open("rb") as fh:
        if fh.read(4) != b"PK\x03\x04":
            raise ReleaseError(f"{source}: not an APK (an APK is a zip file)")
    folder = release_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    with _publishing(folder):
        return _publish(source, folder, version_name=version_name,
                        version_code=version_code, notes=notes, force=force,
                        data_dir=data_dir)


@contextmanager
def _publishing(folder: Path) -> Iterator[None]:
    """One publish at a time (two at once could leave one's APK beside the other's
    ``release.json``): an exclusive ``flock`` on ``.publish.lock`` in the folder."""
    with open(folder / LOCK_NAME, "a+b") as fh:
        try:
            import fcntl
        except ImportError:  # not POSIX: no lock
            yield
            return
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _publish(source: Path, folder: Path, *, version_name: str, version_code: int,
             notes: str, force: bool, data_dir: str | os.PathLike | None) -> dict:
    before = current(data_dir)
    if before and version_code < before["version_code"] and not force:
        raise ReleaseError(
            f"version code {version_code} is lower than the published "
            f"{before['version_code']} ({before['version_name']}); devices would never "
            "install it. Pass --force to publish it anyway.")
    fd, tmp_apk = _temp_in(folder, ".apk")
    tmp_json = None
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as src:
            size, sha = _copy_hashing(src, out)
            out.flush()
            os.fsync(out.fileno())
        if size == 0:
            raise ReleaseError(f"{source}: the file is empty")
        release = {"app": APP, "version_name": version_name, "version_code": version_code,
                   "size": size, "sha256": sha, "notes": notes}
        fd, tmp_json = _temp_in(folder, ".json")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(release, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        for tmp in (tmp_apk, tmp_json):
            os.chmod(tmp, 0o644)
        os.replace(tmp_apk, folder / APK_NAME)
        tmp_apk = None
        os.replace(tmp_json, folder / RELEASE_NAME)
        tmp_json = None
    finally:
        for tmp in (tmp_apk, tmp_json):
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return release


# ------------------------------------------------------------------------------ routes
# No ``from __future__ import annotations`` in this file: FastAPI must see ``Request``
# itself, and it is imported here, lazily, so the script runs without the web stack.
def setup_app_release_routes() -> Any:
    from fastapi import APIRouter, Request
    from fastapi.responses import FileResponse, JSONResponse

    from fork.voice.routes import PREFIX, voice_owner

    router = APIRouter(tags=["fork-voice-app"])

    @router.get(PREFIX + "/app/latest")
    def app_latest(request: Request) -> Any:
        voice_owner(request)
        release = current()
        if release is None:
            return JSONResponse(NO_RELEASE, status_code=404)
        return JSONResponse(release, headers={"cache-control": "no-store"})

    @router.get(PREFIX + "/app/apk")
    def app_apk(request: Request) -> Any:
        voice_owner(request)
        release = current()
        if release is None:
            return JSONResponse(NO_RELEASE, status_code=404)
        return FileResponse(
            release_dir() / APK_NAME, media_type=APK_MEDIA_TYPE,
            filename=f"{APP}-{release['version_code']}.apk",
            headers={"etag": f'"{release["sha256"]}"', "cache-control": "no-store"})

    return router
