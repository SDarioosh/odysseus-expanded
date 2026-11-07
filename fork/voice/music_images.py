"""Fork: library images for the Echo's Music page (docs/fork/voice-assistant.md#music-page).

Contract C8 of the Echo music plan (odysseus-echo ``docs/odysseus/plans/2026-11-music.md``).
Home Assistant proxies the now-playing art but not Music Assistant's library or search
thumbnails: those point at MA's ``imageproxy`` on its LAN address, or at a streaming
service's image CDN. The device never loads them itself; it asks the hub, which runs next
to MA:

``GET /api/chat/voice/v1/music/image?u=<url>&size=<64..512, default 256>`` (API token with
the ``voice`` scope) answers the image scaled down to ``size`` (longest side), as
``image/jpeg`` (``image/png`` when it has transparency), ``Cache-Control:
max-age=604800``; 400 ``{"error": "bad url"}``, 403 ``{"error": "not allowed"}``, 404
``{"error": "not found"}`` (also for anything the source answers that isn't a usable
image).

What the hub fetches (:func:`decide`, pure; :func:`check_addresses`, pure, after DNS):

* MA's ``imageproxy`` on ``FORK_MA_URL`` (same scheme, host and port; both
  ``<base>/imageproxy/<id>`` and ``<base>/imageproxy?path=…&provider=…``), and only as
  the URL asked for, never as a redirect. Unset: never. MA fetches an http(s) ``path``
  itself and reads local files for ``provider=builtin``, so in the query form an http(s)
  ``path`` must pass the internet rules below on its own (DNS included), any other scheme
  is refused, and so is a ``path`` with ``..`` or, for ``builtin``, an absolute one.
* Else only a public ``https`` URL, and only while ``FORK_MUSIC_IMAGES_INTERNET`` is on
  (the default). Every address the host resolves to must be a public one (no private,
  loopback, link-local, shared, multicast or reserved address), and the connection goes
  to the address that was checked (no second lookup), with the TLS name of the URL.
* Redirects are followed by hand, at most :data:`MAX_REDIRECTS`, each hop decided again
  as an internet URL.
* Only ``image/*`` answers, not content-encoded, up to :data:`MAX_BYTES` received;
  :data:`CONNECT_SECONDS` to connect, :data:`TOTAL_SECONDS` in all (DNS, every hop and the
  body under one deadline). Pillow reads JPEG, PNG, WebP and GIF only (the first frame),
  at most :data:`MAX_PIXELS`, in a worker thread, :data:`SCALE_SLOTS` at a time.

Scaled images are kept on disk in ``<DATA_DIR>/music_images/`` (file name: sha256 of the
URL and the size) for :data:`CACHE_SECONDS`, at most :data:`CACHE_BYTES`, oldest out
first (eviction runs at most once per :data:`EVICT_EVERY` stores or
:data:`EVICT_SECONDS`). Logs carry the status and the size, never the URL.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import logging
import os
import re
import socket
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from fork.voice.routes import PREFIX, voice_owner

logger = logging.getLogger(__name__)

ROUTE = PREFIX + "/music/image"
MAX_URL = 4096
MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 40_000_000  # a "5 MB" picture that unpacks to more is a decompression bomb
MAX_REDIRECTS = 3
SIZE_MIN, SIZE_MAX, SIZE_DEFAULT = 64, 512, 256
CONNECT_SECONDS = 5.0
TOTAL_SECONDS = 10.0
CACHE_DIR = "music_images"
CACHE_SECONDS = 7 * 86400
CACHE_BYTES = 200 * 1024 * 1024
EVICT_EVERY = 50
EVICT_SECONDS = 300.0
TMP_SECONDS = 3600.0
SCALE_SLOTS = 2
FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
CACHE_CONTROL = f"max-age={CACHE_SECONDS}"
REDIRECTS = frozenset({301, 302, 303, 307, 308})
_EXTENSIONS = {".jpg": "image/jpeg", ".png": "image/png"}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_SCALING = asyncio.Semaphore(SCALE_SLOTS)  # decode and scale work, at most this many at once
_EVICT_LOCK = threading.Lock()
_EVICTION: dict[str, Any] = {"at": None, "stores": 0}


class BadUrl(ValueError):
    """Not an http(s) URL the route can read (400)."""


class NotAllowed(ValueError):
    """A URL the hub may not fetch (403)."""


class NotFound(Exception):
    """No usable image at the URL (404)."""


@dataclass(frozen=True)
class MusicAssistant:
    """``FORK_MA_URL``, split."""
    scheme: str
    host: str
    port: int
    path: str  # the base path without a trailing slash ("" for most installs)


@dataclass(frozen=True)
class Target:
    url: str
    kind: str  # "ma" (MA's imageproxy) or "internet"
    host: str
    inner: Target | None = None  # MA's query form with an http(s) ``path``: that URL


@dataclass(frozen=True)
class Fetched:
    """One answer, never a followed redirect. ``body`` is empty unless it's an image."""
    status: int
    content_type: str = ""
    body: bytes = b""
    location: str = ""


