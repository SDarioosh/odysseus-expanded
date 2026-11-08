"""Fork: wake word clips from the Echos (fork/voice/wakeword_clips.py and
wakeword_routes.py). Synthetic audio and fictional devices only."""
from __future__ import annotations

import io
import json
import logging
import math
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from fork.voice import devices, wakeword_clips as clips, wakeword_routes

REPO = Path(__file__).resolve().parents[2]
VOICE = {"x-test-scopes": "voice"}
ADMIN = {"x-test-user": "alice"}
CLIP = wakeword_routes.CLIP_PATH


def wav(seconds=1.0, *, rate=16000, channels=1, bits=16, tag=1, extra_chunk=False) -> bytes:
    """A sine tone as WAV (synthetic, never a real recording)."""
    frames = int(seconds * rate)
    width = bits // 8
    samples = b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate))) * channels
        for i in range(frames)) if width == 2 else b"\x00" * frames * channels * width
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * channels * width,
                      channels * width, bits)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    if extra_chunk:
        chunks += b"LIST" + struct.pack("<I", 5) + b"abcde" + b"\x00"  # odd size, padded
    chunks += b"data" + struct.pack("<I", len(samples)) + samples
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


AUDIO = wav(0.5)


def fields(clip_id="clip-0001", **over):
    raw = {"clip_id": clip_id, "device_id": "echo-1", "device_name": "Kitchen Echo",
           "wake_word": "hey_athena", "kind": "guided", "score": "0.42",
           "threshold": "0.6", "prompt": "Normal voice", "recorded_at": "1700000000000"}
    raw.update(over)
    return clips.clean_fields(raw, now_ms=1_700_000_500_000)


# --------------------------------------------------------------------------- WAV
def test_wav_duration_of_the_right_format():
    assert clips.wav_duration_ms(wav(1.0)) == 1000
    assert clips.wav_duration_ms(wav(0.25, extra_chunk=True)) == 250


@pytest.mark.parametrize("data", [
    b"",
    b"not a wav at all, just some bytes" * 3,
    wav(0.2, rate=8000),
    wav(0.2, channels=2),
    wav(0.2, bits=8),
    wav(0.2, tag=3, bits=16),  # float
    b"RIFF\x00\x00\x00\x00WAVEdata\x00\x00\x00\x00",  # no fmt chunk
])
def test_other_audio_is_415(data):
    with pytest.raises(clips.ClipError) as exc:
        clips.wav_duration_ms(data)
    assert exc.value.status == 415


# ------------------------------------------------------------------------ fields
def test_fields_are_cleaned():
    out = fields()
    assert out == {"id": "clip-0001", "wake_word": "hey_athena", "kind": "guided",
                   "device_id": "echo-1", "device_name": "Kitchen Echo", "score": 0.42,
                   "threshold": 0.6, "prompt": "Normal voice", "recorded_at": 1700000000000}
    bare = clips.clean_fields({"clip_id": "abcdefgh", "device_id": "e", "wake_word": "w",
                               "kind": "missed"}, now_ms=5)
    assert (bare["score"], bare["threshold"], bare["prompt"], bare["recorded_at"]) == (
        None, None, "", 5)


@pytest.mark.parametrize("over", [
    {"clip_id": "short"}, {"clip_id": "../../etc/passwd"}, {"clip_id": "a" * 65},
    {"clip_id": "abc_defgh"}, {"clip_id": None},
    {"wake_word": "Hey Athena"}, {"wake_word": "../x"}, {"wake_word": ""},
    {"kind": "other"}, {"kind": None},
    {"device_id": ""}, {"device_id": "x" * 129}, {"device_name": "x" * 101},
    {"prompt": "x" * 101},
    {"score": "1.5"}, {"score": "-0.1"}, {"score": "nan"}, {"threshold": "high"},
    {"recorded_at": "yesterday"}, {"recorded_at": "-5"},
])
def test_bad_fields_are_422(over):
    with pytest.raises(clips.ClipError) as exc:
        fields(**over)
    assert exc.value.status == 422


def test_ids_are_validated_strictly():
    assert clips.valid_clip_id("A1b2-C3d4")
    for bad in ("", "a/b/c/d/e", "abcdefg", "abcdefgh.wav", "abc defgh", None, 12345678):
        assert not clips.valid_clip_id(bad)


def test_owner_folders():
    assert clips.owner_dir_name("Alice") == "alice"
    odd = clips.owner_dir_name("../Bob Smith")
    assert odd.startswith("_h") and "/" not in odd and "." not in odd
    assert odd == clips.owner_dir_name("../bob smith")
    with pytest.raises(clips.ClipError):
        clips.owner_dir_name("")


