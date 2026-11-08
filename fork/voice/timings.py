"""Fork: how long each step of a voice turn took (docs/fork/voice-assistant.md#timings).

Two clocks measure one turn, and each keeps its own durations, so no step mixes them:

* **The hub** (:class:`HubTimer`, ``fork/voice/routes.py``): when Home Assistant's
  request came in, when the turn was ready to run, the model's first token, the first
  answer text, the first sentence sent back, the end, and each tool call.
* **The device** (``POST …/satellite/timing``, ``fork/voice/timing_routes.py``): the
  satellite's pipeline stages in milliseconds after the wake word (run started, speech
  started and stopped, transcript, speech output asked for, first audio, done), so it
  sees Home Assistant's speech-to-text and text-to-speech.

Both land in one row per turn in ``<DATA_DIR>/fork_voice_timings.db``, joined by the
satellite context id the turn claimed and the device reports (:func:`save_device`), or,
for a report without one, by device and time. Rows hold numbers, the device and context
ids and tool names only:
never the question, the answer or audio. Kept :data:`KEEP_DAYS` days.
``scripts/voice_timings.py`` prints them, step by step.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from fork.common import sqlite as common_sqlite
from fork.common.owner import owner_key as _owner

logger = logging.getLogger(__name__)

DB_NAME = "fork_voice_timings.db"
KEEP_DAYS = 90
# A device report joins the hub row its turn produced: the hub got the request between
# the wake word and the device's end, give or take this much clock difference.
MATCH_SLACK_SECONDS = 10.0
MAX_TOOLS = 32

# The device's marks, in pipeline order (the Echo app's PipelineStage names, lower case).
DEVICE_MARKS = ("started", "listening", "voice_started", "voice_stopped",
                "awaiting_response", "awaiting_tts", "starting_tts", "streaming_tts",
                "completed_tts", "ended")
DEVICE_FLAGS = ("thinking_sound", "barge_in", "voice_check")
HUB_MARKS = ("ready", "first_token", "first_text", "first_spoken", "done")

SCHEMA = """
CREATE TABLE IF NOT EXISTS timings (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    at REAL NOT NULL,
    turn_id TEXT,
    device_id TEXT,
    hub TEXT,
    device TEXT
);
CREATE INDEX IF NOT EXISTS timings_owner_at ON timings (owner, at);
"""
# Added after the first schema (fork/common/sqlite.py adds it to an older file): the
# satellite context of the turn (fork/voice/satellite.py), the device report's exact key.
COLUMNS = (("timings", "context_id", "TEXT"),)
MAX_CONTEXT_ID = 64


def db_path() -> Path:
    from src.constants import DATA_DIR

    return Path(DATA_DIR) / DB_NAME


def _connect(path: Path | None = None,
             write: bool = False) -> AbstractContextManager[sqlite3.Connection]:
    return common_sqlite.connect(path or db_path(), SCHEMA, write, columns=COLUMNS)


def _prune(db: sqlite3.Connection, now: float) -> None:
    db.execute("DELETE FROM timings WHERE at < ?", (now - KEEP_DAYS * 86400,))


# ------------------------------------------------------------------------- the hub's clock
class HubTimer:
    """One voice turn on the hub's clock: seconds after the request came in."""

    def __init__(self, now: float | None = None):
        self.received = time.time() if now is None else float(now)
        self._mono = time.monotonic()
        self.marks: dict[str, float] = {}
        self.tools: list[dict] = []
        self._tool: tuple[str, float] | None = None
        self.rounds = 1
        self.tokens = (0, 0)
        self.outcome = "answered"

    def _now(self) -> float:
        return time.monotonic() - self._mono

    def mark(self, name: str) -> None:
        """The first time ``name`` happened (later ones are ignored)."""
        self.marks.setdefault(name, round(self._now(), 3))

    def observe(self, chunk: str) -> None:
        """One SSE chunk of the brain's turn (the events ``openai_api.Answer`` reads)."""
        if not chunk.startswith("data: "):
            return
        body = chunk[6:].strip()
        if body == "[DONE]":
            return
        try:
            data = json.loads(body)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        if isinstance(data.get("delta"), str):
            self.mark("first_token")
            if not data.get("thinking"):
                self.mark("first_text")
            return
        kind = data.get("type")
        if kind == "final_response":
            self.mark("first_token")
            self.mark("first_text")
        elif kind == "tool_start":
            self._end_tool()
            self._tool = (str(data.get("tool") or "?")[:64], self._now())
        elif kind == "tool_output":
            self._end_tool()
        elif kind == "agent_step":
            self._end_tool()
            self.rounds += 1
        elif kind == "metrics":
            m = data.get("data") or {}
            try:
                self.tokens = (int(m.get("input_tokens") or 0), int(m.get("output_tokens") or 0))
            except (TypeError, ValueError):
                pass

    def _end_tool(self) -> None:
        if self._tool is None:
            return
        name, began = self._tool
        self._tool = None
        if len(self.tools) < MAX_TOOLS:
            self.tools.append({"name": name, "seconds": round(self._now() - began, 3)})

    def finish(self, outcome: str = "") -> None:
        self._end_tool()
        if outcome:
            self.outcome = outcome
        self.mark("done")

    def as_json(self) -> dict:
        return {"marks": dict(self.marks), "tools": list(self.tools), "rounds": self.rounds,
                "tokens": {"input": self.tokens[0], "output": self.tokens[1]},
                "outcome": self.outcome}


