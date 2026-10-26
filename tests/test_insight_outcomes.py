"""Today insight outcome log (src/insight_outcomes.py, routes/insight_outcome_routes.py).
Fictional data only."""

import asyncio
import json
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import insight_outcomes as io
from src import today_insight as ti
from tests.test_today_insight import GOOD, NOW, TZ, FakeLLM, _event, _note

DAY = "2026-10-21"
LATER = NOW + timedelta(hours=2)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ti, "owner_timezone", lambda owner: TZ)
    monkeypatch.setattr(ti, "_FILE_LOCKS", {})
    monkeypatch.setattr(ti, "_RUN_LOCKS", {})
    monkeypatch.setattr(io, "_TITLES", {})
    monkeypatch.setattr(io, "_LOCKS", {})
    monkeypatch.setattr(io, "_FINAL", {})
    archived = {}
    monkeypatch.setattr(io, "_load_notes_by_id",
                        lambda owner, ids: {i: archived[i] for i in ids if i in archived})
    return {"path": tmp_path, "archived": archived}


def _inputs(notes=(), events=(), rows=(), extra=(), now=NOW):
    now_local = now.astimezone(TZ)
    today = now_local.date()
    cands = ti.order_candidates(ti.todo_candidates(notes, today, TZ),
                                ti.event_candidates(events, now_local))
    return {"now": now_local, "today": today, "candidates": cands, "extra": list(extra),
            "notes_state": ti.notes_state(notes), "occurrences": list(events),
            "action_rows": list(rows)}


def _insight(day=DAY, votes=(), **sections):
    out = {"date": day, "generated": "x", "headline": "h", "input_hash": "0" * 64,
           "votes": list(votes)}
    for s in ti.SECTIONS:
        out[s] = [{"ref": r, "why": "because", "title": t} for r, t in sections.get(s, ())]
    return out


def _work(*items, due="2026-10-20"):
    return _note("n1", "Work", items, due_date=due)


def _item(ref, day=DAY):
    return io.load("alice", day)["items"][ref]


DENTIST = _event("e1", datetime(2026, 10, 21, 12), datetime(2026, 10, 21, 13), "Dentist",
                 event_type="health")


# ------------------------------------------------------------------ shown items
def test_first_section_and_position_are_kept_across_rewrites(store):
    notes = [_work(("Send the Q4 deck", False), ("Book flights", False))]
    inp = _inputs(notes, [DENTIST])
    io.observe("alice", _insight(focus=[("todo:n1:0", "Send the Q4 deck")],
                                 coming_up=[("event:e1", "Dentist")]), inp, now=NOW)
    io.observe("alice", _insight(focus=[("todo:n1:1", "Book flights")],
                                 also=[("event:e1", "Dentist"), ("todo:n1:0", "Send the Q4 deck")]),
               inp, now=LATER)
    data = io.load("alice", DAY)
    assert data["version"] == 1 and data["final"] is False and data["pushed"] is None
    assert data["items"]["todo:n1:0"] == {
        "kind": "todo", "section": "focus", "position": 1, "first_shown": "2026-10-21T07:00:00Z",
        "bucket": "overdue", "date": "2026-10-20", "note_id": "n1", "note_type": "checklist",
        "outcome": "open", "at": None, "via": None}
    assert data["items"]["event:e1"] == {
        "kind": "event", "section": "coming_up", "position": 1,
        "first_shown": "2026-10-21T07:00:00Z", "bucket": "due_today", "date": DAY,
        "event_type": "health", "importance": "normal", "outcome": "open", "at": None,
        "via": None}
    assert data["items"]["todo:n1:1"]["first_shown"] == "2026-10-21T09:00:00Z"


def test_no_titles_or_text_in_the_file(store):
    notes = [_work(("Send the Q4 deck to Tom", False))]
    loop = {"ref": "loop:a7", "kind": "loop", "bucket": "undated", "title": "Lend Mira the tent",
            "person": "Mira", "link": "http://sound/view/1", "weight": 0}
    io.observe("alice", _insight(focus=[("todo:n1:0", "Send the Q4 deck to Tom")],
                                 also=[("loop:a7", "Lend Mira the tent")]),
               _inputs(notes, extra=[loop], rows=[{"id": "a7", "status": "open"}]), now=NOW)
    text = (store["path"] / "today_insight" / "alice" / f"{DAY}.outcome.json").read_text()
    for word in ("Q4", "Tom", "tent", "Mira", "because", "http"):
        assert word not in text
    assert json.loads(text)["items"]["loop:a7"]["kind"] == "loop"


