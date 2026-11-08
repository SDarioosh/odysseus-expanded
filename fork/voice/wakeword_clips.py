"""Fork: the owner's wake word clips from the Echos (docs/fork/voice-assistant.md#wake-word-clips).

The Echo's wake word model ("Hey Athena", microWakeWord) was trained on synthetic voices
only, so it misses the owner. The Echo can send real clips of the owner saying the wake
word (``POST /api/chat/voice/v1/satellite/wakeword/clip``,
``fork/voice/wakeword_routes.py``); the owner lists, plays and deletes them in Settings and
downloads them as a zip to retrain the model.

Storage: ``<DATA_DIR>/wakeword_clips/<owner>/<wake_word>/<kind>/<clip_id>.wav`` and
``<clip_id>.json`` beside it (the fields of :func:`save`, plus ``received_at``, ``bytes``
and ``duration_ms``), each written atomically (``fork/common/state.py``); the JSON file is
written last, so a clip counts once it has one. ``<owner>`` is the owner's name when it is
a plain one, else a hash (:func:`owner_dir_name`). At most :data:`MAX_CLIPS` clips and
:data:`MAX_TOTAL_BYTES` per owner. The audio stays on this hub: never logged, never sent
anywhere.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fork.common.owner import owner_key
from fork.common.state import write_atomic

DIR_NAME = "wakeword_clips"
KINDS = ("guided", "missed", "heard")
MAX_AUDIO_BYTES = 400 * 1024  # 10 s of 16 kHz mono 16-bit WAV is ~313 KB
MAX_CLIPS = 3000  # per owner
MAX_TOTAL_BYTES = 400 * 1024 * 1024  # per owner, WAV and JSON files
LIST_LIMIT = 500
SAMPLE_RATE = 16000
FIELD_LIMITS = {"device_id": 128, "device_name": 100, "prompt": 100}

CLIP_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
WAKE_WORD = re.compile(r"[a-z0-9_]{1,64}")
_PLAIN_OWNER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

README = """\
Wake word clips from Odysseus
=============================

These are private recordings of your voice, sent by your Echo devices to your
Odysseus hub, for retraining your own wake word model on your own machines.

Never commit them to a repository, never upload them to a cloud service or share
them. Delete them when you are done training.

Layout: <wake_word>/<kind>/<clip_id>.wav (16 kHz mono 16-bit PCM) and
<wake_word>/clips.jsonl (one metadata row per clip).
Kinds: guided (said on purpose in the Echo's training screen), missed (scored
high-ish without waking, followed by a wake within seconds: likely a missed wake
word), heard (the wake that followed such a miss).
"""

_lock = threading.Lock()


class ClipError(ValueError):
    """A clip that can't be stored; ``status`` is the HTTP answer for it."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class StoreFull(ClipError):
    def __init__(self, message: str) -> None:
        super().__init__(507, message)


@dataclass(frozen=True)
class Saved:
    id: str
    already: bool


# ------------------------------------------------------------------------- paths
def root_dir(data_dir: str | os.PathLike | None = None) -> Path:
    """``<DATA_DIR>/wakeword_clips`` (``DATA_DIR`` read at call time, so tests can move it)."""
    if data_dir is None:
        from src.constants import DATA_DIR

        data_dir = DATA_DIR
    return Path(data_dir) / DIR_NAME


def owner_dir_name(owner: Any) -> str:
    """The owner's folder: :func:`~fork.common.owner.owner_key` when it is plain (``alice``), else
    ``_h`` and a hash of it (a plain name never starts with ``_``, so the two can't clash)."""
    key = owner_key(owner)
    if not key:
        raise ClipError(403, "no owner")
    if _PLAIN_OWNER.fullmatch(key):
        return key
    return "_h" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def owner_dir(owner: Any) -> Path:
    return root_dir() / owner_dir_name(owner)


def valid_clip_id(clip_id: Any) -> bool:
    return isinstance(clip_id, str) and CLIP_ID.fullmatch(clip_id) is not None


def valid_wake_word(word: Any) -> bool:
    return isinstance(word, str) and WAKE_WORD.fullmatch(word) is not None


