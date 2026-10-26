"""Today insight weights (src/insight_weights.py, routes/insight_weights_routes.py).
Fictional data only. Outcome files are written in the contract's format
(docs/fork/today-insight.md#outcome-log)."""

import json
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src import insight_weights as iw
from src import today_insight as ti

TZ = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 7, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ti, "owner_timezone", lambda owner: TZ)
    monkeypatch.setattr(ti, "_FILE_LOCKS", {})
    monkeypatch.setattr(ti, "_RUN_LOCKS", {})
    return tmp_path


def _todo(note, outcome, note_type="checklist", index=0):
    return (f"todo:{note}:{index}", {"kind": "todo", "section": "focus", "position": 1,
                                      "bucket": "undated", "date": None, "note_id": note,
                                      "note_type": note_type, "outcome": outcome})


def _ev(uid, outcome, event_type="work", importance="normal"):
    return (f"event:{uid}", {"kind": "event", "section": "coming_up", "position": 1,
                             "bucket": "due_today", "event_type": event_type,
                             "importance": importance, "outcome": outcome})


def _write_day(root, day, items, final=True, owner="alice"):
    path = root / "today_insight" / owner / f"{day.isoformat()}.outcome.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "date": day.isoformat(), "final": final,
                                "finalized": None, "votes": {"up": 0, "down": 0},
                                "items": dict(items)}))


def _history(root, per_note, today=TODAY, owner="alice"):
    """per_note: {(note, note_type): [outcome, ...]}, one outcome per day going back
    from yesterday, so each (ref, day) is shown once."""
    days = {}
    for (note, note_type), outcomes in per_note.items():
        for i, outcome in enumerate(outcomes):
            days.setdefault(today - timedelta(days=i + 1), []).append(
                _todo(note, outcome, note_type))
    for day, items in days.items():
        _write_day(root, day, items, owner=owner)


def _keys(w):
    return {k: v["weight"] for k, v in w["keys"].items()}


# ------------------------------------------------------------------ the formula
def test_round_half_away_from_zero():
    assert [iw.round_half_away(x) for x in (2.5, -2.5, 1.5, -0.5, 0.49, -0.49, 0)] == \
        [3, -3, 2, -1, 0, 0, 0]
    assert iw.round_half_away(Fraction(5, 2)) == 3


def test_weights_with_base_subtraction_and_negative_weights(store):
    # n1: 6 done, 2 ignored -> score 5, rate 5/8; n2: 6 ignored -> -3, rate -1/2
    # base = 2/14 = 1/7; n1: 3*(5/8 - 1/7) = 81/56 -> 1; n2: 3*(-9/14) = -27/14 -> -2
    _history(store, {("n1", "checklist"): ["done"] * 6 + ["ignored"] * 2,
                     ("n2", "todo"): ["ignored"] * 6})
    w = iw.compute("alice", TODAY, now=NOW)
    assert w["base"] == {"todo": round(1 / 7, 4), "event": 0.0}
    assert _keys(w) == {"list:n1": 1, "note_type:checklist": 1,
                        "list:n2": -2, "note_type:todo": -2}
    row = w["keys"]["list:n1"]
    assert row == {"weight": 1, "shown": 8, "done": 6, "promoted": 0, "dismissed": 0,
                   "moved": 0, "ignored": 2, "reset_at": None}
    assert set(w) == {"version", "date", "computed", "window_days", "min_shown", "base",
                      "keys", "resets"}
    assert w["date"] == "2026-10-22" and w["window_days"] == 28 and w["min_shown"] == 6


def test_exact_halves_round_away_and_same_type_cancels(store):
    # a: 6 promoted -> rate 2; b: 4 done, 2 moved -> rate 1/3; base 14/12 = 7/6
    # a: 3*(5/6) = 2.5 -> 3; b: -2.5 -> -3; note_type:goal = base -> 0
    _history(store, {("a", "goal"): ["promoted"] * 6, ("b", "goal"): ["done"] * 4 + ["moved"] * 2})
    w = iw.compute("alice", TODAY)
    assert _keys(w) == {"list:a": 3, "list:b": -3, "note_type:goal": 0}