def test_vote_counts_are_copied(store):
    votes = [{"vote": "up", "at": "a"}, {"vote": "down", "at": "b"}, {"vote": "up", "at": "c"}]
    io.observe("alice", _insight(votes=votes), _inputs(), now=NOW)
    assert io.load("alice", DAY)["votes"] == {"up": 2, "down": 1}


# ------------------------------------------------------------------ observation
def test_done_by_index_and_untick(store):
    insight = _insight(focus=[("todo:n1:0", "Send the Q4 deck")])
    io.observe("alice", insight, _inputs([_work(("Send the Q4 deck", False))]), now=NOW)
    io.observe("alice", insight, _inputs([_work(("Send the Q4 deck", True))]), now=LATER)
    item = _item("todo:n1:0")
    assert (item["outcome"], item["via"], item["at"]) == ("done", "observed", "2026-10-21T09:00:00Z")
    io.observe("alice", insight, _inputs([_work(("Send the Q4 deck", False))]), now=LATER)
    assert _item("todo:n1:0")["outcome"] == "open"


def test_done_refound_by_title_after_lines_moved(store):
    insight = _insight(focus=[("todo:n1:1", "Book flights")])
    io.observe("alice", insight, _inputs([_work(("Send the Q4 deck", False),
                                                ("Book flights", False))]), now=NOW)
    # a line was removed above it and it was ticked; the rewritten insight no longer has it
    moved = [_work(("Book flights", True), ("Water the plants", False))]
    io.observe("alice", _insight(focus=[("todo:n1:1", "Water the plants")]),
               _inputs(moved), now=LATER)
    # todo:n1:1 names another line now: Book flights' record keeps following its line
    assert _item("todo:n1:1#1")["outcome"] == "done"
    assert _item("todo:n1:1#1")["section"] == "focus"
    assert _item("todo:n1:1")["outcome"] == "open"


def test_one_record_per_line_when_a_line_is_inserted_above(store):
    from src import insight_weights as iw

    io.observe("alice", _insight(focus=[("todo:n1:0", "Pay rent")]),
               _inputs([_work(("Pay rent", False))]), now=NOW)
    # the owner adds "Call bank" at the top; the next insight shows both
    notes = [_work(("Call bank", False), ("Pay rent", False))]
    rewritten = _insight(focus=[("todo:n1:0", "Call bank"), ("todo:n1:1", "Pay rent")])
    ti.save("alice", rewritten)
    io.observe("alice", rewritten, _inputs(notes), now=LATER)
    data = io.load("alice", DAY)
    assert sorted(data["items"]) == ["todo:n1:0", "todo:n1:1"]
    assert data["items"]["todo:n1:1"]["first_shown"] == "2026-10-21T07:00:00Z"  # Pay rent's
    assert data["items"]["todo:n1:0"]["first_shown"] == "2026-10-21T09:00:00Z"
    # Call bank ticked on the page, then seen by the pass: not overwritten
    io.record("alice", "todo:n1:0", "done", now=LATER)
    ticked = [_work(("Call bank", True), ("Pay rent", False))]
    io.observe("alice", rewritten, _inputs(ticked), now=LATER + timedelta(minutes=15))
    assert _item("todo:n1:0")["outcome"] == "done" and _item("todo:n1:1")["outcome"] == "open"
    io.finalize("alice", DAY, now=LATER + timedelta(hours=1))
    weights = iw.compute("alice", date(2026, 10, 22), now=NOW + timedelta(days=1))
    row = weights["keys"]["list:n1"]
    assert (row["shown"], row["done"], row["ignored"]) == (2, 1, 1)


def test_a_tick_on_a_rewritten_insight_before_the_pass(store):
    io.observe("alice", _insight(focus=[("todo:n1:0", "Pay rent")]),
               _inputs([_work(("Pay rent", False))]), now=NOW)
    rewritten = _insight(focus=[("todo:n1:0", "Call bank")])
    ti.save("alice", rewritten)  # the page shows it before the pass observes it
    assert io.record("alice", "todo:n1:0", "done", now=LATER)["first_shown"] == \
        "2026-10-21T09:00:00Z"
    assert _item("todo:n1:0#1")["outcome"] == "open"  # Pay rent, untouched
    with pytest.raises(LookupError):
        io.record("alice", "todo:n1:0#1", "done", now=LATER)


