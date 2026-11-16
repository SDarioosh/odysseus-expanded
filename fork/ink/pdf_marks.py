"""Fork: what the owner marked on a PDF datasheet in the Write view (marks), and a
search over them. Contract: the PDF datasheet plan (``docs/fork/handwritten-notes.md``).

A PDF note (``fork/ink/notes.py``, sidecar ``"pdf"``) has PDF pages; the owner writes on
them with the pens and the highlighter. Here a PDF page's strokes are grouped into
**marks**, each with the PDF text under it (words from ``fork.ink.pdf.page_words``, in
page units: width 1000, origin top-left, y down)::

    {"id": "m-<first stroke id>", "page_id", "page_no", "kind": "highlight" | "underline"
     | "ink", "color": "#rrggbb", "box": [x, y, w, h], "text", "near",
     "strokes": [stroke ids], "at": <ms, latest stroke>}

Grouping rules (only PDF pages; blank pages between them have no PDF text):

* **highlight**: highlighter strokes. Strokes of the same colour whose boxes (pen width
  included) come within ``HIGHLIGHT_GAP`` of each other are one mark, also through a
  chain. ``text``: the words whose centre lies in one of its strokes' boxes grown by
  ``TEXT_PAD`` (not the group's box: lines highlighted to different lengths).
* **underline**: one pen stroke that is a nearly straight, nearly horizontal line: its
  points' box at most ``UNDERLINE_MAX_H`` high, at least ``UNDERLINE_MIN_W`` wide,
  ``UNDERLINE_MIN_RATIO`` times wider than high, and its path at most
  ``UNDERLINE_MAX_PATH`` times its box diagonal (no zigzag). ``text``: the one text line
  just above it: of the words whose bottom edge is at most ``UNDERLINE_ABOVE`` above the
  line (or ``UNDERLINE_BELOW`` below it, descenders) and that the line covers for at
  least ``UNDERLINE_OVERLAP`` of their width, those of the nearest line. Such a stroke
  with no text above it (a dash, an arrow's shaft) is ink.
* **ink**: every other pen stroke. Strokes whose boxes come within ``INK_GAP`` of each
  other are one mark (handwriting, arrows, circles). ``text``: the words whose centre
  lies inside the box (a circled value); when there are none, ``near`` is the PDF text
  line closest to the box: the one closest up or down (level with it best), then the
  closest across; both gaps within ``NEAR_MAX``, a gap across counting ``NEAR_ACROSS``
  as much (a margin note beside a line is near it).

Marks of a page are ordered by ``y`` then ``x`` of their box. Text is the words in the
PDF's reading order joined by spaces, at most ``MAX_TEXT`` characters.

:func:`page_marks` is pure (tested with hand-made strokes and words). :func:`note_marks`
and :func:`search` read the owner's notes; a page's marks are cached in memory by
``(note_id, page_id, strokes_hash)``, so words are only read for a page whose strokes
changed (and ``page_words`` has its own cache). What the model read in an ink mark
(``written``, ``reading``) is merged in when a note's marks are answered
(``fork/ink/mark_reading.py``): readings change without the strokes changing.

:func:`search` (the Write list) wants every word of the query; :func:`rank` (programs:
the brain's ``search_datasheet_marks``, ``fork/brain/datasheets.py``) takes a loose
question instead: :func:`query_words` drops stop words, :func:`score` (pure) counts the
query words found in a mark (its text, what was written, its near line) plus those found
in the datasheet's title or PDF name, so every mark of a datasheet named in the query
qualifies.
"""

from __future__ import annotations

import itertools
import math
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from typing import Any