def test_key_clamp_and_min_shown_gate(store):
    # big: 6 promoted (rate 2); small: 6 dismissed (rate -2); base 0 -> ±6 clamped to ±3
    # few: 5 promoted, under MIN_SHOWN -> 0 though it has counts
    _history(store, {("big", "checklist"): ["promoted"] * 6,
                     ("small", "todo"): ["dismissed"] * 6,
                     ("few", "goal"): ["promoted"] * 5})
    w = iw.compute("alice", TODAY)
    assert w["keys"]["list:big"]["weight"] == 3 and w["keys"]["list:small"]["weight"] == -3
    assert w["keys"]["list:few"]["weight"] == 0 and w["keys"]["list:few"]["shown"] == 5


def test_candidate_weight_sums_and_clamps():
    weights = {"keys": {"list:x": {"weight": 3}, "note_type:goal": {"weight": 3},
                        "list:bad": {"weight": 9}, "event_type:work": {"weight": -2},
                        "importance:high": {"weight": -3}}}
    todo = {"ref": "todo:x:0", "kind": "todo", "note_id": "x", "note_type": "goal"}
    assert iw.candidate_weight(todo, weights) == 6
    assert iw.candidate_weight({**todo, "note_id": "bad", "ref": "todo:bad:0"}, weights) == 6
    ev = {"ref": "event:e", "kind": "event", "event_type": "work", "importance": "high"}
    assert iw.candidate_weight(ev, weights) == -5
    assert iw.candidate_weight({"ref": "loop:1", "kind": "loop"}, weights) == 0
    assert iw.candidate_weight(todo, None) == 0 and iw.candidate_weight(todo, {}) == 0
    c = iw.contributions(todo, weights)
    assert [(r["key"], r["weight"]) for r in c] == [("note_type:goal", 3), ("list:x", 3)]
    assert c[0]["shown"] == 0 and c[0]["reset_at"] is None


def test_event_keys_ignore_done_and_ignored(store):
    # work: 3 done (no signal) + 3 promoted -> score 6, rate 1
    # health: 3 done + 3 ignored -> 0; base 6/12 = 1/2
    # work: 3*(1/2) = 1.5 -> 2; health -> -2; importance:normal = base -> 0
    for i in range(6):
        day = TODAY - timedelta(days=i + 1)
        _write_day(store, day, [_ev(f"w{i}", "done" if i < 3 else "promoted", "work"),
                                _ev(f"h{i}", "done" if i < 3 else "ignored", "health")])
    w = iw.compute("alice", TODAY)
    assert w["base"]["event"] == 0.5
    assert _keys(w) == {"event_type:work": 2, "event_type:health": -2, "importance:normal": 0}
    assert w["keys"]["event_type:work"]["done"] == 3


def test_gone_is_ignored(store):
    # 6 done + 3 gone: gone neither shown nor scored
    _history(store, {("n1", "checklist"): ["done"] * 6 + ["gone"] * 3,
                     ("n2", "checklist"): ["ignored"] * 6})
    w = iw.compute("alice", TODAY)
    assert w["keys"]["list:n1"]["shown"] == 6
    # rate 1 vs base (6-3)/12 = 1/4 -> 3*(3/4) = 2.25 -> 2
    assert w["keys"]["list:n1"]["weight"] == 2


def test_window_and_non_final_days(store):
    items = [_todo("n1", "promoted")]
    _write_day(store, TODAY - timedelta(days=29), items)          # outside the window
    _write_day(store, TODAY - timedelta(days=28), items)          # first day inside
    _write_day(store, TODAY - timedelta(days=2), items, final=False)  # not finalized
    _write_day(store, TODAY, items)                               # today never counts
    w = iw.compute("alice", TODAY)
    assert w["keys"]["list:n1"]["shown"] == 1


def test_broken_outcome_files_are_skipped(store):
    path = store / "today_insight" / "alice" / f"{(TODAY - timedelta(days=1)).isoformat()}.outcome.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    _write_day(store, TODAY - timedelta(days=2), [("todo:n1:0", "nonsense"), _todo("n2", "done")])
    w = iw.compute("alice", TODAY)
    assert set(w["keys"]) == {"list:n2", "note_type:checklist"}


# ------------------------------------------------------------------ lazy recompute
def test_current_recomputes_once_a_day(store):
    _history(store, {("n1", "checklist"): ["promoted"] * 6, ("n2", "todo"): ["ignored"] * 6})
    first = iw.current("alice", TODAY, now=NOW)
    path = store / "today_insight" / "alice" / "weights.json"
    assert json.loads(path.read_text()) == first
    assert first["keys"]["list:n1"]["weight"] == 3
    # more outcomes land during the day: today's weights stay put
    _history(store, {("n1", "checklist"): ["dismissed"] * 10})
    assert iw.current("alice", TODAY) == first
    nxt = iw.current("alice", TODAY + timedelta(days=1))
    assert nxt["date"] == "2026-10-23" and nxt != first


