"""Fork: handwritten notes, stage H1 (pen capture). Plan: docs/fork/handwritten-notes.md.

A handwritten note is an ordinary upstream note (``label = "handwritten"``, ``image_url``
= a PNG of page 1 rendered here). Its strokes live in a sidecar file
``<DATA_DIR>/ink/<owner>/<note_id>.json``; nothing changes in ``core/database.py``.

Sidecar (``version`` 1)::

    {"version": 1, "note_id": str, "paper": "lined" | "dotted" | "blank",
     "ruled": bool,               # paper == "lined", for Write views from before ``paper``
     "edited_at": int,            # ms, when the client last changed the strokes
     "saved_at": str,             # ISO, when the hub stored them
     "pages": [{"id", "width", "height",
                "strokes": [{"id", "pen": {"color", "width", "highlighter"},
                             "at": int, "points": [[x, y, t], ...]}],
                "transcript": {"text", "model", "status", "strokes_hash", "at"},
                "photo": {"file", "hash", "width", "height", "at"}}],  # H6: photo pages
     "athena": {"sent_hash", "sent_at", "conversation_id"},
     "render": {"hash", "image_url", "at"}}   # page 1's image, last drawn ``at``

Coordinates are page units (a page is ``PAGE_WIDTH`` wide); ``t`` is ms since the
stroke's ``at``. ``transcript`` and ``athena`` belong to later stages (H2, H4): a save from
the Write view never sets them, it only keeps what the sidecar already had.

A **photo page** (H6, photo import of a paper page) has ``photo``: the photo is a JPEG in
``<ink folder>/photos/<note_id>/<page_id>.jpg`` (:func:`photo_path`), made by
:func:`prepare_photo` (orientation applied, metadata dropped, at most ``PHOTO_MAX_SIDE``
px) and added by :func:`add_photos`. Like ``transcript``, a save from the Write view
never sets ``photo``: it is kept by page id. A photo page takes no pen ink (only the
highlighter, which the recognizer never sees); a page dropped by a save, and the whole
folder with the sidecar, lose their photo files. The page stays ``PAGE_WIDTH`` x
``PAGE_HEIGHT``; the photo is drawn fitted into it, centred, at the top.

Last edit wins: a save whose ``edited_at`` is older than the stored one is refused
(:class:`Stale`) and the client takes the stored version.

Every call into upstream notes code is in the **adapter** section at the end of this
file; nothing else in the ink code imports upstream notes code.
``tests/test_ink_upstream_contract.py`` runs each adapter function against the real
upstream code.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

LABEL = "handwritten"
VERSION = 1
PAGE_WIDTH = 1000
PAGE_HEIGHT = 1414  # A4 proportions
# The background the Write view draws under the ink (never part of the ink, never rendered
# here): lines for writing, a dot grid for drafting designs, or plain paper.
PAPERS = ("lined", "dotted", "blank")
MAX_PAGES = 60
MAX_STROKES_PER_PAGE = 6000
MAX_POINTS_PER_STROKE = 6000
MAX_POINTS_PER_NOTE = 600_000
MAX_TITLE = 200
MAX_TOMBSTONES = 2000
RENDER_SCALE = 1.0  # page 1 PNG is PAGE_WIDTH px wide
# While the owner writes, the Write view saves every few seconds. Page 1's image is
# redrawn at most this often, and on the view's last save (``final``: the page was left
# or the app hidden), so an hour of writing doesn't leave hundreds of unused uploads.
RENDER_MIN_SECONDS = 60

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,63}$")  # as in note links (forkEntityHash.js)
_SHORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._@-]{0,63}$")

TRANSCRIPT_STATUSES = ("none", "pending", "machine", "reviewed", "failed")

# H6: photos of paper pages. A photo is checked and re-encoded before it is stored.
PHOTO_TYPES = ("JPEG", "PNG", "WEBP")   # what Pillow found in the file, not its name
MAX_PHOTO_BYTES = 15 * 1024 * 1024      # per photo as sent
MAX_PHOTO_PIXELS = 40_000_000           # decoded size (decompression bombs)
MAX_PHOTO_ASPECT = 6                    # long side / short side
MIN_PHOTO_SIDE = 64
PHOTO_MAX_SIDE = 2400                   # px, the long side of a stored photo
PHOTO_QUALITY = 88
MAX_PHOTOS_PER_UPLOAD = 10


class InkError(ValueError):
    """The save was refused; ``status`` is the HTTP status to answer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Stale(InkError):
    """A newer edit is already stored (last edit wins)."""

    def __init__(self, current: dict[str, Any]):
        super().__init__("A newer version of this note is already saved", 409)
        self.current = current


class Gone(InkError):
    """The note was deleted (here or in the Notes window)."""

    def __init__(self):
        super().__init__("This note was deleted", 410)


# ------------------------------------------------------------------------- paths
def valid_note_id(note_id: str) -> bool:
    return bool(isinstance(note_id, str) and _ID_RE.match(note_id))


def _owner_dir_name(owner: str | None) -> str:
    """A directory name for ``owner``. ``None`` (single-user mode) is ``_single``; a
    name that isn't a plain file name gets a hash, so two owners never share a dir."""
    if not owner:
        return "_single"
    if _OWNER_RE.match(owner) and owner not in {".", ".."}:
        return owner
    return "u-" + hashlib.sha256(owner.encode("utf-8")).hexdigest()[:24]


