"""Fork: part-number tags of a PDF datasheet in Write (``docs/fork/datasheets.md``,
"Part-number tags").

A datasheet note (``fork/ink/notes.py``, a sidecar with ``"pdf"``) may carry the part
numbers it is about, note level::

    "parts": ["ZX-100", "ZX-100A"]

:func:`clean_parts` is what may be stored: at most ``MAX_PARTS`` tags, each 1 to
``MAX_PART_CHARS`` characters after trimming (inner whitespace collapsed, control
characters dropped, empty ones left out), unique case-insensitively (the first spelling
kept), in the owner's order.

:func:`guess` suggests tags when a PDF is imported (``add_pdf``): tokens that look like
part numbers, first from the first page's largest-font line (the datasheet's title,
spelled as printed), then from the file name. A token looks like a part number when
(:func:`looks_like_part`):

* it is 3 to 20 characters of letters and digits, inner ``-``, ``.``, ``/`` or ``_``
  allowed, with at least one letter and at least two digits (ZX-100, TPS62130, LM317T,
  2N3904; not I2C, X5R, USB2);
* it is not a value with a unit (``5.5V``, ``18uA``, ``100mA``, ``16-bit``, ``802.11n``),
  an R/K/V-notation value (``4K7``, ``3V3``), a size (``3x3mm``), a version (``rev2``,
  ``v1.2``), a package (``SOT-23``, ``QFN32``, ``TO220``) or a rating (``IP67``,
  ``AEC-Q100``);
* it has no run of five or more lowercase letters (``datasheet2``).

Words of the file name are split on spaces, ``_``, brackets and commas; a hyphenated
token loses whole-word ends of four letters or more (``zx100-datasheet`` -> ``zx100``,
``ZX-100-EVAL`` -> ``ZX-100``). A file-name token in lowercase only is upper-cased
(``lm317t.pdf`` -> ``LM317T``). Guesses are unique also without separators (``ZX100``
is not suggested beside ``ZX-100``); at most ``MAX_GUESSES``. Pure.

The largest-font line is the line whose tallest word box is tallest (``Word`` boxes of
``fork.ink.pdf.page_words`` are as tall as the font), the first such line on ties.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any

MAX_PARTS = 20
MAX_PART_CHARS = 40
MAX_GUESSES = 5
GUESS_MIN, GUESS_MAX = 3, 20   # characters of a guessed part number
GUESS_MIN_DIGITS = 2

_PART = re.compile(r"^[A-Za-z0-9]+(?:[-./_][A-Za-z0-9]+)*$")
_SEP = re.compile(r"[-./_]")
_UNIT_VALUE = re.compile(r"^\d+(?:[.,]\d+)?[a-zµΩ%]{1,4}$", re.IGNORECASE)
_RKV_VALUE = re.compile(r"^\d+[RKVMrkvm]\d+$")
_SIZE = re.compile(r"^\d+(?:\.\d+)?x\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)?(?:mm|mil|in)?$",
                   re.IGNORECASE)
_VERSION = re.compile(r"^(?:rev|ver|v|r)\.?\d+(?:\.\d+)*[a-z]?$", re.IGNORECASE)
_PACKAGE = re.compile(r"^(?:SOT|SOIC|SSOP|TSSOP|MSOP|QSOP|QFN|DFN|VQFN|WQFN|LQFP|TQFP|QFP|"
                      r"BGA|LGA|WLCSP|CSP|DIP|PDIP|SOP|SC|TO|DPAK|SMA|SMB|SMC|SOD|USON|"
                      r"WSON|SON)-?\d+[A-Z]?$", re.IGNORECASE)
_RATING = re.compile(r"^(?:IP\d\d[A-Z]?|AEC-?Q\d{3}|ISO\d+|IEC\d+|UL\d+|EN\d+)$",
                     re.IGNORECASE)
_LOWER_RUN = re.compile(r"[a-z]{5,}")
_WORD_END = re.compile(r"^[A-Za-z]{4,}$")
_SPLIT_LINE = re.compile(r"[\s,;:()\[\]{}<>\"'|]+")
_SPLIT_NAME = re.compile(r"[\s_,;:()\[\]{}<>\"'|+]+")
_STRIP = "-./_*#~!?"


class PartsError(ValueError):
    """``parts`` that can't be stored (the route answers 400)."""


# ------------------------------------------------------------------ the set
def _clean_one(value: str) -> str:
    text = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C" or ch in " \t")
    return " ".join(text.split())