def test_moved_when_due_date_moves_later(store):
    insight = _insight(focus=[("todo:n1:0", "Send the Q4 deck")])
    io.observe("alice", insight, _inputs([_work(("Send the Q4 deck", False))]), now=NOW)
    io.observe("alice", insight, _inputs([_work(("Send the Q4 deck", False), due="2026-10-24")]),
               now=LATER)
    assert _item("todo:n1:0")["outcome"] == "moved"
    # done later still wins
    io.observe("alice", insight, _inputs([_work(("Send the Q4 deck", True), due="2026-10-24")]),
               now=LATER)
    assert _item("todo:n1:0")["outcome"] == "done"


def test_gone_and_archived_notes(store):
    insight = _insight(focus=[("todo:n1:0", "Send the Q4 deck")],
                       also=[("todo:n2:0", "Pack the tent")])
    notes = [_work(("Send the Q4 deck", False)), _note("n2", "Trip", [("Pack the tent", False)])]
    io.observe("alice", insight, _inputs(notes), now=NOW)
    # n1 deleted; n2 finished and auto-archived (not loaded by the pass)
    store["archived"]["n2"] = ti.notes_state([_note("n2", "Trip", [("Pack the tent", True)],
                                                    archived=True)])["n2"]
    io.observe("alice", insight, _inputs([]), now=LATER)
    assert _item("todo:n1:0")["outcome"] == "gone"
    assert _item("todo:n2:0")["outcome"] == "done"


def test_events_move_go_and_are_never_done(store):
    other = _event("e2", datetime(2026, 10, 22, 9), datetime(2026, 10, 22, 10), "Standup")
    insight = _insight(coming_up=[("event:e1", "Dentist"), ("event:e2", "Standup")])
    io.observe("alice", insight, _inputs(events=[DENTIST, other]), now=NOW)
    later = _event("e1", datetime(2026, 10, 23, 12), datetime(2026, 10, 23, 13), "Dentist")
    io.observe("alice", insight, _inputs(events=[later]), now=LATER)
    assert _item("event:e1")["outcome"] == "moved"
    assert _item("event:e2")["outcome"] == "gone"
    # a passed event stays open until the day is finalized
    io.observe("alice", _insight(coming_up=[("event:e3", "Lunch")]),
               _inputs(events=[_event("e3", datetime(2026, 10, 21, 8), datetime(2026, 10, 21, 8, 30))]),
               now=LATER)
    assert _item("event:e3")["outcome"] == "open"
    ti.save("alice", insight)
    with pytest.raises(ValueError):
        io.record("alice", "event:e1", "done", now=NOW)


def test_loops_done_gone_and_plans_untouched(store):
    loops = [{"ref": f"loop:{i}", "kind": "loop", "bucket": "undated", "title": f"Loop {i}",
              "weight": 0} for i in ("a1", "a2", "a3")]
    plan = {"ref": "belief:b1", "kind": "plan", "bucket": "undated", "title": "Move in spring",
            "weight": 0}
    insight = _insight(coming_up=[("loop:a1", "Loop a1"), ("loop:a2", "Loop a2"),
                                  ("loop:a3", "Loop a3")], also=[("belief:b1", "Move in spring")])
    rows = [{"id": "a1", "status": "open"}, {"id": "a2", "status": "open"},
            {"id": "a3", "status": "open"}]
    io.observe("alice", insight, _inputs(rows=rows, extra=[*loops, plan]), now=NOW)
    io.observe("alice", insight, _inputs(rows=[{"id": "a1", "status": "open"},
                                               {"id": "a2", "status": "done"}]), now=LATER)
    assert [_item(f"loop:{i}")["outcome"] for i in ("a1", "a2", "a3")] == ["open", "done", "gone"]
    assert _item("belief:b1")["kind"] == "plan" and _item("belief:b1")["outcome"] == "open"
    # module unreachable (no rows): loops keep their outcome
    io.observe("alice", insight, _inputs(), now=LATER)
    assert _item("loop:a1")["outcome"] == "open"


def test_a_closed_loop_is_done_a_deleted_one_gone(store):
    loops = [{"ref": f"loop:{i}", "kind": "loop", "bucket": "undated", "title": f"Loop {i}",
              "weight": 0} for i in ("a1", "a2")]
    insight = _insight(coming_up=[("loop:a1", "Loop a1"), ("loop:a2", "Loop a2")])
    io.observe("alice", insight, _inputs(rows=[{"id": "a1", "status": "open"},
                                               {"id": "a2", "status": "open"}], extra=loops),
               now=NOW)
    assert io.open_loop_ids("alice", date(2026, 10, 21)) == {"a1", "a2"}
    # both left the open items: a1 was closed, a2 deleted; nothing else is open
    inp = {**_inputs(), "action_rows_done": [{"id": "a1", "status": "done"}]}
    io.observe("alice", insight, inp, now=LATER)
    assert [_item(f"loop:{i}")["outcome"] for i in ("a1", "a2")] == ["done", "gone"]
    assert io.open_loop_ids("alice", date(2026, 10, 21)) == set()


