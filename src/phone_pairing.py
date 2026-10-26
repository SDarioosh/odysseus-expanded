"""Fork: pair a phone relay with Odysseus by QR code (routes/sound_relay_routes.py).

The admin opens **Pair a phone** (``/modules/sound/pair``) and gets a QR code holding a
link ``odysseus-pair:v1?url=<Odysseus URL>&code=<pairing code>``. The phone relay scans it
and redeems the code at ``POST /api/pair/redeem``; only then is an API token created,
named "Phone relay <date>", owned by the admin who made the code, with the relay's scopes
(:data:`RELAY_SCOPES`). The answer carries the token once; it is never stored in clear
here and never in the QR. Each phone's token shows in Settings → API tokens and is
revoked there.

Pairing codes:

* random (about 120 bits), single use, valid :data:`CODE_TTL` (10 minutes);
* kept only as SHA-256 hashes, in memory: a restart forgets codes still waiting, which
  only means making a new one;
* redeeming is rate limited per client address and overall (:class:`RateLimit`), and an
  unknown, used or expired code gets the same answer.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import threading
import time
import uuid
from base64 import b64encode
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

CODE_TTL = 10 * 60  # seconds
SCHEME = "odysseus-pair"
# What the relay calls (Athena android-relay: HttpUploader, PdaHttp): uploads and
# the T5's module reads and writes through Odysseus, the calendar (the T5 adds and
# deletes events), checklists (the T5 ticks items) and the Today insight.
RELAY_SCOPES = ["sound:ingest", "sound:read", "sound:write", "calendar:read",
                "calendar:write", "todos:read", "todos:write", "insight:read"]
TOKEN_NAME = "Phone relay"
MAX_PENDING = 20  # open codes at once; older ones are dropped


class PairingError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Pending:
    id: str
    owner: str
    url: str
    expires: float
    redeemed_at: float | None = None
    token_id: str | None = None
    token_name: str | None = None


def check_url(raw: str) -> str:
    """The Odysseus URL the phone will use: http(s), a host, no query, no user info."""
    text = (raw or "").strip().rstrip("/")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PairingError(400, "The Odysseus address must start with http:// or https:// "
                                "and name a host.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise PairingError(400, "The Odysseus address can't have a user, ?query or #part.")
    if len(text) > 300:
        raise PairingError(400, "The Odysseus address is too long.")
    return text


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def pairing_link(url: str, code: str) -> str:
    return f"{SCHEME}:v1?url={quote(url, safe='')}&code={code}"


def parse_link(link: str) -> tuple[str, str]:
    """``(url, code)`` from a pairing link (the relay does the same in Kotlin)."""
    text = (link or "").strip()
    prefix = f"{SCHEME}:v1?"
    if not text.startswith(prefix):
        raise ValueError("not a pairing link")
    query = parse_qs(text[len(prefix):])
    return (query.get("url") or [""])[0], (query.get("code") or [""])[0]


def qr_png(text: str) -> str:
    """The QR code as a ``data:image/png;base64,…`` URI, made here (no CDN)."""
    import qrcode

    image = qrcode.make(text, box_size=8, border=2)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + b64encode(buf.getvalue()).decode("ascii")


@dataclass
class RateLimit:
    """At most ``per_client`` tries per address and ``overall`` tries in ``window`` s."""

    per_client: int = 10
    overall: int = 60
    window: float = 600.0
    _tries: dict[str, deque] = field(default_factory=dict)
    _all: deque = field(default_factory=deque)

    def allow(self, client: str, now: float) -> bool:
        cut = now - self.window
        while self._all and self._all[0] < cut:
            self._all.popleft()
        tries = self._tries.setdefault(client, deque())
        while tries and tries[0] < cut:
            tries.popleft()
        for key in [k for k, v in self._tries.items() if not v and k != client]:
            del self._tries[key]
        if len(tries) >= self.per_client or len(self._all) >= self.overall:
            return False
        tries.append(now)
        self._all.append(now)
        return True


class Pairings:
    """Pending pairing codes, by hash. Thread-safe; one per process."""

    def __init__(self, clock=time.time, rate: RateLimit | None = None) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._by_hash: dict[str, Pending] = {}
        self.rate = rate or RateLimit()

    def _sweep(self, now: float) -> None:
        for key in [k for k, p in self._by_hash.items()
                    if p.redeemed_at is None and p.expires <= now - CODE_TTL]:
            del self._by_hash[key]

    def create(self, owner: str, url: str) -> tuple[Pending, str]:
        """A new code for ``owner``; returns (pending, code). The code is not kept."""
        url = check_url(url)
        code = secrets.token_urlsafe(15)
        now = self._clock()
        pending = Pending(id=uuid.uuid4().hex[:12], owner=owner, url=url,
                          expires=now + CODE_TTL)
        with self._lock:
            self._sweep(now)
            waiting = sorted((p.expires, k) for k, p in self._by_hash.items()
                             if p.redeemed_at is None)
            for _, key in waiting[: max(0, len(waiting) - MAX_PENDING + 1)]:
                del self._by_hash[key]
            self._by_hash[_hash(code)] = pending
        return pending, code

    def state(self, pairing_id: str, owner: str) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            found = next((p for p in self._by_hash.values()
                          if p.id == pairing_id and p.owner == owner), None)
        if found is None:
            return {"status": "unknown"}
        if found.redeemed_at is not None:
            return {"status": "paired", "token_name": found.token_name}
        if found.expires <= now:
            return {"status": "expired"}
        return {"status": "waiting", "expires_in": int(found.expires - now)}

    def redeem(self, code: str, client: str, make_token) -> dict[str, Any]:
        """Consume ``code`` and return ``make_token(owner, name, scopes)``'s token once.

        Raises 429 when rate limited and 400 for any code that can't be used.
        """
        now = self._clock()
        with self._lock:
            if not self.rate.allow(client or "?", now):
                raise PairingError(429, "Too many pairing attempts; wait a few minutes.")
            pending = self._by_hash.get(_hash((code or "").strip()))
            if pending is None or pending.redeemed_at is not None or pending.expires <= now:
                raise PairingError(400, "This pairing code is not valid any more: make a new "
                                        "one in Odysseus (Conversations → Pair a phone).")
            pending.redeemed_at = now  # single use, even if the token can't be made
        name = f"{TOKEN_NAME} {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}"
        token = make_token(pending.owner, name, list(RELAY_SCOPES))
        pending.token_id, pending.token_name = token["id"], name
        return {"url": pending.url, "token": token["token"], "name": name,
                "scopes": list(RELAY_SCOPES)}


def create_api_token(owner: str, name: str, scopes: list[str]) -> dict[str, str]:
    """An Odysseus API token like Settings → API tokens makes (routes/api_token_routes.py):
    ``ody_`` + 32 random bytes, bcrypt hash and 8-character prefix stored."""
    import bcrypt

    from core.database import ApiToken, get_db_session

    raw = "ody_" + secrets.token_urlsafe(32)
    token_id = str(uuid.uuid4())[:8]
    with get_db_session() as db:
        db.add(ApiToken(id=token_id, owner=owner, name=name,
                        token_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
                        token_prefix=raw[:8], scopes=",".join(scopes), is_active=True))
    return {"id": token_id, "token": raw}


PAIRINGS = Pairings()