async def observed(events: AsyncIterator[str], timer: HubTimer) -> AsyncIterator[str]:
    """``events`` unchanged, each one shown to ``timer`` on the way."""
    async for chunk in events:
        try:
            timer.observe(chunk)
        except Exception:  # noqa: BLE001 -- timing never breaks a turn
            logger.debug("voice: timing an event failed", exc_info=True)
        yield chunk


async def first_spoken(out: AsyncIterator[str], timer: HubTimer,
                       spoken: Any) -> AsyncIterator[str]:
    """The response stream unchanged; marks ``first_spoken`` once ``spoken()`` (the text
    sent so far) is not empty, that is when the first sentence went to Home Assistant."""
    async for chunk in out:
        if "first_spoken" not in timer.marks and spoken():
            timer.mark("first_spoken")
        yield chunk


def save_hub(owner: Any, turn_id: str | None, device_id: str | None, timer: HubTimer,
             *, context_id: str | None = None, path: Path | None = None) -> str:
    """The hub's half of a turn's row, written when the turn starts (so a device report
    that arrives first still finds it); returns the row id for :func:`update_hub`."""
    row_id = uuid.uuid4().hex
    now = time.time()
    with _connect(path, write=True) as db:
        _prune(db, now)
        db.execute("INSERT INTO timings (id, owner, at, turn_id, device_id, hub, context_id)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (row_id, _owner(owner), timer.received, turn_id, device_id or None,
                    json.dumps(timer.as_json()), context_id or None))
    return row_id


def set_context(turn_id: str, device_id: str, context_id: str, *,
                path: Path | None = None) -> None:
    """The satellite context posted after the turn started (fork/voice/interrupts.py
    ``adopt``) is this turn's: its row gets the device and context ids."""
    with _connect(path, write=True) as db:
        db.execute("UPDATE timings SET device_id = ?, context_id = ? WHERE turn_id = ?",
                   (device_id or None, context_id, turn_id))


def update_hub(row_id: str, timer: HubTimer, *, path: Path | None = None) -> None:
    """The hub's half once the turn is over."""
    with _connect(path, write=True) as db:
        db.execute("UPDATE timings SET hub = ? WHERE id = ?",
                   (json.dumps(timer.as_json()), row_id))


# ---------------------------------------------------------------------- the device's clock
class BadReport(ValueError):
    pass


