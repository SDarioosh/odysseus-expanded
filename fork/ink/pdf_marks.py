"""Fork: what the owner marked on a PDF datasheet in the Write view (marks), and a
search over them. Contract: the PDF datasheet plan (``docs/fork/handwritten-notes.md``).

A PDF note (``fork/ink/notes.py``, sidecar ``"pdf"``) has PDF pages; the owner writes on
them with the pens and the highlighter. Here a PDF page's strokes are grouped into
**marks**, each with the PDF text under it (words from ``fork.ink.pdf.page_words``, in
page units: width 1000, origin top-left, y down)::

    {"id": "m-<first stroke id>", "page_id", "page_no", "kind": "highlight" | "underline"
     | "ink" | "circle" | "arrow", "color": "#rrggbb", "box": [x, y, w, h], "text",
     "near", "points_to": bool, "strokes": [stroke ids], "at": <ms, latest stroke>}

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
  with no text above it (a dash), or that is an arrow's shaft (below), is ink.
* **ink**: every other pen stroke. Strokes whose boxes come within ``INK_GAP`` of each
  other are one group (handwriting, arrows, circles). In a group, each stroke is one of:

  - a **circle**: a closed loop (:func:`is_loop`: one stroke whose start and end are at
    most ``CIRCLE_CLOSE`` x its larger side apart, its path at least ``CIRCLE_MIN_PATH``
    x that side, at least ``CIRCLE_MIN`` wide and high) with at least one word centre
    inside it (the loop as a polygon); or one **box** (:func:`is_box`) of 1 to
    ``BOX_MAX_STROKES`` strokes at least ``BOX_STROKE_MIN`` long (the group's first
    ``BOX_CANDIDATES`` of them tried, the most strokes first): their points on its
    border, every side drawn along most of its length, a word centre inside it;
  - an **arrow**: a nearly straight shaft (at least ``ARROW_MIN_LEN`` long, path at most
    ``ARROW_STRAIGHT`` x that) with a head: drawn back from its end in the same stroke
    (the tip is the point farthest from the start), or 1 to ``ARROW_MAX_HEADS`` other
    strokes of the group touching one of its ends (its last point first). A head opens
    backwards: every point within ``ARROW_HEAD_ANGLE`` of the shaft, reaching back
    ``ARROW_HEAD_MIN`` to ``ARROW_HEAD_MAX`` x the shaft's length and sideways at least
    ``ARROW_HEAD_OPEN`` x that. It **points to** the PDF line nearest its tip, within
    ``ARROW_REACH``;
  - **writing**: everything else.

  A group with writing is kind ``ink``: ``text`` is the words whose centre lies inside
  the group's box (a circled value), or inside the writing's box when it has an arrow;
  ``near`` is the line an arrow points to (``points_to`` true), else, when ``text`` is
  empty, the PDF text line closest to that box: the one closest up or down (level with
  it best), then the closest across; both gaps within ``NEAR_MAX``, a gap across
  counting ``NEAR_ACROSS`` as much (a margin note beside a line is near it). Only its
  writing strokes (``_writing``, and their box ``_writing_box``; internal, never
  answered) are read by the vision model (``fork/ink/mark_reading.py``). A group with
  no writing and a circle is kind ``circle`` ("Circled"): ``text`` the circled words,
  ``near`` the line an arrow drawn with it points to (``points_to``). A group that is
  only arrows is kind ``arrow``: ``text`` the line(s) they point to (`` / `` between).
  Circles and arrows are not read by the model. Grouping, ids and kinds of strokes
  that are no circle or arrow are as before.

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
in the datasheet's title, part-number tags (``fork/ink/parts.py``) or PDF name, so every
mark of a datasheet named in the query qualifies.
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
# Circles: a closed loop (one stroke) or a box (up to BOX_MAX_STROKES strokes).
CIRCLE_MIN = 10.0         # a loop or box is at least this wide and this high
CIRCLE_CLOSE = 0.3        # a loop's start and end: at most this x its larger side apart
CIRCLE_MIN_PATH = 2.0     # ... and its path at least this x its larger side (goes round)
BOX_MAX_STROKES = 4
BOX_STROKE_MIN = 20.0     # a stroke that may be a box side: at least this long or high
BOX_CANDIDATES = 6        # at most this many such strokes of a group are tried
BOX_TOL_MIN = 4.0         # a point is on the box's border within max(this, ...
BOX_TOL = 0.15            # ... this x the box's smaller side)
BOX_ON_BORDER = 0.9       # this share of the points is on the border
BOX_COVER = 0.7           # and each side is drawn along this share of its length
# Arrows: a straight shaft and a head (in the same stroke, or 1-2 short strokes).
ARROW_MIN_LEN = 30.0      # the shaft is at least this long ...
ARROW_STRAIGHT = 1.2      # ... and its path at most this x its length
ARROW_HEAD_MAX = 0.6      # the head reaches back at most this x the shaft's length ...
ARROW_HEAD_MIN = 4.0      # ... and at least this far
ARROW_HEAD_OPEN = 0.25    # it opens sideways at least this x how far it reaches back
ARROW_HEAD_ANGLE = 75.0   # every head point within this angle of the shaft (degrees)
ARROW_TIP_TOL = 10.0      # a separate head stroke comes this close to the tip
ARROW_REACH = 40.0        # the line pointed to is at most this far from the tip
ARROW_MAX_HEADS = 2

KINDS = ("highlight", "underline", "ink", "circle", "arrow")

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


def _join_lines(lines: Iterable[str]) -> str:
    """Texts joined by `` / ``, each once."""
    return " / ".join(dict.fromkeys(t for t in lines if t))[:MAX_TEXT]


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
          text: str, near: str, points_to: bool = False) -> dict[str, Any]:
    return {"id": "m-" + str(members[0].get("id")), "page_id": page_id, "page_no": page_no,
            "kind": kind, "color": str((members[0].get("pen") or {}).get("color") or "").lower(),
            "box": [round(box[0], 1), round(box[1], 1), round(box[2] - box[0], 1),
                    round(box[3] - box[1], 1)],
            "text": text, "near": near, "points_to": points_to,
            "strokes": [str(s.get("id")) for s in members],
            "at": max(int(s.get("at") or 0) for s in members)}


# ------------------------------------------------------------ circles and arrows
Point = tuple[float, float]


def _pts(stroke: dict[str, Any]) -> list[Point]:
    return [(float(p[0]), float(p[1])) for p in stroke.get("points") or []]


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _poly_length(pts: Sequence[Point]) -> float:
    return sum(_dist(a, b) for a, b in itertools.pairwise(pts))


def _inside_polygon(x: float, y: float, poly: Sequence[Point]) -> bool:
    """Ray casting; the polygon is closed from its last point to its first."""
    inside = False
    n = len(poly)
    for i in range(n):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            inside = not inside
    return inside


def is_loop(stroke: dict[str, Any]) -> bool:
    """One pen stroke drawn round and closed: at least ``CIRCLE_MIN`` wide and high, its
    start and end at most ``CIRCLE_CLOSE`` x its larger side apart, its path at least
    ``CIRCLE_MIN_PATH`` x its larger side."""
    pts = _pts(stroke)
    if len(pts) < 4:
        return False
    x0, y0, x1, y1 = _points_box(stroke)
    big = max(x1 - x0, y1 - y0)
    if min(x1 - x0, y1 - y0) < CIRCLE_MIN:
        return False
    return (_dist(pts[0], pts[-1]) <= CIRCLE_CLOSE * big
            and _poly_length(pts) >= CIRCLE_MIN_PATH * big)


def _loop_words(words: Sequence[Any], stroke: dict[str, Any]) -> list[Any]:
    poly = _pts(stroke)
    return [w for w in words
            if _inside_polygon((w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2, poly)]


def is_box(strokes: Sequence[dict[str, Any]]) -> bool:
    """1 to ``BOX_MAX_STROKES`` pen strokes drawing a box: their points' box at least
    ``CIRCLE_MIN`` wide and high, ``BOX_ON_BORDER`` of the points within the tolerance
    (``BOX_TOL_MIN``, ``BOX_TOL``) of its border, and each of its four sides drawn along
    ``BOX_COVER`` of its length (segments with both ends near that side)."""
    if not 1 <= len(strokes) <= BOX_MAX_STROKES:
        return False
    paths = [_pts(s) for s in strokes]
    allp = [p for path in paths for p in path]
    if len(allp) < 2:
        return False
    x0, y0 = min(p[0] for p in allp), min(p[1] for p in allp)
    x1, y1 = max(p[0] for p in allp), max(p[1] for p in allp)
    w, h = x1 - x0, y1 - y0
    if min(w, h) < CIRCLE_MIN:
        return False
    tol = max(BOX_TOL_MIN, BOX_TOL * min(w, h))
    on = sum(1 for x, y in allp if min(abs(x - x0), abs(x - x1), abs(y - y0), abs(y - y1)) <= tol)
    if on < BOX_ON_BORDER * len(allp):
        return False
    # (which coordinate is along the side, its line's other coordinate, the side's span)
    sides = ((0, 1, y0, (x0, x1)), (0, 1, y1, (x0, x1)), (1, 0, x0, (y0, y1)),
             (1, 0, x1, (y0, y1)))
    for along, across, at, (lo, hi) in sides:
        spans = sorted((min(a[along], b[along]), max(a[along], b[along]))
                       for path in paths for a, b in itertools.pairwise(path)
                       if abs(a[across] - at) <= tol and abs(b[across] - at) <= tol)
        covered, end = 0.0, lo
        for a, b in spans:
            a, b = max(a, end), min(b, hi)
            if b > a:
                covered += b - a
                end = b
        if covered < BOX_COVER * (hi - lo):
            return False
    return True


def _box_of_points(strokes: Sequence[dict[str, Any]]) -> Box:
    box = _points_box(strokes[0])
    for s in strokes[1:]:
        box = _union(box, _points_box(s))
    return box


def _head_fits(head: Sequence[Point], tip: Point, back: Point, length: float,
               *, touch: bool) -> bool:
    """Whether ``head`` points (an arrow head, or one of its barbs) open backwards from
    ``tip`` along ``back`` (unit vector from the tip towards the tail) as a head of a
    shaft ``length`` long. ``touch``: one point must be within ``ARROW_TIP_TOL`` of the
    tip (a separate head stroke)."""
    if len(head) < 2:
        return False
    if touch and min(_dist(p, tip) for p in head) > ARROW_TIP_TOL:
        return False
    slope = math.tan(math.radians(ARROW_HEAD_ANGLE))
    reach = opening = 0.0
    for x, y in head:
        vx, vy = x - tip[0], y - tip[1]
        along = vx * back[0] + vy * back[1]
        lateral = abs(vx * back[1] - vy * back[0])
        if along < -ARROW_TIP_TOL or lateral > along * slope + ARROW_TIP_TOL:
            return False
        reach, opening = max(reach, along), max(opening, lateral)
    if reach > ARROW_HEAD_MAX * length:
        return False
    return reach >= ARROW_HEAD_MIN and opening >= ARROW_HEAD_OPEN * reach


def _straight(pts: Sequence[Point]) -> float:
    """The length of a nearly straight path of at least ``ARROW_MIN_LEN``, else 0."""
    if len(pts) < 2:
        return 0.0
    chord = _dist(pts[0], pts[-1])
    if chord < ARROW_MIN_LEN or _poly_length(pts) > ARROW_STRAIGHT * chord:
        return 0.0
    return chord


def _self_arrow(stroke: dict[str, Any]) -> Point | None:
    """The tip of a one-stroke arrow (a shaft, then the head drawn back from its end),
    else ``None``. The tip is the point farthest from the start."""
    pts = _pts(stroke)
    if len(pts) < 3:
        return None
    k = max(range(len(pts)), key=lambda i: (_dist(pts[0], pts[i]), -i))
    length = _straight(pts[:k + 1])
    if not length or k == len(pts) - 1:
        return None
    tip = pts[k]
    back = ((pts[0][0] - tip[0]) / length, (pts[0][1] - tip[1]) / length)
    return tip if _head_fits(pts[k:], tip, back, length, touch=False) else None


def _arrow_heads(shaft: dict[str, Any], others: Sequence[dict[str, Any]]
                 ) -> tuple[Point, list[dict[str, Any]]] | None:
    """``(tip, head strokes)`` when ``shaft`` is a straight stroke with 1 to
    ``ARROW_MAX_HEADS`` of ``others`` drawn as a head at one of its ends (its last point
    tried first), else ``None``."""
    pts = _pts(shaft)
    length = _straight(pts)
    if not length:
        return None
    for tip, tail in ((pts[-1], pts[0]), (pts[0], pts[-1])):
        back = ((tail[0] - tip[0]) / length, (tail[1] - tip[1]) / length)
        heads = [o for o in others
                 if _head_fits(_pts(o), tip, back, length, touch=True)][:ARROW_MAX_HEADS]
        if heads:
            return tip, heads
    return None


def _line_at(words: Sequence[Any], tip: Point) -> str:
    """The text of the PDF line nearest to an arrow's tip, if within ``ARROW_REACH``
    (the first such line on ties)."""
    lines: dict[Any, list[Any]] = {}
    for w in words:
        lines.setdefault(w.line, []).append(w)
    best, best_d = None, None
    for line_words in lines.values():
        lb = (min(w.x0 for w in line_words), min(w.y0 for w in line_words),
              max(w.x1 for w in line_words), max(w.y1 for w in line_words))
        d = _gap(lb, (tip[0], tip[1], tip[0], tip[1]))
        if d <= ARROW_REACH and (best_d is None or d < best_d):
            best, best_d = line_words, d
    return _join(best) if best else ""


def _shapes(group: list[dict[str, Any]], words: Sequence[Any]
            ) -> tuple[list[Any], list[Point], set[int]]:
    """What an ink group draws besides writing: ``(circled words, arrow tips, indexes of
    the strokes that are circles or arrows)``. Closed loops first, then one box, then
    arrows; each stroke is one thing."""
    used: set[int] = set()
    circled: list[Any] = []
    for i, s in enumerate(group):
        if is_loop(s):
            inside = _loop_words(words, s)
            if inside:
                used.add(i)
                circled.extend(inside)
    sides = [i for i, s in enumerate(group) if i not in used and _may_be_side(s)]
    box = _find_box(group, sides[:BOX_CANDIDATES], words)
    if box is not None:
        combo, inside = box
        used.update(combo)
        circled.extend(inside)
    tips: list[Point] = []
    for i, s in enumerate(group):
        if i in used:
            continue
        tip = _self_arrow(s)
        if tip is not None:
            used.add(i)
            tips.append(tip)
            continue
        free = [(j, o) for j, o in enumerate(group) if j != i and j not in used]
        got = _arrow_heads(s, [o for _, o in free])
        if got is not None:
            tip, heads = got
            used.add(i)
            used.update(j for j, o in free if any(o is h for h in heads))
            tips.append(tip)
    ids = {id(w) for w in circled}
    return [w for w in words if id(w) in ids], tips, used  # reading order, each once


def _may_be_side(stroke: dict[str, Any]) -> bool:
    x0, y0, x1, y1 = _points_box(stroke)
    return max(x1 - x0, y1 - y0) >= BOX_STROKE_MIN and not is_loop(stroke)


def _find_box(strokes: Sequence[dict[str, Any]], candidates: Sequence[int],
              words: Sequence[Any], must: int | None = None
              ) -> tuple[tuple[int, ...], list[Any]] | None:
    """``(indexes, words inside)`` of the first box (:func:`is_box`, most strokes first,
    then in order) made of ``candidates`` (indexes into ``strokes``, with ``must`` among
    them when given) round at least one word centre, else ``None``."""
    for n in range(min(BOX_MAX_STROKES, len(candidates)), 0, -1):
        for combo in itertools.combinations(candidates, n):
            if must is not None and must not in combo:
                continue
            chosen = [strokes[i] for i in combo]
            if is_box(chosen):
                inside = _words_in(words, _box_of_points(chosen))
                if inside:
                    return combo, inside
    return None


def _boxes_words(stroke: dict[str, Any], pens: Sequence[dict[str, Any]],
                 words: Sequence[Any]) -> bool:
    """Whether ``stroke`` is a side of a box drawn round words with pen strokes near it
    (within ``INK_GAP``), so it is no underline."""
    if not _may_be_side(stroke):
        return False
    box = _drawn_box(stroke)
    near = [o for o in pens if o is not stroke and _may_be_side(o)
            and _gap(box, _drawn_box(o)) <= INK_GAP][:BOX_CANDIDATES - 1]
    return _find_box([stroke, *near], range(len(near) + 1), words, must=0) is not None


def _is_arrow_stroke(stroke: dict[str, Any], others: Sequence[dict[str, Any]]) -> bool:
    """A pen stroke that is an arrow's shaft (with its head in it or among ``others``)."""
    return _self_arrow(stroke) is not None or _arrow_heads(stroke, others) is not None


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
        if under and (_is_arrow_stroke(s, [o for o in pens if o is not s])
                      or _boxes_words(s, pens, words)):
            under = []  # an arrow's shaft or a box's side, not an underline
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
        members = [ink[i] for i in group]
        circled, tips, used = _shapes(members, words)
        pointed = [t for t in (_line_at(words, tip) for tip in tips) if t]
        writing = [m for j, m in enumerate(members) if j not in used]
        if writing:  # handwriting (perhaps with an arrow or a circle): read by the model
            wbox = _drawn_box(writing[0])
            for m in writing[1:]:
                wbox = _union(wbox, _drawn_box(m))
            # An arrow's strokes don't count for the text under it or the line beside it.
            arrows = bool(tips)
            tbox = wbox if arrows else box
            inside = _words_in(words, tbox)
            text = _join(inside)
            near = pointed[0] if pointed else ("" if inside else _near_line(words, tbox))
            mark = _mark("ink", members, box, page_id, page_no, text, near,
                         points_to=bool(pointed))
            mark["_writing"] = [str(m.get("id")) for m in writing]
            mark["_writing_box"] = [round(wbox[0], 1), round(wbox[1], 1),
                                    round(wbox[2] - wbox[0], 1), round(wbox[3] - wbox[1], 1)]
        elif circled:
            near = pointed[0] if pointed else ""
            mark = _mark("circle", members, box, page_id, page_no, _join(circled), near,
                         points_to=bool(pointed))
        else:  # only arrows
            mark = _mark("arrow", members, box, page_id, page_no,
                         _join_lines(pointed), "")
        marks.append(mark)

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
    """``(note_id, title, pdf_name, parts, mark)`` for every mark of the owner's PDF
    notes, the mark with what was read in it, ``parts`` the note's part-number tags."""
    from fork.ink import mark_reading
    from fork.ink import notes as ink
    from fork.ink import parts as ink_parts

    for note in ink.handwritten_notes(owner):
        store = note["owner"] if owner is None else owner
        stored = ink.load_sidecar(store, note["id"])
        pdf = (stored or {}).get("pdf")
        if not isinstance(pdf, dict):
            continue
        title = note.get("title") or ""
        name = str(pdf.get("name") or "")
        parts = ink_parts.stored_parts(stored)
        for raw in _stored_marks(owner, note["id"], stored):
            yield note["id"], title, name, parts, mark_reading.merged(raw, stored, note["id"])


