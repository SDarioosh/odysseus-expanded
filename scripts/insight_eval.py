#!/usr/bin/env python3
"""Fork: the Today insight eval: rule order, learned weights and the T5 fallback order.

Loads a fictional day (``scripts/fixtures/insight_eval_day.json``: to-do notes, events,
today's date and a 28-day outcome history) and checks, in a temp ``DATA_DIR``:

* **rule_order**: the candidates without weights (``today_insight.order_candidates``):
  overdue (oldest first) and due today, then due soon, pinned, undated, later; an undated
  shopping list and done items are left out; the cap keeps the head of that order.
* **weights**: the history is written day by day through ``src/insight_outcomes.py``
  (``observe``, ``record``, finalize) and ``insight_weights.compute`` gives the
  hand-computed integer weights (one list always done, one always dismissed).
* **tiers**: with those weights the order changes within tiers only: an overdue item of
  the dismissed list still comes before a due-soon item of the favoured list, and a cap
  never drops an overdue or due-today item for a weighted one.
* **min_shown**: a list shown 5 times keeps weight 0; shown 6 times it moves.
* **t5_table**: ``insight_weights.t5_table`` for the fixture's notes.
* **t5_day**: the T5 PDA host test's own day (``scripts/fixtures/t5_weights_day.json``,
  copied verbatim from Athena ``firmware/pda/test/fixtures/insight_eval_day.json``)
  through the hub's ``order_candidates`` with its weight table: the to-dos of each T5
  section (DO FIRST = tier 0, COMING UP = tier 1, ALSO = pinned then undated) come in
  the order the T5 test expects, with and without weights.
* **owner_marks**: "not today" leaves a ref out of today's candidates; "↑ first" puts it
  first in Do first (``insight_outcomes.apply_owner_order``).
* **reset**: after ``insight_weights.reset`` the key is 0 and the order is the rule order.

Deterministic: every ``now`` is given, no model, no network. Prints expected vs actual
per check and a score (``N/N``); exit code 1 on any mismatch. Fictional data only.

Usage:
    venv/bin/python scripts/insight_eval.py
    venv/bin/python scripts/insight_eval.py --json
    venv/bin/python scripts/insight_eval.py --data-dir /tmp/insight-eval --keep
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

FIXTURE = REPO / "scripts" / "fixtures" / "insight_eval_day.json"
T5_FIXTURE = REPO / "scripts" / "fixtures" / "t5_weights_day.json"
OWNER = "eval-owner"
GATE_OWNER = "eval-owner-gate"
CASES = ("rule_order", "weights", "tiers", "min_shown", "t5_table", "t5_day",
         "owner_marks", "reset")
T5_CAP = 5  # the T5's rule-based items per section
T5_SECTIONS = ("do_first", "coming_up", "also")


@dataclass
class Check:
    case: str
    step: str
    expected: Any
    actual: Any

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


class SetupError(RuntimeError):
    pass


# ------------------------------------------------------------------------ fixture
def load_fixture(path: Path = FIXTURE) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _note(n: dict) -> SimpleNamespace:
    return SimpleNamespace(id=n["id"], title=n["title"], note_type=n.get("note_type", "checklist"),
                           pinned=bool(n.get("pinned")), due_date=n.get("due_date"),
                           items=json.dumps([{"text": i["text"], "done": bool(i["done"])}
                                             for i in n["items"]]),
                           archived=False, source=None, label=None)


def tier(c: dict) -> int:
    """The published rule tier (docs/fork/today-insight.md#rule-order)."""
    b = c.get("bucket")
    if b in ("overdue", "due_today"):
        return 0
    if b == "due_soon":
        return 1
    if c.get("pinned"):
        return 2
    return 3 if b == "undated" else 4


class Day:
    """Today's inputs built from the fixture, fresh for every ordering."""

    def __init__(self, fx: dict):
        from src import today_insight as ti

        self.ti = ti
        self.tz = ZoneInfo(fx.get("timezone") or "UTC")
        self.now = datetime.fromisoformat(fx["now"].replace("Z", "+00:00"))
        self.now_local = self.now.astimezone(self.tz)
        self.today = Date.fromisoformat(fx["today"])
        self.notes = [_note(n) for n in fx["notes"]]
        self.events = [dict(e) for e in fx.get("events") or []]
        self.names = {ti._list_key(p) for p in ti.DEFAULT_SKIP_LISTS.split(",")}

    def candidates(self, *, weights=None, dismissed=(), limit=None) -> list[dict]:
        ti = self.ti
        todos = ti.todo_candidates(self.notes, self.today, self.tz, self.names)
        events = ti.event_candidates(copy.deepcopy(self.events), self.now_local)
        kwargs = {"weights": weights, "dismissed": dismissed}
        if limit is not None:
            kwargs["limit"] = limit
        return ti.order_candidates(todos, events, **kwargs)

    def inputs(self, candidates: list[dict]) -> dict:
        return {"now": self.now_local, "today": self.today, "candidates": candidates,
                "extra": [], "notes_state": self.ti.notes_state(self.notes),
                "occurrences": copy.deepcopy(self.events), "action_rows": []}


def _refs(cands: list[dict]) -> list[str]:
    return [c["ref"] for c in cands]


# ------------------------------------------------------------------------ history
def write_history(fx: dict, owner: str, overrides: dict[str, int] | None = None) -> None:
    """Write the fixture's outcome history through the outcome log, day by day: the
    morning pass shows every entry, the owner dismisses, the evening pass sees the ticks;
    the next day's pass finalizes the day before (``ignored`` = still open)."""
    from src import insight_outcomes as io
    from src import today_insight as ti

    hist = fx["history"]
    first = Date.fromisoformat(hist["first_day"])
    total = int(hist["days"])
    entries = []
    for e in hist["items"]:
        days = int((overrides or {}).get(e["ref"], e["days"]))
        if days > total:
            raise SetupError(f"{e['ref']}: {days} days don't fit the {total}-day window")
        _, note_id, index = e["ref"].split(":")
        entries.append({**e, "days": days, "note_id": note_id, "index": int(index)})
    names = {ti._list_key(p) for p in ti.DEFAULT_SKIP_LISTS.split(",")}
    for d in range(total):
        day = first + timedelta(days=d)
        active = []
        for e in entries:
            start = total - e["days"]
            if d >= start:
                active.append((e, e["cycle"][(d - start) % len(e["cycle"])]))
        if not active:
            continue

        def notes(evening: bool, active=active):
            out = []
            for e, outcome in active:
                lines = [{"text": f"(earlier line {k + 1})", "done": True} for k in range(e["index"])]
                lines.append({"text": e["title"], "done": evening and outcome == "done"})
                if evening and outcome == "gone":
                    lines = []
                out.append(SimpleNamespace(id=e["note_id"], title=e["note_id"], note_type="checklist",
                                           pinned=False, due_date=None, items=json.dumps(lines),
                                           archived=False, source=None, label=None))
            return out

        def inputs(at: datetime, evening: bool, day=day):
            ns = notes(evening)
            return {"now": at, "today": day, "extra": [], "occurrences": [], "action_rows": [],
                    "candidates": ti.order_candidates(ti.todo_candidates(ns, day, timezone.utc, names), []),
                    "notes_state": ti.notes_state(ns)}

        rows = [{"ref": e["ref"], "title": e["title"], "why": ""} for e, _ in active]
        insight = {"date": day.isoformat(), "generated": f"{day}T07:00:00Z",
                   "headline": "A fictional day.", "focus": rows[:5], "coming_up": [],
                   "also": rows[5:], "input_hash": "eval", "votes": []}
        ti.save(owner, insight)
        morning = datetime(day.year, day.month, day.day, 7, 0, tzinfo=timezone.utc)
        io.observe(owner, insight, inputs(morning, False), now=morning)
        for e, outcome in active:
            if outcome == "dismissed":
                io.record(owner, e["ref"], "dismissed", now=morning + timedelta(hours=2))
        evening = morning + timedelta(hours=11)
        io.observe(owner, insight, inputs(evening, True), now=evening)


# ------------------------------------------------------------------------ T5 day
class T5Day:
    """The T5 host test's day (``scripts/fixtures/t5_weights_day.json``, a verbatim copy
    of Athena's ``firmware/pda/test/fixtures/insight_eval_day.json``: a PDA feed
    with ``todos``, ``calendar``, ``weights`` and the T5's expected plan) as hub inputs."""

    def __init__(self, feed: dict):
        from src import today_insight as ti

        self.ti = ti
        self.feed = feed
        self.tz = ZoneInfo(feed["tz"])
        self.now = datetime.fromisoformat(feed["generated"].replace("Z", "+00:00"))
        self.now_local = self.now.astimezone(self.tz)
        self.today = self.now_local.date()
        self.table = {k: int(v) for k, v in feed["weights"]["weights"].items()}
        self.notes = [SimpleNamespace(
            id=n["id"], title=n["title"], note_type=self.note_type(n["id"]),
            pinned=bool(n.get("pinned")), due_date=n.get("due"),
            items=json.dumps([{"text": i["text"], "done": bool(i["done"])} for i in n["items"]]),
            archived=False, source=None, label=None) for n in feed["todos"]]
        self.events = [{"uid": e["id"], "summary": e["title"], "dtstart": e["start"],
                        "dtend": e.get("end"), "all_day": bool(e.get("all_day")),
                        "location": e.get("where")} for e in feed["calendar"]]
        self.todo_titles = {i["text"] for n in feed["todos"] for i in n["items"]}
        self.event_titles = {e["title"] for e in feed["calendar"]}

    def note_type(self, note_id: str) -> str:
        """The T5 table merges a note's ``list:`` and ``note_type:`` weights; a hub key
        holds at most ±3, so a note whose number is beyond ±3 gets its own type here
        (``goal``) and the rest of its number from ``note_type:goal`` (all such notes
        in this fixture have the same number)."""
        return "goal" if abs(self.table.get(f"list:{note_id}", 0)) > 3 else "checklist"

    def weights(self) -> dict:
        """A hub weights dict whose ``candidate_weight`` is the table's number per note."""
        from src import insight_weights as iw

        keys: dict[str, dict] = {}
        big = {v for k, v in self.table.items() if abs(v) > iw.KEY_LIMIT}
        if len(big) > 1:
            raise SetupError("the T5 day has several weights beyond ±3; one note_type can't hold them")
        rest = 0
        if big:
            w = big.pop()
            rest = w - (iw.KEY_LIMIT if w > 0 else -iw.KEY_LIMIT)
            keys["note_type:goal"] = {"weight": w - rest}
        for key, w in self.table.items():
            keys[key] = {"weight": rest if abs(w) > iw.KEY_LIMIT else w}
        return {"version": 1, "date": self.today.isoformat(), "keys": keys, "resets": {}}

    def candidates(self, weights: dict | None) -> list[dict]:
        ti = self.ti
        names = {ti._list_key(p) for p in ti.DEFAULT_SKIP_LISTS.split(",")}
        return ti.order_candidates(ti.todo_candidates(self.notes, self.today, self.tz, names),
                                   ti.event_candidates(copy.deepcopy(self.events), self.now_local),
                                   weights=weights)


def hub_sections(cands: list[dict], kind: str = "todo") -> dict[str, list[str]]:
    """The hub's rule order mapped onto the T5's sections, titles of one kind only:
    tier 0 → DO FIRST, 1 → COMING UP, 2 and 3 → ALSO (pinned, then undated); tier 4
    (unpinned, due later than a week) isn't on the T5's Today."""
    out = {name: [] for name in T5_SECTIONS}
    for c in cands:
        if c.get("kind") == kind and tier(c) != 4:
            out[T5_SECTIONS[min(tier(c), 2)]].append(c["title"])
    return out


def t5_rules(feed: dict, table: dict[str, int] | None) -> dict[str, list[str]]:
    """The T5's rule-based fallback for the feed's to-dos, re-implemented from the
    published rules (Athena docs/t5-pda-plan.md §5): DO FIRST by (-w, due date,
    feed order), COMING UP by (-w, date), ALSO pinned by (-w, feed order) then undated by
    (-w, feed order); undated shopping lists left out; at most 5 per section. Titles."""
    from src import today_insight as ti

    today = datetime.fromisoformat(feed["generated"].replace("Z", "+00:00")).astimezone(
        ZoneInfo(feed["tz"])).date()
    first, coming, also = [], [], []
    seq = 0
    for n in feed["todos"]:
        due = Date.fromisoformat(n["due"][:10]) if n.get("due") else None
        if due is None and ti._list_key(n["title"]) in ("shopping", "groceries", "grocery"):
            continue
        w = int((table or {}).get(f"list:{n['id']}", 0))
        for it in n["items"]:
            if it["done"]:
                continue
            seq += 1
            if due is not None and due <= today:
                first.append(((-w, due.isoformat(), seq), it["text"]))
            elif due is not None and due <= today + timedelta(days=7):
                coming.append(((-w, due.isoformat(), seq), it["text"]))
            elif n.get("pinned"):
                also.append(((0, -w, seq), it["text"]))
            elif due is None:
                also.append(((1, -w, seq), it["text"]))
    return {name: [t for _, t in sorted(items)][:T5_CAP]
            for name, items in zip(T5_SECTIONS, (first, coming, also))}


# --------------------------------------------------------------------------- run
def _key_view(row: dict) -> dict:
    from src import insight_weights as iw

    return {k: row.get(k) for k in ("weight", *iw.COUNTS)}


def _cases(fx: dict, checks: list[Check]) -> None:
    from src import insight_outcomes as io
    from src import insight_weights as iw

    exp = fx["expected"]
    day = Day(fx)
    today, now = day.today, day.now

    def check(case, step, expected, actual):
        checks.append(Check(case, step, expected, actual))

    # -- rule order, no weights
    rule = day.candidates()
    check("rule_order", "order", exp["rule_order"], _refs(rule))
    check("rule_order", "tiers", exp["rule_tiers"], [tier(c) for c in rule])
    check("rule_order", "tiers never go back", sorted(tier(c) for c in rule), [tier(c) for c in rule])
    overdue = [c["date"] for c in rule if c["bucket"] == "overdue"]
    check("rule_order", "overdue oldest first", sorted(overdue), overdue)
    check("rule_order", "left out (undated shopping list, done items)", [],
          [r for r in exp["dropped"] if r in _refs(rule)])
    check("rule_order", "every weight 0", [0] * len(rule), [c["weight"] for c in rule])
    capped = day.candidates(limit=exp["cap"]["limit"])
    check("rule_order", f"cap {exp['cap']['limit']}", exp["cap"]["order"], _refs(capped))

    # -- history and weights
    write_history(fx, OWNER)
    finals = [d["date"] for d in io.days(OWNER, today - timedelta(days=28), today)
              if d.get("final")]
    weights = iw.current(OWNER, today, now=now)
    finals_after = [d["date"] for d in io.days(OWNER, today - timedelta(days=28), today)
                    if d.get("final")]
    hist_days = int(fx["history"]["days"])
    check("weights", "days final before today's first run", hist_days - 1, len(finals))
    check("weights", "days final after it (yesterday finalized)", hist_days, len(finals_after))
    check("weights", "keys learnt", sorted(exp["keys"]), sorted(weights.get("keys") or {}))
    for key, want in exp["keys"].items():
        row = (weights.get("keys") or {}).get(key) or {}
        check("weights", key, want, _key_view(row))
    check("weights", "to-do base", round(float(Fraction(exp["base_todo"])), 4),
          (weights.get("base") or {}).get("todo"))
    again = iw.current(OWNER, today, now=now + timedelta(hours=3))
    check("weights", "stable during the day", weights.get("keys"), again.get("keys"))

    # -- tiers with weights
    weighted = day.candidates(weights=weights)
    refs = _refs(weighted)
    check("tiers", "weighted order", exp["weighted_order"], refs)
    check("tiers", "same tiers as the rule order", sorted(exp["rule_tiers"]),
          [tier(c) for c in weighted])
    check("tiers", "same items as the rule order", sorted(exp["rule_order"]), sorted(refs))
    by_ref = {c["ref"]: c for c in weighted}
    home, work = by_ref.get("todo:note-home:0", {}), by_ref.get("todo:note-work:0", {})
    check("tiers", "dismissed list's overdue item before favoured list's due-soon item",
          (-3, 3, True), (home.get("weight"), work.get("weight"),
                          refs.index("todo:note-home:0") < refs.index("todo:note-work:0")))
    tier0 = sorted(r for r, t in zip(exp["rule_order"], exp["rule_tiers"]) if t == 0)
    check("tiers", "cap keeps every overdue and due-today item", tier0,
          sorted(_refs(day.candidates(weights=weights, limit=len(tier0)))))

    # -- MIN_SHOWN gate
    gate = exp["min_shown_gate"]
    row = (weights.get("keys") or {}).get(gate["key"]) or {}
    check("min_shown", f"{gate['key']} shown {row.get('shown')} < {iw.MIN_SHOWN}",
          {"shown": iw.MIN_SHOWN - 1, "weight": 0},
          {"shown": row.get("shown"), "weight": row.get("weight")})
    write_history(fx, GATE_OWNER, overrides={gate["ref"]: gate["days"]})
    io.finalize_pending(GATE_OWNER, today, now=now)  # what current() does first
    gated = iw.compute(GATE_OWNER, today, now=now)
    row = (gated.get("keys") or {}).get(gate["key"]) or {}
    check("min_shown", f"{gate['key']} shown {gate['days']}",
          {"shown": gate["days"], "weight": gate["weight"]},
          {"shown": row.get("shown"), "weight": row.get("weight")})

    # -- T5 table
    table = iw.t5_table(OWNER, today, notes=day.notes, now=now)
    check("t5_table", "weights", exp["t5_table"], table.get("weights"))
    check("t5_table", "fixture's feed block", fx["weights"], table)
    check("t5_table", "same as the hub's candidate weights",
          {r: c["weight"] for r, c in by_ref.items() if c["kind"] == "todo" and c["weight"]},
          {r: table["weights"].get(f"list:{c['note_id']}", 0) for r, c in by_ref.items()
           if c["kind"] == "todo" and table["weights"].get(f"list:{c['note_id']}", 0)})

    # -- owner marks
    marks = exp["owner_marks"]
    titles = {c["ref"]: c["title"] for c in weighted}
    insight = {"date": today.isoformat(), "generated": fx["now"], "headline": "A fictional day.",
               **{s: [{"ref": r, "title": titles.get(r, r), "why": ""} for r in marks["insight"][s]]
                  for s in ("focus", "coming_up", "also")},
               "input_hash": "eval", "votes": []}
    day.ti.save(OWNER, insight)
    io.observe(OWNER, insight, day.inputs(weighted), now=now)
    io.record(OWNER, marks["not_today"], "dismissed", now=now + timedelta(minutes=1))
    io.record(OWNER, marks["first"], "promoted", now=now + timedelta(minutes=2))
    state = io.owner_marks(OWNER, today)
    check("owner_marks", "dismissed", [marks["not_today"]], sorted(state["dismissed"]))
    check("owner_marks", "promoted", [marks["first"]], state["promoted"])
    without = day.candidates(weights=weights, dismissed=state["dismissed"])
    check("owner_marks", "not today leaves today's candidates",
          [r for r in exp["weighted_order"] if r != marks["not_today"]], _refs(without))
    shown = io.apply_owner_order(insight, io.load(OWNER, today.isoformat()))
    check("owner_marks", "↑ first leads Do first", marks["focus_after"],
          [r["ref"] for r in shown["focus"]])
    check("owner_marks", "coming up after", marks["coming_up_after"],
          [r["ref"] for r in shown["coming_up"]])
    check("owner_marks", "weights unchanged by today's marks", weights.get("keys"),
          iw.current(OWNER, today, now=now + timedelta(minutes=3)).get("keys"))

    # -- reset
    key = exp["reset_key"]
    after = iw.reset(OWNER, key, now=now + timedelta(minutes=5))
    row = (after.get("keys") or {}).get(key) or {}
    check("reset", f"{key} weight", 0, row.get("weight"))
    check("reset", f"{key} reset day", today.isoformat(), row.get("reset_at"))
    check("reset", f"{key} still 0 on the next read", 0,
          ((iw.current(OWNER, today, now=now + timedelta(hours=1)).get("keys") or {})
           .get(key) or {}).get("weight"))
    one = day.candidates(weights=after)
    rule_tier1 = [r for r, t in zip(exp["rule_order"], exp["rule_tiers"]) if t == 1]
    check("reset", "due-soon tier back to the rule order", rule_tier1,
          [c["ref"] for c in one if tier(c) == 1])
    everything = iw.reset(OWNER, None, now=now + timedelta(minutes=6))
    check("reset", "all weights 0 after reset all", {},
          {k: r["weight"] for k, r in (everything.get("keys") or {}).items() if r.get("weight")})
    check("reset", "order is the rule order", exp["rule_order"],
          _refs(day.candidates(weights=everything)))
    check("reset", "T5 table empty", {},
          iw.t5_table(OWNER, today, notes=day.notes, now=now + timedelta(minutes=7))["weights"])


def _t5_day(feed: dict, checks: list[Check]) -> None:
    """The T5 host test's day through the hub's rule order. Compared: the to-dos of each
    T5 section, in order, and which section each event is in. Not compared, because
    the two legitimately differ: the hub's tier 4 (unpinned, due later than a week: the
    hub shows it last, the T5 not at all), the shopping list (both leave it out; the
    T5 table still carries its weight), the hub's 30-candidate cap vs the T5's 5 per
    section, and where an event stands among the to-dos of its section (the T5 places
    events by its own sort key)."""
    from src import insight_weights as iw

    day = T5Day(feed)

    def check(step, expected, actual):
        checks.append(Check("t5_day", step, expected, actual))

    weights = day.weights()
    check("candidate weight per note = the T5 table", day.table,
          {f"list:{n.id}": iw.candidate_weight(
              {"kind": "todo", "note_id": n.id, "note_type": n.note_type}, weights)
           for n in day.notes if f"list:{n.id}" in day.table})
    owner = "eval-owner-t5"
    path = iw.weights_path(owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(weights), encoding="utf-8")
    check("t5_table of those weights = the feed's weights", feed["weights"],
          iw.t5_table(owner, day.today, notes=day.notes, now=day.now))
    for label, w, expected in (("without weights", None, feed["expected_without_weights"]),
                               ("with weights", weights, feed["expected"])):
        cands = day.candidates(w)
        todos = {s: [t for t in expected[s] if t in day.todo_titles] for s in T5_SECTIONS}
        hub = hub_sections(cands)
        for s in T5_SECTIONS:
            check(f"{label}: {s} to-dos", todos[s], hub[s][:T5_CAP])
        check(f"{label}: T5 rules re-implemented", todos,
              t5_rules(feed, None if w is None else day.table))
        events = {s: sorted(t for t in expected[s] if t in day.event_titles) for s in T5_SECTIONS}
        check(f"{label}: events in the same sections", events,
              {s: sorted(v) for s, v in hub_sections(cands, "event").items()})


def run_eval(data_dir: Path, fixture: Path = FIXTURE,
             t5_fixture: Path = T5_FIXTURE) -> list[Check]:
    """Run every case in ``data_dir`` (must not hold an earlier run). DATA_DIR and the
    owner time zone are set for the run and restored after."""
    from src import constants
    from src import insight_outcomes as io
    from src import today_insight as ti

    fx = load_fixture(fixture)
    data_dir = Path(data_dir)
    if (data_dir / "today_insight" / OWNER).exists():
        raise SetupError(f"{data_dir} already holds an eval run; use an empty directory")
    data_dir.mkdir(parents=True, exist_ok=True)
    tz = ZoneInfo(fx.get("timezone") or "UTC")
    saved = (constants.DATA_DIR, ti.owner_timezone, io._TITLES, io._LOCKS,
             io._load_notes_by_id)
    constants.DATA_DIR = str(data_dir)
    ti.owner_timezone = lambda owner: tz
    io._TITLES, io._LOCKS = {}, {}
    io._load_notes_by_id = lambda owner, ids: {}  # every note is in the inputs
    checks: list[Check] = []
    try:
        _cases(fx, checks)
        _t5_day(load_fixture(t5_fixture), checks)
    finally:
        (constants.DATA_DIR, ti.owner_timezone, io._TITLES, io._LOCKS,
         io._load_notes_by_id) = saved
    return checks


def table(checks: list[Check]) -> int:
    failed = 0
    for case in CASES:
        mine = [c for c in checks if c.case == case]
        ok = sum(c.ok for c in mine)
        print(f"[{case}] {ok}/{len(mine)}")
        for c in mine:
            mark = "ok  " if c.ok else "FAIL"
            print(f"  {mark} {c.step}")
            if not c.ok:
                failed += 1
                print(f"       expected: {json.dumps(c.expected, ensure_ascii=False, default=str)}")
                print(f"       got:      {json.dumps(c.actual, ensure_ascii=False, default=str)}")
    cases_ok = sum(all(c.ok for c in checks if c.case == case) for case in CASES)
    print(f"cases: {cases_ok}/{len(CASES)}")
    print(f"score {sum(c.ok for c in checks)}/{len(checks)}")
    return failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixture", type=Path, default=FIXTURE, help="the day fixture (JSON)")
    ap.add_argument("--t5-fixture", type=Path, default=T5_FIXTURE,
                    help="the T5 host test's day (JSON, a copy of the module's fixture)")
    ap.add_argument("--data-dir", type=Path, help="where to write the files (default: a temp dir)")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir")
    ap.add_argument("--json", action="store_true", help="print the checks and score as JSON")
    args = ap.parse_args(argv)
    temp = args.data_dir is None
    data_dir = Path(tempfile.mkdtemp(prefix="insight-eval-")) if temp else args.data_dir
    try:
        try:
            checks = run_eval(data_dir, args.fixture, args.t5_fixture)
        except SetupError as e:
            print(f"insight_eval: {e}", file=sys.stderr)
            return 2
    finally:
        if temp and not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)
    if args.json:
        failed = sum(not c.ok for c in checks)
        print(json.dumps({"score": sum(c.ok for c in checks), "total": len(checks),
                          "cases": {case: all(c.ok for c in checks if c.case == case)
                                    for case in CASES},
                          "checks": [{**asdict(c), "ok": c.ok} for c in checks]},
                         ensure_ascii=False, indent=1, default=str))
    else:
        failed = table(checks)
    if not temp or args.keep:
        print(f"files kept: {data_dir / 'today_insight'}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