def test_current_survives_a_broken_file(store):
    path = store / "today_insight" / "alice" / "weights.json"
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2")
    w = iw.current("alice", TODAY)
    assert w["keys"] == {} and w["date"] == "2026-10-22"
    assert json.loads(path.read_text())["date"] == "2026-10-22"


# ------------------------------------------------------------------ reset
def test_reset_one_key(store):
    _history(store, {("n1", "checklist"): ["promoted"] * 6, ("n2", "todo"): ["ignored"] * 6})
    before = iw.current("alice", TODAY, now=NOW)
    assert before["keys"]["list:n1"]["weight"] == 3
    after = iw.reset("alice", "list:n1", now=NOW)
    assert after["keys"]["list:n1"] == {"weight": 0, "shown": 0, "done": 0, "promoted": 0,
                                        "dismissed": 0, "moved": 0, "ignored": 0,
                                        "reset_at": "2026-10-22"}
    assert after["resets"] == {"list:n1": "2026-10-22"}
    assert after["keys"]["list:n2"]["weight"] == before["keys"]["list:n2"]["weight"]
    assert iw.current("alice", TODAY) == after  # stored for today
    with pytest.raises(ValueError):
        iw.reset("alice", "nope", now=NOW)


def test_outcomes_before_a_reset_are_ignored(store):
    _history(store, {("n1", "checklist"): ["promoted"] * 6, ("n2", "todo"): ["ignored"] * 6})
    iw.reset("alice", "list:n1", now=NOW)
    # the reset day itself and earlier days no longer count; later days do
    later = TODAY + timedelta(days=7)
    for i in range(7):
        _write_day(store, TODAY + timedelta(days=i), [_todo("n1", "dismissed")])
    w = iw.current("alice", later)
    assert w["keys"]["list:n1"]["shown"] == 6  # days TODAY+1 … TODAY+6
    assert w["keys"]["list:n1"]["dismissed"] == 6 and w["keys"]["list:n1"]["weight"] < 0
    assert w["keys"]["note_type:checklist"]["shown"] == 13  # not reset: all days


def test_reset_all_includes_days_not_final_yet(store):
    _history(store, {("n1", "checklist"): ["promoted"] * 6})
    _write_day(store, TODAY - timedelta(days=1), [_todo("n9", "done", "goal"),
                                                  _todo("n1", "promoted")], final=False)
    w = iw.reset("alice", None, now=NOW)
    assert all(v["weight"] == 0 and v["shown"] == 0 for v in w["keys"].values())
    assert set(w["resets"]) == {"list:n1", "note_type:checklist", "list:n9", "note_type:goal"}
    assert set(w["resets"].values()) == {"2026-10-22"}


def test_old_resets_drop_out_of_the_window(store):
    iw.reset("alice", "list:n1", now=NOW)
    w = iw.current("alice", TODAY + timedelta(days=29))
    assert w["resets"] == {} and "list:n1" not in w["keys"]


# ------------------------------------------------------------------ rule order
def _note(id, title="Errands", items=(), note_type="checklist", due_date=None, pinned=False):
    return SimpleNamespace(id=id, title=title, note_type=note_type, due_date=due_date,
                           pinned=pinned, archived=False, source="user", label=None,
                           items=json.dumps([{"text": t, "done": False} for t in items]))


def test_weights_never_cross_tiers():
    weights = {"keys": {"list:hate": {"weight": -3}, "note_type:todo": {"weight": -3},
                        "list:love": {"weight": 3}, "note_type:goal": {"weight": 3}}}
    todos = ti.todo_candidates([
        _note("hate", "Taxes", ["Overdue thing"], note_type="todo", due_date="2026-10-20"),
        _note("plain", "Misc", ["Due today thing"], due_date="2026-10-22"),
        _note("love", "Run", ["Soon thing"], note_type="goal", due_date="2026-10-24"),
        _note("u1", "Someday", ["Undated plain"]),
        _note("love2", "Run 2", ["Undated loved"], note_type="goal"),
        _note("hate2", "Chores", ["Undated hated"], note_type="todo"),
    ], TODAY, TZ, names=set())
    ordered = ti.order_candidates(todos, [], weights=weights)
    assert [(c["title"], c["weight"]) for c in ordered] == [
        ("Due today thing", 0), ("Overdue thing", -6),   # tier 0: weight first
        ("Soon thing", 6),                               # tier 1
        ("Undated loved", 3), ("Undated plain", 0), ("Undated hated", -3)]  # tier 3


