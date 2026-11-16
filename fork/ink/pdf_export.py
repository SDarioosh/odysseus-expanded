"""Fork: PDF datasheets, export with marks: a copy of a datasheet's PDF with the owner's
pen and highlighter as real PDF annotations (``GET /api/ink/notes/{id}/pdf?marks=1``,
``fork/routes/ink_routes.py``; design ``docs/fork/datasheets.md`` "Export with marks").

The stored PDF never changes: the copy is built in memory on each request, with
pypdfium2's raw pdfium API, every call under ``fork.ink.pdf._PDFIUM_LOCK``.

* The copy follows the note's pages: PDF pages in the note's order (a page the owner
  removed is left out), **blank pages** the owner added are inserted where they are, as
  large as the PDF page before them (the one after when there is none; A4 when the note
  has no PDF page), with their ink scaled to fit the page's width (or height) at the top.
  **Photo pages are left out** (a photo is no PDF page).
* Each mark (``fork/ink/pdf_marks.py``) becomes annotations:
  - a highlight: a Highlight annotation with QuadPoints over the words it covers (one quad
    per run of words on a line) and ``/Contents`` the covered text; when it covers no
    words, a semi-transparent Ink annotation of its strokes;
  - every other mark: an Ink annotation of its strokes (one per pen colour and width);
    ``/Contents`` is what was written (an ink mark read by the model or corrected by the
    owner), else, for an underline and any other kind with text (circled words, the line
    an arrow points to), the text it covers.
  Pen strokes that are in no mark (blank pages) become Ink annotations, one per pen.
* Coordinates: page units (1000 wide, origin top-left, y down, as displayed) map back to
  PDF user space through the page's crop box and ``/Rotate`` (the inverse of
  ``pdf._display_map``); a pen's width is scaled the same way into points.

:func:`build` is the core (a PDF file, the note's pages, its marks, the words of a page);
:func:`export` reads the owner's note.
"""

from __future__ import annotations

import ctypes
import io
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from fork.ink import notes as ink
from fork.ink import pdf as ink_pdf

A4_PT = (595.0, 842.0)
HIGHLIGHT_ALPHA = 255     # Highlight annotations: readers blend them (multiply)
HIGHLIGHTER_INK_ALPHA = 90  # a highlight over no words, drawn as Ink: about 35 %
MAX_CONTENTS = 2000       # characters of /Contents
LINE_GAP = 3.0            # page units: words this close on one line share one quad

Point = tuple[float, float]


def marked_name(name: Any) -> str:
    """The download's file name: ``<name without .pdf> (marked).pdf``."""
    name = ink.clean_pdf_name(name)
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    return f"{stem.strip() or 'datasheet'} (marked).pdf"


# ------------------------------------------------------------------ geometry
def user_map(page) -> tuple[Callable[[float, float], Point], float]:
    """``(to_user, pt_per_unit)`` for a pdfium page: ``to_user(x, y)`` maps page units
    (width 1000, origin top-left, y down, as displayed) to PDF user space; the inverse
    of :func:`fork.ink.pdf._display_map`. Call under ``_PDFIUM_LOCK``."""
    left, bottom, right, top = page.get_cropbox()
    uw, uh = right - left, top - bottom
    rotation = page.get_rotation() % 360
    shown_w = uh if rotation in (90, 270) else uw
    k = shown_w / ink.PAGE_WIDTH

    def to_user(x: float, y: float) -> Point:
        dx, dy = x * k, y * k              # displayed points, from the top-left
        if rotation == 90:
            u, v = dy, uh - dx
        elif rotation == 180:
            u, v = uw - dx, uh - dy
        elif rotation == 270:
            u, v = uw - dy, dx
        else:
            u, v = dx, dy
        return left + u, top - v

    return to_user, k


def blank_map(width_pt: float, height_pt: float, page_h_units: float
              ) -> tuple[Callable[[float, float], Point], float]:
    """``(to_user, pt_per_unit)`` for a blank page (``PAGE_WIDTH`` x ``page_h_units``)
    drawn on a new, unrotated PDF page ``width_pt`` x ``height_pt``: one scale, as wide
    as the page (or as tall, when that is the tighter fit), at the top, centred across."""
    k = min(width_pt / ink.PAGE_WIDTH, height_pt / max(1.0, page_h_units))
    x0 = (width_pt - ink.PAGE_WIDTH * k) / 2

    def to_user(x: float, y: float) -> Point:
        return x0 + x * k, height_pt - y * k

    return to_user, k