# ------------------------------------------------------------------------- store
def test_save_and_list(data_dir):
    assert clips.save("alice", fields("clip-0001"), AUDIO, now_ms=10) == clips.Saved(
        "clip-0001", False)
    folder = data_dir / "wakeword_clips" / "alice" / "hey_athena" / "guided"
    assert (folder / "clip-0001.wav").read_bytes() == AUDIO
    meta = json.loads((folder / "clip-0001.json").read_text())
    assert meta["received_at"] == 10 and meta["bytes"] == len(AUDIO)
    assert meta["duration_ms"] == 500 and meta["score"] == 0.42
    assert not list(folder.glob("*.tmp"))
    clips.save("alice", fields("clip-0002", kind="missed", recorded_at="1700000009000"), AUDIO)
    clips.save("alice", fields("clip-0003", kind="heard", recorded_at="1700000005000"), AUDIO)
    clips.save("alice", fields("clip-0004", wake_word="hey_odysseus"), AUDIO)
    out = clips.listing("alice", "hey_athena")
    assert out["wake_words"] == ["hey_athena", "hey_odysseus"]
    assert out["counts"] == {"guided": 1, "missed": 1, "heard": 1}
    assert [c["id"] for c in out["clips"]] == ["clip-0002", "clip-0003", "clip-0001"]
    assert set(out["clips"][0]) == {"id", "wake_word", "kind", "device_id", "device_name",
                                    "score", "threshold", "prompt", "recorded_at",
                                    "duration_ms", "bytes"}
    everything = clips.listing("alice")
    assert everything["counts"] == {"guided": 2, "missed": 1, "heard": 1}
    assert len(clips.listing("alice", limit=2)["clips"]) == 2
    assert clips.listing("bob") == {"wake_words": [], "counts": {
        "guided": 0, "missed": 0, "heard": 0}, "clips": []}


def test_the_same_clip_id_is_stored_once(data_dir):
    clips.save("alice", fields("clip-0001"), AUDIO)
    again = clips.save("alice", fields("clip-0001", kind="missed"), wav(1.0))
    assert again == clips.Saved("clip-0001", True)
    assert clips.listing("alice")["counts"] == {"guided": 1, "missed": 0, "heard": 0}
    # another owner's clip of the same id is that owner's own
    assert clips.save("bob", fields("clip-0001"), AUDIO).already is False


def test_audio_too_large_or_wrong_is_refused(data_dir):
    with pytest.raises(clips.ClipError) as exc:
        clips.save("alice", fields(), b"x" * (clips.MAX_AUDIO_BYTES + 1))
    assert exc.value.status == 413
    with pytest.raises(clips.ClipError) as exc:
        clips.save("alice", fields(), wav(0.2, rate=44100))
    assert exc.value.status == 415
    assert clips.listing("alice")["clips"] == []


def test_the_store_is_capped_by_count_and_size(data_dir, monkeypatch):
    monkeypatch.setattr(clips, "MAX_CLIPS", 2)
    clips.save("alice", fields("clip-0001"), AUDIO)
    clips.save("alice", fields("clip-0002"), AUDIO)
    with pytest.raises(clips.StoreFull) as exc:
        clips.save("alice", fields("clip-0003"), AUDIO)
    assert exc.value.status == 507
    assert clips.save("alice", fields("clip-0002"), AUDIO).already  # a retry still answers
    clips.save("bob", fields("clip-0003"), AUDIO)  # per owner
    monkeypatch.setattr(clips, "MAX_CLIPS", 100)
    monkeypatch.setattr(clips, "MAX_TOTAL_BYTES", 3 * len(AUDIO))
    with pytest.raises(clips.StoreFull):
        clips.save("alice", fields("clip-0003"), AUDIO)


def test_delete(data_dir):
    clips.save("alice", fields("clip-0001"), AUDIO)
    assert clips.audio_path("bob", "clip-0001") is None
    assert not clips.delete("bob", "clip-0001")
    assert clips.audio_path("alice", "clip-0001").read_bytes() == AUDIO
    assert clips.delete("alice", "clip-0001")
    assert not clips.delete("alice", "clip-0001")
    assert clips.audio_path("alice", "clip-0001") is None
    assert not (data_dir / "wakeword_clips" / "alice" / "hey_athena").exists()
    assert not clips.delete("alice", "../../x")


