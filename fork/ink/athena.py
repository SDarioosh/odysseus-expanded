"""Fork: handwritten notes, stage H4: send reviewed text to Athena. Plan:
docs/fork/handwritten-notes.md ("5. Into Athena"); strokes and the sidecar:
``src/ink_notes.py``; transcription and review: ``src/ink_transcribe.py``.

Athena's MCP HTTP app has two plain HTTP routes for written notes, behind the MCP token
(the same link the call recorder uploads through, ``src/call_recorder.py``):

* ``POST /ingest/text`` JSON ``{source_id, text, written_at, link, title?}``, answers
  ``{ok, source_id, result: created|replaced|unchanged, conversation_id, lines,
  erased_lines, transcript_link}``. The same ``source_id`` replaces the note; the same
  lines and ``written_at`` change nothing.
* ``DELETE /ingest/text/{source_id}`` answers ``deleted`` or ``already``.

One Athena note per hub note (plan open question 2, the default): ``source_id`` is
``hub-note:<note id>``, ``text`` is every reviewed page joined in page order
(:func:`note_text`), ``link`` the hub's absolute note URL. Nothing is sent before every
page with writing is reviewed for its writing as it is now (the owner reviews every page
by hand), and nothing is sent by itself: **Send** in the Text tab is the only way, also
after an edit (resend replaces the note).

The one exception is ``INK_AUTO_SEND`` (H6, off by default,
``src/ink_transcribe.py`` :func:`~src.ink_transcribe.auto_send_enabled`): a transcription
run accepts clean pages itself (``transcript.auto``) and, when it ends with no page
waiting, sends the note through the same :func:`send` (:func:`auto_send`). Save review
never sends, with or without it.

Sidecar ``athena``::

    {"sent_hash",        # sha256 of the text Athena last accepted (None: never sent)
     "sent_at",          # ISO, when it accepted it
     "conversation_id",  # Athena's conversation of the note
     "written_at",       # epoch s sent as written_at: fixed when Athena first accepts
                         # the note, so a resend of the same lines is "unchanged"
     "sending",          # epoch s while a send is in flight (None otherwise)
     "maybe_sent",       # True: a send failed without an answer (timeout, Athena down),
                         # so Athena may hold the note although it never confirmed it
     "auto",             # True: what Athena accepted last came from an automatic send
     "auto_error"}       # why the last automatic send failed (cleared by a good send)

Deleted notes: a sidecar whose note is gone and that was sent (or is being sent, or
whose send got no answer) stays until Athena answers ``deleted`` or ``already`` to the
DELETE (:func:`sweep`), so an unreachable Athena is asked again on the next run. The
sweep runs at the start of the hourly fact sync (``src/module_facts.py``,
:func:`sweep_all`), when the Write view lists notes, right after a delete in the Write
view, and when a send ends on a note deleted meanwhile.

The hub's base URL for ``link``: the ``app_public_url`` setting (Settings, Reminders,
"Public URL"), else the ``APP_PUBLIC_URL`` environment variable, else the origin the
browser used for the Send request (on the tablet, the hub's Tailscale address).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from fork.athena import call_recorder as rec
from fork.athena import client as athena_client
from fork.ink import notes as ink
from fork.ink import transcribe as tr

logger = logging.getLogger(__name__)

SOURCE_PREFIX = "hub-note:"
# Athena's limits for a written note (server/athena/validation.py, SDarioosh/Athena#108,
# #110)
MAX_TEXT_CHARS = 200_000
MAX_TEXT_LINES = 5_000
MAX_LINE_CHARS = 4_000       # a longer line is wrapped by Athena; each part counts as a line
MAX_TITLE_CHARS = 200
MAX_LINK_CHARS = 2_000
EARLIEST, LATEST = 946_684_800, 4_102_444_800  # written_at: 2000-01-01 .. 2100-01-01 UTC
# A send that started less than this long ago may still reach Athena: the sweep doesn't
# ask Athena to delete the note meanwhile (its answer could arrive before the note).
SEND_GRACE_SECONDS = 300
RESULTS = ("created", "replaced", "unchanged")

# Athena's link rule: http(s), no spaces, control characters, quotes, ( ) < > { } | \ ^
# or backtick, no user info, square brackets only around an IPv6 host.
_LINK_BAD = re.compile(r"[\s\x00-\x1f\x7f()<>\"'`{}|\\^]")
_LINK = re.compile(r"^https?://(\[[0-9A-Fa-f:.]+\]|[^\s/?#\[\]@]+)(:\d+)?([/?#][^\[\]]*)?$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Notes being sent by this process: a second Send waits for the first one's answer.
_SENDING: set[tuple[str, str]] = set()
# For tests: the transport every Athena call uses when none is passed.
TRANSPORT: httpx.AsyncBaseTransport | None = None


# --------------------------------------------------------------------------- text
def source_id(note_id: str) -> str:
    return SOURCE_PREFIX + note_id


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def note_text(stored: dict[str, Any]) -> str:
    """What is sent: the text of the pages reviewed now, in page order, a blank line
    between pages. Unlike the note's ``content`` (:func:`src.ink_transcribe.note_content`)
    a page's earlier reviewed text (``previous``) is never sent: a page read again waits
    for review anyway, and a page whose writing was erased shows no text, so it sends
    none."""
    parts = []
    for page in stored.get("pages") or []:
        t = page.get("transcript") if isinstance(page.get("transcript"), dict) else {}
        text = t.get("text")
        if t.get("status") == "reviewed" and isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def _wrapped(line: str) -> int:
    """How many lines Athena makes of one line (it wraps at the last space before
    ``MAX_LINE_CHARS``, or cuts there)."""
    n = 0
    while len(line) > MAX_LINE_CHARS:
        cut = line.rfind(" ", 0, MAX_LINE_CHARS + 1)
        if cut <= 0:
            cut = MAX_LINE_CHARS
        line = line[cut:].lstrip()
        n += 1
    return n + (1 if line else 0)


def athena_lines(text: str) -> int:
    """The number of lines Athena stores for ``text`` (empty lines left out)."""
    total = 0
    for raw in text.splitlines():
        line = " ".join(_CONTROL.sub("", raw).split())
        if line:
            total += _wrapped(line)
    return total


def _page_sizes(stored: dict[str, Any]) -> list[tuple[int, int, int]]:
    """(page number, characters, lines) of each page that has text to send."""
    out = []
    for i, page in enumerate(stored.get("pages") or []):
        text = note_text({"pages": [page]})
        if text:
            out.append((i + 1, len(text), athena_lines(text)))
    return out


def limit_problem(stored: dict[str, Any], text: str) -> str | None:
    """Why Athena would refuse ``text`` for its size, said for the owner, or None."""
    if len(text) > MAX_TEXT_CHARS:
        big = sorted(_page_sizes(stored), key=lambda p: -p[1])[:3]
        pages = ", ".join(f"page {n} ({c:,})" for n, c, _ in big)
        return (f"The text is {len(text):,} characters; Athena takes at most "
                f"{MAX_TEXT_CHARS:,}. Longest pages: {pages}. Shorten the text or move "
                "pages to another note.")
    lines = athena_lines(text)
    if lines > MAX_TEXT_LINES:
        big = sorted(_page_sizes(stored), key=lambda p: -p[2])[:3]
        pages = ", ".join(f"page {n} ({k:,})" for n, _, k in big)
        return (f"The text has {lines:,} lines; Athena takes at most {MAX_TEXT_LINES:,} "
                f"(a line over {MAX_LINE_CHARS:,} characters counts once for each "
                f"{MAX_LINE_CHARS:,}). Most lines: {pages}. Join lines or move pages to "
                "another note.")
    return None


def has_words(text: str) -> bool:
    return athena_lines(text) > 0


# -------------------------------------------------------------------------- pages
_WAITING = {"machine": "machine text to check", "failed": "transcription failed",
            "pending": "being transcribed"}


def waiting_pages(stored: dict[str, Any]) -> list[dict[str, Any]]:
    """Pages that keep the note from being sent, ``[{page, why}]`` (page numbers from 1):
    machine text not reviewed, a failed or running transcription, a page with writing
    never transcribed, or reviewed text older than the page's strokes."""
    out = []
    for i, page in enumerate(stored.get("pages") or []):
        t = tr._transcript(page)
        status = tr._status(t)
        why = _WAITING.get(status)
        if why is None and status == "none" and tr.has_ink(page):
            why = "not transcribed"
        if why is None and status == "reviewed" and t.get("strokes_hash") != tr.writing_hash(page):
            why = "the writing changed since it was reviewed"
        if why:
            out.append({"page": i + 1, "why": why})
    return out