def _hex_rgb(color: Any) -> tuple[int, int, int]:
    c = str(color or "")
    if len(c) == 7 and c.startswith("#"):
        try:
            return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
        except ValueError:
            pass
    return 0, 0, 0


def word_runs(words: Sequence[Any], boxes: Iterable[tuple[float, float, float, float]],
              pad: float) -> list[tuple[float, float, float, float]]:
    """The boxes (page units ``x0, y0, x1, y1``) to highlight: the words whose centre is
    in one of ``boxes`` grown by ``pad`` (as ``pdf_marks`` finds a highlight's text),
    joined into one box per run of neighbouring words on a line."""
    grown = [(b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad) for b in boxes]
    hit = [w for w in words
           if any(g[0] <= (w.x0 + w.x1) / 2 <= g[2] and g[1] <= (w.y0 + w.y1) / 2 <= g[3]
                  for g in grown)]
    runs: list[list[float]] = []
    last_line = None
    for w in hit:  # reading order
        if runs and w.line == last_line and w.x0 >= runs[-1][2] - LINE_GAP \
                and w.x0 - runs[-1][2] <= 3 * max(1.0, w.y1 - w.y0):
            r = runs[-1]
            r[:] = [r[0], min(r[1], w.y0), max(r[2], w.x1), max(r[3], w.y1)]
        else:
            runs.append([w.x0, w.y0, w.x1, w.y1])
        last_line = w.line
    return [tuple(r) for r in runs]  # type: ignore[misc]


# ---------------------------------------------------------------- annotations
def _set_contents(annot, text: str) -> None:
    import pypdfium2.raw as c

    raw = text[:MAX_CONTENTS].encode("utf-16-le") + b"\x00\x00"
    buf = ctypes.create_string_buffer(raw, len(raw))
    c.FPDFAnnot_SetStringValue(annot, b"Contents", ctypes.cast(buf, ctypes.POINTER(c.FPDF_WCHAR)))


def _set_rect(annot, pts: Iterable[Point], grow: float) -> None:
    import pypdfium2.raw as c

    pts = list(pts)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    c.FPDFAnnot_SetRect(annot, c.FS_RECTF(min(xs) - grow, max(ys) + grow,
                                          max(xs) + grow, min(ys) - grow))


def _ink_annot(page, strokes: list[dict[str, Any]], to_user, k: float, *, alpha: int,
               contents: str = "") -> bool:
    """One Ink annotation of ``strokes`` (one pen), True when made."""
    import pypdfium2.raw as c

    paths = [[to_user(float(p[0]), float(p[1])) for p in s.get("points") or []]
             for s in strokes]
    paths = [p for p in paths if p]
    if not paths:
        return False
    pen = strokes[0].get("pen") or {}
    width = max(0.1, float(pen.get("width") or 1) * k)
    annot = c.FPDFPage_CreateAnnot(page.raw, c.FPDF_ANNOT_INK)
    if not annot:
        return False
    try:
        for path in paths:
            if len(path) == 1:  # a dot: pdfium draws nothing for one point
                path = [path[0], (path[0][0] + 0.01, path[0][1])]
            arr = (c.FS_POINTF * len(path))(*(c.FS_POINTF(x, y) for x, y in path))
            c.FPDFAnnot_AddInkStroke(annot, arr, len(path))
        c.FPDFAnnot_SetColor(annot, c.FPDFANNOT_COLORTYPE_Color, *_hex_rgb(pen.get("color")),
                             alpha)
        c.FPDFAnnot_SetBorder(annot, 0, 0, width)
        _set_rect(annot, (pt for path in paths for pt in path), width)
        c.FPDFAnnot_SetFlags(annot, c.FPDF_ANNOT_FLAG_PRINT)
        if contents:
            _set_contents(annot, contents)
    finally:
        c.FPDFPage_CloseAnnot(annot)
    return True