def clean_device(body: Any) -> dict:
    """The device's report, checked: ``{"device_id", "started_at" (epoch ms of the wake
    word), "marks": {stage: ms after it}, "flags": {name: bool}, "start_mode"?,
    "context_id"?}`` (the id ``POST …/satellite/turn`` answered; empty means none)."""
    if not isinstance(body, dict):
        raise BadReport("The body must be a JSON object.")
    device_id = body.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
        raise BadReport("device_id is required (at most 128 characters).")
    started = body.get("started_at")
    if isinstance(started, bool) or not isinstance(started, int) or not 0 < started < 10 ** 14:
        raise BadReport("started_at must be epoch milliseconds.")
    raw = body.get("marks")
    if not isinstance(raw, dict):
        raise BadReport("marks must be an object.")
    marks = {}
    for name in DEVICE_MARKS:
        value = raw.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not 0 <= value <= 3_600_000:
            raise BadReport(f"marks.{name} must be milliseconds after the wake word.")
        marks[name] = int(value)
    flags_raw = body.get("flags") if isinstance(body.get("flags"), dict) else {}
    flags = {name: bool(flags_raw.get(name)) for name in DEVICE_FLAGS if name in flags_raw}
    mode = body.get("start_mode")
    mode = mode[:32] if isinstance(mode, str) else None
    context_id = body.get("context_id")
    if context_id is not None and (not isinstance(context_id, str)
                                   or len(context_id) > MAX_CONTEXT_ID):
        raise BadReport(f"context_id must be text (at most {MAX_CONTEXT_ID} characters).")
    return {"device_id": device_id.strip(), "started_at": started, "marks": marks,
            "flags": flags, "start_mode": mode, "context_id": (context_id or "").strip() or None}


def save_device(owner: Any, report: dict, *, path: Path | None = None) -> str | None:
    """Joins a device report to the hub row of its turn: with the report's
    ``context_id``, the row of the turn that claimed that context. Without one, or when
    no turn claimed it, by window: same owner, the same device (or a hub row that knows
    no device), the hub's request between the wake word and the device's end (a row of
    another context is left to its own report). With neither (Home Assistant answered the
    command itself, or the hub never saw the turn) the report gets a row of its own.
    Returns the joined turn id."""
    owner = _owner(owner)
    wake = report["started_at"] / 1000.0
    end = wake + max(report["marks"].values(), default=0) / 1000.0
    context_id = report.get("context_id") or None
    device = json.dumps({k: report[k] for k in ("started_at", "marks", "flags", "start_mode")})
    with _connect(path, write=True) as db:
        _prune(db, time.time())
        row = None
        if context_id:
            row = db.execute(
                "SELECT id, turn_id FROM timings WHERE owner = ? AND context_id = ?"
                " AND device IS NULL AND hub IS NOT NULL ORDER BY at DESC LIMIT 1",
                (owner, context_id)).fetchone()
        if row is None:
            row = db.execute(
                "SELECT id, turn_id FROM timings WHERE owner = ? AND device IS NULL"
                " AND hub IS NOT NULL AND (device_id = ? OR device_id IS NULL)"
                " AND (? IS NULL OR context_id IS NULL) AND at BETWEEN ? AND ?"
                " ORDER BY (device_id IS NULL), ABS(at - ?) LIMIT 1",
                (owner, report["device_id"], context_id, wake - MATCH_SLACK_SECONDS,
                 end + MATCH_SLACK_SECONDS, wake)).fetchone()
        if row is not None:
            db.execute("UPDATE timings SET device = ?, device_id = ?,"
                       " context_id = COALESCE(context_id, ?) WHERE id = ?",
                       (device, report["device_id"], context_id, row["id"]))
            return row["turn_id"]
        db.execute("INSERT INTO timings (id, owner, at, device_id, device, context_id)"
                   " VALUES (?, ?, ?, ?, ?, ?)",
                   (uuid.uuid4().hex, owner, wake, report["device_id"], device, context_id))
    return None


# ------------------------------------------------------------------------------ the report
def _gap(marks: dict, a: str, b: str, scale: float = 1.0) -> float | None:
    if a not in marks or b not in marks:
        return None
    d = (marks[b] - marks[a]) * scale
    return d if d >= 0 else None