def waiting_message(waiting: list[dict[str, Any]]) -> str:
    parts = [f"page {w['page']} ({w['why']})" for w in waiting[:8]]
    more = f" and {len(waiting) - 8} more" if len(waiting) > 8 else ""
    return ("Every page is reviewed before its text leaves the hub. Waiting: "
            + ", ".join(parts) + more + ".")


# --------------------------------------------------------------------------- state
def _athena(stored: dict[str, Any]) -> dict[str, Any]:
    a = stored.get("athena")
    if not isinstance(a, dict):
        a = {"sent_hash": None, "sent_at": None, "conversation_id": None}
        stored["athena"] = a
    return a


def _sending(a: dict[str, Any], now: float) -> bool:
    started = a.get("sending")
    return isinstance(started, (int, float)) and now - started < SEND_GRACE_SECONDS


def state(stored: dict[str, Any]) -> str:
    """``not_sent``, ``sent`` (Athena has the current reviewed text) or ``changed``
    (the reviewed text changed since it was sent, or a page waits for review again
    because its writing changed: Send replaces it once it is reviewed)."""
    a = stored.get("athena") if isinstance(stored.get("athena"), dict) else {}
    if not a.get("sent_hash"):
        return "not_sent"
    text = note_text(stored)
    if not has_words(text) or a["sent_hash"] != text_hash(text) or waiting_pages(stored):
        return "changed"
    return "sent"


