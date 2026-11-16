"""Fork: marks on a PDF datasheet page (fork/ink/pdf_marks.py ``page_marks``), pure.

Hand-made strokes and words in page units (width 1000, y down); a fictional part.
"""
from __future__ import annotations

from typing import NamedTuple

from fork.ink import pdf_marks as pm

PEN = {"color": "#1f5fbf", "width": 2.5, "highlighter": False}
RED = {"color": "#d0021b", "width": 2.5, "highlighter": False}
YELLOW = {"color": "#FFD400", "width": 14.0, "highlighter": True}
GREEN = {"color": "#7ed321", "width": 14.0, "highlighter": True}


class Word(NamedTuple):  # the shape of fork.ink.pdf.Word
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    line: int


def line_words(line: int, y0: float, words: list[tuple[str, float, float]]) -> list[Word]:
    return [Word(t, x0, y0, x1, y0 + 16, line) for t, x0, x1 in words]


# Three text lines of a fictional datasheet, 16 units high, 34 apart.
WORDS = (
    line_words(0, 200, [("Input", 120, 165), ("voltage", 170, 230), ("range", 235, 280),
                        ("2.7", 285, 310), ("V", 314, 324), ("to", 328, 345),
                        ("5.5", 349, 374), ("V", 378, 388)])
    + line_words(1, 234, [("Output", 120, 180), ("current", 185, 245), ("up", 250, 268),
                          ("to", 272, 289), ("1.2", 293, 318), ("A", 322, 332)])
    + line_words(2, 268, [("Quiescent", 120, 200), ("current", 205, 265), ("18", 270, 290),
                          ("uA", 294, 314), ("typical", 318, 375)])
)


def stroke(sid, pen, pts, at=0):
    return {"id": sid, "pen": pen, "at": at, "points": [[x, y, i * 16] for i, (x, y) in enumerate(pts)]}


def hline(sid, pen, x0, x1, y, at=0):
    return stroke(sid, pen, [(x0, y), ((x0 + x1) / 2, y + 1), (x1, y)], at)


def marks(*strokes, words=WORDS):
    return pm.page_marks({"id": "pd000000000001", "strokes": list(strokes)}, 3, words)


# ------------------------------------------------------------------ highlights
def test_a_highlight_takes_the_words_under_it():
    (m,) = marks(hline("h1", YELLOW, 280, 392, 208, at=500))
    assert m["kind"] == "highlight"
    assert m["text"] == "2.7 V to 5.5 V"
    assert m["near"] == ""
    assert m["id"] == "m-h1" and m["strokes"] == ["h1"]
    assert m["page_id"] == "pd000000000001" and m["page_no"] == 3
    assert m["color"] == "#ffd400" and m["at"] == 500
    x, y, w, h = m["box"]
    assert (x, y, w, h) == (273.0, 201.0, 126.0, 15.0)  # points plus half the pen


def test_touching_highlights_of_one_colour_merge_also_through_a_chain():
    wide = {**YELLOW, "width": 28.0}   # as tall as a text line: boxes 5 units apart
    a = hline("h1", wide, 120, 230, 208, at=1)
    b = hline("h2", wide, 120, 236, 242, at=3)
    c = hline("h3", wide, 120, 200, 276, at=2)
    (m,) = marks(a, b, c)
    assert m["strokes"] == ["h1", "h2", "h3"] and m["id"] == "m-h1" and m["at"] == 3
    assert m["text"] == "Input voltage Output current Quiescent"


def test_highlights_apart_or_of_another_colour_stay_apart():
    a = hline("h1", YELLOW, 120, 230, 208)
    far = hline("h2", YELLOW, 600, 700, 208)               # same line, far to the right
    other = hline("h3", GREEN, 285, 392, 208)               # touches h1, another colour
    got = marks(a, far, other)
    assert [m["strokes"] for m in got] == [["h1"], ["h3"], ["h2"]]   # y, then x
    assert [m["text"] for m in got] == ["Input voltage", "2.7 V to 5.5 V", ""]


def test_a_highlight_gap_just_over_the_limit_does_not_merge():
    a = hline("h1", YELLOW, 100, 200, 208)
    b = hline("h2", YELLOW, 200 + 14 + pm.HIGHLIGHT_GAP + 1, 400, 208)
    assert len(marks(a, b, words=[])) == 2
    b = hline("h2", YELLOW, 200 + 14 + pm.HIGHLIGHT_GAP - 1, 400, 208)
    assert len(marks(a, b, words=[])) == 1


# ------------------------------------------------------------------ underlines
def test_an_underline_takes_the_text_line_just_above_it():
    (m,) = marks(hline("u1", RED, 290, 390, 222))   # under "2.7 V to 5.5 V" (bottom 216)
    assert m["kind"] == "underline"
    assert m["text"] == "2.7 V to 5.5 V"
    assert m["color"] == "#d0021b" and m["near"] == ""


def test_an_underline_takes_only_words_it_covers_and_only_the_nearest_line():
    # Bottom of line 1 is 250; line 0 (bottom 216) is also within reach of nothing.
    (m,) = marks(hline("u1", PEN, 118, 248, 253))
    assert m["text"] == "Output current"
    # Just touching the start of "up" (250..268) is not enough to take it.
    (m,) = marks(hline("u1", PEN, 118, 254, 253))
    assert m["text"] == "Output current"


