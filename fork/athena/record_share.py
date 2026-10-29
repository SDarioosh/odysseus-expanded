"""Fork: share a record (an Athena conversation) as a file.

The Records reader's **Share** button calls
``GET /api/modules/sound/conversations/{id}/export?format=md|html&include=...``
(``routes/sound_module_routes.py``), which gets the file from the module's relay route
``GET /conversations/{id}/export`` (Athena MCP 0.19.0) with the ``convo`` MCP server's
URL and token, like the call recorder (``src/call_recorder.py``). Odysseus never reads the
module's storage, and the file is made by the module: what goes in it (no signed links, no
ids) is the module's contract.

``include`` is a comma list of ``summary`` (with key points), ``action_items``, ``facts``
and ``transcript``. The hub checks both values before calling the module, and checks the
answer is Markdown or HTML before handing it on.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

import httpx

from fork.athena import call_recorder as rec

logger = logging.getLogger(__name__)

FORMATS = {"md": "text/markdown", "html": "text/html"}
SECTIONS = ("summary", "action_items", "facts", "transcript")
DEFAULT_INCLUDE = ("summary", "action_items", "transcript")
MAX_BYTES = 20 * 1024 * 1024  # a day-long transcript is a few MB
OLD_MODULE = ("This version of Athena can't share records yet: update Athena "
              "(scripts/fork_deploy.sh deploy).")
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')


@dataclass(frozen=True)
class SharedFile:
    filename: str
    content_type: str
    data: bytes


def check_format(fmt: str | None) -> str:
    value = (fmt or "md").strip().lower()
    if value not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    return value


def check_include(raw: str | None) -> tuple[str, ...]:
    """``"summary,transcript"`` -> the sections in page order; empty is the default."""
    if raw is None or not raw.strip():
        return DEFAULT_INCLUDE
    asked = {p.strip().lower() for p in raw.split(",") if p.strip()}
    unknown = asked - set(SECTIONS)
    if unknown:
        raise ValueError(f"include takes {', '.join(SECTIONS)}")
    if not asked:
        raise ValueError("pick at least one part to share")
    return tuple(s for s in SECTIONS if s in asked)


def filename_from(disposition: str, fmt: str) -> str:
    """The module's file name (``filename*`` first), made safe again; a plain name
    otherwise. It always ends in the asked format's extension."""
    name = ""
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", disposition or "", re.IGNORECASE)
    if m:
        name = unquote(m.group(1).strip())
    else:
        m = re.search(r'filename\s*=\s*"([^"]*)"', disposition or "", re.IGNORECASE)
        name = m.group(1) if m else ""
    name = " ".join(_UNSAFE_NAME.sub(" ", name).split()).strip(" .")[:120]
    stem = (name[: -len(fmt) - 1] if name.lower().endswith("." + fmt) else name).strip(" .")
    return f"{stem or 'Athena record'}.{fmt}"


def content_disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


async def fetch(link: rec.Link, conversation_id: str, fmt: str, include: tuple[str, ...],
                transport: httpx.AsyncBaseTransport | None = None) -> SharedFile:
    """The file from the module. Raises LookupError (no such conversation) or
    :class:`call_recorder.RecorderError` with a status to answer and words to show."""
    params = {"format": fmt, "include": ",".join(include)}
    path = f"/conversations/{rec.path_part(conversation_id)}/export"
    try:
        async with rec._client(transport) as client:
            res = await client.get(f"{link.base}{path}", params=params,
                                   headers=rec.auth_headers(link))
    except httpx.HTTPError as exc:
        # Log only the base and the error's type (never the request).
        logger.warning("Athena unreachable at %s: %s", link.base, type(exc).__name__)
        raise rec.RecorderError(502, "Athena is not reachable") from None
    if res.status_code in (404, 405):
        detail = rec._detail(res)
        if detail and detail != "Not Found":
            raise LookupError(detail)  # "conversation not found"
        raise rec.RecorderError(502, OLD_MODULE)  # the route itself is missing
    if res.status_code != 200:
        raise rec._module_error(res)
    ctype = res.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != FORMATS[fmt]:
        raise rec.RecorderError(502, "Athena sent something that is not the file asked for")
    if len(res.content) > MAX_BYTES:
        raise rec.RecorderError(502, "The record is too large to share from here")
    return SharedFile(filename=filename_from(res.headers.get("content-disposition", ""), fmt),
                      content_type=f"{FORMATS[fmt]}; charset=utf-8", data=res.content)