def view(stored: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """What the Text tab shows about Athena (part of the transcripts answer)."""
    now = time.time() if now is None else now
    a = stored.get("athena") if isinstance(stored.get("athena"), dict) else {}
    text = note_text(stored)
    return {"state": state(stored), "sent_at": a.get("sent_at"),
            "conversation_id": a.get("conversation_id"),
            "transcript_link": a.get("transcript_link"),
            "waiting": waiting_pages(stored), "has_text": has_words(text),
            "sending": _sending(a, now), "auto": bool(a.get("sent_hash") and a.get("auto")),
            "auto_error": a.get("auto_error") if isinstance(a.get("auto_error"), str) else None}


# ---------------------------------------------------------------------------- link
def _base(url: str) -> str:
    """``url`` as a base for :func:`note_url`: the scheme in lower case (Athena's link
    rule is case-sensitive), no query or fragment (the note's ``#open=...`` goes last),
    no trailing ``/``."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.rstrip("/")
    if not parts.scheme or not parts.netloc:
        return url.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, "", "")).rstrip("/")


def hub_base(request_base: str | None = None) -> str:
    """The hub's own address, for links that leave it: the first of the Public URL
    setting, ``APP_PUBLIC_URL`` and the request's own base that makes a link Athena
    takes (else the first one given, so the Send refusal asks for the Public URL)."""
    try:
        from src.settings import get_setting

        configured = (get_setting("app_public_url", "") or "").strip()
    except Exception:  # noqa: BLE001 -- settings unreadable: fall through
        configured = ""
    from fork.common import settings

    candidates = [_base(c) for c in (configured, settings.app_public_url(),
                                     request_base or "") if c and c.strip()]
    for base in candidates:
        if check_link(note_url(base, "00000000")) is None:
            return base
    return candidates[0] if candidates else ""


def check_link(link: str) -> str | None:
    """Why Athena would refuse ``link``, or None."""
    if not link or len(link) > MAX_LINK_CHARS or not _LINK.match(link) or _LINK_BAD.search(link):
        return ("The hub's address can't be used in a link: set Settings → Reminders → "
                "Public URL to the address you open Odysseus at (http or https, for "
                "example the Tailscale name).")
    return None


def note_url(base: str, note_id: str) -> str:
    """``<hub>/#open=notes&note=<id>``: the absolute form of the adapter's note link."""
    return base.rstrip("/") + ink.note_link(note_id)


# --------------------------------------------------------------------------- send
def _clean_title(title: str) -> str | None:
    title = " ".join(_CONTROL.sub("", title or "").split())[:MAX_TITLE_CHARS].strip()
    return title or None


def _photo_time(page: dict[str, Any]) -> int | None:
    """When a photo page's photo came in (``photo.at``, ISO-8601), in seconds."""
    at = page.get("photo", {}).get("at") if ink.is_photo(page) else None
    try:
        when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp())