# ------------------------------------------------------------------ T5 table
def _write_weights(root, keys, day=TODAY, owner="alice"):
    path = root / "today_insight" / owner / "weights.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "date": day.isoformat(), "keys": {
        k: {"weight": v} for k, v in keys.items()}, "resets": {}}))


def test_t5_table_merges_drops_zeros_clamps_and_limits(store):
    keys = {"note_type:checklist": 1, "list:a": 2, "list:b": -1, "list:c": 5,
            "note_type:goal": 3, "list:g": 3}
    keys.update({f"list:z{i:02d}": -2 for i in range(40)})
    _write_weights(store, keys)
    notes = [SimpleNamespace(id="a", note_type="checklist"),
             SimpleNamespace(id="b", note_type="checklist"),   # -1 + 1 = 0: dropped
             SimpleNamespace(id="c", note_type="todo"),        # 5 clamped to 3
             SimpleNamespace(id="g", note_type="goal"),        # 3 + 3 = 6
             SimpleNamespace(id="plain", note_type="checklist"),  # 1 from the type
             *[SimpleNamespace(id=f"z{i:02d}", note_type="checklist") for i in range(40)]]
    t = iw.t5_table("alice", TODAY, notes=notes)
    assert t["version"] == 1 and t["date"] == "2026-10-22"
    w = t["weights"]
    assert len(w) == 32
    assert list(w)[:3] == ["list:g", "list:a", "list:c"]  # |6|, then |3| by key
    assert w["list:g"] == 6 and w["list:a"] == 3 and w["list:c"] == 3
    assert "list:b" not in w  # -1 + 1 = 0
    assert w["list:plain"] == 1 and w["list:z00"] == -1 and w["list:z27"] == -1
    assert "list:z28" not in w  # past the limit of 32
    assert all(-6 <= v <= 6 and v for v in w.values())


def test_t5_table_empty(store):
    assert iw.t5_table("alice", TODAY, notes=[]) == {"version": 1, "date": "2026-10-22",
                                                      "weights": {}}


# ------------------------------------------------------------------ routes
def _real_today():
    return ti._now_local(TZ).date()


