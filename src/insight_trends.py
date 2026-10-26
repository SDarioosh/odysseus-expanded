"""Fork: weekly counts of the Today insight, for the trends panel on ``/today`` (Phase 3.5).

Per ISO week (Monday to Sunday, newest first): how many days had an insight, the 👍/👎
votes, and how many items were shown and what became of them, from the outcome files
(``src/insight_outcomes.py``) and the insight files next to them. Counts only, no titles.
A week without data is kept with zeros, so the panel shows the gap.

* ``shown``: items of the day's outcome file that weren't ``gone`` (as the weights count).
* ``done``, ``promoted``, ``dismissed``, ``moved``, ``ignored``: items with that outcome
  (an item still ``open`` today counts as shown only).
* ``insights``: days with an insight file. ``votes_up``/``votes_down``: the outcome file's
  ``votes``, else (no outcome file) the insight's own ``votes`` list.

Docs: docs/fork/today-insight.md#trends-and-the-insight-eval.
"""

from __future__ import annotations

import re
from datetime import date as Date
from datetime import timedelta
from typing import Any

DEFAULT_WEEKS = 8
MAX_WEEKS = 26
OUTCOME_COUNTS = ("done", "promoted", "dismissed", "moved", "ignored")
FIELDS = ("insights", "votes_up", "votes_down", "shown", *OUTCOME_COUNTS)
_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def _week_start(day: Date) -> Date:
    return day - timedelta(days=day.weekday())


def _week_label(start: Date) -> str:
    year, week, _ = start.isocalendar()
    return f"{year}-W{week:02d}"


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _insight_days(owner: str | None, since: Date, until: Date) -> dict[str, dict]:
    """The insight files of the days from ``since`` to ``until``, by day."""
    from src import today_insight as ti

    folder = ti.insight_path(owner, "2000-01-01").parent
    try:
        names = sorted(p.name for p in folder.iterdir())
    except OSError:
        return {}
    lo, hi = since.isoformat(), until.isoformat()
    out = {}
    for name in names:
        m = _DAY.match(name)
        if m and lo <= m.group(1) <= hi:
            insight = ti.load(owner, m.group(1))
            if insight is not None:
                out[m.group(1)] = insight
    return out


def _insight_votes(insight: dict) -> tuple[int, int]:
    up = down = 0
    for v in insight.get("votes") or []:
        if isinstance(v, dict):
            up += v.get("vote") == "up"
            down += v.get("vote") == "down"
    return up, down


def trends(owner: str | None, today: Date, weeks: int = DEFAULT_WEEKS) -> dict:
    """``{"weeks": [{"week", "start", "insights", "votes_up", "votes_down", "shown",
    "done", "promoted", "dismissed", "moved", "ignored"}]}``, the week of ``today`` first,
    ``weeks`` (1–26) weeks in all."""
    from src import insight_outcomes

    weeks = max(1, min(MAX_WEEKS, int(weeks)))
    newest = _week_start(today)
    starts = [newest - timedelta(weeks=i) for i in range(weeks)]
    rows = {s: {"week": _week_label(s), "start": s.isoformat(), **{f: 0 for f in FIELDS}}
            for s in starts}
    since = starts[-1]
    insights = _insight_days(owner, since, today)
    outcome_days: set[str] = set()
    for data in insight_outcomes.days(owner, since, today):
        try:
            day = Date.fromisoformat(str(data.get("date")))
        except ValueError:
            continue
        row = rows.get(_week_start(day))
        if row is None:
            continue
        outcome_days.add(day.isoformat())
        votes = data.get("votes") if isinstance(data.get("votes"), dict) else {}
        row["votes_up"] += _int(votes.get("up"))
        row["votes_down"] += _int(votes.get("down"))
        for item in (data.get("items") or {}).values():
            outcome = item.get("outcome") if isinstance(item, dict) else None
            if not outcome or outcome == "gone":
                continue
            row["shown"] += 1
            if outcome in OUTCOME_COUNTS:
                row[outcome] += 1
    for day, insight in insights.items():
        row = rows.get(_week_start(Date.fromisoformat(day)))
        if row is None:
            continue
        row["insights"] += 1
        if day not in outcome_days:
            up, down = _insight_votes(insight)
            row["votes_up"] += up
            row["votes_down"] += down
    return {"weeks": [rows[s] for s in starts]}