HIGHLIGHT_GAP = 8.0       # highlighter boxes this close (same colour) are one mark
TEXT_PAD = 4.0            # a word counts when its centre is in the box grown by this
UNDERLINE_MAX_H = 14.0    # an underline's points: at most this high ...
UNDERLINE_MIN_W = 25.0    # ... at least this wide ...
UNDERLINE_MIN_RATIO = 4.0  # ... and this many times wider than high
UNDERLINE_MAX_PATH = 1.5  # path length / box diagonal: more is a scribble, not a line
UNDERLINE_ABOVE = 18.0    # underlined words end at most this far above the line ...
UNDERLINE_BELOW = 6.0     # ... or this far below it (descenders, a line drawn high)
UNDERLINE_OVERLAP = 0.4   # ... and the line covers at least this share of their width
INK_GAP = 30.0            # pen strokes this close are one mark
NEAR_MAX = 80.0           # an ink mark's "near" line is at most this far away
# Across the page a gap counts this much: a note written in the margin beside a line is
# "near" it (a datasheet's margin can be 300 units from the end of a line), while one a
# few lines up or down is not.
NEAR_ACROSS = 0.25
MAX_TEXT = 2000
CACHE_PAGES = 2000        # pages of marks kept in memory

KINDS = ("highlight", "underline", "ink")

Box = tuple[float, float, float, float]  # x0, y0, x1, y1


# ------------------------------------------------------------------ geometry
def _points_box(stroke: dict[str, Any]) -> Box:
    xs = [float(p[0]) for p in stroke.get("points") or []]
    ys = [float(p[1]) for p in stroke.get("points") or []]
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _grow(box: Box, by: float) -> Box:
    return (box[0] - by, box[1] - by, box[2] + by, box[3] + by)


def _drawn_box(stroke: dict[str, Any]) -> Box:
    """The stroke's box as drawn: its points plus half the pen's width."""
    pen = stroke.get("pen") or {}
    return _grow(_points_box(stroke), float(pen.get("width") or 0) / 2)


def _union(a: Box, b: Box) -> Box:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _gap(a: Box, b: Box) -> float:
    """Distance between two boxes (0 when they touch or overlap)."""
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _path_length(stroke: dict[str, Any]) -> float:
    pts = stroke.get("points") or []
    return sum(math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
               for a, b in itertools.pairwise(pts))


def _centre_in(word: Any, box: Box) -> bool:
    cx, cy = (word.x0 + word.x1) / 2, (word.y0 + word.y1) / 2
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


# ------------------------------------------------------------------- grouping
def is_underline(stroke: dict[str, Any]) -> bool:
    """A pen stroke shaped like an underline (see the module docstring)."""
    if (stroke.get("pen") or {}).get("highlighter"):
        return False
    x0, y0, x1, y1 = _points_box(stroke)
    w, h = x1 - x0, y1 - y0
    if h > UNDERLINE_MAX_H or w < UNDERLINE_MIN_W or w < UNDERLINE_MIN_RATIO * h:
        return False
    return _path_length(stroke) <= UNDERLINE_MAX_PATH * math.hypot(w, h)