# (label, which clock, from mark, to mark). "wake" is the device's 0.
STEPS = (
    ("Wake word to listening", "device", "wake", "listening"),
    ("You speaking", "device", "voice_started", "voice_stopped"),
    ("Speech to text (HA)", "device", "voice_stopped", "awaiting_response"),
    ("Odysseus: getting ready", "hub", "received", "ready"),
    ("Odysseus: model's first token", "hub", "ready", "first_token"),
    ("Odysseus: thinking", "hub", "first_token", "first_text"),
    ("Odysseus: first sentence sent", "hub", "received", "first_spoken"),
    ("Odysseus: whole answer", "hub", "received", "done"),
    ("Answer, as the device waited", "device", "awaiting_response", "awaiting_tts"),
    ("Text to speech: first audio", "device", "awaiting_tts", "streaming_tts"),
    ("Answer playing", "device", "streaming_tts", "completed_tts"),
    ("Silence after you stopped", "device", "voice_stopped", "streaming_tts"),
    ("Whole turn", "device", "wake", "ended"),
)


def steps_of(row: dict) -> dict[str, float]:
    """One row's step durations in seconds, by label."""
    out: dict[str, float] = {}
    dev = row.get("device") or {}
    dmarks = dict(dev.get("marks") or {})
    if dmarks:
        dmarks["wake"] = 0
    hub = row.get("hub") or {}
    hmarks = dict(hub.get("marks") or {})
    if hub:
        hmarks["received"] = 0.0
    for label, clock, a, b in STEPS:
        d = _gap(dmarks, a, b, 0.001) if clock == "device" else _gap(hmarks, a, b)
        if d is not None:
            out[label] = d
    tools = hub.get("tools") or []
    if tools:
        out["Odysseus: tool calls (all)"] = sum(float(t.get("seconds") or 0) for t in tools)
    return out


def rows(owner: Any | None = None, *, since: float = 0.0, path: Path | None = None) -> list[dict]:
    """The stored rows, oldest first (every owner's when ``owner`` is None)."""
    sql = "SELECT * FROM timings WHERE at >= ?"
    args: list[Any] = [since]
    if owner is not None:
        sql += " AND owner = ?"
        args.append(_owner(owner))
    with _connect(path) as db:
        found = db.execute(sql + " ORDER BY at", args).fetchall()
    out = []
    for r in found:
        out.append({"id": r["id"], "owner": r["owner"], "at": r["at"], "turn_id": r["turn_id"],
                    "device_id": r["device_id"], "context_id": r["context_id"],
                    "hub": json.loads(r["hub"]) if r["hub"] else None,
                    "device": json.loads(r["device"]) if r["device"] else None})
    return out


def _pct(values: list[float], q: float) -> float:
    values = sorted(values)
    k = (len(values) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def summary(found: list[dict]) -> list[dict]:
    """Per step: how many turns had it, median, 90th percentile and slowest, in seconds."""
    by: dict[str, list[float]] = {}
    for row in found:
        for label, value in steps_of(row).items():
            by.setdefault(label, []).append(value)
    order = [s[0] for s in STEPS] + ["Odysseus: tool calls (all)"]
    out = []
    for label in order:
        vals = by.get(label)
        if not vals:
            continue
        out.append({"step": label, "turns": len(vals), "median": statistics.median(vals),
                    "p90": _pct(vals, 0.9), "max": max(vals)})
    return out


def tool_summary(found: list[dict]) -> list[dict]:
    """Per tool: calls, median and slowest seconds, slowest median first."""
    by: dict[str, list[float]] = {}
    for row in found:
        for t in (row.get("hub") or {}).get("tools") or []:
            by.setdefault(str(t.get("name")), []).append(float(t.get("seconds") or 0))
    out = [{"tool": name, "calls": len(v), "median": statistics.median(v), "max": max(v)}
           for name, v in by.items()]
    return sorted(out, key=lambda t: -t["median"])
