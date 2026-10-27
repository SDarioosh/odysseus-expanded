#!/usr/bin/env python3
"""Fork: handwritten notes, stage H5: how well the handwriting recognizer reads the
owner's pages. Plan: docs/fork/handwritten-notes.md ("Phases", "Tests and measures");
the recognizer: ``src/ink_transcribe.py``; sidecars: ``src/ink_notes.py``.

The set is every page the owner reviewed: (page strokes, reviewed text) pairs from the
sidecars at ``<DATA_DIR>/ink/<owner>/<note_id>.json``. A page counts when its status is
``reviewed``, it has ink, its reviewed text is for its current strokes (``strokes_hash ==
writing_hash(page)``) and isn't empty. Pages accepted automatically (``transcript.auto``,
H6) are not ground truth and are skipped; photo pages (``page.photo``, H6) are skipped
unless ``--include-photos``.

Modes:

* default: each page is read again with the recognizer (``read_page`` with the hub's
  vision model, ``default_reader``; or ``--model`` / ``--base-url``, local endpoints
  only) and timed. Run it against the test instance's DATA_DIR, not production.
* ``--stored``: no model calls; scores the model text a review replaced
  (``transcript.machine``, kept by ``save_review`` for the same strokes).
* ``--fixtures``: fictional pages in a temp DATA_DIR and a fake reader (sandbox; no
  model). Combine with ``--stored`` to score the fixtures' stored machine text.

Metrics (Levenshtein edits / reference length):

* **CER** over characters (Unicode code points after NFC), **WER** over words; overall
  is micro (total edits / total reference length), per page median and p90.
* **default** normalization: line breaks unified, each line stripped, whitespace runs
  collapsed to one space, blank lines dropped (a line break is one character).
  **loose**: also case-folded, punctuation dropped and line breaks read as spaces.
* ``[?]`` marks in the model text are counted. For pages **without** ``[?]`` (the pages
  ``INK_AUTO_SEND`` would send without review): their number, CER, WER and the share
  read exactly right (default normalization). That share is the auto-send number.
* Seconds per page (mean, p90) in re-read mode; pages the model failed on are counted
  and left out of the error rates.

Privacy: the output holds numbers and note/page ids only, never page text (these are
the owner's notes). ``--show-diffs`` prints per-page line diffs, locally, on request.

Exit code 0; 2 when there are no pairs; 3 when no local model can read the pages.

Usage:
    venv/bin/python scripts/ink_eval.py --fixtures
    venv/bin/python scripts/ink_eval.py --stored
    venv/bin/python scripts/ink_eval.py --data-dir ~/odysseus-test-data --owner alice
    venv/bin/python scripts/ink_eval.py --json --out /tmp/ink-eval.json --limit 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Hashable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

UNREADABLE = "[?]"  # as src/ink_transcribe.py UNREADABLE


# ------------------------------------------------------------------ edit distance
def levenshtein(a: Sequence[Hashable], b: Sequence[Hashable]) -> int:
    """Exact Levenshtein distance (insert, delete, substitute, each 1) between two
    sequences of characters or words.

    Myers' / Hyyrö's bit-parallel algorithm with Python's big integers: one pass over the
    longer sequence, a handful of integer operations on ``len(shorter)``-bit numbers per
    step, so two 20,000-character pages take well under a second, where the plain
    two-row table would take minutes."""
    if len(a) < len(b):
        a, b = b, a
    m = len(b)
    if m == 0:
        return len(a)
    peq: dict[Hashable, int] = {}
    for i, sym in enumerate(b):
        peq[sym] = peq.get(sym, 0) | (1 << i)
    mask = (1 << m) - 1
    last = 1 << (m - 1)
    pv, mv, score = mask, 0, m
    for sym in a:
        eq = peq.get(sym, 0)
        xv = eq | mv
        xh = ((((eq & pv) + pv) & mask) ^ pv) | eq
        ph = (mv | ~(xh | pv)) & mask
        mh = pv & xh
        if ph & last:
            score += 1
        elif mh & last:
            score -= 1
        ph = ((ph << 1) | 1) & mask
        mh = (mh << 1) & mask
        pv = (mh | ~(xv | ph)) & mask
        mv = ph & xv
    return score


# ------------------------------------------------------------------ normalization
def _base(text: Any) -> str:
    """Line breaks unified, unreadable-word markers as ``[?]`` (as the recognizer stores
    them), NFC."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    try:
        from src.ink_transcribe import _UNREADABLE_RE

        text = _UNREADABLE_RE.sub(UNREADABLE, text)
    except ImportError:  # metrics work without the hub's code
        pass
    return unicodedata.normalize("NFC", text)


