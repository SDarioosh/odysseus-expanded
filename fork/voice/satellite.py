"""Fork: what a voice satellite says about a turn before Home Assistant asks Odysseus
(docs/fork/voice-assistant.md#satellites-owner-only-answers-and-announcements).

The satellite (the Echo app) posts ``POST /api/chat/voice/v1/satellite/turn``
(``fork/voice/satellite_routes.py``) when the owner stops speaking: which device and room
heard the question, the wake word, and optionally the spoken command as WAV. The hub keeps
that as a :class:`SatelliteContext` in memory for :data:`TTL_SECONDS`, per token owner,
and asks Athena, in the background, whose voice it is (Athena's ``POST
/speakers/identify`` relay route, through the MCP server Odysseus has for it,
``fork/athena/client.py``). The audio is only forwarded: never written, never logged, and
dropped once Athena answers.

The voice turn (``fork/voice/routes.py``) calls :func:`claim` when Home Assistant's
question arrives, and :func:`personal_allowed` to decide whether personal tools may run
(``FORK_VOICE_PRIVATE``).

Contexts live in this process only: after a restart, or with several workers, a turn may
find none (it then answers as if no satellite had posted, which is the safe side when
``FORK_VOICE_PRIVATE=owner``).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TTL_SECONDS = 30.0
# A turn takes only a context posted this recently: the Echo posts when the owner stops
# speaking and Home Assistant's request follows within seconds, so an older one belongs
# to a turn Home Assistant answered itself and must not lend its speaker to the next.
CLAIM_SECONDS = 10.0
MAX_PER_OWNER = 8
MAX_OWNERS = 64
IDENTIFY_PATH = "/speakers/identify"
IDENTIFY_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
MIN_SPEECH_SECONDS = 1.0  # Athena's own floor: under it, it couldn't compare

PRIVATE_REPLY = "I can only answer that for the owner of this assistant."


@dataclass(frozen=True)
class Speaker:
    verified: bool          # Athena compared the voice (False: no audio/unknown)
    is_owner: bool
    name: str | None
    score: float | None

    def as_json(self) -> dict[str, Any]:
        return {"verified": self.verified, "is_owner": self.is_owner, "name": self.name,
                "score": self.score}


@dataclass(frozen=True)
class SatelliteContext:
    context_id: str
    device_id: str
    device_name: str
    area: str
    wake_word: str
    started_at_ms: int
    speaker: Speaker | None
    # the device's barge-in came before Home Assistant asked: the turn answers empty
    # (fork/voice/interrupts.py)
    interrupted: bool = False


@dataclass
class _Entry:
    ctx: SatelliteContext
    created: float
    task: asyncio.Task | None = None
    claimed: bool = False


_lock = threading.Lock()
_contexts: OrderedDict[str, list[_Entry]] = OrderedDict()
_tasks: set[asyncio.Task] = set()  # strong references while a check runs


def _now() -> float:
    return time.monotonic()


def _prune(entries: list[_Entry], now: float) -> list[_Entry]:
    return [e for e in entries if now - e.created < TTL_SECONDS]


def forget_all() -> None:
    """Drop every context (tests)."""
    with _lock:
        _contexts.clear()


# ------------------------------------------------------------------ speaker check
def speaker_from_answer(body: Any) -> Speaker | None:
    """Athena's identify answer -> a :class:`Speaker`; None when it couldn't compare
    (too little speech, no score, a shape we don't know)."""
    if not isinstance(body, dict):
        return None
    score = body.get("score")
    score = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
    speech = body.get("speech_seconds")
    if isinstance(speech, (int, float)) and speech < MIN_SPEECH_SECONDS:
        return None
    found = body.get("speaker")
    if found is None:
        if score is None:
            return None
        return Speaker(verified=True, is_owner=False, name=None, score=score)
    if not isinstance(found, dict):
        return None
    name = found.get("name")
    return Speaker(verified=True, is_owner=found.get("is_owner") is True,
                   name=str(name) if name else None, score=score)


async def identify(audio: bytes, *, transport: httpx.AsyncBaseTransport | None = None
                   ) -> Speaker | None:
    """Ask Athena whose voice ``audio`` (WAV) is. None whenever it couldn't say: no Athena
    server set up, unreachable, a timeout, an older Athena without the route (404), or a
    refusal. Never raises; logs only the outcome, never the audio."""
    from fork.athena import client as athena

    try:
        link = athena.RECORDER.link()
        res = await athena.request(
            link, "POST", IDENTIFY_PATH,
            files={"audio": ("command.wav", audio, "audio/wav")},
            timeout=IDENTIFY_TIMEOUT, transport=transport,
            timed_out=(504, "Athena didn't answer in time"))
    except athena.AthenaError as exc:
        logger.info("voice satellite: speaker check skipped (%s)", exc.kind)
        return None
    except Exception as exc:  # noqa: BLE001 -- a speaker check never breaks a turn
        logger.warning("voice satellite: speaker check failed: %s", type(exc).__name__)
        return None
    if res.status_code in (404, 405):
        logger.info("voice satellite: this Athena can't identify speakers yet (HTTP %s)",
                    res.status_code)
        return None
    if res.status_code != 200:
        logger.warning("voice satellite: Athena answered the speaker check with HTTP %s",
                       res.status_code)
        return None
    try:
        return speaker_from_answer(res.json())
    except ValueError:
        return None


# ----------------------------------------------------------------------- contexts
def _finish(entry: _Entry, task: asyncio.Task) -> None:
    _tasks.discard(task)
    speaker = None
    if not task.cancelled() and task.exception() is None:
        speaker = task.result()
    with _lock:
        entry.ctx = replace(entry.ctx, speaker=speaker)


def register(owner: str, *, device_id: str, device_name: str = "", area: str = "",
             wake_word: str = "", started_at_ms: int | None = None,
             audio: bytes | None = None) -> _Entry:
    """Keep a new context for ``owner``; with ``audio``, start the speaker check in the
    background (the caller's event loop). The audio is held only by that task."""
    ctx = SatelliteContext(
        context_id=uuid.uuid4().hex, device_id=device_id, device_name=device_name,
        area=area, wake_word=wake_word,
        started_at_ms=int(started_at_ms if started_at_ms is not None else time.time() * 1000),
        speaker=None)
    entry = _Entry(ctx=ctx, created=_now())
    if audio:
        task = asyncio.get_running_loop().create_task(identify(audio))
        _tasks.add(task)
        entry.task = task
        task.add_done_callback(lambda t, e=entry: _finish(e, t))
    with _lock:
        entries = _prune(_contexts.pop(owner, []), entry.created)
        entries.append(entry)
        _contexts[owner] = entries[-MAX_PER_OWNER:]
        while len(_contexts) > MAX_OWNERS:
            _contexts.popitem(last=False)
    return entry


async def wait_for_check(entry: _Entry, seconds: float) -> SatelliteContext:
    """The entry's context once its speaker check is done, or as it is after ``seconds``."""
    task = entry.task
    if task is not None and not task.done() and seconds > 0:
        try:
            same_loop = task.get_loop() is asyncio.get_running_loop()
        except RuntimeError:
            same_loop = False
        if same_loop:
            await asyncio.wait({task}, timeout=seconds)
            await asyncio.sleep(0)  # let the done callback store the speaker
        else:  # a check started on another loop (tests): poll
            deadline = _now() + seconds
            while not task.done() and _now() < deadline:
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.02)
    with _lock:
        return entry.ctx