def test_export_zip(data_dir):
    clips.save("alice", fields("clip-0001"), AUDIO)
    clips.save("alice", fields("clip-0002", kind="missed", recorded_at="1700000009000"), AUDIO)
    clips.save("alice", fields("clip-0003", wake_word="hey_odysseus"), AUDIO)
    buf = io.BytesIO()
    assert clips.write_zip("alice", buf, "hey_athena") == 2
    zf = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    assert sorted(zf.namelist()) == ["README.txt", "hey_athena/clips.jsonl",
                                     "hey_athena/guided/clip-0001.wav",
                                     "hey_athena/missed/clip-0002.wav"]
    assert zf.read("hey_athena/guided/clip-0001.wav") == AUDIO
    assert "never" in zf.read("README.txt").decode().lower()
    rows = [json.loads(line) for line in zf.read("hey_athena/clips.jsonl").decode().splitlines()]
    assert [r["id"] for r in rows] == ["clip-0001", "clip-0002"]  # oldest first
    assert rows[1]["file"] == "missed/clip-0002.wav" and rows[1]["kind"] == "missed"
    buf = io.BytesIO()
    assert clips.write_zip("alice", buf) == 3
    assert "hey_odysseus/clips.jsonl" in zipfile.ZipFile(buf).namelist()


# ---------------------------------------------------------------------------- routes
@pytest.fixture
def api(data_dir, monkeypatch):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        request.state.api_token_owner = request.headers.get("x-test-owner", "alice") \
            if scopes is not None else None
        request.state.api_token_scopes = scopes.split(",") if scopes else []
        return await call_next(request)

    def require_admin(request):
        if request.headers.get("x-test-user") != "alice":
            raise HTTPException(403, "Admin only")

    monkeypatch.setattr("core.middleware.require_admin", require_admin)
    monkeypatch.setattr("src.auth_helpers.get_current_user",
                        lambda request: request.headers.get("x-test-user"))
    app.include_router(wakeword_routes.setup_wakeword_routes())
    with TestClient(app) as client:
        yield client


def post(api, audio=AUDIO, headers=VOICE, **over):
    data = {"clip_id": "clip-0001", "device_id": "echo-1", "device_name": "Kitchen Echo",
            "wake_word": "hey_athena", "kind": "guided", "score": "0.42",
            "threshold": "0.6", "prompt": "Quietly"}
    data.update(over)
    data = {k: v for k, v in data.items() if v is not None}
    files = {"audio": ("clip.wav", audio, "audio/wav")} if audio is not None else None
    return api.post(CLIP, data=data, files=files, headers=headers)


def test_the_device_uploads_a_clip(api, caplog):
    caplog.set_level(logging.INFO)
    res = post(api)
    assert res.status_code == 200
    assert res.json() == {"ok": True, "id": "clip-0001", "already": False}
    assert post(api).json() == {"ok": True, "id": "clip-0001", "already": True}
    assert devices.get("alice", "echo-1").name == "Kitchen Echo"
    clip = clips.listing("alice")["clips"][0]
    assert clip["prompt"] == "Quietly" and clip["device_id"] == "echo-1"
    assert clip["recorded_at"] > 1_700_000_000_000  # default: now
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RIFF" not in logged and "Quietly" not in logged


def test_the_upload_route_checks_its_input(api):
    assert post(api, headers={}).status_code == 403
    assert post(api, headers={"x-test-scopes": "chat"}).status_code == 403
    assert post(api, audio=b"").status_code == 422  # empty
    no_audio = api.post(CLIP, data={"clip_id": "clip-0001", "device_id": "echo-1",
                                    "wake_word": "hey_athena", "kind": "guided"},
                        files={"note": ("a.txt", b"x", "text/plain")}, headers=VOICE)
    assert no_audio.status_code == 422
    assert post(api, audio=b"x" * (clips.MAX_AUDIO_BYTES + 1)).status_code == 413
    assert post(api, audio=b"hello there, not audio at all" * 4).status_code == 415
    assert post(api, audio=wav(0.2, channels=2)).status_code == 415
    assert post(api, kind="sometimes").status_code == 422
    assert post(api, clip_id="../../../x").status_code == 422
    assert post(api, wake_word="Hey Athena").status_code == 422
    assert post(api, score="2").status_code == 422
    assert post(api, device_id=None).status_code == 422
    res = api.post(CLIP, data={"clip_id": "clip-0001"}, headers=VOICE)  # urlencoded
    assert res.status_code == 415
    assert clips.listing("alice")["clips"] == []