def _clusters(boxes: list[Box], gap: float, same: Sequence[Any] | None = None
              ) -> list[list[int]]:
    """Indexes grouped so boxes within ``gap`` of each other (and, with ``same``, of
    equal ``same[i]``) are in one group, also through a chain. Groups and their members
    keep the input order."""
    parent = list(range(len(boxes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if same is not None and same[i] != same[j]:
                continue
            if _gap(boxes[i], boxes[j]) <= gap:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    groups: dict[int, list[int]] = {}
    for i in range(len(boxes)):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: g[0])


def _join(words: Iterable[Any]) -> str:
    text = " ".join(w.text.strip() for w in words if w.text and w.text.strip())
    return text[:MAX_TEXT]


def _words_in(words: Sequence[Any], box: Box) -> list[Any]:
    return [w for w in words if _centre_in(w, box)]


def _underlined(words: Sequence[Any], stroke: dict[str, Any]) -> list[Any]:
    """The words of the one text line just above an underline stroke."""
    x0, _, x1, _ = _points_box(stroke)
    pts = stroke.get("points") or []
    line_y = sum(float(p[1]) for p in pts) / len(pts)
    found = []
    for w in words:
        width = max(w.x1 - w.x0, 1e-6)
        cover = min(w.x1, x1) - max(w.x0, x0)
        if cover < UNDERLINE_OVERLAP * width:
            continue
        if line_y - UNDERLINE_ABOVE <= w.y1 <= line_y + UNDERLINE_BELOW:
            found.append(w)
    if not found:
        return []
    nearest = min(found, key=lambda w: abs(line_y - w.y1)).line
    return [w for w in found if w.line == nearest]


def _near_line(words: Sequence[Any], box: Box) -> str:
    """The text of the PDF line closest to ``box``, if within ``NEAR_MAX``."""
    lines: dict[Any, list[Any]] = {}
    for w in words:
        lines.setdefault(w.line, []).append(w)
    best, best_key = None, None
    for line_words in lines.values():
        lb = (min(w.x0 for w in line_words), min(w.y0 for w in line_words),
              max(w.x1 for w in line_words), max(w.y1 for w in line_words))
        across = max(0.0, lb[0] - box[2], box[0] - lb[2]) * NEAR_ACROSS
        dy = max(0.0, lb[1] - box[3], box[1] - lb[3])
        if dy > NEAR_MAX or across > NEAR_MAX:
            continue
        key = (round(dy), across)  # level with the note first, then the closest across
        if best_key is None or key < best_key:
            best, best_key = line_words, key
    return _join(best) if best else ""


def _mark(kind: str, members: list[dict[str, Any]], box: Box, page_id: str, page_no: int,
          text: str, near: str) -> dict[str, Any]:
    return {"id": "m-" + str(members[0].get("id")), "page_id": page_id, "page_no": page_no,
            "kind": kind, "color": str((members[0].get("pen") or {}).get("color") or "").lower(),
            "box": [round(box[0], 1), round(box[1], 1), round(box[2] - box[0], 1),
                    round(box[3] - box[1], 1)],
            "text": text, "near": near,
            "strokes": [str(s.get("id")) for s in members],
            "at": max(int(s.get("at") or 0) for s in members)}


def page_marks(page: dict[str, Any], page_no: int, words: Sequence[Any]) -> list[dict[str, Any]]:
    """The marks on one PDF page (``page`` as stored: ``id``, ``strokes``), with
    ``words`` the page's PDF words (``text, x0, y0, x1, y1, line`` in page units, reading
    order). Pure: the same strokes and words give the same marks."""
    page_id = str(page.get("id") or "")
    strokes = [s for s in page.get("strokes") or [] if isinstance(s, dict) and s.get("points")]
    highlights = [s for s in strokes if (s.get("pen") or {}).get("highlighter")]
    pens = [s for s in strokes if not (s.get("pen") or {}).get("highlighter")]
    marks: list[dict[str, Any]] = []

    # Highlights: same colour, touching.
    boxes = [_drawn_box(s) for s in highlights]
    colours = [str((s.get("pen") or {}).get("color") or "").lower() for s in highlights]
    for group in _clusters(boxes, HIGHLIGHT_GAP, colours):
        box = boxes[group[0]]
        for i in group[1:]:
            box = _union(box, boxes[i])
        # Per stroke, not the group's box: two lines highlighted to different lengths
        # don't take the words beside the shorter one.
        padded = [_grow(boxes[i], TEXT_PAD) for i in group]
        text = _join(w for w in words if any(_centre_in(w, b) for b in padded))
        marks.append(_mark("highlight", [highlights[i] for i in group], box, page_id,
                           page_no, text, ""))

    # Underlines: one stroke each, only with text above it; the rest is ink.
    ink: list[dict[str, Any]] = []
    for s in pens:
        under = _underlined(words, s) if is_underline(s) else []
        if under:
            marks.append(_mark("underline", [s], _drawn_box(s), page_id, page_no,
                               _join(under), ""))
        else:
            ink.append(s)

    # Ink: strokes close together.
    boxes = [_drawn_box(s) for s in ink]
    for group in _clusters(boxes, INK_GAP):
        box = boxes[group[0]]
        for i in group[1:]:
            box = _union(box, boxes[i])
        inside = _words_in(words, box)
        text = _join(inside)
        near = "" if inside else _near_line(words, box)
        marks.append(_mark("ink", [ink[i] for i in group], box, page_id, page_no, text, near))

    marks.sort(key=lambda m: (m["box"][1], m["box"][0]))
    return marks


# -------------------------------------------------------------------- service
_CACHE: OrderedDict[tuple[str, str, str], list[dict[str, Any]]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def is_pdf_page(page: Any) -> bool:
    return isinstance(page, dict) and isinstance(page.get("pdf"), dict)


def _cached_page_marks(owner: str | None, note_id: str, page: dict[str, Any],
                       pdf_path: Any) -> list[dict[str, Any]]:
    """:func:`page_marks` of a stored PDF page, cached by its strokes. ``pdf_path`` is
    the note's PDF file, or a callable giving it (asked only on a cache miss)."""
    from fork.ink import notes as ink

    if not page.get("strokes"):
        return []
    key = (note_id, str(page.get("id")), ink.strokes_hash(page))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
    page_no = int(page["pdf"].get("page") or 0)
    words: list[Any] = []
    ok = True
    try:
        path = pdf_path() if callable(pdf_path) else pdf_path
        if path is not None:
            from fork.ink import pdf as ink_pdf

            words = list(ink_pdf.page_words(path, page_no))
        else:
            ok = False
    except (ink.InkError, OSError, ValueError, RuntimeError):  # RuntimeError: pdfium
        ok = False  # no text this time: marks still listed, not cached
    marks = page_marks(page, page_no, words)
    from fork.ink import mark_reading  # imports this module

    for mark in marks:  # what a reading is checked against (never answered)
        mark["_hash"] = mark_reading.mark_hash(mark_reading.mark_strokes(page, mark))
    if ok:
        with _CACHE_LOCK:
            _CACHE[key] = marks
            while len(_CACHE) > CACHE_PAGES:
                _CACHE.popitem(last=False)
    return marks


def _stored_marks(owner: str | None, note_id: str, stored: dict[str, Any]
                  ) -> list[dict[str, Any]]:
    from fork.ink import notes as ink

    path: list[Any] = []

    def pdf_path():  # looked up once, and only when a page isn't cached
        if not path:
            path.append(ink.pdf_file(owner, note_id))
        return path[0]

    out: list[dict[str, Any]] = []
    for page in stored.get("pages") or []:
        if is_pdf_page(page):
            out.extend(_cached_page_marks(owner, note_id, page, pdf_path))
    return out


def note_marks(owner: str | None, note_id: str) -> dict[str, Any] | None:
    """``{"note_id", "title", "marks", "unread"}`` for the owner's handwritten note (marks
    of its PDF pages, in page order, each with what was read in it:
    ``fork/ink/mark_reading.py`` ``view``), or ``None`` when it isn't the owner's note.
    ``unread``: an ink mark waits for the model (the route then schedules a run)."""
    from fork.ink import mark_reading
    from fork.ink import notes as ink

    note = ink.load(owner, note_id)  # the owner check, the pages and the title
    if note is None:
        return None
    marks = _stored_marks(owner, note_id, note)
    readings = mark_reading.readings_of(note)
    now = mark_reading.now_ms()
    unread = any(mark_reading.needs_reading(m, readings.get(m["id"]), now) for m in marks)
    return {"note_id": note_id, "title": note.get("title") or "",
            "marks": [mark_reading.merged(m, note, note_id) for m in marks],
            "unread": unread}


def _pdf_notes_marks(owner: str | None):
    """``(note_id, title, pdf_name, mark)`` for every mark of the owner's PDF notes, the
    mark with what was read in it."""
    from fork.ink import mark_reading
    from fork.ink import notes as ink

    for note in ink.handwritten_notes(owner):
        store = note["owner"] if owner is None else owner
        stored = ink.load_sidecar(store, note["id"])
        pdf = (stored or {}).get("pdf")
        if not isinstance(pdf, dict):
            continue
        title = note.get("title") or ""
        name = str(pdf.get("name") or "")
        for raw in _stored_marks(owner, note["id"], stored):
            yield note["id"], title, name, mark_reading.merged(raw, stored, note["id"])


def search(owner: str | None, q: str, limit: int = 30) -> list[dict[str, Any]]:
    """Marks of every PDF note of the owner matching all words of ``q`` (case-insensitive,
    in the mark's text, near line and written text, the note's title and the PDF's name),
    newest mark first: ``[{"note_id", "title", "pdf_name", "mark"}]`` (the mark with what
    was read in it)."""
    terms = [t for t in str(q or "").lower().split() if t]
    if not terms:
        return []
    found: list[dict[str, Any]] = []
    for note_id, title, name, mark in _pdf_notes_marks(owner):
        about = f"{title} {name}".lower()
        hay = f"{mark['text']} {mark['near']} {mark['written']} {about}".lower()
        if all(t in hay for t in terms):
            found.append({"note_id": note_id, "title": title, "pdf_name": name,
                          "mark": mark})
    found.sort(key=lambda r: r["mark"]["at"], reverse=True)
    return found[:max(0, int(limit))]


# -------------------------------------------------------------- ranked search
# Words a question about marks is made of that say nothing about what is marked.
STOP_WORDS = frozenset("""
a an the and or but if of on in to at by for from with without into onto about as is are
was were be been am do does did done have has had it its this that these those there here
what which who whom whose when where why how i me my mine we our you your he she they them
his her their any some all every each no not can could would should will shall may might
must please tell show find give list say said get got look looking know
note notes noted mark marks marked marking wrote write written writing highlight
highlights highlighted underline underlines underlined circled datasheet datasheets sheet
sheets pdf pdfs page pages document documents
""".split())
TITLE_WEIGHT = 1.0  # a query word found in the datasheet's title or PDF name
_WORD = re.compile(r"\w+(?:[.\-/+]\w+)*")
_SEP = re.compile(r"[.\-/+]")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(str(text or "").lower())


def query_words(q: str) -> list[str]:
    """The words of a loose query that can find a mark: lowercase, each once, without
    stop words and one-letter words ("ZX-100's enable pin" -> zx-100, enable, pin)."""
    out: dict[str, None] = {}
    for t in _tokens(q):
        if len(t) > 1 and t not in STOP_WORDS:
            out[t] = None
    return list(out)


def _token_set(*texts: str) -> set[str]:
    """Tokens of ``texts``, with a joined token's parts and the token without its
    separators (``zx-100`` also gives ``zx``, ``100`` and ``zx100``)."""
    out: set[str] = set()
    for text in texts:
        for t in _tokens(text):
            out.add(t)
            if _SEP.search(t):
                out.update(p for p in _SEP.split(t) if p)
                out.add(_SEP.sub("", t))
    return out


def _found(word: str, tokens: set[str]) -> bool:
    """``word`` is one of ``tokens``, starts one (three letters or more, not a number:
    "volt" finds "voltage", "100" doesn't find "1000"), or is a plural of one ("pins"
    finds "pin"). A word with separators also counts without them ("zx-100" finds
    "zx100")."""
    if word in tokens or _SEP.sub("", word) in tokens:
        return True
    if len(word) >= 3 and word.isalpha() and any(t.startswith(word) for t in tokens):
        return True
    return len(word) > 3 and word.endswith("s") and word[:-1] in tokens


def score(words: Sequence[str], mark: dict[str, Any], title: str = "",
          pdf_name: str = "") -> float:
    """How well a mark answers ``words`` (:func:`query_words`): one for each word found
    in the mark (its PDF text, what was written on it, its near line), plus
    :data:`TITLE_WEIGHT` for each word found in the datasheet's title or PDF name. 0: the
    mark doesn't answer. Pure."""
    in_mark = _token_set(str(mark.get("text") or ""), str(mark.get("written") or ""),
                         str(mark.get("near") or ""))
    about = _token_set(title, re.sub(r"\.pdf$", "", str(pdf_name or ""), flags=re.IGNORECASE))
    hits = sum(1 for w in words if _found(w, in_mark))
    named = sum(1 for w in words if _found(w, about))
    return hits + TITLE_WEIGHT * named


def rank(owner: str | None, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """The owner's marks that best answer a loose ``query``, best first (then newest):
    ``[{"note_id", "title", "pdf_name", "mark", "score"}]``, only marks with a score
    (:func:`score`). Empty when the query has no words left (:func:`query_words`)."""
    words = query_words(query)
    limit = max(0, int(limit))
    if not words or not limit:
        return []
    found: list[dict[str, Any]] = []
    for note_id, title, name, mark in _pdf_notes_marks(owner):
        s = score(words, mark, title, name)
        if s > 0:
            found.append({"note_id": note_id, "title": title, "pdf_name": name,
                          "mark": mark, "score": s})
    found.sort(key=lambda r: (r["score"], r["mark"]["at"]), reverse=True)
    return found[:limit]
