"""Fork: handwritten notes, PDF datasheets: read a PDF with pypdfium2. Plan:
docs/fork/handwritten-notes.md; the note side (sidecar, files, routes) is
``fork/ink/notes.py`` (``add_pdf``) and ``fork/routes/ink_routes.py``.

The owner imports a component datasheet into the Write view and writes on it. Each PDF
page becomes a page of one handwritten note, ``PAGE_WIDTH`` (1000) units wide and as tall
as the PDF page is in proportion. This module only reads the PDF:

* :func:`inspect_pdf`: check an upload and list its pages' sizes (points, as displayed:
  the page's ``/Rotate`` applied).
* :func:`render_page`: one page as a PNG, a given number of pixels wide (the Write view's
  background, and page 1's image under the ink).
* :func:`page_words`: the words of a page with their boxes in page units (width 1000,
  origin top-left, y down, as displayed), in pdfium's reading order, each with the number
  of its text line. What a highlight or underline covers is found from these
  (``fork/ink/pdf_marks.py``).

pdfium is not thread-safe: every pdfium call in the process holds ``_PDFIUM_LOCK``.
Nothing here writes files; the hub keeps the PDF unchanged after import.
"""

from __future__ import annotations

import hashlib
import io
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, NamedTuple

from fork.ink.notes import PAGE_WIDTH, InkError

MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_PDF_PAGES = 500
WIDTH_TIERS = (600, 1200, 2000, 3000)  # px wide, picked by the Write view for its zoom
MIN_PAGE_HEIGHT = 250    # page units (width 1000): a page wider than 4:1 is refused
MAX_PAGE_HEIGHT = 4000   # and one taller than 1:4
WORDS_CACHE_SIZE = 64    # pages of words kept in memory

_PDFIUM_LOCK = threading.RLock()  # every pdfium call, in every thread


class Word(NamedTuple):
    """A word of a PDF page, its box in page units (as displayed, origin top-left)."""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    line: int  # the text line it is on; increases down the page in reading order


def page_height(width_pt: float, height_pt: float) -> int:
    """A PDF page's height in page units: ``PAGE_WIDTH`` wide, same proportions."""
    return int(round(PAGE_WIDTH * height_pt / width_pt))


def tier(width_px: int) -> int:
    """The smallest of ``WIDTH_TIERS`` at least ``width_px`` wide, else the largest."""
    for t in WIDTH_TIERS:
        if width_px <= t:
            return t
    return WIDTH_TIERS[-1]


def _open(source: bytes | Path):
    """A pdfium document of bytes or a file. Call under ``_PDFIUM_LOCK``."""
    import pypdfium2 as pdfium

    return pdfium.PdfDocument(bytes(source) if isinstance(source, (bytes, bytearray))
                              else str(source))


def _open_file(path: Path):
    """:func:`_open` for a stored file: ``OSError`` when it is missing, ``ValueError``
    when pdfium can't read it."""
    import pypdfium2 as pdfium

    try:
        return _open(Path(path))
    except pdfium.PdfiumError as exc:
        raise ValueError(f"the PDF can't be read: {exc}") from None