# ---------------------------------------------------------------------------- WAV
def wav_duration_ms(data: bytes) -> int:
    """The length of a WAV that is 16 kHz mono 16-bit PCM, in ms. Raises :class:`ClipError`
    (415) for anything else: not RIFF/WAVE, no ``fmt `` or ``data`` chunk, another format."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ClipError(415, "audio must be WAV (16 kHz mono 16-bit PCM)")
    fmt: tuple[int, int, int, int] | None = None
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if cid == b"fmt ":
            if size < 16 or body + 16 > len(data):
                break
            tag, channels, rate = struct.unpack("<HHI", data[body:body + 8])
            bits = struct.unpack("<H", data[body + 14:body + 16])[0]
            if tag == 0xFFFE and size >= 40 and body + 26 <= len(data):
                # WAVE_FORMAT_EXTENSIBLE: the real format is the sub-format's first two bytes
                tag = struct.unpack("<H", data[body + 24:body + 26])[0]
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            if fmt != (1, 1, SAMPLE_RATE, 16):
                break
            frames = max(0, min(size, len(data) - body)) // 2
            if frames == 0:
                raise ClipError(422, "audio has no samples")
            return round(frames * 1000 / SAMPLE_RATE)
        pos = body + size + (size & 1)
    raise ClipError(415, "audio must be WAV, 16 kHz mono 16-bit PCM")


# ------------------------------------------------------------------------ fields
def clean_text(value: Any, limit: int, name: str, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ClipError(422, f"{name} must be text")
    value = " ".join(value.split())
    if required and not value:
        raise ClipError(422, f"{name} is required")
    if len(value) > limit:
        raise ClipError(422, f"{name} is too long (at most {limit} characters)")
    return value


def unit_float(value: Any, name: str) -> float | None:
    """A float from 0 to 1, or None when left out."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        raise ClipError(422, f"{name} must be a number from 0 to 1") from None
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ClipError(422, f"{name} must be a number from 0 to 1")
    return number


def epoch_ms(value: Any, name: str, *, now_ms: int) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return now_ms
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ClipError(422, f"{name} must be epoch milliseconds") from None
    if not 0 < number < 10 ** 14:
        raise ClipError(422, f"{name} must be epoch milliseconds")
    return number


