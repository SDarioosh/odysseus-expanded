"""Open loops and plans as Today insight candidates (src/insight_inputs.py) and their
labels in the prompt (src/today_insight.py). Fictional data only."""

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src import evidence_intake as ledger
from src import insight_inputs as ii
from src import today_insight as ti

TZ = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 7, tzinfo=timezone.utc)


def _row(id, title="Send the slides", status="open", with_=("Alice",), mine=True,
         due_date=None, created="2026-10-20T10:00:00+00:00", **kw):
    row = {"id": id, "status": status, "title": title, "text": title + " to Alice",
           "due_date": due_date, "due_time": None, "created_at": created,
           "link": f"http://sound.local/view/c{id}?sig=x#t=3", **kw}
    if with_ is not None:
        row["with"] = [{"id": f"p{i}", "name": n} for i, n in enumerate(with_)]
    if mine is not None:
        row["mine"] = mine
    return row


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- loops
def test_loop_candidate_shape(store):
    [c] = ii.loop_candidates("alice", TODAY, [_row(7, with_=("Alice Smith", "Bob"),
                                                  due_date="2026-10-22")])
    assert c == {"ref": "loop:7", "kind": "loop", "bucket": "due_today",
                 "title": "Send the slides", "date": "2026-10-22", "time": None,
                 "person": "Alice", "link": "http://sound.local/view/c7?sig=x#t=3",
                 "weight": 0}


def test_loops_need_mine_with_open_and_not_filed(store):
    rows = [_row(1), _row(2, mine=False), _row(3, with_=None), _row(4, with_=()),
            _row(5, status="done"), _row(6), _row(8, mine=None, owner="Me"),
            _row(9, mine=None, owner="Bob"), _row(10, with_=("Me",))]
    refs = [c["ref"] for c in ii.loop_candidates("alice", TODAY, rows, filed={"6"})]
    assert sorted(refs) == ["loop:1", "loop:8"]


def test_a_loop_names_its_person_in_its_text(store):
    rows = [_row(1, title="Buy milk", text="Buy milk", with_=("Ana", "Tom")),  # just there
            _row(2, title="Lend the tent", text="Lend Tom the tent", with_=("Ana", "Tom")),
            _row(3, title="Send ANA SILVA the photos", text="", with_=("Ana Silva",)),
            _row(4, title="Call Anabel", text="Call Anabel", with_=("Ana",)),  # not a word
            _row(5, title="Pay Tom's share", text="", with_=("Tom Berg",))]
    cands = ii.loop_candidates("alice", TODAY, rows, filed=set())
    assert {c["ref"]: c["person"] for c in cands} == {"loop:2": "Tom", "loop:3": "Ana",
                                                     "loop:5": "Tom"}
    # rows that aren't loops stay in the context lines
    lines = ti._action_item_lines("alice", rows, skip=["2", "3", "5"])
    assert lines == ["Buy milk", "Call Anabel"]


def test_filed_items_from_the_module_state(store):
    from src import module_action_items as mai

    mai.save_state("alice", {"items": {}, "filed": {"1": {"target": "todo"}}})
    refs = [c["ref"] for c in ii.loop_candidates("alice", TODAY, [_row(1), _row(2)])]
    assert refs == ["loop:2"]


def test_loops_by_due_date_then_newest_capped(store):
    rows = [_row(1, created="2026-10-01T00:00:00Z"), _row(2, created="2026-10-21T00:00:00Z"),
            _row(3, due_date="2026-10-25"), _row(4, due_date="2026-10-21"),
            _row(5, created="2026-10-10T00:00:00Z"), _row(6, created="2026-09-01T00:00:00Z"),
            _row(7, due_date="2026-11-30")]
    cands = ii.loop_candidates("alice", TODAY, rows)
    assert [c["ref"] for c in cands] == ["loop:4", "loop:3", "loop:7", "loop:2", "loop:5"]
    assert [c["bucket"] for c in cands[:3]] == ["overdue", "due_soon", "later"]


# --------------------------------------------------------------------------- plans
def _belief(id, attribute="plans", status="confirmed", first_seen="2026-10-20", about="owner",
            text="Plans to repaint the kitchen"):
    return {"id": id, "attribute": attribute, "status": status, "first_seen": first_seen,
            "about": about, "text": text, "shown_text": text}