def test_closed_loops_are_read_only_when_an_open_loop_left(running):
    calls = []
    open_rows = [{"id": 7, "status": "open", "mine": True, "title": "Lend Mira the tent",
                  "text": "Lend Mira the tent", "with": [{"id": "p1", "name": "Mira"}]}]

    async def call(tool, args):
        calls.append((tool, args))
        if tool == "list_action_items":
            rows = open_rows if args["status"] == "open" else [{"id": 7, "status": "done"}]
            return json.dumps(rows)
        return "No digest"

    answer = json.dumps({"headline": "h", "focus": [{"ref": "T1", "why": "today"}],
                         "coming_up": [{"ref": "L1", "why": "Mira waits"}], "also": []})
    st = asyncio.run(ti.run("alice", now=NOW, call=call, llm=FakeLLM(answer)))
    assert _item("loop:7")["outcome"] == "open"
    assert ("list_action_items", {"status": "done", "format": "json"}) not in calls
    open_rows.clear()  # closed in odysseus-sound
    calls.clear()
    asyncio.run(ti.run("alice", now=LATER, call=call, llm=FakeLLM(answer)))
    assert calls.count(("list_action_items", {"status": "done", "format": "json"})) == 1
    assert _item("loop:7")["outcome"] == "done"
    calls.clear()  # nothing open any more: not read again
    asyncio.run(ti.run("alice", now=LATER, call=call, llm=FakeLLM(answer)))
    assert ("list_action_items", {"status": "done", "format": "json"}) not in calls


# ------------------------------------------------------------------ owner marks
def test_owner_marks_are_not_overwritten_but_promoted_can_become_done(store):
    insight = _insight(focus=[("todo:n1:0", "Send the Q4 deck"), ("todo:n1:1", "Book flights")])
    ti.save("alice", insight)
    notes = [_work(("Send the Q4 deck", False), ("Book flights", False))]
    io.observe("alice", insight, _inputs(notes), now=NOW)
    assert io.record("alice", "todo:n1:0", "dismissed", now=NOW)["outcome"] == "dismissed"
    assert io.record("alice", "todo:n1:1", "promoted", now=NOW)["via"] == "today"
    assert io.owner_marks("alice", date(2026, 10, 21)) == {"dismissed": {"todo:n1:0"},
                                                          "promoted": ["todo:n1:1"]}
    io.observe("alice", insight, _inputs(notes), now=LATER)
    assert _item("todo:n1:0")["outcome"] == "dismissed"
    assert _item("todo:n1:1")["outcome"] == "promoted"
    ticked = [_work(("Send the Q4 deck", True), ("Book flights", True))]
    io.observe("alice", insight, _inputs(ticked), now=LATER)
    assert _item("todo:n1:0")["outcome"] == "dismissed"
    assert _item("todo:n1:1")["outcome"] == "done"
    # clear undoes a mark; open only un-ticks a done
    assert io.record("alice", "todo:n1:0", "clear", now=LATER)["outcome"] == "open"
    assert io.record("alice", "todo:n1:0", "open", now=LATER)["outcome"] == "open"
    assert io.record("alice", "todo:n1:1", "open", now=LATER)["outcome"] == "open"
    assert io.record("alice", "todo:n1:1", "done", now=LATER)["outcome"] == "done"
    with pytest.raises(LookupError):
        io.record("alice", "todo:zz:0", "done", now=LATER)
    with pytest.raises(ValueError):
        io.record("alice", "todo:n1:1", "maybe", now=LATER)


def test_a_mark_on_a_done_item_changes_nothing(store):
    insight = _insight(focus=[("todo:n1:0", "Send the Q4 deck")])
    ti.save("alice", insight)
    io.observe("alice", insight, _inputs([_work(("Send the Q4 deck", True))]), now=NOW)
    for mark in ("dismissed", "promoted"):
        item = io.record("alice", "todo:n1:0", mark, now=LATER)
        assert (item["outcome"], item["via"]) == ("done", "observed")


def test_finalize_counts_votes_cast_after_the_last_pass(store):
    insight = _insight(votes=[{"vote": "up", "at": "a"}])
    io.observe("alice", insight, _inputs(), now=NOW)
    ti.save("alice", {**insight, "votes": [{"vote": "up", "at": "a"},
                                           {"vote": "down", "at": "b"}]})
    assert io.finalize("alice", DAY, now=LATER)["votes"] == {"up": 1, "down": 1}


