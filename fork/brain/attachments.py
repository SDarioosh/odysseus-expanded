"""Fork: text and PDF attachments of a brain turn (docs/fork/brain.md, "Attachments").

The brain takes a turn whose attachments are all files upstream reads as text: text,
markdown, csv, json, code, PDF and the office formats upstream extracts. Any image,
audio or other file, an id upstream's owner check refuses, or a file whose text can't be
read leaves the turn to upstream (:func:`read` returns None), which then handles it as it
always has (vision, OCR of scanned pages, the PDF viewer).

Upstream's pieces, reused:

- ``UploadHandler.resolve_upload(id, owner=<the chat's owner>)``: the owner check and
  reservation ``ChatHandler.preprocess_message`` runs on every attachment id;
- ``is_image_file`` / ``is_audio_file`` / ``is_document_file``: the order in which
  ``build_user_content`` sorts an attachment; and its checks that the file exists inside
  the upload directory;
- ``extract_local_document`` (``src/document_processor.py``): upstream's side-effect-free
  extraction (text files, PDFs with text only, office documents through markitdown,
  legacy ``.doc``). It creates no editor document and makes no vision-model call.

What goes into the turn is what upstream's ``build_user_content`` builds: the owner's
message, then each file's extracted text in upstream's own wrapper (``=== File: … ===``,
``[PDF content]:``, ``[Document content — …]:``), which the chat window hides under the
attachment chips. The brain caps it smaller than upstream (:data:`PER_FILE_CHARS` each,
:data:`TOTAL_CHARS` in all, a note saying how much was left out) so a big PDF can't fill
the context of a CPU model. The message is saved with upstream's ``attachment_meta``
(the chips, and reload). The file's text is outside content: the turn's run is armed,
and so is a later turn's while the message is still in the replayed history
(:func:`in_history`).

Extraction runs in a worker thread, before the owner's message is saved: a failure
hands the turn to upstream instead of failing after the message is stored.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

PER_FILE_CHARS = 8000
TOTAL_CHARS = 16000
MIN_SLICE = 500  # below this, a file is named but its text left out
SOURCE = "chat attachment"  # the run's external source, as the security context lists it
# how upstream's extractors start a text they could read; anything else (a failure or
# "no readable content" banner) leaves the turn to upstream
READABLE_PREFIXES = ("=== File:", "[PDF content]:", "[Document content", "[Legacy Word content")


@dataclass(frozen=True)
class Attached:
    """The attachments of a turn, read: upstream's ``attachment_meta`` and the text that
    follows the owner's message."""
    meta: tuple[dict, ...]
    text: str

    def content(self, message: str) -> str:
        """The user message as upstream builds it: the message, then the files' text."""
        return (str(message or "") + self.text).strip()


def attachment_ids(value: Any) -> list[str]:
    """The ids of the hook's ``attachments`` (a list, or upstream's JSON form string)."""
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return [str(value)]
    return [str(v) for v in value if str(v).strip()]


def _meta(info: dict) -> dict:
    """The chip upstream stores with the user message (``ChatHandler.preprocess_message``)."""
    return {
        "id": info["id"],
        "name": info.get("name") or info.get("original_name") or info["id"],
        "mime": info.get("mime", ""),
        "size": info.get("size", 0),
        "checksum_sha256": info.get("checksum_sha256") or info.get("hash"),
        "created_at": info.get("created_at") or info.get("uploaded_at"),
        "width": info.get("width"),
        "height": info.get("height"),
    }


def _inside_uploads(handler: Any, path: str) -> bool:
    """``build_user_content``'s check: the file is inside the upload directory."""
    from src.path_confinement import is_inside

    root = getattr(handler, "upload_dir", None)
    if root:
        return bool(is_inside(root, path))
    if hasattr(handler, "inside_base_dir"):
        return bool(handler.inside_base_dir(path))
    return False


def _close_fence(text: str) -> str:
    """A cut that ends inside a code block gets the block closed again."""
    fences = sum(1 for line in text.splitlines() if line.lstrip().startswith("```"))
    return text + "\n```" if fences % 2 else text


def fit(texts: list[tuple[str, str]], *, per_file: int = PER_FILE_CHARS,
        total: int = TOTAL_CHARS) -> str:
    """The files' texts (``(name, text)``) joined, each cut to ``per_file`` characters
    and all of them to ``total``, with a note where text was left out."""
    from fork.brain.tools import trim

    out, left = [], total
    for name, text in texts:
        text = text or ""
        limit = min(per_file, left)
        if len(text) > limit and limit < MIN_SLICE:
            out.append(f"\n\n[Attachment {name}: its text was left out, the attachments' "
                       f"{total:,}-character limit is used up.]")
            left = 0
            continue
        if len(text) > limit:
            text = _close_fence(trim(text, limit))
        out.append(text)
        left = max(0, left - len(text))
    return "".join(out)


def _read_sync(handler: Any, owner: Any, ids: list[str]) -> Attached | None:
    infos = []
    for att_id in ids:
        info = handler.resolve_upload(att_id, owner=owner)  # upstream's owner check
        if not isinstance(info, dict) or not info.get("id"):
            return None
        path = info.get("path")
        if not path or not os.path.exists(path) or not _inside_uploads(handler, path):
            return None
        name = info.get("name") or info.get("original_name") or path
        mime = info.get("mime") or ""
        if handler.is_image_file(name, mime) or handler.is_audio_file(name, mime) \
                or not handler.is_document_file(name, mime):
            return None
        infos.append((info, path, name))

    from src.document_processor import extract_local_document

    texts = []
    for info, path, name in infos:
        text = extract_local_document(path, display_name=name, owner=owner,
                                      analyze_embedded_images=False)
        if not str(text or "").strip().startswith(READABLE_PREFIXES):
            return None  # unreadable here: upstream's turn (OCR, its own banner)
        texts.append((os.path.basename(name), text))
    return Attached(meta=tuple(_meta(info) for info, _p, _n in infos), text=fit(texts))


async def read(sess: Any, chat_handler: Any, attachments: Any) -> Attached | None:
    """The turn's attachments, owner-checked and read in a worker thread, or None when
    the turn must stay upstream's (no upload handler, an id refused, an image or other
    file the brain doesn't read, a file it can't read, any error)."""
    ids = attachment_ids(attachments)
    handler = getattr(chat_handler, "upload_handler", None)
    if not ids or handler is None or not hasattr(handler, "resolve_upload"):
        return None
    try:
        return await asyncio.to_thread(_read_sync, handler, getattr(sess, "owner", None), ids)
    except Exception:  # noqa: BLE001 -- upstream reads it instead
        logger.warning("brain: attachments unreadable, the turn stays upstream's", exc_info=True)
        return None


def in_history(rows: list, summary: tuple[str, str] | None = None) -> bool:
    """True when a user message with attachments is in the history this turn replays
    (after the summary's cut, as ``prompt.history_messages`` replays it)."""
    from fork.brain import prompt as brain_prompt

    rows = list(rows or ())
    if summary:
        cut = next((i for i, m in enumerate(rows) if brain_prompt.message_id(m) == summary[0]),
                   None)
        if cut is not None:
            rows = rows[cut + 1:]
    return any(brain_prompt._role(m) == "user" and brain_prompt._metadata(m).get("attachments")
               for m in rows)