def test_plans_window_status_and_order():
    beliefs = [_belief(1), _belief(2, attribute="decision", first_seen="2026-10-21",
                                   text="Decided to take the train to Berlin"),
               _belief(3, first_seen="2026-10-14"),  # eight days ago
               _belief(4, first_seen="2026-10-15"),  # seven days ago: in
               _belief(5, status="stale"), _belief(6, status="rejected"),
               _belief(7, attribute="likes"), _belief(8, about="person"),
               _belief(9, status="tentative", first_seen="2026-10-22")]
    cands = ii.plan_candidates("alice", TODAY, beliefs=beliefs)
    assert [c["ref"] for c in cands] == ["belief:9", "belief:2", "belief:1", "belief:4"]
    assert cands[1] == {"ref": "belief:2", "kind": "plan", "bucket": "undated",
                        "title": "Take the train to Berlin",
                        "text": "Decided to take the train to Berlin", "date": None,
                        "time": None, "attribute": "decision", "weight": 0}
    many = [_belief(i) for i in range(10, 20)]
    assert len(ii.plan_candidates("alice", TODAY, beliefs=many)) == 5


def test_plans_from_the_ledger(store):
    ledger.submit("alice", [ledger.Evidence(
        source="notes", source_id="n1:0", subject="", about="owner",
        text="Plans to visit Alice in Lisbon", link="/#note-n1", seen_first="2026-10-20",
        seen_last="2026-10-20", source_confirmed=True)], now=NOW)
    [b] = ledger.list_beliefs("alice")
    assert ledger.apply_structure("alice", b["id"], "plans", "visit Alice in Lisbon", now=NOW)
    [c] = ii.plan_candidates("alice", TODAY)
    assert c["ref"] == f"belief:{b['id']}" and c["title"] == "Visit Alice in Lisbon"


@pytest.mark.parametrize("text,value,title", [
    (("The owner plans to slow down feature development for Odysseus after phase 4 to "
      "focus on bug fixing."), None,
     "Slow down feature development for Odysseus after phase 4 to focus on bug fixing"),
    (("Marisol plans to have Odysseus save notes as flat PNGs, transcribe them, and feed "
      "them into the personal context system."), None,
     "Have Odysseus save notes as flat PNGs, transcribe them, and feed them into the…"),
    ("The owner decided to stabilise the base system before side projects.", None,
     "Stabilise the base system before side projects"),
    ("Plans to visit Alice in Lisbon", "visit Alice in Lisbon", "Visit Alice in Lisbon"),
])
def test_plan_titles_are_short_todo_phrases(text, value, title):
    got = ii.plan_title(text, value)
    assert got == title and len(got) <= ii.MAX_PLAN_TITLE


def test_beliefs_that_are_not_plans_are_left_out():
    beliefs = [_belief(1, text="Marisol believes a separate server is needed for agentic "
                                "tasks."),
               _belief(2, text="Marisol has been thinking of getting rid of his truck."),
               _belief(3, text="Marisol wants to sell the truck before winter.")]
    cands = ii.plan_candidates("alice", TODAY, beliefs=beliefs)
    assert [(c["ref"], c["title"]) for c in cands] == [
        ("belief:3", "Sell the truck before winter")]


def test_the_owners_correction_is_the_title_source():
    b = {**_belief(1, text="Plans to repaint the kitchen"), "value": "repaint kitchen",
         "owner_text": "Plans to repaint the kitchen blue",
         "shown_text": "Plans to repaint the kitchen blue"}
    [c] = ii.plan_candidates("alice", TODAY, beliefs=[b])
    assert c["title"] == "Repaint the kitchen blue"