@pytest.fixture
def page(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.middleware import require_admin
    from routes.insight_weights_routes import setup_insight_weights_routes

    state = {"notes": [], "events": []}
    monkeypatch.setattr(ti, "_load_notes", lambda owner: state["notes"])
    monkeypatch.setattr(ti, "_load_events", lambda owner, now_local: state["events"])
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = "alice"
        request.state.api_token = False
        return await call_next(request)

    app.dependency_overrides[require_admin] = lambda: None
    app.include_router(setup_insight_weights_routes())
    state["client"] = TestClient(app)
    return state


def _why_setup(store, page):
    today = _real_today()
    page["notes"] = [_note("n1", "Work", ["Send the Q4 deck"],
                           due_date=(today - timedelta(days=1)).isoformat()),
                     _note("n2", "Home", ["Fix the shelf"])]
    tomorrow = (today + timedelta(days=1)).isoformat()
    page["events"] = [{"uid": "e1", "dtstart": f"{tomorrow}T10:00:00",
                       "dtend": f"{tomorrow}T11:00:00", "summary": "Call Mira",
                       "all_day": False, "is_utc": False, "importance": "normal",
                       "event_type": "personal"}]
    # n1: 7 done, 1 promoted, 1 ignored -> 8.5/9; n2: 9 ignored -> -1/2; base 2/9
    # list:n1 = round(3 * 13/18) = 2; note_type:checklist = base -> 0
    _history(store, {("n1", "checklist"): ["done"] * 7 + ["promoted", "ignored"],
                     ("n2", "checklist"): ["ignored"] * 9}, today=today)
    ti.save("alice", {"date": today.isoformat(), "generated": "x", "headline": "h",
                      "focus": [{"ref": "todo:n1:0", "why": "w", "title": "Send the Q4 deck"}],
                      "coming_up": [{"ref": "event:e1", "why": "w", "title": "Call Mira"}],
                      "also": [], "input_hash": "", "votes": []})
    return today


def test_weights_route_rows_and_labels(store, page):
    _why_setup(store, page)
    page["notes"] = [n for n in page["notes"] if n.id == "n1"]  # n2 deleted since
    body = page["client"].get("/api/insight/weights").json()
    assert body["min_shown"] == 6 and body["window_days"] == 28
    rows = {r["key"]: r for r in body["rows"]}
    assert rows["list:n1"]["label"] == "Work" and rows["list:n1"]["weight"] == 2
    assert rows["list:n2"]["label"] == "a list that no longer exists"
    assert rows["note_type:checklist"]["label"] == "Checklist notes"
    assert body["rows"][0]["key"] == "list:n1"
    assert rows["list:n1"]["done"] == 7 and rows["list:n1"]["reset_at"] is None


def test_reset_route(store, page):
    today = _why_setup(store, page)
    c = page["client"]
    r = c.post("/api/insight/weights/reset", json={"key": "list:n1"})
    rows = {x["key"]: x for x in r.json()["rows"]}
    assert r.json()["ok"] is True and rows["list:n1"]["weight"] == 0
    assert rows["list:n1"]["reset_at"] == today.isoformat()
    assert c.post("/api/insight/weights/reset", json={"key": "bad key"}).status_code == 400
    rows = c.post("/api/insight/weights/reset", json={}).json()["rows"]
    assert rows and all(x["weight"] == 0 and x["shown"] == 0 for x in rows)


def test_why_route(store, page):
    today = _why_setup(store, page)
    c = page["client"]
    body = c.get("/api/insight/today/why", params={"ref": "todo:n1:0"}).json()
    yday = today - timedelta(days=1)
    assert body["section"] == "focus" and body["position"] == 1
    assert body["tier"] == "overdue or due today" and body["rule_position"] == 1
    assert body["weight"] == 2 and body["picked_by"] == "model"
    assert [(x["key"], x["weight"], x["label"]) for x in body["contributions"]] == [
        ("note_type:checklist", 0, "Checklist notes"), ("list:n1", 2, "Work")]
    assert body["text"] == (f"Overdue since {yday:%b} {yday.day}; 1st in the rule order; "
                            "your Work list counts +2 (you finished 7 of 9, moved up 1). "
                            "The model put it first.")
    ev = c.get("/api/insight/today/why", params={"ref": "event:e1"}).json()
    tm = today + timedelta(days=1)
    assert ev["rule_position"] == 2 and ev["tier"] == "due in the next 7 days"
    assert ev["text"] == (f"On {tm:%b} {tm.day} at 10:00; 2nd in the rule order. "
                          "The model put it 1st in Coming up.")
    assert c.get("/api/insight/today/why", params={"ref": "todo:zz:0"}).status_code == 404
    # the owner moved it up
    _write_day(store, today, [("event:e1", {"kind": "event", "outcome": "promoted"})],
               final=False)
    ev = c.get("/api/insight/today/why", params={"ref": "event:e1"}).json()
    assert ev["picked_by"] == "owner" and ev["text"].endswith("You moved it up.")


def test_why_route_without_insight(store, page):
    r = page["client"].get("/api/insight/today/why", params={"ref": "todo:n1:0"})
    assert r.status_code == 404 and r.json() == {"error": "no insight yet"}


def test_token_route_scope(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes.insight_weights_routes import setup_insight_weights_routes

    monkeypatch.setattr(ti, "_load_notes", lambda owner: [SimpleNamespace(id="n1", note_type="checklist")])
    state = {"scopes": ["todos:read"]}
    app = FastAPI()

    @app.middleware("http")
    async def fake_token(request, call_next):
        request.state.api_token = True
        request.state.current_user = "api"
        request.state.api_token_owner = "alice"
        request.state.api_token_scopes = state["scopes"]
        return await call_next(request)

    app.include_router(setup_insight_weights_routes())
    client = TestClient(app)
    assert client.get("/api/codex/insight/weights").status_code == 403
    assert client.get("/api/insight/weights").status_code in (401, 403)
    state["scopes"] = ["insight:read"]
    r = client.get("/api/codex/insight/weights")
    assert r.status_code == 200
    assert r.json() == {"version": 1, "date": _real_today().isoformat(), "weights": {}}
    _write_weights(store, {"list:n1": 2}, day=_real_today())
    assert client.get("/api/codex/insight/weights").json()["weights"] == {"list:n1": 2}


def test_why_text_names_the_skips_behind_a_negative_weight():
    from routes.insight_weights_routes import _because

    row = {"key": "list:n1", "shown": 28, "done": 0, "dismissed": 28}
    assert _because(row) == "you finished 0 of 28, skipped 28"