def _first_written(stored: dict[str, Any]) -> int | None:
    """When the note was first written: its earliest stroke (device clock, ms), or the
    earliest photo of a paper page (H6: it has no strokes). Times outside 2000..2100 (a
    clock not set yet) are left out."""
    pages = stored.get("pages") or []
    times = [int(at // 1000) for p in pages for s in p.get("strokes") or []
             if isinstance(at := s.get("at"), (int, float)) and not isinstance(at, bool)]
    times += [t for p in pages if (t := _photo_time(p)) is not None]
    times = [t for t in times if EARLIEST <= t < LATEST]
    return min(times) if times else None


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class _NothingNew(ink.InkError):
    """An automatic send found Athena already has the note's text."""


def _prepare(owner: str | None, note_id: str, base: str, now: float, auto: bool = False
             ) -> tuple[str | None, dict[str, Any] | None, str | None, int | None]:
    """Check the note and mark it ``sending``. Returns (store, payload, text hash,
    written_at); the payload is None when the note has no words left and was sent before
    (it is removed from Athena instead). ``written_at`` is stored only once Athena accepts
    the note (:func:`_finish`), so a failed first send doesn't fix it. Raises
    ``InkError`` with what to fix; ``auto`` (:func:`auto_send`): :class:`_NothingNew`
    when Athena has the text already (another run's automatic send just sent it)."""
    store = tr._check(owner, note_id)
    with ink._lock(store):
        stored = tr._load(store, note_id)
        waiting = waiting_pages(stored)
        if waiting:
            raise ink.InkError(waiting_message(waiting), 409)
        a = _athena(stored)
        if _sending(a, now) or (store or "", note_id) in _SENDING:
            raise ink.InkError("This note is being sent: wait for Athena's answer.", 409)
        if auto and state(stored) == "sent":
            raise _NothingNew("Athena has this text already.", 409)
        text = note_text(stored)
        payload: dict[str, Any] | None = None
        written: int | None = None
        if not has_words(text):
            if not a.get("sent_hash"):
                raise ink.InkError("No page has reviewed text yet: review a page, then send.",
                                   400)
        else:
            problem = limit_problem(stored, text)
            if problem:
                raise ink.InkError(problem, 413)
            link = note_url(base, note_id)
            problem = check_link(link)
            if problem:
                raise ink.InkError(problem, 400)
            written = a.get("written_at")
            if not (isinstance(written, (int, float)) and EARLIEST <= written < LATEST):
                written = _first_written(stored) or int(now)
            written = int(written)
            payload = {"source_id": source_id(note_id), "text": text,
                       "written_at": _iso(written), "link": link}
            title = _clean_title(ink.note_title(owner, note_id))
            if title:
                payload["title"] = title
        a["sending"] = now
        ink._write_json(ink.sidecar_path(store, note_id), stored)
    return store, payload, text_hash(text) if payload else None, written


def _finish(store: str | None, note_id: str, sent: dict[str, Any] | None,
            now_iso: str, maybe_sent: bool = False) -> None:
    """Store what Athena accepted (``sent``; None: nothing new is in Athena) and clear
    ``sending``. ``maybe_sent``: the send got no answer, so Athena may hold the note
    (the sidecar of a deleted note then stays until Athena confirms a DELETE). The note
    may have been deleted meanwhile: the sidecar is still there (``sending`` kept it)."""
    with ink._lock(store):
        stored = ink.load_sidecar(store, note_id)
        if stored is None:
            return
        a = _athena(stored)
        a["sending"] = None
        if sent is not None:
            a.update(sent)
            a["sent_at"] = now_iso if sent.get("sent_hash") else None
            a["maybe_sent"] = False
            a.pop("auto_error", None)
        elif maybe_sent:
            a["maybe_sent"] = True
        ink._write_json(ink.sidecar_path(store, note_id), stored)


def _module_failure(status: int, body: Any) -> ink.InkError:
    detail = str(body.get("detail") or "") if isinstance(body, dict) else ""
    if isinstance(body, dict) and isinstance(body.get("detail"), list):  # 422 from FastAPI
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d)
                           for d in body["detail"])[:300]
    if status in (400, 413, 422):
        return ink.InkError(f"Athena refused the note: {detail or f'HTTP {status}'}",
                            413 if status == 413 else 400)
    return ink.InkError(f"Athena answered HTTP {status}{f': {detail}' if detail else ''}", 502)