def inspect_pdf(data: bytes) -> dict[str, Any]:
    """Check an uploaded PDF: ``{"hash": sha256 hex, "pages": [{"page" (from 1),
    "width_pt", "height_pt"}]}`` with sizes as displayed (rotation applied). Raises
    :class:`~fork.ink.notes.InkError`: 413 over ``MAX_PDF_BYTES`` or ``MAX_PDF_PAGES``,
    400 not a PDF, password protected, no pages, or a page narrower or wider than the
    Write view can show (height outside ``MIN_PAGE_HEIGHT``..``MAX_PAGE_HEIGHT`` units)."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    if not isinstance(data, (bytes, bytearray)) or not data:
        raise InkError("The PDF is empty")
    if len(data) > MAX_PDF_BYTES:
        raise InkError(f"A PDF is at most {MAX_PDF_BYTES // (1024 * 1024)} MB", 413)
    pages = []
    with _PDFIUM_LOCK:
        try:
            doc = _open(data)
        except pdfium.PdfiumError as exc:
            if getattr(exc, "err_code", None) == pdfium_c.FPDF_ERR_PASSWORD:
                raise InkError("The PDF is password protected: save it without a password "
                               "first") from None
            raise InkError("The file is not a PDF that can be read") from None
        try:
            count = len(doc)
            if count < 1:
                raise InkError("The PDF has no pages")
            if count > MAX_PDF_PAGES:
                raise InkError(f"A PDF has at most {MAX_PDF_PAGES} pages", 413)
            for i in range(count):
                page = doc[i]
                try:
                    w, h = page.get_size()  # as displayed: pdfium applies /Rotate
                finally:
                    page.close()
                if not (w > 0 and h > 0):
                    raise InkError(f"Page {i + 1} of the PDF has no size")
                units = page_height(w, h)
                if not MIN_PAGE_HEIGHT <= units <= MAX_PAGE_HEIGHT:
                    raise InkError(f"Page {i + 1} of the PDF is too narrow or too wide "
                                   "to write on")
                pages.append({"page": i + 1, "width_pt": round(float(w), 3),
                              "height_pt": round(float(h), 3)})
        except InkError:
            raise
        except Exception:  # noqa: BLE001 -- anything pdfium can't read is refused
            raise InkError("The file is not a PDF that can be read") from None
        finally:
            doc.close()
    return {"hash": hashlib.sha256(bytes(data)).hexdigest(), "pages": pages}


# A rendered page is at most this many pixels: a tall page (up to MAX_PAGE_HEIGHT units)
# at the widest tier would otherwise be a 3000 x 12000 bitmap. An A4 or Letter page at
# 3000 px stays under it.
MAX_RENDER_PIXELS = 16_000_000


def capped_width(width_px: int, page_height_units: float) -> int:
    """``width_px``, lowered so a page ``page_height_units`` tall (per 1000 wide) stays
    within :data:`MAX_RENDER_PIXELS`. The client draws the image into the page's box, so
    a narrower image only means a little less sharpness on very tall pages."""
    ratio = max(1.0, float(page_height_units)) / 1000.0
    limit = int((MAX_RENDER_PIXELS / ratio) ** 0.5)
    return max(1, min(int(width_px), limit))


def render_image(path: Path, page_no: int, width_px: int):
    """A PDF page (``page_no`` from 1) as a Pillow RGB image exactly ``width_px`` wide,
    white under transparent parts, as displayed (rotation applied). Raises
    ``ValueError`` for a page the file doesn't have or a file pdfium can't read,
    ``OSError`` for a missing file."""
    from PIL import Image

    width_px = max(1, int(width_px))
    with _PDFIUM_LOCK:
        doc = _open_file(path)
        try:
            if not 1 <= page_no <= len(doc):
                raise ValueError(f"the PDF has no page {page_no}")
            page = doc[page_no - 1]
            try:
                w, h = page.get_size()
                bitmap = page.render(scale=width_px / w, may_draw_forms=True)
                try:
                    img = bitmap.to_pil().convert("RGB").copy()  # own memory, not pdfium's
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            doc.close()
    height_px = max(1, int(round(width_px * h / w)))
    if img.size != (width_px, height_px):  # pdfium rounds the scaled size its own way
        img = img.resize((width_px, height_px), Image.LANCZOS)
    return img


def render_page(path: Path, page_no: int, width_px: int) -> bytes:
    """A PDF page (``page_no`` from 1) as a PNG exactly ``width_px`` wide (see
    :func:`render_image`)."""
    img = render_image(path, page_no, width_px)
    out = io.BytesIO()
    img.save(out, format="PNG", compress_level=3)  # rendered once per tier, then cached
    return out.getvalue()


# ------------------------------------------------------------------------ words
_WORDS: OrderedDict[tuple[str, int, int], list[Word]] = OrderedDict()
_WORDS_LOCK = threading.Lock()


def _display_map(page) -> tuple[Any, float]:
    """``(to_display, scale)``: ``to_display(x, y)`` maps PDF user space (origin bottom
    left of the media box, unrotated) to displayed points, origin top-left, y down, the
    page's rotation (clockwise) applied; ``scale`` turns displayed points into page
    units."""
    left, bottom, right, top = page.get_cropbox()
    uw, uh = right - left, top - bottom   # unrotated size
    rotation = page.get_rotation() % 360

    def to_display(x: float, y: float) -> tuple[float, float]:
        u, v = x - left, top - y          # unrotated, from the top-left
        if rotation == 90:
            return uh - v, u
        if rotation == 180:
            return uw - u, uh - v
        if rotation == 270:
            return v, uw - u
        return u, v

    shown_w = uh if rotation in (90, 270) else uw
    return to_display, PAGE_WIDTH / shown_w


def _read_words(path: Path, page_no: int) -> list[Word]:
    import pypdfium2.raw as pdfium_c

    words: list[Word] = []
    with _PDFIUM_LOCK:
        doc = _open_file(path)
        try:
            if not 1 <= page_no <= len(doc):
                raise ValueError(f"the PDF has no page {page_no}")
            page = doc[page_no - 1]
            try:
                to_display, scale = _display_map(page)
                text_page = page.get_textpage()
                try:
                    line = 0
                    line_has_words = False
                    chars: list[str] = []
                    box: list[float] = []

                    def flush() -> None:
                        nonlocal line_has_words
                        if chars and box:
                            words.append(Word("".join(chars), *(round(v * scale, 1)
                                                               for v in box), line))
                            line_has_words = True
                        chars.clear()
                        box.clear()

                    for i in range(text_page.count_chars()):
                        code = pdfium_c.FPDFText_GetUnicode(text_page.raw, i)
                        ch = chr(code) if 0 < code < 0x110000 else ""
                        if ch in ("\r", "\n"):
                            flush()
                            if line_has_words:  # "\r\n" (pdfium's or the file's) ends a line
                                line += 1
                                line_has_words = False
                            continue
                        if not ch or ch.isspace() or ch == "\x00":
                            flush()
                            continue
                        if 0xD800 <= code <= 0xDFFF:
                            continue  # half of a pair pdfium reports separately
                        x0, y0, x1, y1 = text_page.get_charbox(i, loose=True)
                        (ax, ay), (bx, by) = to_display(x0, y0), to_display(x1, y1)
                        cb = [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]
                        if box:
                            box[:] = [min(box[0], cb[0]), min(box[1], cb[1]),
                                      max(box[2], cb[2]), max(box[3], cb[3])]
                        else:
                            box[:] = cb
                        chars.append(ch)
                    flush()
                finally:
                    text_page.close()
            finally:
                page.close()
        finally:
            doc.close()
    return words


def page_words(path: Path, page_no: int) -> list[Word]:
    """The words of a PDF page (``page_no`` from 1), see :class:`Word`. Kept in memory
    for the last ``WORDS_CACHE_SIZE`` pages, keyed by the file and its mtime. Raises
    ``OSError`` for a missing file, ``ValueError`` for a page it doesn't have."""
    path = Path(path)
    key = (str(path), path.stat().st_mtime_ns, int(page_no))
    with _WORDS_LOCK:
        if key in _WORDS:
            _WORDS.move_to_end(key)
            return list(_WORDS[key])
    words = _read_words(path, int(page_no))
    with _WORDS_LOCK:
        _WORDS[key] = words
        _WORDS.move_to_end(key)
        while len(_WORDS) > WORDS_CACHE_SIZE:
            _WORDS.popitem(last=False)
    return list(words)
