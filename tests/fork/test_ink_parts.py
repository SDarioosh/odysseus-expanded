"""Fork: part-number tags of a datasheet (fork/ink/parts.py), pure: the stored set and
the guesser. The note side (kept across saves, answered, set over HTTP) is in
tests/fork/test_ink_marks_routes.py. Fictional parts only.
"""
from __future__ import annotations

from typing import NamedTuple

import pytest

from fork.ink import parts as pt


class Word(NamedTuple):  # the shape of fork.ink.pdf.Word
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    line: int


# ------------------------------------------------------------------ the set
def test_clean_parts_trims_dedupes_case_insensitively_and_keeps_order():
    got = pt.clean_parts(["  ZX-100 ", "zx-100", "", "ZX 100\tA", "  ", "AB-200", "Zx-100"])
    assert got == ["ZX-100", "ZX 100 A", "AB-200"]
    assert pt.clean_parts([]) == []
    assert pt.clean_parts(["a\x00b"]) == ["ab"]   # control characters dropped


def test_clean_parts_limits():
    assert len(pt.clean_parts([f"P{i}" for i in range(pt.MAX_PARTS)])) == pt.MAX_PARTS
    with pytest.raises(pt.PartsError):
        pt.clean_parts([f"P{i}" for i in range(pt.MAX_PARTS + 1)])
    # Duplicates don't count toward the limit.
    assert len(pt.clean_parts([f"P{i}" for i in range(pt.MAX_PARTS)] + ["p0"])) == pt.MAX_PARTS
    assert pt.clean_parts(["x" * pt.MAX_PART_CHARS]) == ["x" * pt.MAX_PART_CHARS]
    with pytest.raises(pt.PartsError):
        pt.clean_parts(["x" * (pt.MAX_PART_CHARS + 1)])
    for bad in ("ZX-100", None, {"a": 1}, [1], ["ok", None]):
        with pytest.raises(pt.PartsError):
            pt.clean_parts(bad)


def test_stored_parts_is_a_clean_list_or_empty():
    assert pt.stored_parts(None) == [] and pt.stored_parts({}) == []
    assert pt.stored_parts({"parts": "ZX-100"}) == []
    assert pt.stored_parts({"parts": ["ZX-100", 3, "zx-100"]}) == ["ZX-100"]


# ------------------------------------------------------------------ guesser
@pytest.mark.parametrize("token", ["ZX-100", "TPS62130", "LM317T", "2N3904", "AB-200",
                                   "ESP32-S3", "QX7720-Q1", "NE555"])
def test_looks_like_a_part(token):
    assert pt.looks_like_part(token)


@pytest.mark.parametrize("token", [
    "I2C", "X5R", "USB2", "ZX", "2026", "100", "datasheet2", "5.5V", "18uA", "100mA",
    "16-bit", "802.11n", "4K7", "3V3", "3x3mm", "rev2", "v1.2", "SOT-23", "QFN32",
    "TO220", "IP67", "AEC-Q100", "A" * 21 + "12"])
def test_does_not_look_like_a_part(token):
    assert not pt.looks_like_part(token)


def test_guess_from_the_title_line_then_the_file_name():
    words = [Word("ZX-100", 72, 60, 160, 90, 0), Word("Buck", 165, 60, 210, 90, 0),
             Word("Regulator", 215, 60, 330, 90, 0),
             Word("Pin-compatible", 72, 120, 160, 135, 1), Word("AB-200", 165, 120, 210, 135, 1)]
    # The file name's zx100 is ZX-100 again; AB-200 isn't on the largest line.
    assert pt.guess("zx100-datasheet.pdf", words) == ["ZX-100"]
    assert pt.guess("QX7720 rev3.pdf", words) == ["ZX-100", "QX7720"]


def test_guess_from_the_file_name_alone():
    assert pt.guess("zx100-datasheet.pdf") == ["ZX100"]     # lowercase: upper-cased
    assert pt.guess("TPS62130 Datasheet (Rev C).pdf") == ["TPS62130"]
    assert pt.guess("2N3904_ds_rev2.PDF") == ["2N3904"]
    assert pt.guess("ZX-100-EVAL board.pdf") == ["ZX-100"]   # a word end trimmed
    assert pt.guess("Zx100b.pdf") == ["Zx100b"]               # mixed case kept as typed
    assert pt.guess("datasheet.pdf") == [] and pt.guess("") == []


def test_guess_takes_at_most_a_few_and_is_deterministic():
    name = " ".join(f"ZX-{100 + i}" for i in range(10)) + ".pdf"
    assert pt.guess(name) == [f"ZX-{100 + i}" for i in range(pt.MAX_GUESSES)]
    assert pt.guess(name) == pt.guess(name)


def test_the_largest_line_is_the_one_with_the_tallest_word():
    words = [Word("Small", 0, 0, 10, 10, 0), Word("Big", 0, 20, 10, 50, 1),
             Word("Title", 20, 20, 40, 45, 1), Word("Also", 0, 60, 10, 90, 2)]
    assert pt.largest_line(words) == "Big Title"   # first on ties
    assert pt.largest_line([]) == ""
