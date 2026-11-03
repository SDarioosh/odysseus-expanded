"""Fork: speak a message on the voice satellites through Home Assistant
(docs/fork/voice-assistant.md#announcements).

:func:`announce` calls Home Assistant's ``assist_satellite.announce`` service
(``POST <FORK_HA_URL>/api/services/assist_satellite/announce``, ``{"entity_id": [...],
"message": "..."}``) with the long-lived token ``FORK_HA_TOKEN``, on the entities of
``FORK_VOICE_ANNOUNCE_ENTITIES`` (or the ones given). Home Assistant turns the text into
speech with its own (local) text-to-speech.

``FORK_HA_URL`` must be a local, private-network or Tailscale host: a name is resolved and
every address it has must be one (``fork.upstream.is_local_or_private_host``), so the
message never leaves the owner's network. Anything else is refused.

:func:`announce_reminder` is the hook for Odysseus's due reminders
(``src/builtin_actions.py``, the note-reminder scanner): "Reminder: <title>", in the
background, never failing or slowing the reminder itself. Off while unconfigured.

Reusable: a later feature (a spoken daily briefing) calls :func:`announce` the same way.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

SERVICE_PATH = "/api/services/assist_satellite/announce"
TIMEOUT = httpx.Timeout(10.0, connect=3.0)
MAX_MESSAGE = 500
MAX_ENTITIES = 20

_tasks: set[asyncio.Task] = set()


class Refused(Exception):
    """Why nothing was announced (the words the admin test route shows)."""


def configured() -> bool:
    from fork.common import settings

    return bool(settings.ha_url() and settings.ha_token() and settings.voice_announce_entities())


def _private_ip(text: str) -> bool:
    from fork.upstream import is_local_or_private_host

    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    return is_local_or_private_host(str(ip)) or ip.is_loopback or ip.is_link_local or (
        ip.version == 6 and ip.is_private)


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.to_thread(socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def check_url(url: str) -> str:
    """``url`` without a trailing slash when its host is on the owner's network, else
    :class:`Refused`. A name counts only when every address it resolves to is private."""
    from fork.upstream import is_local_or_private_host

    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise Refused("FORK_HA_URL must be an http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise Refused("FORK_HA_URL must be a plain base URL")
    if not (is_local_or_private_host(host) or _private_ip(host)):
        try:
            addresses = await _resolve(host)
        except (OSError, UnicodeError):
            raise Refused("FORK_HA_URL's host doesn't resolve") from None
        if not addresses or not all(_private_ip(a) for a in addresses):
            raise Refused("FORK_HA_URL must be a local, private-network or Tailscale host")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


async def send(message: str, entities: list[str] | None = None, *,
               transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Announce, or raise :class:`Refused` saying why not."""
    from fork.common import settings

    text = " ".join((message or "").split())[:MAX_MESSAGE]
    if not text:
        raise Refused("Nothing to announce")
    targets = [e.strip() for e in (entities if entities is not None
                                   else settings.voice_announce_entities()) if e and e.strip()]
    targets = list(dict.fromkeys(targets))[:MAX_ENTITIES]
    if not targets:
        raise Refused("No satellites to announce on (FORK_VOICE_ANNOUNCE_ENTITIES)")
    if not settings.ha_url():
        raise Refused("FORK_HA_URL is not set")
    token = settings.ha_token()
    if not token:
        raise Refused("FORK_HA_TOKEN is not set")
    base = await check_url(settings.ha_url())
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport,
                                     follow_redirects=False) as client:
            res = await client.post(base + SERVICE_PATH,
                                    json={"entity_id": targets, "message": text},
                                    headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise Refused(f"Home Assistant is not reachable ({type(exc).__name__})") from None
    if not res.is_success:
        raise Refused(f"Home Assistant answered HTTP {res.status_code}")


async def announce(message: str, entities: list[str] | None = None, *,
                   transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """Speak ``message`` on the satellites; True when Home Assistant took it. Never
    raises; a refusal is logged without the message."""
    try:
        await send(message, entities, transport=transport)
        return True
    except Refused as exc:
        logger.info("voice announce: not sent: %s", exc)
    except Exception as exc:  # noqa: BLE001 -- announcing never breaks its caller
        logger.warning("voice announce: failed: %s", type(exc).__name__)
    return False


def announce_reminder(title: Any) -> None:
    """Hook for a reminder that just fired: "Reminder: <title>" in the background, when
    announcements are configured. Never raises, never waits."""
    try:
        if not configured():
            return
        text = " ".join(str(title or "").split()) or "Reminder"
        if not text.lower().startswith("reminder"):
            text = f"Reminder: {text}"
        task = asyncio.get_running_loop().create_task(announce(text))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception as exc:  # noqa: BLE001
        logger.debug("voice announce: reminder hook skipped: %s", type(exc).__name__)