def test_a_full_store_is_507(api, monkeypatch):
    monkeypatch.setattr(clips, "MAX_CLIPS", 1)
    assert post(api).status_code == 200
    res = post(api, clip_id="clip-0002")
    assert res.status_code == 507 and "full" in res.json()["detail"]


def test_a_registry_failure_does_not_fail_the_upload(api, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(devices, "seen", broken)
    assert post(api).status_code == 200


def test_the_admin_lists_plays_deletes_and_exports(api):
    post(api, clip_id="clip-0001", recorded_at="1700000000000")
    post(api, clip_id="clip-0002", kind="missed", recorded_at="1700000001000")
    post(api, clip_id="clip-0009", headers={**VOICE, "x-test-owner": "bob"})
    res = api.get("/api/fork/voice/wakeword/clips?wake_word=hey_athena", headers=ADMIN)
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    data = res.json()
    assert data["wake_words"] == ["hey_athena"]
    assert data["counts"] == {"guided": 1, "missed": 1, "heard": 0}
    assert [c["id"] for c in data["clips"]] == ["clip-0002", "clip-0001"]
    audio = api.get("/api/fork/voice/wakeword/clips/clip-0001/audio", headers=ADMIN)
    assert audio.status_code == 200 and audio.content == AUDIO
    assert audio.headers["content-type"] == "audio/wav"
    # bob's clip is bob's
    assert api.get("/api/fork/voice/wakeword/clips/clip-0009/audio",
                   headers=ADMIN).status_code == 404
    zipped = api.get("/api/fork/voice/wakeword/export.zip?wake_word=hey_athena", headers=ADMIN)
    assert zipped.status_code == 200 and zipped.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(zipped.content)).namelist()
    assert sorted(names) == ["README.txt", "hey_athena/clips.jsonl",
                             "hey_athena/guided/clip-0001.wav",
                             "hey_athena/missed/clip-0002.wav"]
    assert api.delete("/api/fork/voice/wakeword/clips/clip-0001",
                      headers=ADMIN).json() == {"ok": True}
    assert api.delete("/api/fork/voice/wakeword/clips/clip-0001",
                      headers=ADMIN).status_code == 404
    assert api.get("/api/fork/voice/wakeword/clips/..%2F..%2Fx/audio",
                   headers=ADMIN).status_code == 404
    assert api.get("/api/fork/voice/wakeword/clips?wake_word=../x",
                   headers=ADMIN).status_code == 422


def test_the_admin_routes_are_admin_only(api):
    post(api)
    for path in ("/api/fork/voice/wakeword/clips",
                 "/api/fork/voice/wakeword/clips/clip-0001/audio",
                 "/api/fork/voice/wakeword/export.zip"):
        assert api.get(path, headers={"x-test-user": "bob"}).status_code == 403
        assert api.get(path).status_code == 403
        assert api.get(path, headers={**VOICE, **ADMIN}).status_code == 403  # API token
    assert api.delete("/api/fork/voice/wakeword/clips/clip-0001",
                      headers={**VOICE, **ADMIN}).status_code == 403
    assert clips.listing("alice")["counts"]["guided"] == 1


def test_mounted_with_the_fork_routers():
    from fork.routes import ROUTERS

    assert ("fork.voice.wakeword_routes", "setup_wakeword_routes") in ROUTERS


# --------------------------------------------------------------------- the block's JS
@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_block_formats_clips():
    script = """
const m = await import(process.argv[1]);
console.log(JSON.stringify({
  counts: m.countsLine({guided: 12, missed: 3}),
  line: m.clipLine({kind: 'missed', device_name: 'Kitchen Echo', prompt: 'Quietly',
                    score: 0.42, threshold: 0.6, duration_ms: 1234, recorded_at: 5},
                   {formatTime: (ms) => 'at ' + ms}),
  bare: m.clipLine({kind: 'heard', device_id: 'echo-1', score: null}),
  query: [m.wordQuery(''), m.wordQuery('hey_athena')],
}));
"""
    url = (REPO / "static" / "js" / "forkWakewordClips.js").as_uri()
    done = subprocess.run(["node", "--input-type=module", "-e", script, url],
                          capture_output=True, text=True, check=True, timeout=30)
    out = json.loads(done.stdout)
    assert out["counts"] == "Guided 12 · Missed 3 · Heard 0"
    assert out["line"] == "missed · Kitchen Echo · “Quietly” · score 0.42/0.60 · 1.2 s · at 5"
    assert out["bare"] == "heard · echo-1"
    assert out["query"] == ["", "?wake_word=hey_athena"]