def test_record_without_insight_or_outcome_file(store):
    with pytest.raises(LookupError):
        io.record("alice", "todo:n1:0", "done", now=NOW)
    ti.save("alice", _insight(also=[("todo:n1:0", "Send the Q4 deck")]))
    item = io.record("alice", "todo:n1:0", "promoted", now=NOW)
    assert (item["section"], item["position"], item["note_id"]) == ("also", 1, "n1")


def test_apply_owner_order(store):
    insight = _insight(focus=[("todo:a:0", "A"), ("todo:b:0", "B")],
                       coming_up=[("event:c", "C")], also=[("todo:d:0", "D"), ("todo:e:0", "E")])
    outcome = {"items": {
        "todo:d:0": {"outcome": "promoted", "at": "2026-10-21T08:00:00Z"},
        "event:c": {"outcome": "promoted", "at": "2026-10-21T09:00:00Z"},
        "todo:b:0": {"outcome": "dismissed", "at": "2026-10-21T08:00:00Z"},
        "todo:a:0": {"outcome": "done", "at": "2026-10-21T08:00:00Z"}}}
    out = io.apply_owner_order(insight, outcome)
    assert [r["ref"] for r in out["focus"]] == ["event:c", "todo:d:0", "todo:a:0"]
    assert out["coming_up"] == [] and [r["ref"] for r in out["also"]] == ["todo:e:0"]
    assert [r["ref"] for r in insight["focus"]] == ["todo:a:0", "todo:b:0"]  # a copy
    assert io.apply_owner_order(insight, None) == insight
    assert [r["ref"] for r in io.dismissed_rows(insight, outcome)] == ["todo:b:0"]


def test_marked_rows_survive_a_rewrite_of_the_insight(store):
    notes = [_work(("Send the Q4 deck", False), ("Book flights", False))]
    first = _insight(focus=[("todo:n1:0", "Send the Q4 deck")], also=[("todo:n1:1", "Book flights")])
    ti.save("alice", first)
    io.observe("alice", first, _inputs(notes), now=NOW)
    io.record("alice", "todo:n1:0", "dismissed", now=NOW)
    io.record("alice", "todo:n1:1", "promoted", now=NOW)
    rewritten = _insight(focus=[("event:e1", "Dentist")])
    out = io.ordered("alice", rewritten)
    assert [r["ref"] for r in out["focus"]] == ["todo:n1:1", "event:e1"]
    assert out["focus"][0]["title"] == "Book flights"
    rows = io.dismissed_rows(rewritten, io.load("alice", DAY), "alice")
    assert rows == [{"ref": "todo:n1:0", "why": "", "title": "Send the Q4 deck", "section": "focus",
                     "not_task": False}]


def test_not_a_task_on_an_open_loop(store):
    loop = {"ref": "loop:7", "kind": "loop", "bucket": "undated", "title": "Move the truck",
            "weight": 0}
    insight = _insight(coming_up=[("loop:7", "Move the truck")], focus=[("event:e1", "Dentist")])
    ti.save("alice", insight)
    io.observe("alice", insight, _inputs(rows=[{"id": "7", "status": "open"}], extra=[loop]),
               now=NOW)
    item = io.record("alice", "loop:7", "dismissed", not_task=True, now=NOW)
    assert item["outcome"] == "dismissed" and item["not_task"] is True
    assert io.dismissed_rows(insight, io.load("alice", DAY)) == [
        {"ref": "loop:7", "why": "because", "title": "Move the truck", "section": "coming_up",
         "not_task": True}]
    # the module dismissed it: the next pass sees it gone, the mark stays
    io.observe("alice", insight, _inputs(rows=[{"id": "8", "status": "open"}]), now=LATER)
    assert _item("loop:7")["outcome"] == "dismissed" and _item("loop:7")["not_task"] is True
    # undo drops the flag
    item = io.record("alice", "loop:7", "clear", now=LATER)
    assert item["outcome"] == "open" and "not_task" not in item
    # only a dismissed open loop can be "not a task"
    with pytest.raises(ValueError):
        io.record("alice", "event:e1", "dismissed", not_task=True, now=NOW)
    with pytest.raises(ValueError):
        io.record("alice", "loop:7", "promoted", not_task=True, now=NOW)


