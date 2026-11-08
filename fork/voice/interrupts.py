"""Fork: a satellite's barge-in ends the voice turn it interrupted
(docs/fork/voice-assistant.md#barge-in).

Saying the wake word while the Echo is still thinking starts a new question. Home
Assistant runs one Assist pipeline per satellite and only ends the interrupted one when
its conversation agent (Odysseus) has answered; the Echo starts the new question only
after that (odysseus-echo ``odysseus/HaRunGate.kt``), and gives up after 15 s. A voice turn
that looks things up takes longer than that, so the new question was dropped.

So the Echo tells the hub (``POST /api/chat/voice/v1/satellite/interrupt``,
``fork/voice/satellite_routes.py``) and the hub cuts that device's turn short: the turn
stops (its model call too, so the new question doesn't queue behind it) and Home
Assistant gets an empty answer at once, which it doesn't speak; its run ends in about a
second and the new question goes ahead.

A turn is matched by the satellite context it claimed (``fork/voice/satellite.py``). The
Echo posts that context when the owner stops speaking, in the background, so Home
Assistant's question can arrive first and find none; a context posted within
:data:`ADOPT_SECONDS` after a turn without one started is that turn's (:func:`adopt`).
The barge-in names the context (``context_id``, what ``POST …/satellite/turn``
answered): the turn running with it is cut, or, when Home Assistant hasn't asked yet,
the context is marked and the turn that claims it answers empty at once. A barge-in
without an id, or with one the hub doesn't know (it restarted, the context expired),
does the same by device: every running turn of that device, else its contexts nobody
claimed. Any other turn without a context (another satellite, HA's text Assist) is never
cut.

In this process's memory only, like the satellite contexts.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fork.common.owner import owner_key as _owner

logger = logging.getLogger(__name__)


@dataclass
class _Running:
    owner: str
    device_id: str
    stop: asyncio.Event
    loop: asyncio.AbstractEventLoop
    context_id: str = ""
    started: float = field(default_factory=time.monotonic)


# A context posted this soon after a turn without one started belongs to it: Home
# Assistant asks as soon as speech-to-text is done, and the Echo posts at the same moment.
ADOPT_SECONDS = 3.0

_lock = threading.Lock()
_running: dict[str, _Running] = {}  # turn id -> the running turn


def start(turn_id: str, owner: object, device_id: str | None,
          context_id: str | None = None) -> asyncio.Event:
    """A voice turn starts; the event is set when its device interrupts it. ``device_id``
    and ``context_id`` are None when the turn found no satellite context."""
    stop = asyncio.Event()
    with _lock:
        _running[turn_id] = _Running(owner=_owner(owner), device_id=device_id or "",
                                     stop=stop, loop=asyncio.get_running_loop(),
                                     context_id=context_id or "")
    return stop


def context_of(turn_id: str) -> tuple[str, str]:
    """``(device id, context id)`` of a running turn, as :func:`adopt` may have set them
    after it started; ``("", "")`` when it has none or isn't running."""
    with _lock:
        r = _running.get(turn_id)
        return (r.device_id, r.context_id) if r is not None else ("", "")


def end(turn_id: str) -> None:
    with _lock:
        _running.pop(turn_id, None)


def _cut(hits: list[_Running]) -> int:
    for r in hits:
        r.loop.call_soon_threadsafe(r.stop.set)
    return len(hits)


def adopt(owner: object, device_id: str, context_id: str) -> str | None:
    """A satellite context for ``device_id`` was just posted: if exactly one turn of
    ``owner`` without a context started in the last :data:`ADOPT_SECONDS`, it is that
    device's (Home Assistant asked before the post arrived). Returns that turn's id when
    adopted; the caller then marks the context claimed and stores its id on the turn."""
    key, now = _owner(owner), time.monotonic()
    with _lock:
        found = [(tid, r) for tid, r in _running.items()
                 if r.owner == key and not r.device_id and not r.stop.is_set()
                 and now - r.started <= ADOPT_SECONDS]
        if len(found) != 1 or not device_id:
            return None
        turn_id, r = found[0]
        r.device_id, r.context_id = device_id, context_id or ""
    return turn_id


def interrupt(owner: object, device_id: str,
              context_id: str | None = None) -> tuple[int, int]:
    """``device_id``'s wake word interrupted its turn: cut it short, or, when Home
    Assistant hasn't asked yet, mark the waiting context. With ``context_id``, only that
    context's turn or context; without it, or when the hub knows no such context, the
    device's. ``(turns cut, contexts marked)``."""
    from fork.voice import satellite

    key = _owner(owner)
    if context_id:
        with _lock:
            mine = [r for r in _running.values()
                    if r.owner == key and r.context_id == context_id and not r.stop.is_set()]
        if mine:
            return _cut(mine), 0
        marked = satellite.interrupt_context(str(owner), context_id)
        if marked is not None:
            return 0, marked
    if not device_id:
        return 0, 0
    with _lock:
        mine = [r for r in _running.values()
                if r.owner == key and r.device_id == device_id and not r.stop.is_set()]
    if mine:
        return _cut(mine), 0
    return 0, satellite.interrupt_unclaimed(str(owner), device_id)


def running_count() -> int:
    with _lock:
        return len(_running)


def forget_all() -> None:
    """Drop every running turn (tests)."""
    with _lock:
        _running.clear()


async def until_stopped(events: AsyncIterator[str], stop: asyncio.Event) -> AsyncIterator[str]:
    """``events`` until ``stop`` is set; then the turn is closed (its model call ends with
    it: a voice turn isn't detached, ``fork/brain/turn.py`` ``read_only_turn``)."""
    it = events.__aiter__()
    waiter = asyncio.ensure_future(stop.wait())
    try:
        while not stop.is_set():
            step = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({step, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if step in done:
                try:
                    chunk = step.result()
                except StopAsyncIteration:
                    return
                yield chunk
                continue
            step.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                await step
            return
    finally:
        waiter.cancel()
        close = getattr(it, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()