Fetcher = Callable[[str, "str | None"], Awaitable[Fetched]]
Resolver = Callable[[str], Awaitable[list[str]]]


# ------------------------------------------------------------------------ the decision
def music_assistant(raw: str | None) -> MusicAssistant | None:
    """``FORK_MA_URL`` as :class:`MusicAssistant`, or None when unset or not a plain
    http(s) base URL."""
    text = (raw or "").strip()
    if not text:
        return None
    parts = urlsplit(text)
    try:
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme not in _DEFAULT_PORTS or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        return None
    return MusicAssistant(parts.scheme, parts.hostname.lower(),
                          port or _DEFAULT_PORTS[parts.scheme], parts.path.rstrip("/"))


def _imageproxy(path: str, base: str) -> str | None:
    """``"id"`` for ``<base>/imageproxy/<id>``, ``"query"`` for ``<base>/imageproxy``,
    else None."""
    prefix = base + "/imageproxy"
    if path in (prefix, prefix + "/"):
        return "query"
    if not path.startswith(prefix + "/"):
        return None
    segments = path[len(prefix) + 1:].split("/")
    for raw in segments:
        seg = unquote(raw)
        if seg in (".", "..") or "/" in seg or "\\" in seg:
            return None
    return "id" if segments[0] else None


def _ma_query(query: str, internet: bool) -> Target | None:
    """The ``path`` of MA's query form, checked: the inner internet :class:`Target` for an
    http(s) one, None for a provider's own image path; :class:`NotAllowed` otherwise."""
    params = parse_qs(query, keep_blank_values=True)
    paths, providers = params.get("path", []), params.get("provider", [])
    if len(paths) != 1 or len(providers) > 1:
        raise NotAllowed("not allowed")
    value = paths[0]
    for _ in range(5):  # MA unquotes a path with "%" in it once more; be stricter
        if "%" not in value or unquote(value) == value:
            break
        value = unquote(value)
    value = value.strip()
    provider = (providers[0] if providers else "builtin").strip().lower()
    if not value or any(c in value for c in "\x00\r\n"):
        raise NotAllowed("not allowed")
    if _SCHEME.match(value):
        if value.split(":", 1)[0].lower() not in _DEFAULT_PORTS:
            raise NotAllowed("not allowed")  # file:, data:, ftp:, C:\…
        try:
            return decide(value, ma=None, internet=internet)
        except BadUrl:
            raise NotAllowed("not allowed") from None
    if value.startswith(("//", "\\\\")) or ".." in value:
        raise NotAllowed("not allowed")
    if provider == "builtin" and value.startswith(("/", "\\", "~")):
        raise NotAllowed("not allowed")
    return None


def on_music_assistant(url: str, ma: MusicAssistant | None) -> bool:
    """True when ``url`` is on MA's scheme, host and port. Pure."""
    if ma is None:
        return False
    parts = urlsplit((url or "").strip())
    try:
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme)
    except ValueError:
        return False
    return (parts.scheme == ma.scheme and (parts.hostname or "").lower() == ma.host
            and port == ma.port)