def norm_default(text: Any) -> str:
    """Each line stripped, whitespace runs as one space, blank lines dropped."""
    lines = (" ".join(ln.split()) for ln in _base(text).split("\n"))
    return "\n".join(ln for ln in lines if ln)


def norm_loose(text: Any) -> str:
    """Case-folded, punctuation dropped, all whitespace (line breaks too) as one space."""
    text = _base(text).casefold()
    text = "".join(" " if unicodedata.category(ch).startswith("P") else ch for ch in text)
    return " ".join(text.split())


NORMS: dict[str, Callable[[Any], str]] = {"default": norm_default, "loose": norm_loose}


def char_edits(ref: str, hyp: str) -> tuple[int, int]:
    """``(edits, reference characters)``."""
    return levenshtein(ref, hyp), len(ref)


def word_edits(ref: str, hyp: str) -> tuple[int, int]:
    """``(edits, reference words)``."""
    r, h = ref.split(), hyp.split()
    return levenshtein(r, h), len(r)


def rate(edits: int, total: int) -> float:
    """Edits per reference unit; an empty reference scores 0 when the hypothesis is
    empty too, else 1 per inserted unit."""
    return edits / total if total else float(edits)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..1)."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)]


# ---------------------------------------------------------------------- the set
@dataclass
class Pair:
    owner: str | None
    note_id: str
    page_id: str
    index: int
    page: dict[str, Any]
    reference: str
    stored: str | None  # transcript.machine text for these strokes, if kept
    model: str | None = None


@dataclass
class PageScore:
    owner: str | None
    note_id: str
    page_id: str
    index: int
    model: str | None = None
    error: str | None = None
    seconds: float | None = None
    unreadable: int = 0
    exact: bool = False
    counts: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    # counts[norm]["char" | "word"] = [edits, reference length]


SKIP_REASONS = ("not_reviewed", "no_ink", "stale", "empty_text", "auto", "photo",
                "no_stored_machine")


def classify(page: dict[str, Any], *, include_photos: bool = False) -> str | None:
    """Why a page is not a (page, reviewed text) pair, else ``None``."""
    from src import ink_transcribe as tr

    t = page.get("transcript") if isinstance(page.get("transcript"), dict) else {}
    if t.get("status") != "reviewed":
        return "not_reviewed"
    if t.get("auto"):
        return "auto"
    photo = page.get("photo") is not None
    if photo and not include_photos:
        return "photo"
    if not photo and not tr.has_ink(page):
        return "no_ink"
    if t.get("strokes_hash") != tr.writing_hash(page):
        return "stale"
    if not str(t.get("text") or "").strip():
        return "empty_text"
    return None