def ink_dir(owner: str | None) -> Path:
    from src import constants  # read at call time: tests point DATA_DIR elsewhere

    return Path(constants.DATA_DIR) / "ink" / _owner_dir_name(owner)


def sidecar_path(owner: str | None, note_id: str) -> Path:
    if not valid_note_id(note_id):
        raise InkError("Invalid note id")
    return ink_dir(owner) / f"{note_id}.json"


def photo_dir(owner: str | None, note_id: str) -> Path:
    """Where a note's photos are: ``<ink folder>/photos/<note_id>``."""
    if not valid_note_id(note_id):
        raise InkError("Invalid note id")
    return ink_dir(owner) / "photos" / note_id


def photo_path(owner: str | None, note_id: str, page_id: str) -> Path:
    if not isinstance(page_id, str) or not _SHORT_ID_RE.match(page_id):
        raise InkError("Invalid page id")
    return photo_dir(owner, note_id) / f"{page_id}.jpg"


def is_photo(page: dict[str, Any]) -> bool:
    return isinstance(page, dict) and isinstance(page.get("photo"), dict)


def _tombstone_path(owner: str | None) -> Path:
    return ink_dir(owner) / "deleted.json"


# One lock per owner folder: saves, deletes, sweeps and the tombstone file of one owner
# never interleave (one person writes on one or two devices, so this costs nothing).
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(owner: str | None) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(_owner_dir_name(owner), threading.RLock())


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def load_sidecar(owner: str | None, note_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(sidecar_path(owner, note_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sidecar_ids(owner: str | None) -> list[str]:
    folder = ink_dir(owner)
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.json") if valid_note_id(p.stem))


# -------------------------------------------------------------------- tombstones
def _tombstones(owner: str | None) -> list[str]:
    try:
        data = json.loads(_tombstone_path(owner).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [i for i in data if isinstance(i, str)] if isinstance(data, list) else []


def _add_tombstones(owner: str | None, ids: Iterable[str]) -> None:
    ids = [i for i in ids if valid_note_id(i)]
    if not ids:
        return
    with _lock(owner):  # read-modify-write of the owner's one file
        current = [i for i in _tombstones(owner) if i not in ids]
        _write_json(_tombstone_path(owner), (current + ids)[-MAX_TOMBSTONES:])


def is_deleted(owner: str | None, note_id: str) -> bool:
    return note_id in set(_tombstones(owner))


# -------------------------------------------------------------------- validation
def _num(value: Any, lo: float, hi: float, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InkError(f"{what} must be a number")
    value = float(value)
    if value != value or value < lo or value > hi:  # NaN or out of range
        raise InkError(f"{what} is out of range")
    return value


def _clean_pen(pen: Any) -> dict[str, Any]:
    if not isinstance(pen, dict):
        raise InkError("pen must be an object")
    color = pen.get("color")
    if not isinstance(color, str) or not _COLOR_RE.match(color):
        raise InkError("pen color must be #rrggbb")
    width = _num(pen.get("width"), 0.5, 60, "pen width")
    return {"color": color.lower(), "width": round(width, 2),
            "highlighter": bool(pen.get("highlighter", False))}


def _clean_stroke(stroke: Any, width: float, height: float) -> dict[str, Any]:
    if not isinstance(stroke, dict):
        raise InkError("stroke must be an object")
    sid = stroke.get("id")
    if not isinstance(sid, str) or not _SHORT_ID_RE.match(sid):
        raise InkError("stroke id is invalid")
    points = stroke.get("points")
    if not isinstance(points, list) or not points:
        raise InkError("stroke has no points")
    if len(points) > MAX_POINTS_PER_STROKE:
        raise InkError("stroke has too many points")
    margin = 50.0
    clean = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise InkError("a point is [x, y, t]")
        x = _num(p[0], -margin, width + margin, "x")
        y = _num(p[1], -margin, height + margin, "y")
        t = _num(p[2] if len(p) > 2 else 0, 0, 86_400_000, "t")
        clean.append([round(x, 1), round(y, 1), int(t)])
    at = stroke.get("at", 0)
    at = int(_num(at, 0, 1e14, "at")) if at is not None else 0
    return {"id": sid, "pen": _clean_pen(stroke.get("pen")), "at": at, "points": clean}


def _empty_transcript() -> dict[str, Any]:
    return {"text": "", "model": None, "status": "none", "strokes_hash": None, "at": None}


def _empty_athena() -> dict[str, Any]:
    return {"sent_hash": None, "sent_at": None, "conversation_id": None}


def clean_document(body: Any) -> dict[str, Any]:
    """Validate a save from the Write view and return the stroke part of a sidecar
    (``paper``, ``ruled``, ``edited_at``, ``pages`` with ``id``, ``width``, ``height``, ``strokes``).
    Raises :class:`InkError` on anything malformed."""
    if not isinstance(body, dict):
        raise InkError("body must be an object")
    pages = body.get("pages")
    if not isinstance(pages, list) or not pages:
        raise InkError("a note has at least one page")
    if len(pages) > MAX_PAGES:
        raise InkError(f"at most {MAX_PAGES} pages")
    edited_at = int(_num(body.get("edited_at", 0), 0, 1e14, "edited_at"))
    seen_pages: set[str] = set()
    total = 0
    clean_pages = []
    for page in pages:
        if not isinstance(page, dict):
            raise InkError("page must be an object")
        pid = page.get("id")
        if not isinstance(pid, str) or not _SHORT_ID_RE.match(pid) or pid in seen_pages:
            raise InkError("page id is invalid or repeated")
        seen_pages.add(pid)
        # Every page is the Write view's page size, so a request can't ask the renderer
        # for a huge bitmap.
        width = _num(page.get("width", PAGE_WIDTH), PAGE_WIDTH, PAGE_WIDTH, "page width")
        height = _num(page.get("height", PAGE_HEIGHT), PAGE_HEIGHT, PAGE_HEIGHT, "page height")
        strokes = page.get("strokes") or []
        if not isinstance(strokes, list):
            raise InkError("strokes must be a list")
        if len(strokes) > MAX_STROKES_PER_PAGE:
            raise InkError("too many strokes on a page")
        clean_strokes = [_clean_stroke(s, width, height) for s in strokes]
        total += sum(len(s["points"]) for s in clean_strokes)
        if total > MAX_POINTS_PER_NOTE:
            raise InkError("the note is too large")
        clean_pages.append({"id": pid, "width": int(width), "height": int(height),
                            "strokes": clean_strokes})
    paper = paper_of(body)
    return {"paper": paper, "ruled": paper == "lined", "edited_at": edited_at,
            "pages": clean_pages}


def paper_of(doc: dict[str, Any]) -> str:
    """The note's paper: ``paper`` when it is one of :data:`PAPERS`, else from the older
    ``ruled`` flag (true or missing: lined; false: blank)."""
    paper = doc.get("paper")
    if paper in PAPERS:
        return paper
    return "lined" if doc.get("ruled", True) else "blank"


def strokes_hash(page: dict[str, Any]) -> str:
    """Stable hash of a page's strokes (what H2 compares to know a page changed), and of
    its photo on a photo page (H6; a page without one hashes as before)."""
    parts = [page.get("width"), page.get("height"), page.get("strokes") or []]
    if is_photo(page):
        parts.append({"photo": page["photo"].get("hash")})
    raw = json.dumps(parts, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------ render
def render_page_png(page: dict[str, Any], *, scale: float = RENDER_SCALE,
                    highlighter: bool = True, photo: Path | None = None,
                    fmt: str = "PNG") -> bytes:
    """A PNG of one page: white paper, highlighter under the ink, no ruling. The same
    strokes always give the same bytes. ``photo`` (a photo page's file, H6) is drawn
    first, fitted into the page; ``fmt`` ``"JPEG"`` suits a page with a photo."""
    from PIL import Image, ImageDraw

    width = max(1, int(round(page.get("width", PAGE_WIDTH) * scale)))
    height = max(1, int(round(page.get("height", PAGE_HEIGHT) * scale)))
    base = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    if photo is not None:
        _paste_photo(base, photo)
    strokes = page.get("strokes") or []
    marks = [s for s in strokes if s["pen"].get("highlighter")] if highlighter else []
    ink = [s for s in strokes if not s["pen"].get("highlighter")]
    if marks:
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        _draw_strokes(ImageDraw.Draw(layer), marks, scale, opaque=True)
        # One alpha for the whole layer, so overlapping marks don't darken.
        alpha = layer.getchannel("A").point(lambda a: 90 if a else 0)
        layer.putalpha(alpha)
        base = Image.alpha_composite(base, layer)
    _draw_strokes(ImageDraw.Draw(base), ink, scale, opaque=True)
    out = io.BytesIO()
    if fmt == "JPEG":
        base.convert("RGB").save(out, format="JPEG", quality=PHOTO_QUALITY)
    else:
        base.convert("RGB").save(out, format="PNG", optimize=False)
    return out.getvalue()


def photo_box(photo_w: float, photo_h: float, page_w: float, page_h: float
              ) -> tuple[float, float, float, float]:
    """``(x, y, w, h)`` of a photo fitted into a page: as large as fits, centred
    across, at the top (the Write view draws it the same way, static/js/forkInk.js)."""
    k = min(page_w / max(1.0, photo_w), page_h / max(1.0, photo_h))
    w, h = photo_w * k, photo_h * k
    return (page_w - w) / 2, 0.0, w, h


def _paste_photo(base, path: Path) -> None:
    from PIL import Image

    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
    except (OSError, ValueError):
        return  # the file is gone: the page is drawn without it
    x, y, w, h = photo_box(img.width, img.height, base.width, base.height)
    size = (max(1, round(w)), max(1, round(h)))
    base.paste(img.resize(size, Image.LANCZOS), (round(x), round(y)))


def smooth_points(points: list[list[float]], steps: int = 6) -> list[tuple[float, float]]:
    """The same curve the Write view draws (static/js/forkInk.js ``strokePath``): from
    the first point, a quadratic curve through each point to the midpoint of the next
    segment, then a line to the last point. Returned as a polyline."""
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    start = pts[0]
    for i in range(1, len(pts) - 1):
        (cx, cy), (nx, ny) = pts[i], pts[i + 1]
        end = ((cx + nx) / 2, (cy + ny) / 2)
        for k in range(1, steps + 1):
            t = k / steps
            a, b, c = (1 - t) ** 2, 2 * (1 - t) * t, t * t
            out.append((a * start[0] + b * cx + c * end[0], a * start[1] + b * cy + c * end[1]))
        start = end
    out.append(pts[-1])
    return out


def _draw_strokes(draw, strokes: list[dict[str, Any]], scale: float, *, opaque: bool) -> None:
    for stroke in strokes:
        pen = stroke["pen"]
        rgb = tuple(int(pen["color"][i:i + 2], 16) for i in (1, 3, 5))
        fill = (*rgb, 255)
        w = max(1, int(round(pen["width"] * scale)))
        pts = [(x * scale, y * scale) for x, y in smooth_points(stroke["points"])]
        if len(pts) > 1:
            draw.line(pts, fill=fill, width=w, joint="curve")
        # Round caps, w pixels across (Pillow's ellipse box is inclusive).
        lo, hi = (w - 1) / 2, w / 2 - 0.5
        for x, y in (pts[0], pts[-1]):
            draw.ellipse((x - lo, y - lo, x + hi, y + hi), fill=fill)


# --------------------------------------------------------------------- the notes
def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_title(now: float | None = None) -> str:
    stamp = datetime.fromtimestamp(now if now is not None else time.time())
    return "Handwritten " + stamp.strftime("%Y-%m-%d %H:%M")


def save(owner: str | None, note_id: str, body: Any, *, title: str | None = None,
         final: bool = False, now: float | None = None) -> dict[str, Any]:
    """Store a save from the Write view: create the note on its first save (the id is
    made by the client, so a note can start offline), write the sidecar, and refresh
    the note's page-1 image when page 1 changed (at most every ``RENDER_MIN_SECONDS``,
    unless ``final``). Returns the stored sidecar plus ``title``.

    Raises :class:`Gone` for a deleted note, :class:`Stale` when a newer edit is
    stored, and :class:`InkError` for a bad body or another owner's id (404)."""
    if not valid_note_id(note_id):
        raise InkError("Invalid note id")
    doc = clean_document(body)
    if title is not None:
        if not isinstance(title, str):
            raise InkError("title must be a string")
        title = title.strip()[:MAX_TITLE]  # an empty title stays empty ("Untitled")
    if note_id_taken(note_id) and not existing_note_ids(owner, [note_id]):
        # Another owner's note, or one of this owner's notes that isn't handwritten
        # (a checklist, a typed note, an Athena note): never touched from here.
        raise InkError("Note not found", 404)
    store = _store_owner(owner, note_id)
    with _lock(store):
        current = load_sidecar(store, note_id)
        exists, created = _ensure_note(owner, note_id, store, current, title)
        if exists and current is not None and doc["edited_at"] < int(current.get("edited_at") or 0):
            raise Stale(public(current, note_title(owner, note_id)))
        try:
            stored = _merge(current, doc, note_id)
            _render_if_changed(owner, note_id, stored, final=final,
                               now=time.time() if now is None else now, store=store)
            _write_json(sidecar_path(store, note_id), stored)
        except BaseException:
            if created:  # no note without its strokes
                delete_note(owner, note_id)
                _remove_sidecar(store, note_id)
            raise
        _drop_photos(store, note_id, current, stored)  # H6: photo pages removed
        if exists and title is not None:
            update_note(owner, note_id, title=title)
        if current is not None and _page_ids(current) != _page_ids(stored):
            # Pages removed or moved: the note's reviewed text follows them.
            from src import ink_transcribe  # imports this module

            update_note(owner, note_id, content=ink_transcribe.note_content(stored))
        return public(stored, note_title(owner, note_id))


def _ensure_note(owner: str | None, note_id: str, store: str | None,
                 current: dict[str, Any] | None, title: str | None) -> tuple[bool, bool]:
    """``(exists, created)``: create the owner's note on its first write (the id comes
    from the client). Raises :class:`Gone` for a deleted note (its sidecar is dropped
    and its id remembered) and 404 for an id another note took. Call under the lock."""
    if existing_note_ids(owner, [note_id]):
        return True, False
    if note_id_taken(note_id):
        raise InkError("Note not found", 404)
    if current is not None or is_deleted(store, note_id):
        # The note was deleted (e.g. in the Notes window): don't bring it back.
        _drop_sidecar(store, note_id, current)
        _add_tombstones(store, [note_id])
        raise Gone()
    if not create_note(owner, default_title() if title is None else title, LABEL,
                       None, note_id=note_id):
        raise InkError("Note not found", 404)  # taken meanwhile
    return False, True


def _page_ids(stored: dict[str, Any]) -> list[Any]:
    return [p.get("id") for p in stored.get("pages") or [] if isinstance(p, dict)]


def _store_owner(owner: str | None, note_id: str) -> str | None:
    """Whose ink folder holds this note's strokes. In single-user mode (``owner`` None)
    every note is visible, so an existing note's strokes are in its own owner's folder;
    a new one goes to ``_single``."""
    if owner is not None:
        return owner
    return note_owner(note_id)


def _merge(current: dict[str, Any] | None, doc: dict[str, Any], note_id: str
           ) -> dict[str, Any]:
    """The new strokes, keeping each page's ``transcript`` (by page id) and the note's
    ``athena`` and ``render`` from the stored sidecar."""
    current = current or {}
    old_pages = {p.get("id"): p for p in current.get("pages") or [] if isinstance(p, dict)}
    pages = []
    for page in doc["pages"]:
        old = old_pages.get(page["id"]) or {}
        transcript = old.get("transcript") if isinstance(old.get("transcript"), dict) else None
        new = {**page, "transcript": transcript or _empty_transcript()}
        if is_photo(old):  # H6: only add_photos makes a photo page
            if any(not s["pen"].get("highlighter") for s in page["strokes"]):
                raise InkError("A photo page takes only the highlighter, not pen ink")
            new["photo"] = old["photo"]
        pages.append(new)
    athena = current.get("athena") if isinstance(current.get("athena"), dict) else None
    render = current.get("render") if isinstance(current.get("render"), dict) else None
    return {"version": VERSION, "note_id": note_id, "paper": doc["paper"],
            "ruled": doc["ruled"],
            "edited_at": doc["edited_at"], "saved_at": _now_iso(), "pages": pages,
            "athena": athena or _empty_athena(),
            "render": render or {"hash": None, "image_url": None, "at": None}}


def _render_if_changed(owner: str | None, note_id: str, stored: dict[str, Any], *,
                       final: bool, now: float, store: str | None = None) -> None:
    first = stored["pages"][0]
    digest = strokes_hash(first)
    render = stored["render"]
    if render.get("hash") == digest:
        return
    recent = now - float(render.get("at") or 0) < RENDER_MIN_SECONDS
    if render.get("image_url") and recent and not final:
        return  # still writing: redrawn on a later save
    if is_photo(first):  # H6: the photo, with any highlighting, as a JPEG
        path = photo_path(_store_owner(owner, note_id) if store is None else store,
                          note_id, first["id"])
        url = save_page_image(owner, render_page_png(first, photo=path, fmt="JPEG"),
                              filename="handwritten.jpg")
        if url is None:
            return
    elif not first["strokes"]:
        url = None  # nothing to show yet
    else:
        url = save_page_image(owner, render_page_png(first))
        if url is None:
            return  # upload refused (rate limit, no handler): try again on the next save
    update_note(owner, note_id, image_url=url or "")
    stored["render"] = {"hash": digest, "image_url": url, "at": now}


def public(stored: dict[str, Any], title: str | None) -> dict[str, Any]:
    """What the Write view gets: the sidecar without the render bookkeeping."""
    out = {k: v for k, v in stored.items() if k != "render"}
    out["paper"] = paper_of(stored)  # sidecars from before ``paper`` have only ``ruled``
    out["title"] = title or ""
    out["image_url"] = (stored.get("render") or {}).get("image_url")
    out["link"] = note_link(stored.get("note_id") or "")
    return out


def load(owner: str | None, note_id: str) -> dict[str, Any] | None:
    """The note's sidecar, or ``None`` when it isn't this owner's handwritten note."""
    if not valid_note_id(note_id) or not existing_note_ids(owner, [note_id]):
        return None
    store = _store_owner(owner, note_id)
    with _lock(store):
        stored = load_sidecar(store, note_id)
    if stored is None:
        return None
    return public(stored, note_title(owner, note_id))


def _remove_sidecar(owner: str | None, note_id: str) -> None:
    try:
        sidecar_path(owner, note_id).unlink()
    except FileNotFoundError:
        pass
    _remove_photos(owner, note_id)  # H6: a note's photos go with its sidecar


def _remove_photos(owner: str | None, note_id: str) -> None:
    folder = photo_dir(owner, note_id)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def _drop_photos(store: str | None, note_id: str, before: dict[str, Any] | None,
                 after: dict[str, Any]) -> None:
    """Delete the photo files of photo pages a save removed."""
    kept = {p.get("id") for p in after.get("pages") or [] if is_photo(p)}
    for page in (before or {}).get("pages") or []:
        if is_photo(page) and page.get("id") not in kept:
            try:
                photo_path(store, note_id, page["id"]).unlink()
            except (FileNotFoundError, InkError):
                pass


def sent_to_athena(stored: dict[str, Any] | None) -> bool:
    """Whether Athena may hold this note (sent, a send was started, or a send got no
    answer): its sidecar is kept after the note is deleted until Athena confirms it
    removed the note (``src/ink_athena.py``)."""
    athena = (stored or {}).get("athena")
    return isinstance(athena, dict) and bool(
        athena.get("sent_hash") or athena.get("sending") or athena.get("maybe_sent"))


def _drop_sidecar(owner: str | None, note_id: str, stored: dict[str, Any] | None) -> None:
    """The note is gone: remove its strokes, unless Athena may still hold the note. Then
    the sidecar stays (the note never comes back: its id is a tombstone) until the
    Athena sweep has asked Athena to remove it."""
    if not sent_to_athena(stored):
        _remove_sidecar(owner, note_id)


def delete(owner: str | None, note_id: str) -> bool:
    """Delete the note and its strokes. ``False`` when it isn't this owner's note."""
    if not valid_note_id(note_id):
        return False
    if note_id_taken(note_id) and not existing_note_ids(owner, [note_id]):
        return False  # not this owner's handwritten note: never deleted from here
    store = _store_owner(owner, note_id)
    with _lock(store):
        stored = load_sidecar(store, note_id)
        removed = delete_note(owner, note_id)
        if not removed and stored is None:
            return False
        _drop_sidecar(store, note_id, stored)
        _add_tombstones(store, [note_id])
    return True


# ------------------------------------------------------------------ photos (H6)
def prepare_photo(data: bytes) -> dict[str, Any]:
    """Check a photo of a paper page and make the JPEG that is stored: ``{"jpeg",
    "width", "height", "hash"}``. Only JPEG, PNG and WebP (by content, not by name),
    at most ``MAX_PHOTO_BYTES`` and ``MAX_PHOTO_PIXELS`` (checked from the header,
    before decoding), the camera's orientation applied, at most ``PHOTO_MAX_SIDE`` px,
    transparency on white, and re-encoded so no metadata (EXIF, GPS, ICC, comments) is
    kept. Raises :class:`InkError` (400, or 413 for size)."""
    from PIL import Image, ImageOps

    if not isinstance(data, (bytes, bytearray)) or not data:
        raise InkError("The photo is empty")
    if len(data) > MAX_PHOTO_BYTES:
        raise InkError(f"A photo is at most {MAX_PHOTO_BYTES // (1024 * 1024)} MB", 413)
    too_big = InkError("The photo has too many pixels", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(bytes(data)))
            if img.format not in PHOTO_TYPES:
                raise InkError("A photo must be a JPEG, PNG or WebP image")
            w, h = img.size  # from the header: nothing decoded yet
            if w * h > MAX_PHOTO_PIXELS:
                raise too_big
            if min(w, h) < MIN_PHOTO_SIDE or max(w, h) > MAX_PHOTO_ASPECT * min(w, h):
                raise InkError("The photo is too small or too narrow to read")
            if img.format == "JPEG":
                img.draft("RGB", (PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))  # decode smaller
            img = ImageOps.exif_transpose(img)
            img.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE), Image.LANCZOS)
            if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
            else:
                flat = Image.new("RGB", img.size, (255, 255, 255))
                flat.paste(img.convert("RGB"))
    except InkError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise too_big from None
    except Exception:  # noqa: BLE001 -- anything Pillow can't read is refused
        raise InkError("The file is not an image that can be read") from None
    out = io.BytesIO()
    flat.save(out, format="JPEG", quality=PHOTO_QUALITY, optimize=True)  # a new image: no metadata
    jpeg = out.getvalue()
    return {"jpeg": jpeg, "width": flat.width, "height": flat.height,
            "hash": hashlib.sha256(jpeg).hexdigest()}


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def add_photos(owner: str | None, note_id: str, photos: list[dict[str, Any]], *,
               edited_at: Any = 0, after: str | None = None, title: str | None = None,
               paper: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Add photos (from :func:`prepare_photo`) as new pages of the owner's note, one
    page each, after the page ``after`` (else at the end). The first upload creates the
    note (client-made id, like the first save; ``title`` and ``paper`` are used then,
    ``paper`` blank by default). The note's ``edited_at`` becomes at least ``edited_at``
    and newer than the stored one, so the device that sent the photos has the last
    edit. Returns the stored note (:func:`public`). Raises :class:`Gone` (410), 404 for
    another owner's note, 413 past ``MAX_PAGES``."""
    if not valid_note_id(note_id):
        raise InkError("Invalid note id")
    if not photos:
        raise InkError("No photo was sent")
    if len(photos) > MAX_PHOTOS_PER_UPLOAD:
        raise InkError(f"At most {MAX_PHOTOS_PER_UPLOAD} photos at a time", 413)
    edited_at = int(_num(edited_at or 0, 0, 1e14, "edited_at"))
    if title is not None:
        if not isinstance(title, str):
            raise InkError("title must be a string")
        title = title.strip()[:MAX_TITLE]
    if note_id_taken(note_id) and not existing_note_ids(owner, [note_id]):
        raise InkError("Note not found", 404)
    store = _store_owner(owner, note_id)
    with _lock(store):
        current = load_sidecar(store, note_id)
        _, created = _ensure_note(owner, note_id, store, current, title)
        base = current if current is not None else {
            "paper": paper if paper in PAPERS else "blank", "edited_at": 0, "pages": []}
        pages = [p for p in base.get("pages") or [] if isinstance(p, dict)]
        if len(pages) + len(photos) > MAX_PAGES:
            raise InkError(f"A note has at most {MAX_PAGES} pages: this note is full", 413)
        ids = [p.get("id") for p in pages]
        at = ids.index(after) + 1 if after in ids else len(pages)
        written: list[Path] = []
        try:
            new_pages = []
            for photo in photos:
                pid = "ph" + uuid.uuid4().hex[:12]
                path = photo_path(store, note_id, pid)
                _write_bytes(path, photo["jpeg"])
                written.append(path)
                new_pages.append({
                    "id": pid, "width": PAGE_WIDTH, "height": PAGE_HEIGHT, "strokes": [],
                    "transcript": _empty_transcript(),
                    "photo": {"file": f"photos/{note_id}/{pid}.jpg", "hash": photo["hash"],
                              "width": int(photo["width"]), "height": int(photo["height"]),
                              "at": _now_iso()}})
            pages[at:at] = new_pages
            paper_now = paper_of(base)
            athena = base.get("athena") if isinstance(base.get("athena"), dict) else None
            render = base.get("render") if isinstance(base.get("render"), dict) else None
            stored = {"version": VERSION, "note_id": note_id, "paper": paper_now,
                      "ruled": paper_now == "lined",
                      "edited_at": max(edited_at, int(base.get("edited_at") or 0) + 1),
                      "saved_at": _now_iso(), "pages": pages,
                      "athena": athena or _empty_athena(),
                      "render": render or {"hash": None, "image_url": None, "at": None}}
            _render_if_changed(owner, note_id, stored, final=True,
                               now=time.time() if now is None else now, store=store)
            _write_json(sidecar_path(store, note_id), stored)
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            if created:  # no note without its pages
                delete_note(owner, note_id)
                _remove_sidecar(store, note_id)
            raise
        return public(stored, note_title(owner, note_id))


def photo_file(owner: str | None, note_id: str, page_id: str) -> Path | None:
    """The stored photo of a photo page of the owner's handwritten note, else None."""
    if not valid_note_id(note_id) or not isinstance(page_id, str) \
            or not _SHORT_ID_RE.match(page_id) or not existing_note_ids(owner, [note_id]):
        return None
    store = _store_owner(owner, note_id)
    with _lock(store):
        stored = load_sidecar(store, note_id)
    page = next((p for p in (stored or {}).get("pages") or []
                 if isinstance(p, dict) and p.get("id") == page_id), None)
    if page is None or not is_photo(page):
        return None
    path = photo_path(store, note_id, page_id)
    return path if path.is_file() else None


def owner_folders() -> list[str | None]:
    """Every ink folder, as the owner to pass for it: a folder name is its owner's name,
    or a hash of it (:func:`_owner_dir_name`), which maps to itself; ``_single`` (single-
    user mode's folder) is ``None``."""
    from src import constants  # read at call time: tests point DATA_DIR elsewhere

    root = Path(constants.DATA_DIR) / "ink"
    if not root.is_dir():
        return []
    return [None if p.name == "_single" else p.name
            for p in sorted(root.iterdir()) if p.is_dir()]


def sweep(owner: str | None) -> list[str]:
    """Remove sidecars whose note is gone (deleted in the Notes window or by a tool),
    and remember their ids so a queued save from a tablet doesn't bring them back.
    Returns the removed ids. A note sent to Athena keeps its sidecar until Athena
    confirms it removed the note (``src/ink_athena.py`` ``sweep``). Single-user mode
    (``owner`` None) sees every note, so it sweeps every owner's folder."""
    if owner is not None:
        return sweep_folder(owner)
    return [i for folder in owner_folders() for i in sweep_folder(folder)]


def sweep_folder(owner: str | None) -> list[str]:
    """:func:`sweep` for one ink folder (``owner`` None: ``_single``)."""
    with _lock(owner):  # no save, delete or tombstone write of this owner meanwhile
        ids = sidecar_ids(owner)
        _sweep_photos(owner, set(ids))
        if not ids:
            return []
        # Any note with that id keeps its strokes (also one relabelled in Notes).
        alive = {i for i in ids if note_id_taken(i)}
        removed = []
        gone = []
        for note_id in ids:
            if note_id not in alive:
                gone.append(note_id)
                if not sent_to_athena(load_sidecar(owner, note_id)):
                    _remove_sidecar(owner, note_id)
                    removed.append(note_id)
        _add_tombstones(owner, gone)
        return removed


def _sweep_photos(owner: str | None, sidecars: set[str]) -> None:
    """Photo folders without a sidecar (a write cut short) are removed. Call under the
    lock."""
    root = ink_dir(owner) / "photos"
    if not root.is_dir():
        return
    for folder in root.iterdir():
        if folder.is_dir() and valid_note_id(folder.name) and folder.name not in sidecars:
            shutil.rmtree(folder, ignore_errors=True)


def list_notes(owner: str | None) -> list[dict[str, Any]]:
    """The owner's handwritten notes, newest first, after a sweep."""
    sweep(owner)
    out = []
    for note in handwritten_notes(owner):
        store = note["owner"] if owner is None else owner
        stored = load_sidecar(store, note["id"])
        pages = len(stored.get("pages") or []) if stored else 0
        out.append({**note, "pages": pages,
                    "edited_at": int(stored.get("edited_at") or 0) if stored else 0,
                    "link": note_link(note["id"]),
                    # H2: pages to review, failed, being read (src/ink_transcribe.py)
                    "transcripts": _transcript_summary(stored),
                    # H4: not_sent, sent or changed (src/ink_athena.py)
                    "athena": _athena_state(stored)})
    return out


def _athena_state(stored: dict[str, Any] | None) -> str:
    from src import ink_athena  # imports this module

    return ink_athena.state(stored) if stored else "not_sent"


def _transcript_summary(stored: dict[str, Any] | None) -> dict[str, Any]:
    from src import ink_transcribe  # imports this module

    if not stored:
        return {"review": [], "failed": [], "pending": 0}
    return ink_transcribe.summary(stored)


# =================================================================== the adapter
# Every call into upstream notes code. When an upstream sync changes the notes code,
# tests/test_ink_upstream_contract.py fails and the fix belongs here.
#
# Notes are written through the ``Note`` model with the same owner rules as upstream's
# notes routes (routes/note/note_routes.py): owner ``None`` (single-user mode) sees every
# note; otherwise the owner must match exactly. Upstream's ``do_manage_notes`` tool isn't
# used: it can't set ``image_url`` or a client-made id (needed to start a note offline),
# marks notes ``source="agent"`` and merges notes with identical text.


def _note_query(db, owner: str | None):
    from core.database import Note

    q = db.query(Note)
    if owner is not None:
        q = q.filter(Note.owner == owner)
    return q


def _reserve(owner: str | None, *values: Any) -> None:
    from src.tool_utils import get_upload_handler
    from src.upload_handler import reserve_upload_references

    missing = reserve_upload_references(get_upload_handler(), owner, *values)
    if missing:
        raise InkError(f"Referenced upload is no longer available: {missing}", 409)


def create_note(owner: str | None, title: str, label: str, image_url: str | None,
                *, note_id: str | None = None) -> str | None:
    """Create a plain note and return its id; ``None`` if ``note_id`` is taken."""
    import uuid

    from core.database import Note, SessionLocal

    _reserve(owner, image_url)
    db = SessionLocal()
    try:
        new_id = note_id or str(uuid.uuid4())
        if db.query(Note).filter(Note.id == new_id).first() is not None:
            return None
        db.add(Note(id=new_id, owner=owner, title=title, content="", note_type="note",
                    label=label, image_url=image_url or None, source="user",
                    repeat="none", sort_order=0))
        db.commit()
        return new_id
    finally:
        db.close()


def update_note(owner: str | None, note_id: str, *, content: str | None = None,
                image_url: str | None = None, title: str | None = None) -> bool:
    """Change fields of the owner's handwritten note (``image_url=""`` clears it)."""
    from core.database import Note, SessionLocal

    if image_url:
        _reserve(owner, image_url)
    db = SessionLocal()
    try:
        note = _note_query(db, owner).filter(Note.id == note_id, Note.label == LABEL).first()
        if note is None:
            return False
        if content is not None:
            note.content = content
        if image_url is not None:
            note.image_url = image_url or None
        if title is not None:
            note.title = title
        db.commit()
        return True
    finally:
        db.close()


def delete_note(owner: str | None, note_id: str) -> bool:
    """Delete the owner's handwritten note; any other note is left alone."""
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        note = _note_query(db, owner).filter(Note.id == note_id, Note.label == LABEL).first()
        if note is None:
            return False
        db.delete(note)
        db.commit()
        return True
    finally:
        db.close()


def existing_note_ids(owner: str | None, ids: Iterable[str]) -> set[str]:
    """Which of ``ids`` are handwritten notes of this owner (archived ones count)."""
    from core.database import Note, SessionLocal

    ids = [i for i in ids if isinstance(i, str)]
    if not ids:
        return set()
    db = SessionLocal()
    try:
        found: set[str] = set()
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = (_note_query(db, owner).filter(Note.id.in_(chunk), Note.label == LABEL)
                    .with_entities(Note.id))
            found.update(r[0] for r in rows)
        return found
    finally:
        db.close()


def note_title(owner: str | None, note_id: str) -> str:
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        note = _note_query(db, owner).filter(Note.id == note_id, Note.label == LABEL).first()
        return (note.title or "") if note is not None else ""
    finally:
        db.close()


def note_id_taken(note_id: str) -> bool:
    """Whether any note (any owner, any label) has this id."""
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        return db.query(Note.id).filter(Note.id == note_id).first() is not None
    finally:
        db.close()


def note_owner(note_id: str) -> str | None:
    """The owner stored on a note (``None`` for an unknown id or an ownerless note)."""
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(Note.owner).filter(Note.id == note_id).first()
        return row[0] if row else None
    finally:
        db.close()


def handwritten_notes(owner: str | None) -> list[dict[str, Any]]:
    """The owner's unarchived notes labelled ``handwritten``, newest first."""
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        rows = (_note_query(db, owner)
                .filter(Note.label == LABEL, Note.archived == False)  # noqa: E712
                .order_by(Note.updated_at.desc()).all())
        return [{"id": n.id, "owner": n.owner, "title": n.title or "", "image_url": n.image_url,
                 "updated_at": n.updated_at.isoformat() if n.updated_at else None}
                for n in rows]
    finally:
        db.close()


def save_page_image(owner: str | None, png_bytes: bytes,
                    filename: str = "handwritten.png") -> str | None:
    """Store a rendered page through upstream's upload store (the one the drawing
    canvas posts to) and return its URL, ``/api/upload/<id>``; ``None`` if refused.
    ``filename`` ``handwritten.jpg`` for a JPEG (a photo page, H6)."""
    from fastapi import HTTPException, UploadFile

    from src.tool_utils import get_upload_handler

    handler = get_upload_handler()
    if handler is None or not png_bytes:
        return None
    upload = UploadFile(file=io.BytesIO(png_bytes), filename=filename)
    try:
        meta = handler.save_upload(upload, f"fork-ink:{owner or ''}", owner=owner)
    except HTTPException:
        return None
    return f"/api/upload/{meta['id']}"


def note_link(note_id: str) -> str:
    """The hub link that opens the note (static/js/forkEntityHash.js)."""
    return f"/#open=notes&note={note_id}"