# ------------------------------------------------------------------ finalize
def test_finalize_turns_open_into_ignored(store):
    insight = _insight(focus=[("todo:n1:0", "Send the Q4 deck"), ("todo:n1:1", "Book flights")],
                       coming_up=[("event:e1", "Dentist")])
    io.observe("alice", insight, _inputs([_work(("Send the Q4 deck", True), ("Book flights", False))],
                                         [DENTIST]), now=NOW)
    data = io.finalize("alice", DAY, now=LATER)
    assert data["final"] is True and data["finalized"] == "2026-10-21T09:00:00Z"
    assert data["items"]["todo:n1:0"]["outcome"] == "done"
    assert data["items"]["todo:n1:1"] == {**data["items"]["todo:n1:1"], "outcome": "ignored",
                                          "via": "final"}
    assert data["items"]["event:e1"]["outcome"] == "ignored"
    assert io.finalize("alice", DAY, now=NOW + timedelta(days=1))["finalized"] == data["finalized"]
    assert io.finalize("alice", "2026-10-01") == {}
    # a final day isn't observed any more
    io.observe("alice", insight, _inputs([_work(("Send the Q4 deck", True), ("Book flights", True))]),
               now=LATER)
    assert _item("todo:n1:1")["outcome"] == "ignored"


def test_the_first_pass_after_midnight_finalizes_yesterday(store):
    io.observe("alice", _insight(focus=[("todo:n1:0", "Send the Q4 deck")]),
               _inputs([_work(("Send the Q4 deck", False))]), now=NOW)
    io.observe("alice", _insight(day="2026-10-20"), _inputs(), now=NOW)  # an older day
    assert io.load("alice", "2026-10-20")["final"] is True  # older than today: closed at once
    tomorrow = datetime(2026, 10, 21, 22, 5, tzinfo=timezone.utc)  # 00:05 in Berlin
    io.observe("alice", _insight(day="2026-10-22"), _inputs(now=tomorrow), now=tomorrow)
    assert _item("todo:n1:0")["outcome"] == "ignored"
    assert io.load("alice", DAY)["final"] is True and io.load("alice", "2026-10-22")["final"] is False
    assert io.finalize_pending("alice", date(2026, 10, 22)) == 0
    assert [d["date"] for d in io.days("alice", date(2026, 10, 20), date(2026, 10, 21))] == \
        ["2026-10-20", DAY]


def test_finalize_pending_reads_the_window_once(store, monkeypatch):
    today = date(2026, 10, 21)
    for back in (1, 2, 28, 29, 40):
        day = (today - timedelta(days=back)).isoformat()
        io.save("alice", {**io.blank(day), "items": {
            "todo:n1:0": {"kind": "todo", "outcome": "open", "at": None, "via": None}}})
    io._TITLES[(ti._slug("alice"), "2026-10-20")] = {"todo:n1:0": "Pay rent"}
    reads = []
    real_load = io.load
    monkeypatch.setattr(io, "load", lambda owner, day: reads.append(day) or real_load(owner, day))
    assert io.finalize_pending("alice", today, now=NOW) == 3  # yesterday … 28 days back
    assert io.load("alice", "2026-09-22")["final"] is False  # 29 days back: left alone
    assert io.load("alice", "2026-09-11")["final"] is False
    assert (ti._slug("alice"), "2026-10-20") not in io._TITLES  # evicted with its day
    reads.clear()
    assert io.finalize_pending("alice", today, now=NOW) == 0
    assert reads == []  # final days aren't parsed again


def test_push_helpers(store):
    assert io.pushed("alice", DAY) is None and io.push_tries("alice", DAY) == 0
    assert io.note_push_try("alice", DAY) == 1 and io.note_push_try("alice", DAY) == 2
    io.mark_pushed("alice", DAY, now=NOW)
    assert io.pushed("alice", DAY) == "2026-10-21T07:00:00Z"
    assert io.load("alice", DAY)["push_tries"] == 2


# ------------------------------------------------------------------ the run
def _soon_dentist():
    """An event two hours from the real clock (for the routes, which use it)."""
    start = datetime.now(TZ).replace(tzinfo=None, microsecond=0) + timedelta(hours=2)
    return _event("e1", start, start + timedelta(hours=1), "Dentist")


@pytest.fixture
def running(store, monkeypatch):
    state = {"notes": [_note("n1", "Work", [("Send the Q4 deck", False)], due_date="2026-10-20"),
                       _note("n4", "Run 10k", [("Long run Sunday", False)], note_type="goal")],
             "events": [DENTIST]}
    monkeypatch.setattr(ti, "_load_notes", lambda owner: state["notes"])
    monkeypatch.setattr(ti, "_load_events", lambda owner, now_local: state["events"])
    monkeypatch.setattr(ti, "_load_memory", lambda owner, query=None: [])
    return state