def _by_pen(strokes: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Strokes grouped by pen colour and width, in order of first use."""
    groups: dict[tuple[str, float], list[dict[str, Any]]] = {}
    for s in strokes:
        pen = s.get("pen") or {}
        groups.setdefault((str(pen.get("color") or ""), float(pen.get("width") or 1)),
                          []).append(s)
    return list(groups.values())


def _ink_annots(page, strokes, to_user, k, *, alpha: int, contents: str = "") -> int:
    made = 0
    for group in _by_pen(strokes):
        if _ink_annot(page, group, to_user, k, alpha=alpha,
                      contents=contents if not made else ""):
            made += 1
    return made


def _highlight_annot(page, runs, color: Any, to_user, contents: str) -> bool:
    import pypdfium2.raw as c

    annot = c.FPDFPage_CreateAnnot(page.raw, c.FPDF_ANNOT_HIGHLIGHT)
    if not annot:
        return False
    corners: list[Point] = []
    try:
        for x0, y0, x1, y1 in runs:
            # QuadPoints order: top-left, top-right, bottom-left, bottom-right (as shown).
            tl, tr, bl, br = (to_user(x0, y0), to_user(x1, y0), to_user(x0, y1),
                              to_user(x1, y1))
            corners += [tl, tr, bl, br]
            c.FPDFAnnot_AppendAttachmentPoints(annot, c.FS_QUADPOINTSF(
                tl[0], tl[1], tr[0], tr[1], bl[0], bl[1], br[0], br[1]))
        c.FPDFAnnot_SetColor(annot, c.FPDFANNOT_COLORTYPE_Color, *_hex_rgb(color),
                             HIGHLIGHT_ALPHA)
        _set_rect(annot, corners, 0)
        c.FPDFAnnot_SetFlags(annot, c.FPDF_ANNOT_FLAG_PRINT)
        if contents:
            _set_contents(annot, contents)
    finally:
        c.FPDFPage_CloseAnnot(annot)
    return True


def _contents(mark: dict[str, Any]) -> str:
    """What a PDF reader shows for a mark (see the module docstring)."""
    written = str(mark.get("written") or "").strip()
    if written and mark.get("reading") in ("read", "reviewed"):
        return written
    if mark.get("kind") == "ink":
        return ""
    return str(mark.get("text") or "").strip()


def _annotate_pdf_page(page, note_page: dict[str, Any], marks: list[dict[str, Any]],
                       words: Sequence[Any]) -> int:
    from fork.ink import pdf_marks

    to_user, k = user_map(page)
    strokes = {str(s.get("id")): s for s in note_page.get("strokes") or []
               if isinstance(s, dict) and s.get("points")}
    used: set[str] = set()
    made = 0
    for mark in marks:
        members = [strokes[i] for i in mark.get("strokes") or [] if i in strokes]
        if not members:
            continue
        used.update(str(s.get("id")) for s in members)
        if mark.get("kind") == "highlight":
            boxes = [pdf_marks._drawn_box(s) for s in members]
            runs = word_runs(words, boxes, pdf_marks.TEXT_PAD)
            if runs and _highlight_annot(page, runs, mark.get("color"), to_user,
                                         _contents(mark)):
                made += 1
                continue
            made += _ink_annots(page, members, to_user, k, alpha=HIGHLIGHTER_INK_ALPHA,
                                contents=_contents(mark))
            continue
        made += _ink_annots(page, members, to_user, k, alpha=255, contents=_contents(mark))
    made += _loose_strokes(page, [s for i, s in strokes.items() if i not in used], to_user, k)
    return made


def _loose_strokes(page, strokes: list[dict[str, Any]], to_user, k: float) -> int:
    """Strokes in no mark: pen ink opaque, highlighter see-through, one per pen."""
    pens = [s for s in strokes if not (s.get("pen") or {}).get("highlighter")]
    marker = [s for s in strokes if (s.get("pen") or {}).get("highlighter")]
    return (_ink_annots(page, marker, to_user, k, alpha=HIGHLIGHTER_INK_ALPHA)
            + _ink_annots(page, pens, to_user, k, alpha=255))


# ---------------------------------------------------------------------- build
def _layout(pages: list[dict[str, Any]]) -> list[tuple[dict[str, Any], tuple[float, float] | None]]:
    """The note's pages that go into the copy, each with the size of a new blank page
    (None for a PDF page). Photo pages are left out."""
    out: list[tuple[dict[str, Any], tuple[float, float] | None]] = []
    last: tuple[float, float] | None = None
    pending: list[dict[str, Any]] = []  # blank pages before the first PDF page
    for page in pages:
        if ink.is_photo(page):
            continue
        if ink.is_pdf(page):
            p = page["pdf"]
            last = (float(p.get("width_pt") or A4_PT[0]), float(p.get("height_pt") or A4_PT[1]))
            out.extend((b, last) for b in pending)
            pending = []
            out.append((page, None))
        elif last is None:
            pending.append(page)
        else:
            out.append((page, last))
    out.extend((b, A4_PT) for b in pending)  # a note with no PDF page left
    order = {id(p): i for i, p in enumerate(pages)}
    out.sort(key=lambda item: order[id(item[0])])
    return out


def build(pdf_path: Path, pages: list[dict[str, Any]], marks: list[dict[str, Any]],
          words_of: Callable[[int], Sequence[Any]]) -> tuple[bytes, int]:
    """The annotated copy: ``(pdf bytes, number of annotations made)``. ``pages`` are the
    note's pages as stored, ``marks`` its marks (``pdf_marks`` marks with ``written`` and
    ``reading``), ``words_of(page_no)`` a PDF page's words (``pdf.page_words``; called
    before pdfium is locked). Raises ``ValueError`` for a file pdfium can't read,
    ``OSError`` for a missing one, :class:`~fork.ink.notes.InkError` (404) when no page
    is left to export."""
    import pypdfium2.raw as c

    layout = _layout(pages)
    if not layout:
        raise ink.InkError("The note has no page to export", 404)
    by_page: dict[str, list[dict[str, Any]]] = {}
    for m in marks:
        by_page.setdefault(str(m.get("page_id")), []).append(m)
    words: dict[int, Sequence[Any]] = {}
    for page, size in layout:
        if size is None and by_page.get(str(page.get("id"))):
            n = int(page["pdf"]["page"])
            try:
                words[n] = list(words_of(n))
            except (OSError, ValueError, RuntimeError):
                words[n] = []  # highlights become see-through ink
    made = 0
    with ink_pdf._PDFIUM_LOCK:
        doc = ink_pdf._open_file(Path(pdf_path))
        try:
            count = len(doc)
            wanted = [int(p["pdf"]["page"]) for p, size in layout if size is None]
            if any(not 1 <= n <= count for n in wanted) or len(set(wanted)) != len(wanted):
                raise ValueError("the note's pages don't match its PDF")
            keep = set(wanted)
            for i in range(count - 1, -1, -1):  # pages the owner removed
                if i + 1 not in keep:
                    doc.del_page(i)
            remaining = sorted(keep)
            if wanted != remaining:  # the owner reordered pages
                arr = (ctypes.c_int * len(wanted))(*(remaining.index(n) for n in wanted))
                if not c.FPDF_MovePages(doc.raw, arr, len(wanted), 0):
                    raise ValueError("the PDF's pages could not be reordered")
            for index, (page, size) in enumerate(layout):
                if size is not None:
                    doc.new_page(size[0], size[1], index).close()
            for index, (note_page, size) in enumerate(layout):
                if not note_page.get("strokes"):
                    continue
                page = doc[index]
                try:
                    if size is None:
                        made += _annotate_pdf_page(
                            page, note_page, by_page.get(str(note_page.get("id")), []),
                            words.get(int(note_page["pdf"]["page"]), []))
                    else:
                        to_user, k = blank_map(size[0], size[1],
                                               float(note_page.get("height") or ink.PAGE_HEIGHT))
                        made += _loose_strokes(page, [s for s in note_page["strokes"]
                                                      if s.get("points")], to_user, k)
                finally:
                    page.close()
            out = io.BytesIO()
            doc.save(out)
        finally:
            doc.close()
    return out.getvalue(), made


def export(owner: str | None, note_id: str) -> tuple[bytes, str] | None:
    """``(pdf bytes, file name)``: the owner's datasheet with its marks as annotations,
    or None when it isn't the owner's PDF note or its file is missing. Raises
    ``ValueError`` when pdfium can't read the stored file."""
    from fork.ink import pdf_marks

    path = ink.pdf_file(owner, note_id)  # the owner check
    if path is None:
        return None
    found = pdf_marks.note_marks(owner, note_id)  # marks with what was written
    note = ink.load(owner, note_id)
    if found is None or note is None or not isinstance(note.get("pdf"), dict):
        return None
    data, _ = build(path, note.get("pages") or [], found["marks"],
                    lambda n: ink_pdf.page_words(path, n))
    return data, marked_name(note["pdf"].get("name"))