def test_a_slanted_short_or_zigzag_line_is_not_an_underline():
    steep = stroke("s1", PEN, [(120, 220), (220, 250)])          # 30 high
    short = hline("s2", PEN, 300, 320, 222)                       # 20 wide
    zigzag = stroke("s3", PEN, [(120, 220), (240, 222), (130, 224), (240, 226)])
    for s in (steep, short, zigzag):
        assert not pm.is_underline(s)
        assert [m["kind"] for m in marks(s)] == ["ink"]
    assert pm.is_underline(hline("ok", PEN, 120, 240, 222))
    assert not pm.is_underline(hline("hl", YELLOW, 120, 240, 222))


def test_an_underline_shape_with_no_text_above_is_ink():
    (m,) = marks(hline("u1", PEN, 600, 700, 500))
    assert m["kind"] == "ink" and m["text"] == ""


def test_an_underline_far_below_the_text_is_ink():
    # 30 units under line 2's bottom (284): more than UNDERLINE_ABOVE.
    (m,) = marks(hline("u1", PEN, 120, 300, 314))
    assert m["kind"] == "ink" and m["near"] == "Quiescent current 18 uA typical"


# ------------------------------------------------------------------------- ink
def test_handwriting_strokes_close_together_are_one_ink_mark_with_the_nearest_line():
    letters = [stroke(f"w{i}", PEN, [(300 + i * 20, 330), (310 + i * 20, 345), (315 + i * 20, 332)],
                      at=10 + i) for i in range(4)]
    (m,) = marks(*letters)
    assert m["kind"] == "ink"
    assert m["strokes"] == ["w0", "w1", "w2", "w3"] and m["id"] == "m-w0" and m["at"] == 13
    assert m["text"] == ""
    assert m["near"] == "Quiescent current 18 uA typical"   # 46 units below line 2


def test_ink_far_from_any_text_has_no_near_line():
    (m,) = marks(stroke("w1", PEN, [(600, 700), (640, 720)]))
    assert m["text"] == "" and m["near"] == ""


def test_ink_groups_split_when_far_apart():
    a = stroke("a", PEN, [(600, 400), (620, 420)])
    b = stroke("b", PEN, [(620 + pm.INK_GAP + 10, 400), (700, 420)])
    c = stroke("c", PEN, [(600, 600), (620, 620)])
    got = marks(a, b, c, words=[])
    assert [m["strokes"] for m in got] == [["a"], ["b"], ["c"]]


def test_a_circled_value_takes_the_words_inside():
    # A loop around "1.2 A" (293..332, 234..250), then an arrow from it to a note.
    circle = stroke("c1", RED, [(286, 228), (340, 226), (344, 242), (338, 256),
                                (290, 257), (284, 242), (288, 229)], at=7)
    arrow = stroke("c2", RED, [(344, 242), (420, 242)], at=8)   # touches the circle
    (m,) = marks(circle, arrow)
    assert m["kind"] == "ink" and m["color"] == "#d0021b"
    assert m["strokes"] == ["c1", "c2"]
    assert m["text"] == "1.2 A"   # the arrow's box reaches no further word centres
    assert m["near"] == ""


# -------------------------------------------------------------------- the page
def test_marks_are_ordered_by_y_then_x_and_kinds_are_separate():
    got = marks(
        stroke("ink", PEN, [(600, 330), (640, 345)]),
        hline("u", RED, 120, 245, 253),
        hline("h", YELLOW, 285, 392, 208),
        stroke("left", PEN, [(50, 330), (80, 345)]),
    )
    assert [m["id"] for m in got] == ["m-h", "m-u", "m-left", "m-ink"]
    assert [m["kind"] for m in got] == ["highlight", "underline", "ink", "ink"]
    for m in got:
        assert set(m) == {"id", "page_id", "page_no", "kind", "color", "box", "text",
                          "near", "strokes", "at"}


def test_a_page_without_strokes_or_words():
    assert pm.page_marks({"id": "pd1", "strokes": []}, 1, WORDS) == []
    (m,) = pm.page_marks({"id": "pd1", "strokes": [hline("h", YELLOW, 10, 90, 10)]}, 1, [])
    assert m["text"] == "" and m["near"] == ""


def test_the_same_strokes_give_the_same_marks():
    s = [hline("h", YELLOW, 285, 392, 208), stroke("w", PEN, [(600, 330), (640, 345)])]
    assert marks(*s) == marks(*s)


def test_lines_highlighted_to_different_lengths_take_only_their_own_words():
    wide = {**YELLOW, "width": 28.0}
    short = hline("h1", wide, 125, 160, 208)          # "Input" only
    long = hline("h2", wide, 125, 300, 242)           # "Output current up to 1.2"
    (m,) = marks(short, long)
    assert m["text"] == "Input Output current up to 1.2"


def test_a_margin_note_beside_a_line_is_near_that_line():
    # Written in the right margin (x 700..780), level with line 2 (234..250): 368 units
    # across from the end of the line, counting 92: too far. At x 600 it is near it.
    (m,) = marks(stroke("w0", PEN, [(700, 236), (740, 248), (780, 238)]))
    assert m["near"] != "Output current up to 1.2 A"
    # Level with line 2, 268 across (67): near line 2, though line 1 ends nearer across.
    (m,) = marks(stroke("w1", PEN, [(600, 236), (640, 248), (680, 238)]))
    assert m["kind"] == "ink" and m["near"] == "Output current up to 1.2 A"
    # The same note a few lines lower is near nothing.
    (m,) = marks(stroke("w2", PEN, [(600, 420), (640, 432), (680, 422)]))
    assert m["near"] == ""