def test_every_run_observes(running):
    asyncio.run(ti.run("alice", now=NOW, call=None, llm=FakeLLM(GOOD)))
    data = io.load("alice", DAY)
    assert set(data["items"]) == {"todo:n1:0", "event:e1", "todo:n4:0"}
    assert data["items"]["todo:n4:0"]["note_type"] == "goal"
    running["notes"][0].items = json.dumps([{"text": "Send the Q4 deck", "done": True}])
    asyncio.run(ti.run("alice", now=LATER, call=None, llm=FakeLLM(GOOD)))
    assert _item("todo:n1:0")["outcome"] == "done"


def test_a_page_tick_during_the_model_call_is_kept(running):
    asyncio.run(ti.run("alice", now=NOW, call=None, llm=FakeLLM(GOOD)))

    class TickWhileThinking(FakeLLM):
        async def __call__(self, messages):  # the owner ticks the deck on /today meanwhile
            running["notes"][0].items = json.dumps([{"text": "Send the Q4 deck", "done": True}])
            io.record("alice", "todo:n1:0", "done", now=LATER + timedelta(seconds=30))
            return await super().__call__(messages)

    running["events"] = []  # the inputs changed: the model is asked again
    asyncio.run(ti.run("alice", now=LATER, call=None, llm=TickWhileThinking(GOOD), force=True))
    assert _item("todo:n1:0")["outcome"] == "done"  # not undone by the older notes
    assert _item("todo:n1:0")["via"] == "today"


def test_dismissed_items_leave_the_candidates_and_the_chat_block(running):
    asyncio.run(ti.run("alice", now=NOW, call=None, llm=FakeLLM(GOOD)))
    io.record("alice", "todo:n4:0", "promoted", now=NOW)
    io.record("alice", "event:e1", "dismissed", now=NOW)
    block = ti.context_block("alice", now=NOW)
    assert "Do first: 1. Long run Sunday" in block and "Dentist" not in block
    inputs = asyncio.run(ti.gather("alice", now=NOW, call=None))
    assert "event:e1" not in [c["ref"] for c in inputs["candidates"]]


# ------------------------------------------------------------------ routes
def _client(owner="alice", token=False, scopes=()):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.middleware import require_admin
    from routes.today_insight_routes import setup_today_insight_routes

    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = "api" if token else owner
        request.state.api_token = token
        if token:
            request.state.api_token_owner = owner
            request.state.api_token_scopes = list(scopes)
        return await call_next(request)

    if not token:
        app.dependency_overrides[require_admin] = lambda: None
    app.include_router(setup_today_insight_routes())
    return TestClient(app)