def clean_parts(value: Any) -> list[str]:
    """The tags as stored (see the module docstring). Raises :class:`PartsError` when
    ``value`` isn't a list of strings, a tag is longer than ``MAX_PART_CHARS`` or there
    are more than ``MAX_PARTS``."""
    if not isinstance(value, list):
        raise PartsError("parts must be a list of part numbers")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise PartsError("each part number must be text")
        part = _clean_one(item)
        if not part:
            continue
        if len(part) > MAX_PART_CHARS:
            raise PartsError(f"a part number is at most {MAX_PART_CHARS} characters")
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(part)
    if len(out) > MAX_PARTS:
        raise PartsError(f"at most {MAX_PARTS} part numbers")
    return out


def stored_parts(stored: dict[str, Any] | None) -> list[str]:
    """The tags of a sidecar as answered: a clean list, ``[]`` when it has none (or a
    damaged field)."""
    value = (stored or {}).get("parts")
    if not isinstance(value, list):
        return []
    try:
        return clean_parts([v for v in value if isinstance(v, str)])[:MAX_PARTS]
    except PartsError:
        return []


# ------------------------------------------------------------------ the guesser
def looks_like_part(token: str) -> bool:
    """Whether ``token`` (already trimmed) looks like a part number."""
    if not GUESS_MIN <= len(token) <= GUESS_MAX or not _PART.match(token):
        return False
    if not any(c.isalpha() for c in token):
        return False
    if sum(c.isdigit() for c in token) < GUESS_MIN_DIGITS:
        return False
    bare = _SEP.sub("", token)
    if (_UNIT_VALUE.match(bare) or _UNIT_VALUE.match(token) or _RKV_VALUE.match(bare)
            or _SIZE.match(token) or _VERSION.match(token) or _PACKAGE.match(token)
            or _RATING.match(token)):
        return False
    return not _LOWER_RUN.search(token)


def _trim_word_ends(token: str) -> str:
    """A hyphenated token without whole-word ends (``zx100-datasheet`` -> ``zx100``)."""
    pieces = token.split("-")
    while len(pieces) > 1 and _WORD_END.match(pieces[-1]):
        pieces.pop()
    while len(pieces) > 1 and _WORD_END.match(pieces[0]):
        pieces.pop(0)
    return "-".join(pieces)


def _candidates(text: str, splitter: re.Pattern[str], *, upper: bool) -> list[str]:
    out = []
    for raw in splitter.split(text or ""):
        token = _trim_word_ends(raw.strip(_STRIP))
        if looks_like_part(token):
            out.append(token.upper() if upper and token == token.lower() else token)
    return out


def largest_line(words: Sequence[Any]) -> str:
    """The text of the line whose tallest word is tallest (first on ties), ``""``."""
    lines: dict[Any, list[Any]] = {}
    for w in words:
        lines.setdefault(w.line, []).append(w)
    best, best_h = None, 0.0
    for line_words in lines.values():
        h = max(float(w.y1) - float(w.y0) for w in line_words)
        if h > best_h:
            best, best_h = line_words, h
    return " ".join(str(w.text).strip() for w in best or [] if str(w.text).strip())


def guess(file_name: str, first_page_words: Iterable[Any] = ()) -> list[str]:
    """Suggested tags for an imported PDF (see the module docstring)."""
    stem = re.sub(r"\.pdf$", "", str(file_name or ""), flags=re.IGNORECASE)
    found = (_candidates(largest_line(list(first_page_words)), _SPLIT_LINE, upper=False)
             + _candidates(stem, _SPLIT_NAME, upper=True))
    out: list[str] = []
    seen: set[str] = set()
    for token in found:
        key = _SEP.sub("", token).casefold()
        if key not in seen:
            seen.add(key)
            out.append(token)
    return out[:MAX_GUESSES]


# ------------------------------------------------------------------ the owner
def set_parts(owner: str | None, note_id: str, value: Any) -> list[str]:
    """Store the owner's tags of a datasheet (:func:`clean_parts`) and return them.
    Raises ``InkError``: 404 not the owner's handwritten note, 400 not a datasheet or
    bad ``parts``. The strokes and ``edited_at`` don't change."""
    from fork.ink import notes as ink

    try:
        parts = clean_parts(value)
    except PartsError as exc:
        raise ink.InkError(str(exc)) from None
    if not ink.valid_note_id(note_id) or not ink.existing_note_ids(owner, [note_id]):
        raise ink.InkError("Note not found", 404)
    store = ink._store_owner(owner, note_id)
    with ink._lock(store):
        stored = ink.load_sidecar(store, note_id)
        if stored is None:
            raise ink.InkError("Note not found", 404)
        if not ink.is_pdf_note(stored):
            raise ink.InkError("Only a datasheet has part numbers")
        stored["parts"] = parts
        ink._write_json(ink.sidecar_path(store, note_id), stored)
    return parts