def decide(url: str, *, ma: MusicAssistant | None, internet: bool) -> Target:
    """What ``url`` is to the route, before any DNS: :class:`Target`, or :class:`BadUrl` /
    :class:`NotAllowed`. Pure."""
    text = (url or "").strip()
    if not text or len(text) > MAX_URL:
        raise BadUrl("bad url")
    parts = urlsplit(text)
    try:
        port = parts.port
    except ValueError:
        raise BadUrl("bad url") from None
    host = (parts.hostname or "").lower()
    if parts.scheme not in _DEFAULT_PORTS or not host:
        raise BadUrl("bad url")
    if parts.username is not None or parts.password is not None:
        raise BadUrl("bad url")
    port = port or _DEFAULT_PORTS[parts.scheme]
    if ma is not None and parts.scheme == ma.scheme and host == ma.host and port == ma.port:
        form = _imageproxy(parts.path, ma.path)
        if form == "id":
            return Target(text, "ma", host)
        if form == "query":
            return Target(text, "ma", host, _ma_query(parts.query, internet))
    if parts.scheme != "https" or not internet:
        raise NotAllowed("not allowed")
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES) or (
            "." not in host and ":" not in host):
        raise NotAllowed("not allowed")
    if _ip(host) is not None and not public_address(host):
        raise NotAllowed("not allowed")
    return Target(text, "internet", host)


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def public_address(text: str) -> bool:
    """True for a public unicast address; False for private, loopback, link-local, shared
    (CGNAT, Tailscale), multicast, reserved, unspecified, and IPv6 forms that embed one."""
    ip = _ip(text)
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if ip.teredo:
            embedded = ip.teredo[1]
        if ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not public_address(str(embedded)):
            return False
    return bool(ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback
                                      or ip.is_link_local or ip.is_private
                                      or ip.is_unspecified))


def check_addresses(target: Target, addresses: list[str]) -> str | None:
    """The address to connect to (None: connect by name, MA's own address). Every address
    of an internet host must be public, else :class:`NotAllowed`. Pure."""
    if target.kind == "ma":
        return None
    if not addresses:
        raise NotFound("not found")
    for address in addresses:
        if not public_address(address):
            raise NotAllowed("not allowed")
    return addresses[0].split("%", 1)[0]


def parse_size(raw: str | None) -> int:
    """``size`` as a number from 64 to 512 (clamped); missing or not a number: 256."""
    try:
        size = int(str(raw or "").strip())
    except ValueError:
        return SIZE_DEFAULT
    return max(SIZE_MIN, min(SIZE_MAX, size))