def test_outcome_route_and_the_page_answer(running):
    running["events"] = [_soon_dentist()]
    client = _client()
    r = client.post("/api/insight/today/outcome", json={"ref": "todo:n1:0", "outcome": "done"})
    assert r.status_code == 404
    asyncio.run(ti.run("alice", call=None, llm=FakeLLM(GOOD)))  # today, real clock
    assert client.post("/api/insight/today/outcome",
                       json={"ref": "todo:zz:0", "outcome": "done"}).status_code == 404
    assert client.post("/api/insight/today/outcome",
                       json={"ref": "todo:n1:0", "outcome": "later"}).status_code == 422
    assert client.post("/api/insight/today/outcome",
                       json={"ref": "event:e1", "outcome": "done"}).status_code == 400
    r = client.post("/api/insight/today/outcome", json={"ref": "todo:n4:0", "outcome": "promoted"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["item"]["outcome"] == "promoted" and r.json()["item"]["via"] == "today"
    client.post("/api/insight/today/outcome", json={"ref": "event:e1", "outcome": "dismissed"})
    client.post("/api/insight/today/outcome", json={"ref": "todo:n1:0", "outcome": "done"})
    body = client.get("/api/insight/today").json()
    assert [row["ref"] for row in body["focus"]] == ["todo:n4:0", "todo:n1:0"]
    assert body["coming_up"] == [] and body["also"] == []
    assert body["outcomes"] == {"todo:n1:0": "done", "event:e1": "dismissed",
                                "todo:n4:0": "promoted"}
    assert [(d["ref"], d["section"], d["title"]) for d in body["dismissed"]] == \
        [("event:e1", "coming_up", "Dentist")]
    r = client.post("/api/insight/today/outcome", json={"ref": "event:e1", "outcome": "clear"})
    assert r.json()["item"]["outcome"] == "open"
    assert client.get("/api/insight/today").json()["dismissed"] == []
    assert body["dismissed"][0]["not_task"] is False
    r = client.post("/api/insight/today/outcome",
                    json={"ref": "event:e1", "outcome": "dismissed", "not_task": True})
    assert r.status_code == 400


def test_token_route_applies_the_owner_order_without_outcomes(running):
    running["events"] = [_soon_dentist()]
    asyncio.run(ti.run("alice", call=None, llm=FakeLLM(GOOD)))
    io.record("alice", "todo:n4:0", "promoted")
    io.record("alice", "event:e1", "dismissed")
    body = _client(token=True, scopes=["insight:read"]).get("/api/codex/insight/today").json()
    assert set(body) == {"date", "generated", "headline", "focus", "coming_up", "also"}
    assert [r["ref"] for r in body["focus"]] == ["todo:n4:0", "todo:n1:0"]
    assert body["coming_up"] == []


def test_outcome_route_refuses_api_tokens(running):
    client = _client(token=True, scopes=["insight:read"])
    r = client.post("/api/insight/today/outcome", json={"ref": "todo:n1:0", "outcome": "done"})
    assert r.status_code in (401, 403)


# ------------------------------------------------------------------ long lines
LONG = "Call the landlord about the heating " + "and the window in the back room " * 7


def test_a_line_longer_than_the_title_is_matched(store):
    assert len(LONG) > 200
    [cand] = ti.todo_candidates([_work((LONG, False))], date(2026, 10, 21), TZ)
    insight = _insight(focus=[("todo:n1:0", cand["title"])])
    io.observe("alice", insight, _inputs([_work((LONG, False))]), now=NOW)
    assert _item("todo:n1:0")["outcome"] == "open"
    io.observe("alice", insight, _inputs([_work(("Water the plants", False), (LONG, True))]),
               now=LATER)  # moved down and ticked: re-found by its first 200 characters
    assert _item("todo:n1:0")["outcome"] == "done"


# ------------------------------------------------------------------ the page's helpers
_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "today.js"


def _page_functions(*names):
    """The named top-level functions of static/js/today.js, as source."""
    src = _JS.read_text(encoding="utf-8")
    out = []
    for name in names:
        m = re.search(rf"^function {name}\(.*?^}}$", src, re.S | re.M)
        assert m, name
        out.append(m.group(0))
    return "\n".join(out)


def _node(js):
    proc = subprocess.run(["node", "--input-type=module"], input=js, capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")
def test_the_page_finds_a_long_line_by_its_first_200_characters():
    title = " ".join(LONG.split())[:200]
    note = {"items": [{"text": "Water the plants"}, {"text": LONG}]}
    got = _node(_page_functions("lineKey", "sameText", "itemIndex") + f"""
    const note = {json.dumps(note)};
    console.log(JSON.stringify([itemIndex(note, {{index: 1}}, {json.dumps(title)}),
                                itemIndex(note, {{index: 0}}, {json.dumps(title)}),
                                itemIndex(note, {{index: 0}}, 'Water the plants')]));
    """)
    assert got == [1, 1, 0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")
def test_the_page_only_links_to_web_pages_and_site_paths():
    hrefs = ["https://sound.local/view/1", "/beliefs", "//evil.example/x", "/\\evil.example",
             "/\\/evil.example", "javascript:alert(1)", ""]
    got = _node(_page_functions("safeLink") +
                f"console.log(JSON.stringify({json.dumps(hrefs)}.map(safeLink)));")
    assert got == ["https://sound.local/view/1", "/beliefs", None, None, None, None, None]


@pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")
def test_not_a_task_dismisses_the_module_item():
    src = _JS.read_text(encoding="utf-8")
    fn = re.search(r"^async function setLoopStatus\(.*?^}$", src, re.S | re.M).group(0)
    got = _node("""
    const posted = [];
    async function postJSON(path, body) {
      posted.push([path, body]);
      return path.endsWith('/9') ? { ok: false, message: 'No action item #9.' } : { ok: true };
    }
    """ + fn + """
    const errors = [];
    for (const ref of ['loop:7', 'loop:9', 'todo:n1:0']) {
      try { await setLoopStatus(ref, 'dismissed'); } catch (e) { errors.push(e.message); }
    }
    console.log(JSON.stringify({ posted, errors }));
    """)
    assert got["posted"] == [["/api/modules/sound/action-items/7", {"status": "dismissed"}],
                             ["/api/modules/sound/action-items/9", {"status": "dismissed"}]]
    assert got["errors"] == ["No action item #9.", "not an open loop"]