def test_extra_candidates_fail_soft(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger locked")

    monkeypatch.setattr(ii, "plan_candidates", boom)
    out = ii.extra_candidates("alice", TODAY, [_row(1)], now=NOW)
    assert [c["ref"] for c in out] == ["loop:1"]
    monkeypatch.setattr(ii, "loop_candidates", boom)
    assert ii.extra_candidates("alice", TODAY, [_row(1)], now=NOW) == []


# ---------------------------------------------------------------- prompt and refs
CANDS = [{"ref": "todo:n1:0", "kind": "todo", "bucket": "due_today", "title": "Book the dentist",
          "date": "2026-10-22", "time": None},
         {"ref": "event:e1", "kind": "event", "bucket": "due_today", "title": "Team lunch",
          "date": "2026-10-22", "time": "12:00"}]
EXTRA = [{"ref": "loop:7", "kind": "loop", "bucket": "undated", "title": "Send the slides",
          "date": None, "time": None, "person": "Alice", "link": "http://sound.local/v/7",
          "weight": 0},
         {"ref": "belief:12", "kind": "plan", "bucket": "undated",
          "title": "Plans to repaint the kitchen", "date": None, "time": None,
          "attribute": "plans", "weight": 0}]


def test_labels_put_loops_and_plans_after():
    assert ti.labels(CANDS, EXTRA) == {"T1": "todo:n1:0", "E1": "event:e1", "L1": "loop:7",
                                       "P1": "belief:12"}
    assert ti.labels([*CANDS, *EXTRA]) == ti.labels(CANDS, EXTRA)
    assert ti.labels(CANDS) == {"T1": "todo:n1:0", "E1": "event:e1"}


def test_resolve_ref_accepts_l_p_and_exact_refs():
    pool = [*CANDS, *EXTRA]
    assert ti.resolve_ref("L1", pool) == "loop:7"
    assert ti.resolve_ref("p1", pool) == "belief:12"
    assert ti.resolve_ref("loop:7", pool) == "loop:7"
    assert ti.resolve_ref("belief:12", pool) == "belief:12"
    assert ti.resolve_ref("L2", pool) is None


def test_validate_keeps_person_and_link_on_loops():
    answer = {"headline": "h", "focus": [{"ref": "T1", "why": "today"}],
              "coming_up": [{"ref": "L1", "why": "Alice waits"}],
              "also": [{"ref": "P1", "why": "this week"}, {"ref": "L1", "why": "dup"}]}
    out = ti.validate(answer, CANDS, EXTRA)
    assert out["coming_up"] == [{"ref": "loop:7", "why": "Alice waits",
                                 "title": "Send the slides", "person": "Alice",
                                 "link": "http://sound.local/v/7"}]
    assert out["also"] == [{"ref": "belief:12", "why": "this week",
                            "title": "Plans to repaint the kitchen"}]
    assert ti.validate(answer, CANDS)["coming_up"] == []  # without extra: unknown


def _inputs(**kw):
    base = {"now": datetime(2026, 10, 22, 9, 0, tzinfo=TZ), "today": TODAY,
            "candidates": CANDS, "memory": [], "digest": None, "action_items": [],
            "extra": [], "routines": ""}
    return {**base, **kw}


def test_prompt_blocks_only_when_non_empty():
    user = ti.build_messages(_inputs())[1]["content"]
    assert "OPEN LOOPS" not in user and "PLANS THIS WEEK" not in user
    assert "ROUTINES" not in user
    user = ti.build_messages(_inputs(
        extra=EXTRA, routines="- Gym: Tuesdays around 18:00 (4 of the last 6 weeks)"))[1]["content"]
    assert ("OPEN LOOPS (things the person said they would do for someone; labels L…):\n"
            "- L1 Send the slides (for Alice)") in user
    assert "PLANS THIS WEEK (labels P…):\n- P1 Plans to repaint the kitchen" in user
    assert ("ROUTINES (habits seen in the calendar):\n"
            "- Gym: Tuesdays around 18:00 (4 of the last 6 weeks)") in user
    system = ti.build_messages(_inputs())[0]["content"]
    assert "L1" in system and "P1" in system


def test_loop_rows_leave_the_action_item_lines(store):
    rows = [_row(1), _row(2, with_=None, title="Water the plants")]
    raw = json.dumps(rows)
    assert len(ti._action_item_lines("alice", raw)) == 2
    lines = ti._action_item_lines("alice", rows, skip=["1"])
    assert lines == ["Water the plants to Alice"]


def test_gather_moves_loops_out_of_the_context(store, monkeypatch):
    import asyncio

    monkeypatch.setattr(ti, "owner_timezone", lambda owner: TZ)
    monkeypatch.setattr(ti, "_load_notes", lambda owner: [])
    monkeypatch.setattr(ti, "_load_events", lambda owner, now_local: [])
    monkeypatch.setattr(ti, "_load_memory", lambda owner, query=None: [])
    monkeypatch.setattr("src.insight_routines.summary", lambda owner, today, now=None: "")
    rows = [_row(1), _row(2, with_=None, title="Water the plants")]

    async def call(tool, args):
        return json.dumps(rows) if tool == "list_action_items" else "No digest"

    inputs = asyncio.run(ti.gather("alice", now=NOW, call=call))
    assert [c["ref"] for c in inputs["extra"]] == ["loop:1"]
    assert inputs["action_items"] == ["Water the plants to Alice"]


def test_a_title_is_not_a_first_name(store):
    rows = [_row(1, title="Send Dr. Patel the forms", text="", with_=("Dr. Patel",)),
            _row(2, title="Book Dr. Weiss", text="", with_=("Dr. Patel",)),  # another doctor
            _row(3, title="Call Patel back", text="", with_=("Dr. Patel",))]
    cands = ii.loop_candidates("alice", TODAY, rows, filed=set())
    assert {c["ref"]: c["person"] for c in cands} == {"loop:1": "Dr. Patel",
                                                     "loop:3": "Dr. Patel"}