async def _link() -> rec.Link:
    try:
        return await asyncio.to_thread(athena_client.RECORDER.link)
    except rec.RecorderError as exc:
        raise ink.InkError(exc.message, exc.status) from None


async def _call(method: str, path: str, *, json: Any = None,
                transport: httpx.AsyncBaseTransport | None = None) -> tuple[int, Any]:
    link = await _link()
    try:
        return await rec.forward(link, method, path, json=json,
                                 transport=transport if transport is not None else TRANSPORT)
    except rec.RecorderError as exc:
        raise ink.InkError(exc.message, exc.status) from None


async def send(owner: str | None, note_id: str, *, request_base: str | None = None,
               transport: httpx.AsyncBaseTransport | None = None,
               clock: Callable[[], float] = time.time, auto: bool = False) -> dict[str, Any]:
    """Send the note's reviewed text to Athena, or replace what Athena has (``auto``: by
    :func:`auto_send`, stored as ``athena.auto``). Returns the
    transcripts answer (``src/ink_transcribe.py``, with ``athena``) plus ``sent``:
    ``{result, conversation_id, lines, erased_lines, transcript_link}`` (``result``
    ``deleted`` when the note has no words left and Athena had it). Raises ``InkError``
    (409 pages waiting for review or a send in flight, 413 over Athena's limits, 400 no
    text or a bad link, 503 Athena not set up, 502 Athena unreachable or failing)."""
    await _link()  # fail before marking anything when Athena isn't set up
    base = hub_base(request_base)
    store, payload, digest, written = await asyncio.to_thread(
        _prepare, owner, note_id, base, clock(), auto)
    key = (store or "", note_id)
    _SENDING.add(key)
    sent: dict[str, Any] | None = None
    maybe_sent = False  # the POST went out and no answer says Athena didn't store it
    try:
        if payload is None:
            out = await _delete(note_id, transport)
            sent = {"sent_hash": None, "conversation_id": None, "transcript_link": None,
                    "written_at": None, "auto": auto}
            answer = {"result": "deleted" if out == "deleted" else "already",
                      "conversation_id": None}
        else:
            maybe_sent = True
            status, body = await _call("POST", "/ingest/text", json=payload, transport=transport)
            # A refusal (bad request, too large, unknown route, auth) stored nothing.
            maybe_sent = status >= 500 or status == 200
            if status != 200 or not isinstance(body, dict):
                raise _module_failure(status, body)
            if body.get("result") not in RESULTS or not body.get("conversation_id"):
                raise ink.InkError("Athena sent an unreadable answer", 502)
            sent = {"sent_hash": digest, "conversation_id": str(body["conversation_id"]),
                    "transcript_link": body.get("transcript_link")
                    if isinstance(body.get("transcript_link"), str) else None,
                    "written_at": written, "auto": auto}
            answer = {k: body.get(k) for k in ("result", "conversation_id", "lines",
                                               "erased_lines", "transcript_link")}
            logger.info("ink: note sent to Athena (%s, %s lines)", answer["result"],
                        answer.get("lines"))
    finally:
        _SENDING.discard(key)
        await asyncio.to_thread(_finish, store, note_id, sent, ink._now_iso(), maybe_sent)
        if not await asyncio.to_thread(ink.note_id_taken, note_id):
            # Deleted while it was being sent: take it out of Athena now (the delete
            # couldn't while the send was in flight; the sweep retries if this fails).
            await after_delete(store, note_id, transport=transport)
    out = await asyncio.to_thread(tr.transcripts, owner, note_id)
    return {**out, "sent": answer}


