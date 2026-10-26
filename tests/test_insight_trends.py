"""Today insight trends (src/insight_trends.py, routes/insight_trends_routes.py).
Fictional data only; files are written in the contract's formats
(docs/fork/today-insight.md#outcome-log)."""

import json
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import insight_trends as it
from src import today_insight as ti

TZ = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 10, 22)  # a Thursday, ISO week 2026-W43


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ti, "owner_timezone", lambda owner: TZ)
    return tmp_path / "today_insight" / "alice"


def _insight(folder, day, votes=()):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{day}.json").write_text(json.dumps(
        {"date": str(day), "headline": "Send the Q4 deck first.", "focus": [], "coming_up": [],
         "also": [], "votes": [{"vote": v, "at": f"{day}T08:00:00Z"} for v in votes]}))


def _outcome(folder, day, outcomes, up=0, down=0, final=True):
    folder.mkdir(parents=True, exist_ok=True)
    items = {f"todo:n1:{i}": {"kind": "todo", "outcome": o} for i, o in enumerate(outcomes)}
    (folder / f"{day}.outcome.json").write_text(json.dumps(
        {"version": 1, "date": str(day), "final": final, "votes": {"up": up, "down": down},
         "items": items}))


def test_empty_weeks_are_zeros(store):
    out = it.trends("alice", TODAY, 3)
    assert [w["week"] for w in out["weeks"]] == ["2026-W43", "2026-W42", "2026-W41"]
    assert [w["start"] for w in out["weeks"]] == ["2026-10-19", "2026-10-12", "2026-10-05"]
    for w in out["weeks"]:
        assert all(w[f] == 0 for f in it.FIELDS)


def test_counts_per_week(store):
    _insight(store, TODAY, votes=["up", "up"])           # outcome votes win
    _outcome(store, TODAY, ["done", "open", "promoted"], up=2, final=False)
    _insight(store, TODAY - timedelta(days=3), votes=["down"])  # Monday W43
    _outcome(store, TODAY - timedelta(days=3), ["dismissed", "gone", "ignored"], down=1)
    _insight(store, TODAY - timedelta(days=4), votes=["up", "down"])  # Sunday W42, no outcome
    _outcome(store, TODAY - timedelta(days=9), ["moved", "done"])  # W42, no insight file
    _insight(store, TODAY - timedelta(days=60))            # outside the window
    (store / "weights.json").write_text("{}")               # not a day file
    w43, w42, w41 = it.trends("alice", TODAY, 3)["weeks"]
    assert w43 == {"week": "2026-W43", "start": "2026-10-19", "insights": 2, "votes_up": 2,
                   "votes_down": 1, "shown": 5, "done": 1, "promoted": 1, "dismissed": 1,
                   "moved": 0, "ignored": 1}
    assert w42 == {"week": "2026-W42", "start": "2026-10-12", "insights": 1, "votes_up": 1,
                   "votes_down": 1, "shown": 2, "done": 1, "promoted": 0, "dismissed": 0,
                   "moved": 1, "ignored": 0}
    assert all(w41[f] == 0 for f in it.FIELDS)


def test_iso_week_across_the_year(store):
    out = it.trends("alice", date(2027, 1, 2), 2)  # Saturday of 2026-W53
    assert [(w["week"], w["start"]) for w in out["weeks"]] == [
        ("2026-W53", "2026-12-28"), ("2026-W52", "2026-12-21")]


def test_weeks_clamped(store):
    assert len(it.trends("alice", TODAY, 0)["weeks"]) == 1
    assert len(it.trends("alice", TODAY, 99)["weeks"]) == it.MAX_WEEKS


@pytest.fixture
def client(store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.middleware import require_admin
    from routes.insight_trends_routes import setup_insight_trends_routes

    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = "alice"
        request.state.api_token = False
        return await call_next(request)

    app.dependency_overrides[require_admin] = lambda: None
    app.include_router(setup_insight_trends_routes())
    return TestClient(app)


def test_route(client, store):
    today = date.fromisoformat(ti.today_str("alice"))
    _outcome(store, today, ["done", "ignored"], up=1)
    r = client.get("/api/insight/trends")
    assert r.status_code == 200
    weeks = r.json()["weeks"]
    assert len(weeks) == 8
    assert weeks[0]["shown"] == 2 and weeks[0]["done"] == 1 and weeks[0]["votes_up"] == 1
    assert len(client.get("/api/insight/trends", params={"weeks": 26}).json()["weeks"]) == 26
    assert client.get("/api/insight/trends", params={"weeks": 0}).status_code == 422
    assert client.get("/api/insight/trends", params={"weeks": 27}).status_code == 422


def test_route_needs_admin(store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes.insight_trends_routes import setup_insight_trends_routes

    app = FastAPI()

    @app.middleware("http")
    async def token(request, call_next):
        request.state.api_token = True
        request.state.current_user = "api"
        return await call_next(request)

    app.include_router(setup_insight_trends_routes())
    assert TestClient(app).get("/api/insight/trends").status_code in (401, 403)