def clean_fields(raw: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    """The clip's metadata from the form's fields (all text), checked. Raises
    :class:`ClipError` (422) for a bad or missing one."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    clip_id = raw.get("clip_id")
    clip_id = clip_id.strip() if isinstance(clip_id, str) else clip_id
    if not valid_clip_id(clip_id):
        raise ClipError(422, "clip_id must be 8 to 64 letters, digits or dashes")
    wake_word = raw.get("wake_word")
    wake_word = wake_word.strip() if isinstance(wake_word, str) else wake_word
    if not valid_wake_word(wake_word):
        raise ClipError(422, "wake_word must be 1 to 64 lower-case letters, digits or _")
    kind = raw.get("kind")
    kind = kind.strip() if isinstance(kind, str) else kind
    if kind not in KINDS:
        raise ClipError(422, "kind must be one of " + ", ".join(KINDS))
    return {
        "id": clip_id,
        "wake_word": wake_word,
        "kind": kind,
        "device_id": clean_text(raw.get("device_id"), FIELD_LIMITS["device_id"],
                                "device_id", required=True),
        "device_name": clean_text(raw.get("device_name"), FIELD_LIMITS["device_name"],
                                  "device_name"),
        "score": unit_float(raw.get("score"), "score"),
        "threshold": unit_float(raw.get("threshold"), "threshold"),
        "prompt": clean_text(raw.get("prompt"), FIELD_LIMITS["prompt"], "prompt"),
        "recorded_at": epoch_ms(raw.get("recorded_at"), "recorded_at", now_ms=now_ms),
    }


# ------------------------------------------------------------------------- store
def _meta_files(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return [p for p in base.glob("*/*/*.json")
            if valid_clip_id(p.stem) and p.parent.name in KINDS
            and valid_wake_word(p.parent.parent.name)]


def _find(base: Path, clip_id: str) -> Path | None:
    """The JSON file of ``clip_id`` in an owner's folder, or None."""
    if not valid_clip_id(clip_id) or not base.is_dir():
        return None
    for path in base.glob(f"*/*/{clip_id}.json"):
        if path.parent.name in KINDS and valid_wake_word(path.parent.parent.name):
            return path
    return None


def _usage(base: Path) -> tuple[int, int]:
    """(clips, bytes) an owner holds: JSON files counted, WAV and JSON sizes summed."""
    clips = total = 0
    if not base.is_dir():
        return 0, 0
    for path in base.glob("*/*/*"):
        if path.suffix == ".json" and valid_clip_id(path.stem):
            clips += 1
        if path.suffix in (".json", ".wav"):
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return clips, total


def save(owner: Any, fields: dict[str, Any], audio: bytes, *,
         now_ms: int | None = None) -> Saved:
    """Store one clip (``fields`` from :func:`clean_fields`). A clip id the owner already
    has answers ``already`` and stores nothing. Raises :class:`ClipError` for audio that
    isn't 16 kHz mono 16-bit PCM WAV (415) or too large (413), :class:`StoreFull` (507)
    when the owner's store is at :data:`MAX_CLIPS` or would pass :data:`MAX_TOTAL_BYTES`."""
    if len(audio) > MAX_AUDIO_BYTES:
        raise ClipError(413, "audio is too large (10 s of 16 kHz WAV at most)")
    duration = wav_duration_ms(audio)
    base = owner_dir(owner)
    clip_id = fields["id"]
    meta = {key: fields.get(key) for key in (
        "id", "wake_word", "kind", "device_id", "device_name", "score", "threshold",
        "prompt", "recorded_at")}
    meta.update(received_at=int(time.time() * 1000) if now_ms is None else now_ms,
                bytes=len(audio), duration_ms=duration)
    body = json.dumps(meta, ensure_ascii=False, sort_keys=True).encode("utf-8")
    with _lock:
        if _find(base, clip_id) is not None:
            return Saved(clip_id, True)
        clips, total = _usage(base)
        if clips >= MAX_CLIPS:
            raise StoreFull(f"The wake word clip store is full ({MAX_CLIPS} clips). "
                            "Download and delete some in Settings.")
        if total + len(audio) + len(body) > MAX_TOTAL_BYTES:
            raise StoreFull(f"The wake word clip store is full "
                            f"({MAX_TOTAL_BYTES // (1024 * 1024)} MB). "
                            "Download and delete some in Settings.")
        folder = base / meta["wake_word"] / meta["kind"]
        folder.mkdir(parents=True, exist_ok=True)
        write_atomic(folder / f"{clip_id}.wav", audio)
        write_atomic(folder / f"{clip_id}.json", body)
    return Saved(clip_id, False)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    # the folders are the truth for id, wake word and kind
    meta.update(id=path.stem, kind=path.parent.name, wake_word=path.parent.parent.name)
    return meta


PUBLIC_FIELDS = ("id", "wake_word", "kind", "device_id", "device_name", "score",
                 "threshold", "prompt", "recorded_at", "duration_ms", "bytes")


def _row(meta: dict[str, Any]) -> dict[str, Any]:
    return {key: meta.get(key) for key in PUBLIC_FIELDS}


def _sort_key(meta: dict[str, Any]) -> tuple[int, int, str]:
    def num(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return num(meta.get("recorded_at")), num(meta.get("received_at")), str(meta.get("id"))


def all_clips(owner: Any, wake_word: str = "") -> list[dict[str, Any]]:
    """Every clip's metadata (``wake_word`` only, when given), newest first."""
    rows = []
    for path in _meta_files(owner_dir(owner)):
        if wake_word and path.parent.parent.name != wake_word:
            continue
        meta = _read(path)
        if meta is not None:
            rows.append(meta)
    rows.sort(key=_sort_key, reverse=True)
    return rows


def wake_words(owner: Any) -> list[str]:
    base = owner_dir(owner)
    if not base.is_dir():
        return []
    return sorted({p.parent.parent.name for p in _meta_files(base)})


def listing(owner: Any, wake_word: str = "", *, limit: int = LIST_LIMIT) -> dict[str, Any]:
    """``{"wake_words", "counts", "clips"}`` for the admin page: the owner's wake words, the
    clips per kind (of ``wake_word``, or all) and the newest ``limit`` clips."""
    if wake_word and not valid_wake_word(wake_word):
        raise ClipError(422, "bad wake_word")
    rows = all_clips(owner, wake_word)
    counts = {kind: 0 for kind in KINDS}
    for meta in rows:
        counts[meta["kind"]] += 1
    return {"wake_words": wake_words(owner), "counts": counts,
            "clips": [_row(m) for m in rows[:max(0, limit)]]}


def audio_path(owner: Any, clip_id: str) -> Path | None:
    """The WAV of one of the owner's clips, or None when there is no such clip."""
    meta = _find(owner_dir(owner), clip_id)
    if meta is None:
        return None
    wav = meta.with_suffix(".wav")
    return wav if wav.is_file() else None


def delete(owner: Any, clip_id: str) -> bool:
    """Delete one clip (WAV and JSON); False when the owner has no such clip."""
    with _lock:
        meta = _find(owner_dir(owner), clip_id)
        if meta is None:
            return False
        for path in (meta.with_suffix(".wav"), meta):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for folder in (meta.parent, meta.parent.parent):
            try:
                folder.rmdir()  # only when empty
            except OSError:
                break
    return True


def write_zip(owner: Any, target: Any, wake_word: str = "") -> int:
    """Write the owner's clips (``wake_word`` only, when given) as a zip to ``target`` (a
    path or a binary file): ``README.txt``, ``<wake_word>/<kind>/<clip_id>.wav`` and
    ``<wake_word>/clips.jsonl``. Answers how many clips it holds."""
    if wake_word and not valid_wake_word(wake_word):
        raise ClipError(422, "bad wake_word")
    rows = list(reversed(all_clips(owner, wake_word)))  # oldest first in the files
    base = owner_dir(owner)
    lines: dict[str, list[str]] = {}
    count = 0
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("README.txt", README)
        for meta in rows:
            wav = base / meta["wake_word"] / meta["kind"] / f"{meta['id']}.wav"
            if not wav.is_file():
                continue
            zf.write(wav, f"{meta['wake_word']}/{meta['kind']}/{meta['id']}.wav")
            row = {**_row(meta), "received_at": meta.get("received_at"),
                   "file": f"{meta['kind']}/{meta['id']}.wav"}
            lines.setdefault(meta["wake_word"], []).append(
                json.dumps(row, ensure_ascii=False, sort_keys=True))
            count += 1
        for word, rows_of_word in sorted(lines.items()):
            zf.writestr(f"{word}/clips.jsonl", "\n".join(rows_of_word) + "\n")
    return count