# ------------------------------------------------------------------ auto-send (H6)
AUTO_SEND_WAIT = 60.0  # seconds a finished run waits for a send of the note in flight


def _auto_ready(owner: str | None, note_id: str) -> tuple[str | None, bool]:
    """(store, whether the note should be sent now): a handwritten note of ``owner``
    with words, no page waiting (none being read by another run either) and text Athena
    doesn't have yet."""
    try:
        store = tr._check(owner, note_id)
    except ink.InkError:
        return None, False  # deleted meanwhile
    with ink._lock(store):
        stored = ink.load_sidecar(store, note_id)
    if stored is None or not isinstance(stored.get("pages"), list):
        return store, False
    if waiting_pages(stored) or not has_words(note_text(stored)):
        return store, False
    return store, state(stored) != "sent"


def _auto_failed(store: str | None, note_id: str, message: str) -> None:
    """Keep why an automatic send failed, for the Text tab (a good send clears it)."""
    with ink._lock(store):
        stored = ink.load_sidecar(store, note_id)
        if stored is None:
            return
        _athena(stored)["auto_error"] = message[:300]
        ink._write_json(ink.sidecar_path(store, note_id), stored)


async def auto_send(owner: str | None, note_id: str, *, request_base: str | None = None,
                    transport: httpx.AsyncBaseTransport | None = None,
                    clock: Callable[[], float] = time.time,
                    wait: float = AUTO_SEND_WAIT) -> str | None:
    """INK_AUTO_SEND: send the note after a transcription run accepted pages by itself,
    if it is on, no page waits for review and Athena doesn't have the text yet. Waits
    for a send of the note already in flight (one send per note), then sends through
    :func:`send`. Returns Athena's result, ``waiting`` (a page waits or another send
    holds the note: left for later), ``failed`` (kept as ``athena.auto_error`` for the
    owner, who can Send by hand) or None (nothing to do)."""
    if not tr.auto_send_enabled():
        return None
    store, ready = await asyncio.to_thread(_auto_ready, owner, note_id)
    deadline = time.monotonic() + wait
    while ready and (store or "", note_id) in _SENDING and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    if ready and (store or "", note_id) in _SENDING:
        return "waiting"
    if ready:  # the send that ended may have sent this text already
        store, ready = await asyncio.to_thread(_auto_ready, owner, note_id)
    if not ready:
        return None
    try:
        out = await send(owner, note_id, request_base=request_base, transport=transport,
                         clock=clock, auto=True)
    except _NothingNew:  # another run's automatic send sent this text just now
        return None
    except ink.InkError as exc:
        if exc.status == 409:  # a page waits again, or a send holds the note
            return "waiting"
        if exc.status == 404:
            return None
        logger.warning("ink: automatic send to Athena failed (HTTP %s)", exc.status)
        await asyncio.to_thread(_auto_failed, store, note_id,
                                f"The automatic send failed: {exc}")
        return "failed"
    return (out.get("sent") or {}).get("result")