def search(owner: str | None, q: str, limit: int = 30) -> list[dict[str, Any]]:
    """Marks of every PDF note of the owner matching all words of ``q`` (case-insensitive,
    in the mark's text, near line and written text, the note's title, its part numbers
    and the PDF's name), newest mark first: ``[{"note_id", "title", "pdf_name", "parts",
    "mark"}]`` (the mark with what was read in it)."""
    terms = [t for t in str(q or "").lower().split() if t]
    if not terms:
        return []
    found: list[dict[str, Any]] = []
    for note_id, title, name, parts, mark in _pdf_notes_marks(owner):
        about = f"{title} {name} {' '.join(parts)}".lower()
        hay = f"{mark['text']} {mark['near']} {mark['written']} {about}".lower()
        if all(t in hay for t in terms):
            found.append({"note_id": note_id, "title": title, "pdf_name": name,
                          "parts": parts, "mark": mark})
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
highlights highlighted underline underlines underlined circled circle circles arrow arrows
datasheet datasheets sheet
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
          pdf_name: str = "", parts: Sequence[str] = ()) -> float:
    """How well a mark answers ``words`` (:func:`query_words`): one for each word found
    in the mark (its PDF text, what was written on it, its near line), plus
    :data:`TITLE_WEIGHT` for each word found in the datasheet's title, part numbers or
    PDF name. 0: the mark doesn't answer. Pure."""
    in_mark = _token_set(str(mark.get("text") or ""), str(mark.get("written") or ""),
                         str(mark.get("near") or ""))
    about = _token_set(title, re.sub(r"\.pdf$", "", str(pdf_name or ""), flags=re.IGNORECASE),
                       *(str(p) for p in parts or ()))
    hits = sum(1 for w in words if _found(w, in_mark))
    named = sum(1 for w in words if _found(w, about))
    return hits + TITLE_WEIGHT * named


def rank(owner: str | None, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """The owner's marks that best answer a loose ``query``, best first (then newest):
    ``[{"note_id", "title", "pdf_name", "parts", "mark", "score"}]``, only marks with a score
    (:func:`score`). Empty when the query has no words left (:func:`query_words`)."""
    words = query_words(query)
    limit = max(0, int(limit))
    if not words or not limit:
        return []
    found: list[dict[str, Any]] = []
    for note_id, title, name, parts, mark in _pdf_notes_marks(owner):
        s = score(words, mark, title, name, parts)
        if s > 0:
            found.append({"note_id": note_id, "title": title, "pdf_name": name,
                          "parts": parts, "mark": mark, "score": s})
    found.sort(key=lambda r: (r["score"], r["mark"]["at"]), reverse=True)
    return found[:limit]