async def claim(owner: str, *, wait_seconds: float = 3.0) -> SatelliteContext | None:
    """The newest unclaimed context of this owner posted in the last CLAIM_SECONDS; if its
    speaker check is still running, wait up to wait_seconds for it. Each context is claimed
    once, and the owner's older unclaimed ones are dropped with it: they belong to turns
    that never reached the hub, and their speaker must not count for a later turn."""
    now = _now()
    with _lock:
        entries = _prune(_contexts.get(owner, []), now)
        if entries:
            _contexts[owner] = entries
        else:
            _contexts.pop(owner, None)
        entry = next((e for e in reversed(entries)
                      if not e.claimed and now - e.created < CLAIM_SECONDS), None)
        for e in entries:
            if not e.claimed and (entry is None or e.created <= entry.created):
                e.claimed = True
        if entry is None:
            return None
    return await wait_for_check(entry, wait_seconds)


def interrupt_unclaimed(owner: str, device_id: str) -> int:
    """A barge-in on ``device_id`` before Home Assistant asked for its turn: mark that
    device's contexts nobody claimed, so the turn that claims one answers empty at once
    (fork/voice/interrupts.py). The number marked."""
    now = _now()
    marked = 0
    with _lock:
        for e in _prune(_contexts.get(owner, []), now):
            if not e.claimed and e.ctx.device_id == device_id and now - e.created < CLAIM_SECONDS:
                e.ctx = replace(e.ctx, interrupted=True)
                marked += 1
    return marked


def mark_claimed(entry: _Entry) -> None:
    """The context belongs to a turn that is already running (fork/voice/interrupts.py
    ``adopt``): no later turn may claim it."""
    with _lock:
        entry.claimed = True


def personal_allowed(ctx: SatelliteContext | None) -> bool:
    """FORK_VOICE_PRIVATE (env/config): "off" (default) -> always True.
    "owner" -> True only if ctx and ctx.speaker and ctx.speaker.verified and is_owner."""
    from fork.common import settings

    if settings.voice_private() != "owner":
        return True
    return bool(ctx and ctx.speaker and ctx.speaker.verified and ctx.speaker.is_owner)