# ------------------------------------------------------------------------- the network
async def resolve(host: str) -> list[str]:
    infos = await asyncio.to_thread(socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def http_fetch(url: str, pin: str | None) -> Fetched:
    """One GET without following redirects; with ``pin``, to that address (Host header
    and TLS name from ``url``). Raises :class:`NotFound` past :data:`MAX_BYTES` received
    or for a content-encoded body (never decompressed)."""
    target = httpx.URL(url)
    headers = {"Accept": "image/*", "Accept-Encoding": "identity",
               "User-Agent": "Odysseus music images"}
    extensions: dict[str, Any] = {}
    if pin:
        headers["Host"] = target.netloc.decode("ascii")
        extensions["sni_hostname"] = target.host
        target = target.copy_with(host=pin)
    timeout = httpx.Timeout(TOTAL_SECONDS, connect=CONNECT_SECONDS)
    async with (
        httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout) as client,
        client.stream("GET", target, headers=headers, extensions=extensions) as res,
    ):
        if res.status_code in REDIRECTS:
            return Fetched(res.status_code, location=res.headers.get("location", ""))
        ctype = res.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if res.status_code != 200 or not ctype.startswith("image/"):
            return Fetched(res.status_code, ctype)
        encoding = res.headers.get("content-encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            raise NotFound("encoded")
        length = res.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BYTES:
            raise NotFound("too big")
        body = bytearray()
        async for chunk in res.aiter_raw():
            body += chunk
            if len(body) > MAX_BYTES:
                raise NotFound("too big")
        return Fetched(200, ctype, bytes(body))


# --------------------------------------------------------------------------- the image
def scale(data: bytes, size: int) -> tuple[bytes, str]:
    """``data`` shrunk to ``size`` on its longest side (never enlarged): JPEG, or PNG when
    it has transparency. :class:`NotFound` when Pillow can't read it."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data), formats=FORMATS) as img:
            if img.width * img.height > MAX_PIXELS:
                raise NotFound("too many pixels")
            if getattr(img, "n_frames", 1) > 1:
                img.seek(0)  # animated: the first frame
            if img.format == "JPEG":
                img.draft("RGB", (size, size))
            img.thumbnail((size, size))
            alpha = img.mode in ("RGBA", "LA", "PA") or (
                img.mode == "P" and "transparency" in img.info)
            out = io.BytesIO()
            if alpha:
                img.convert("RGBA").save(out, "PNG", optimize=True)
                return out.getvalue(), "image/png"
            img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
            return out.getvalue(), "image/jpeg"
    except NotFound:
        raise
    except Exception:  # noqa: BLE001 -- any undecodable picture is "not found"
        raise NotFound("not an image") from None


# --------------------------------------------------------------------------- the cache
def cache_dir() -> Path:
    from src.constants import DATA_DIR

    return Path(DATA_DIR) / CACHE_DIR


def cache_key(url: str, size: int) -> str:
    return hashlib.sha256(f"{url}\n{size}".encode()).hexdigest()


def cached(url: str, size: int, now: float) -> tuple[bytes, str] | None:
    folder = cache_dir()
    key = cache_key(url, size)
    for ext, ctype in _EXTENSIONS.items():
        path = folder / (key + ext)
        try:
            if now - path.stat().st_mtime < CACHE_SECONDS:
                return path.read_bytes(), ctype
        except OSError:
            continue
    return None


def store(url: str, size: int, data: bytes, ctype: str, now: float) -> None:
    folder = cache_dir()
    ext = next(e for e, t in _EXTENSIONS.items() if t == ctype)
    path = folder / (cache_key(url, size) + ext)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.utime(tmp, (now, now))
        os.replace(tmp, path)
        with _EVICT_LOCK:
            _EVICTION["stores"] += 1
            due = evict_due(now, _EVICTION["at"], _EVICTION["stores"])
            if due:
                _EVICTION.update(at=now, stores=0)
        if due:
            evict(now)
    except OSError:
        logger.warning("voice: couldn't cache a music image", exc_info=True)


def evict_due(now: float, last: float | None, stores: int) -> bool:
    """Whether a store runs :func:`evict`: the first time, then once per
    :data:`EVICT_EVERY` stores or :data:`EVICT_SECONDS` (or when the clock went back). Pure."""
    return (last is None or stores >= EVICT_EVERY or now - last >= EVICT_SECONDS
            or now < last)


def evict(now: float) -> None:
    """Drop expired files and ``*.tmp`` leftovers older than :data:`TMP_SECONDS`, then the
    oldest until the folder is within :data:`CACHE_BYTES`."""
    files = []
    for path in cache_dir().iterdir():
        if path.suffix not in _EXTENSIONS and path.suffix != ".tmp":
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if path.suffix == ".tmp":
            if now - st.st_mtime >= TMP_SECONDS:
                path.unlink(missing_ok=True)
            continue
        if now - st.st_mtime >= CACHE_SECONDS:
            path.unlink(missing_ok=True)
            continue
        files.append((st.st_mtime, st.st_size, path))
    total = sum(size for _, size, _ in files)
    for _, size, path in sorted(files, key=lambda f: f[0]):
        if total <= CACHE_BYTES:
            break
        path.unlink(missing_ok=True)
        total -= size


# --------------------------------------------------------------------------- one image
async def load(url: str, size: int, *, fetcher: Fetcher | None = None,
               resolver: Resolver | None = None, ma_url: str | None = None,
               internet: bool | None = None, now: float | None = None) -> tuple[bytes, str]:
    """``(image bytes, content type)`` for ``url`` at ``size``, from the cache or fetched;
    :class:`BadUrl`, :class:`NotAllowed` or :class:`NotFound` otherwise."""
    from fork.common import settings

    ma = music_assistant(settings.ma_url() if ma_url is None else ma_url)
    internet = settings.music_images_internet() if internet is None else internet
    fetcher = fetcher or http_fetch
    resolver = resolver or resolve
    now = time.time() if now is None else now

    target = decide(url, ma=ma, internet=internet)
    asked = target.url
    hit = await asyncio.to_thread(cached, asked, size, now)
    if hit is not None:
        return hit
    try:
        got = await asyncio.wait_for(_follow(target, ma, internet, fetcher, resolver),
                                     TOTAL_SECONDS)
    except TimeoutError:
        logger.info("voice: music image fetch failed (deadline)")
        raise NotFound("fetch failed") from None
    async with _SCALING:
        data, ctype = await asyncio.to_thread(scale, got.body, size)
    logger.info("voice: music image fetched, status 200, %d bytes, %d scaled",
                len(got.body), len(data))
    await asyncio.to_thread(store, asked, size, data, ctype, now)
    return data, ctype


async def _addresses(target: Target, resolver: Resolver) -> str | None:
    """The checked address to connect to (see :func:`check_addresses`); for MA's query
    form with an http(s) ``path``, that URL's host is checked too (MA fetches it)."""
    if target.inner is not None:
        await _addresses(target.inner, resolver)
    if target.kind != "internet":
        return None
    if _ip(target.host) is not None:
        addresses = [target.host.strip("[]")]
    else:
        try:
            addresses = await resolver(target.host)
        except (OSError, UnicodeError):
            raise NotFound("doesn't resolve") from None
    return check_addresses(target, addresses)


async def _follow(target: Target, ma: MusicAssistant | None, internet: bool,
                  fetcher: Fetcher, resolver: Resolver) -> Fetched:
    """Every hop of ``target`` (DNS, redirects); the image answer, else :class:`NotFound`.
    MA only as the first hop: a redirect is decided as an internet URL."""
    got = Fetched(0)
    for _ in range(MAX_REDIRECTS + 1):
        pin = await _addresses(target, resolver)
        try:
            got = await fetcher(target.url, pin)
        except NotFound:
            logger.info("voice: music image refused (over %d bytes or encoded)", MAX_BYTES)
            raise
        except (httpx.HTTPError, OSError) as exc:
            logger.info("voice: music image fetch failed (%s)", type(exc).__name__)
            raise NotFound("fetch failed") from None
        if got.status in REDIRECTS and got.location:
            nxt = urljoin(target.url, got.location)
            if on_music_assistant(nxt, ma):
                raise NotAllowed("not allowed")
            target = decide(nxt, ma=None, internet=internet)
            continue
        break
    else:
        logger.info("voice: music image, too many redirects")
        raise NotFound("too many redirects")
    if got.status != 200 or not got.content_type.startswith("image/") or not got.body:
        logger.info("voice: music image fetch answered %s", got.status)
        raise NotFound("not found")
    return got


# --------------------------------------------------------------------------- the route
def _error(status: int, text: str) -> JSONResponse:
    return JSONResponse({"error": text}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def setup_music_image_routes() -> APIRouter:
    router = APIRouter(tags=["fork-voice-music"])

    @router.get(ROUTE)
    async def music_image(request: Request, u: str = "", size: str = "") -> Any:
        voice_owner(request)
        try:
            data, ctype = await load(u, parse_size(size))
        except BadUrl:
            return _error(400, "bad url")
        except NotAllowed:
            logger.info("voice: music image not allowed")
            return _error(403, "not allowed")
        except NotFound:
            return _error(404, "not found")
        return Response(data, media_type=ctype, headers={"Cache-Control": CACHE_CONTROL})

    return router