def stored_machine(page: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(text, model)`` the model gave for the page's current strokes before review."""
    from src import ink_transcribe as tr

    t = page.get("transcript") if isinstance(page.get("transcript"), dict) else {}
    m = t.get("machine")
    if isinstance(m, dict) and isinstance(m.get("text"), str) \
            and m.get("strokes_hash") == tr.writing_hash(page):
        return m["text"], m.get("model")
    return None, None


def collect_pairs(owners: Sequence[str | None], *, include_photos: bool = False,
                  limit: int | None = None) -> tuple[list[Pair], dict[str, int]]:
    """The pairs of ``owners``' sidecars (note and page order) and skip counts."""
    from src import ink_notes as ink

    pairs: list[Pair] = []
    skipped = {r: 0 for r in SKIP_REASONS}
    for owner in owners:
        for note_id in ink.sidecar_ids(owner):
            stored = ink.load_sidecar(owner, note_id)
            if not stored or not isinstance(stored.get("pages"), list):
                continue
            for i, page in enumerate(stored["pages"]):
                if not isinstance(page, dict):
                    continue
                why = classify(page, include_photos=include_photos)
                if why:
                    skipped[why] += 1
                    continue
                if ink.is_photo(page):  # read from its file, as a transcription run does
                    page = {**page, "_photo": str(ink.photo_path(owner, note_id, page["id"]))}
                text, model = stored_machine(page)
                pairs.append(Pair(owner, note_id, str(page.get("id")), i, page,
                                  page["transcript"]["text"], text, model))
    if limit is not None:
        pairs = pairs[:max(0, limit)]
    return pairs, skipped


# ---------------------------------------------------------------------- scoring
def score_page(pair: Pair, hypothesis: str | None, *, model: str | None = None,
               seconds: float | None = None, error: str | None = None) -> PageScore:
    s = PageScore(pair.owner, pair.note_id, pair.page_id, pair.index, model=model,
                  seconds=seconds, error=error)
    if hypothesis is None:
        return s
    s.unreadable = _base(hypothesis).count(UNREADABLE)
    for name, norm in NORMS.items():
        ref, hyp = norm(pair.reference), norm(hypothesis)
        s.counts[name] = {"char": list(char_edits(ref, hyp)), "word": list(word_edits(ref, hyp))}
        if name == "default":
            s.exact = ref == hyp
    return s


def _rates(scores: Sequence[PageScore], norm: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for unit, key in (("char", "cer"), ("word", "wer")):
        edits = sum(s.counts[norm][unit][0] for s in scores)
        total = sum(s.counts[norm][unit][1] for s in scores)
        per = [rate(*s.counts[norm][unit]) for s in scores]
        out[key] = rate(edits, total) if scores else None
        out[f"page_{key}"] = {"median": statistics.median(per) if per else None,
                              "p90": percentile(per, 0.9)}
        out[f"{unit}_edits"], out[f"ref_{unit}s"] = edits, total
    out["exact"] = sum(1 for s in scores if s.exact)
    out["exact_share"] = out["exact"] / len(scores) if scores else None
    return out


def summarize(scores: Sequence[PageScore], *, mode: str, skipped: dict[str, int],
              pairs: int) -> dict[str, Any]:
    ok = [s for s in scores if s.error is None and s.counts]
    failed = [s for s in scores if s.error is not None]
    clear = [s for s in ok if s.unreadable == 0]
    secs = [s.seconds for s in scores if s.seconds is not None and s.error is None]
    models = sorted({s.model for s in ok if s.model})
    clear_rates = _rates(clear, "default")
    return {
        "mode": mode,
        "models": models,
        "pairs": pairs,
        "scored": len(ok),
        "failed": len(failed),
        "skipped": skipped,
        "default": _rates(ok, "default"),
        "loose": _rates(ok, "loose"),
        "unreadable": {"pages": len(ok) - len(clear),
                       "marks": sum(s.unreadable for s in ok)},
        "auto_send": {  # pages INK_AUTO_SEND would send unreviewed (no [?])
            "pages": len(clear),
            "share_of_scored": len(clear) / len(ok) if ok else None,
            "cer": clear_rates["cer"], "wer": clear_rates["wer"],
            "exact": clear_rates["exact"], "exact_share": clear_rates["exact_share"],
        },
        "seconds": None if not secs else {
            "mean": statistics.fmean(secs), "p90": percentile(secs, 0.9),
            "total": sum(secs)},
        "pages": [_page_row(s) for s in scores],
    }


def _page_row(s: PageScore) -> dict[str, Any]:
    """One page, numbers and ids only (never text)."""
    row: dict[str, Any] = {"owner": s.owner, "note_id": s.note_id, "page_id": s.page_id,
                           "index": s.index, "model": s.model, "error": s.error,
                           "seconds": s.seconds}
    if s.counts:
        d, lo = s.counts["default"], s.counts["loose"]
        row.update(ref_chars=d["char"][1], ref_words=d["word"][1],
                   cer=rate(*d["char"]), wer=rate(*d["word"]),
                   cer_loose=rate(*lo["char"]), wer_loose=rate(*lo["word"]),
                   unreadable=s.unreadable, exact=s.exact)
    return row


# ---------------------------------------------------------------------- readers
Reader = Callable[[list[dict]], Awaitable[str]]
ReaderFor = Callable[[Pair], "tuple[str, Reader]"]


class NoReader(RuntimeError):
    pass


def hub_reader_for(model: str | None = None, base_url: str | None = None) -> ReaderFor:
    """Readers per owner: the hub's own (``default_reader``), or ``model`` (on
    ``base_url``, or wherever the hub finds it), local endpoints only."""
    from src import ink_transcribe as tr

    cache: dict[Any, tuple[str, Reader]] = {}

    def override(owner: str | None) -> tuple[str, Reader]:
        if base_url:
            if not model:
                raise NoReader("--base-url needs --model")
            candidate = (base_url, model, {})
        else:
            from src.ai_interaction import _resolve_model

            try:
                candidate = _resolve_model(model, owner=owner)
            except Exception:
                raise NoReader(f"the model '{model}' is not on any configured endpoint") \
                    from None
        url, name, headers = candidate
        if not tr._is_local(url):
            raise NoReader(f"'{url}' is not on your network; handwriting is only read "
                           "by local models")

        async def call(messages: list[dict]) -> str:
            from src.llm_core import llm_call_async

            return await llm_call_async(url, name, messages, headers=headers or {},
                                        temperature=0.0, max_tokens=2000, timeout=300,
                                        thinking_mode="off")

        return name, call

    def reader_for(pair: Pair) -> tuple[str, Reader]:
        if pair.owner not in cache:
            if model or base_url:
                cache[pair.owner] = override(pair.owner)
            else:
                try:
                    cache[pair.owner] = tr.default_reader(pair.owner)
                except tr.NoModel as exc:
                    raise NoReader(str(exc)) from None
        return cache[pair.owner]

    return reader_for


async def reread(pairs: Sequence[Pair], reader_for: ReaderFor,
                 clock: Callable[[], float] = time.monotonic,
                 texts: dict[tuple[str, str], str] | None = None) -> list[PageScore]:
    """Read each page again and score it; a model error marks that page failed. The
    model text goes into ``texts`` (``--show-diffs``) when given."""
    from src import ink_transcribe as tr

    scores = []
    for pair in pairs:
        model, read = reader_for(pair)
        started = clock()
        try:
            text = await tr.read_page(pair.page, read)
        except Exception as exc:  # counted, not fatal (the error text may hold page text)
            scores.append(score_page(pair, None, model=model, error=type(exc).__name__,
                                     seconds=clock() - started))
            continue
        model = getattr(read, "answered_by", None) or model
        if texts is not None:
            texts[(pair.note_id, pair.page_id)] = text
        scores.append(score_page(pair, text, model=model, seconds=clock() - started))
    return scores


def stored_scores(pairs: Sequence[Pair], skipped: dict[str, int]) -> list[PageScore]:
    out = []
    for pair in pairs:
        if pair.stored is None:
            skipped["no_stored_machine"] += 1
            continue
        out.append(score_page(pair, pair.stored, model=pair.model))
    return out


# ---------------------------------------------------------------------- fixtures
FIXTURE_OWNER = "eval-writer"
PEN = {"color": "#1f1f1f", "width": 2.0, "highlighter": False}

# (reviewed text, model answer or an Exception, extra transcript fields, page extras)
FIXTURES: list[tuple[str, Any, dict[str, Any], dict[str, Any]]] = [
    ("Buy oat milk\nCall Dana about the boat", "Buy oat milk\nCall Dana about the boat", {}, {}),
    ("Plan the garden shed\nOrder 12 tiles", "Plan the garden shad\nOrder 12 tiles", {}, {}),
    ("Pick up the parcel\nBook the dentist for Friday",
     "Pick up the [?]\nBook the dentist for friday.", {}, {}),
    ("Water the ferns", RuntimeError("model offline"), {}, {}),
    # skipped: machine text not reviewed, accepted automatically, empty review, photo
    ("Not reviewed yet", "Not reviewed yet", {"status": "machine"}, {}),
    ("Sent automatically", "Sent automatically", {"auto": True}, {}),
    ("", "", {}, {}),
    ("A paper page", "A paper page", {}, {"photo": {"id": "fictional"}}),
]


def _line_stroke(y: float, sid: str, length: int = 300) -> dict[str, Any]:
    pts = [[120 + i * 10, y + (8 if i % 2 else -8), i * 16] for i in range(length // 10)]
    return {"id": sid, "pen": dict(PEN), "at": 0, "points": pts}


def write_fixtures(owner: str = FIXTURE_OWNER) -> dict[str, Any]:
    """Fictional sidecars in the current DATA_DIR; returns ``{page_id: model answer}``
    for the fake reader. One note per page; a further page whose strokes changed after
    its review (stale)."""
    from src import ink_notes as ink
    from src import ink_transcribe as tr

    answers: dict[str, Any] = {}
    specs = list(FIXTURES) + [("Stale review", "Stale review", {"stale": True}, {})]
    for n, (reference, answer, extra, page_extra) in enumerate(specs):
        pid = f"fx{n}"
        lines = max(1, len(reference.split("\n")))
        page = {"id": pid, "width": ink.PAGE_WIDTH, "height": ink.PAGE_HEIGHT,
                "strokes": [_line_stroke(150 + 44 * k, f"{pid}-{k}") for k in range(lines)]}
        page.update(page_extra)
        current = tr.writing_hash(page)
        t = {"text": reference, "model": None, "status": "reviewed", "strokes_hash": current,
             "at": "2026-10-01T09:00:00Z"}
        if not isinstance(answer, Exception) and extra.get("status") != "machine":
            t["machine"] = {"text": answer, "model": "fixture-fake",
                            "at": "2026-10-01T08:59:00Z", "strokes_hash": current}
        if extra.pop("stale", False):
            page["strokes"].append(_line_stroke(400, f"{pid}-new"))
        t.update(extra)
        page["transcript"] = t
        answers[pid] = answer
        note_id = str(uuid.UUID(int=0x1234_0000 + n))
        ink._write_json(ink.sidecar_path(owner, note_id),
                        {"paper": "lined", "ruled": True, "edited_at": 1000, "pages": [page]})
    return answers


def fixture_reader_for(answers: dict[str, Any]) -> ReaderFor:
    """A fake reader per page: the fixture's answer for the page's first band, empty
    text for any further band."""

    def reader_for(pair: Pair) -> tuple[str, Reader]:
        queue = [answers.get(pair.page_id, "")]

        async def call(messages: list[dict]) -> str:
            answer = queue.pop(0) if queue else ""
            if isinstance(answer, Exception):
                raise answer
            return json.dumps({"text": answer})

        return "fixture-fake", call

    return reader_for


# ---------------------------------------------------------------------- output
def _pct(v: float | None) -> str:
    return "-" if v is None else f"{100 * v:.1f}%"


def _num(v: float | None, digits: int = 1) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def format_report(rep: dict[str, Any], *, per_page: bool = False) -> str:
    out = [(f"ink eval ({rep['mode']}): {rep['pairs']} pairs, {rep['scored']} scored, "
            f"{rep['failed']} failed; models: {', '.join(rep['models']) or '-'}")]
    skipped = ", ".join(f"{k} {v}" for k, v in rep["skipped"].items() if v)
    out.append(f"skipped pages: {skipped or 'none'}")
    out.append("")
    out.append(f"{'':<24}{'CER':>8}{'WER':>8}{'page CER med/p90':>20}"
               f"{'page WER med/p90':>20}{'exact':>8}")
    rows = [("default", rep["default"]), ("loose", rep["loose"])]
    for name, r in rows:
        out.append(f"{name:<24}{_pct(r['cer']):>8}{_pct(r['wer']):>8}"
                   f"{_pct(r['page_cer']['median']) + ' / ' + _pct(r['page_cer']['p90']):>20}"
                   f"{_pct(r['page_wer']['median']) + ' / ' + _pct(r['page_wer']['p90']):>20}"
                   f"{_pct(r['exact_share']):>8}")
    a = rep["auto_send"]
    out.append(f"{'pages without [?]':<24}{_pct(a['cer']):>8}{_pct(a['wer']):>8}"
               f"{'':>20}{'':>20}{_pct(a['exact_share']):>8}")
    out.append("")
    u = rep["unreadable"]
    out.append(f"[?] marks: {u['marks']} on {u['pages']} of {rep['scored']} pages")
    out.append(f"INK_AUTO_SEND: {a['pages']} pages without [?] "
               f"({_pct(a['share_of_scored'])} of scored); {a['exact']} of them exactly "
               f"right ({_pct(a['exact_share'])}), CER {_pct(a['cer'])}, WER {_pct(a['wer'])}")
    s = rep["seconds"]
    if s:
        out.append(f"seconds per page: mean {_num(s['mean'])}, p90 {_num(s['p90'])}, "
                   f"total {_num(s['total'])}")
    if per_page:
        out.append("")
        out.append(f"{'note':<38}{'page':<10}{'chars':>7}{'CER':>8}{'WER':>8}"
                   f"{'[?]':>5}{'exact':>7}{'sec':>7}")
        for p in rep["pages"]:
            if p.get("error"):
                out.append(f"{p['note_id']:<38}{p['page_id']:<10}  failed ({p['error']})")
                continue
            out.append(f"{p['note_id']:<38}{p['page_id']:<10}{p['ref_chars']:>7}"
                       f"{_pct(p['cer']):>8}{_pct(p['wer']):>8}{p['unreadable']:>5}"
                       f"{'yes' if p['exact'] else 'no':>7}{_num(p['seconds']):>7}")
    return "\n".join(out)


def format_diffs(pairs: Sequence[Pair], hyps: dict[tuple[str, str], str]) -> str:
    """Line diffs of reference (-) and model text (+). Holds page text: local only."""
    import difflib

    out = ["", "=== diffs (page text; keep local) ==="]
    for pair in pairs:
        hyp = hyps.get((pair.note_id, pair.page_id))
        if hyp is None:
            continue
        ref, got = norm_default(pair.reference), norm_default(hyp)
        if ref == got:
            continue
        out.append(f"--- {pair.note_id} {pair.page_id}")
        out.extend(ln for ln in difflib.ndiff(ref.split("\n"), got.split("\n"))
                   if not ln.startswith("  "))
    return "\n".join(out)


# ---------------------------------------------------------------------- main
def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CER and WER of the handwriting recognizer on "
                                "the owner's reviewed pages (numbers and ids only).")
    p.add_argument("--data-dir", help="the hub's DATA_DIR (default: ODYSSEUS_DATA_DIR / "
                   "src/constants.py)")
    p.add_argument("--owner", action="append", help="owner to read (repeatable; default: "
                   "every ink folder)")
    p.add_argument("--limit", type=int, help="at most this many pairs")
    p.add_argument("--stored", action="store_true", help="score the stored model text "
                   "(transcript.machine) instead of reading pages again; no model calls")
    p.add_argument("--model", help="read with this model instead of the vision_model setting")
    p.add_argument("--base-url", help="endpoint for --model (local endpoints only)")
    p.add_argument("--include-photos", action="store_true", help="also score photo pages")
    p.add_argument("--fixtures", action="store_true", help="fictional pages in a temp "
                   "DATA_DIR and a fake reader (sandbox)")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument("--out", help="also write the JSON report to this file")
    p.add_argument("--per-page", action="store_true", help="a row per page (ids, numbers)")
    p.add_argument("--show-diffs", action="store_true", help="print per-page line diffs: "
                   "this prints page text, keep it local")
    return p.parse_args(argv)


def _set_data_dir(path: str) -> None:
    os.environ["ODYSSEUS_DATA_DIR"] = path  # read when src.constants is first imported
    from src import constants

    constants.DATA_DIR = path  # ink paths read it at call time


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    tmp = None
    if args.fixtures:
        if args.data_dir:
            print("--fixtures writes fictional pages to a temp dir; leave out --data-dir",
                  file=sys.stderr)
            return 2
        tmp = tempfile.mkdtemp(prefix="ink-eval-")
        _set_data_dir(tmp)
    elif args.data_dir:
        _set_data_dir(str(Path(args.data_dir).expanduser()))
    try:
        return _run(args)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _run(args: argparse.Namespace) -> int:
    from src import ink_notes as ink

    answers = write_fixtures() if args.fixtures else None
    owners: list[str | None] = list(args.owner) if args.owner else ink.owner_folders()
    pairs, skipped = collect_pairs(owners, include_photos=args.include_photos,
                                   limit=args.limit)
    if not pairs:
        print("ink eval: no reviewed pages to score (status reviewed, ink, text for the "
              "current strokes, not accepted automatically)", file=sys.stderr)
        return 2
    if args.stored:
        mode = "stored"
        scores = stored_scores(pairs, skipped)
        hyps = {(p.note_id, p.page_id): p.stored for p in pairs if p.stored is not None}
    else:
        mode = "reread"
        reader_for = (fixture_reader_for(answers) if answers is not None
                      else hub_reader_for(args.model, args.base_url))
        hyps = {}
        try:
            scores = asyncio.run(reread(pairs, reader_for,
                                        texts=hyps if args.show_diffs else None))
        except NoReader as exc:
            print(f"ink eval: no model to read with: {exc}", file=sys.stderr)
            return 3
    rep = summarize(scores, mode=mode, skipped=skipped, pairs=len(pairs))
    if args.out:
        Path(args.out).write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(rep, indent=2) if args.json else format_report(rep, per_page=args.per_page))
    if args.show_diffs:
        print(format_diffs(pairs, hyps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