# ------------------------------------------------------------------ delete, sweep
async def _delete(note_id: str, transport: httpx.AsyncBaseTransport | None) -> str:
    """Ask Athena to remove the note; ``deleted`` or ``already``. Raises ``InkError``."""
    status, body = await _call("DELETE", f"/ingest/text/{rec.path_part(source_id(note_id))}",
                               transport=transport)
    result = body.get("result") if isinstance(body, dict) else None
    if status != 200 or result not in ("deleted", "already"):
        raise _module_failure(status, body) if status != 200 else ink.InkError(
            "Athena sent an unreadable answer", 502)
    return result


def _pending(owner: str | None, now: float) -> list[str]:
    """Sidecars of this owner folder whose note is gone and that Athena may hold."""
    out = []
    with ink._lock(owner):
        for note_id in ink.sidecar_ids(owner):
            if ink.note_id_taken(note_id):
                continue
            stored = ink.load_sidecar(owner, note_id)
            if ink.sent_to_athena(stored) and not _sending(_athena(stored), now):
                out.append(note_id)
    return out


def _forget(owner: str | None, note_id: str) -> None:
    with ink._lock(owner):
        if not ink.note_id_taken(note_id):
            ink._remove_sidecar(owner, note_id)
            ink._add_tombstones(owner, [note_id])


async def sweep(owner: str | None, *, transport: httpx.AsyncBaseTransport | None = None,
                clock: Callable[[], float] = time.time) -> dict[str, int]:
    """Remove the sidecars of deleted notes (``ink.sweep``), and ask Athena to remove the
    ones it was sent. A sidecar goes only once Athena answered ``deleted`` or
    ``already``; when Athena can't be reached it stays for the next run. Counts:
    ``removed`` (never sent), ``retracted``, ``kept``. Single-user mode (``owner``
    None) sees every note, so it sweeps every owner's folder (:func:`sweep_all`)."""
    if owner is None:
        return await sweep_all(transport=transport, clock=clock)
    return await _sweep_folder(owner, transport=transport, clock=clock)


async def _sweep_folder(owner: str | None, *,
                        transport: httpx.AsyncBaseTransport | None = None,
                        clock: Callable[[], float] = time.time) -> dict[str, int]:
    """:func:`sweep` for one ink folder (``owner`` None: ``_single``)."""
    removed = await asyncio.to_thread(ink.sweep_folder, owner)
    pending = await asyncio.to_thread(_pending, owner, clock())
    stats = {"removed": len(removed), "retracted": 0, "kept": 0}
    for i, note_id in enumerate(pending):
        try:
            await _delete(note_id, transport)
        except ink.InkError as exc:
            # Athena down or not set up: everything left waits for the next run.
            logger.warning("ink: deleted note not removed from Athena yet: %s", exc)
            stats["kept"] += len(pending) - i
            break
        await asyncio.to_thread(_forget, owner, note_id)
        stats["retracted"] += 1
    return stats


async def sweep_all(*, transport: httpx.AsyncBaseTransport | None = None,
                    clock: Callable[[], float] = time.time) -> dict[str, int]:
    """:func:`sweep` for every owner's ink folder (the hourly fact sync)."""
    total = {"removed": 0, "retracted": 0, "kept": 0}
    for owner in await asyncio.to_thread(ink.owner_folders):
        for key, value in (await _sweep_folder(owner, transport=transport,
                                               clock=clock)).items():
            total[key] += value
    return total


async def after_delete(store: str | None, note_id: str, *,
                       transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """Right after the Write view deleted a note (``store``: the folder that held its
    strokes, ``ink._store_owner`` taken before the delete): remove it from Athena if it
    was sent. ``deleted``, ``already``, ``pending`` (Athena unreachable or the note
    still being sent: the sweep retries) or None (never sent)."""
    stored = await asyncio.to_thread(ink.load_sidecar, store, note_id)
    if not ink.sent_to_athena(stored) or await asyncio.to_thread(ink.note_id_taken, note_id):
        return None
    if _sending(_athena(stored), time.time()):
        return "pending"
    try:
        result = await _delete(note_id, transport)
    except ink.InkError as exc:
        logger.warning("ink: deleted note not removed from Athena yet: %s", exc)
        return "pending"
    await asyncio.to_thread(_forget, store, note_id)
    return result
