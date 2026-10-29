"""Fork: the Today insight's files (``fork/insight/today.py``).

One file per owner and day, ``<DATA_DIR>/today_insight/<owner>/<YYYY-MM-DD>.json``, and
the insight's stored and public shapes.
"""

from __future__ import annotations

import re
from datetime import date as Date
from datetime import datetime, timezone
from pathlib import Path

from fork.insight.today_prompt import SECTIONS


def _slug(owner: str | None) -> str:
    from fork.common.state import owner_slug

    return owner_slug(owner)


def insight_path(owner: str | None, day: str) -> Path:
    from src.constants import DATA_DIR

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        raise ValueError("day must be YYYY-MM-DD")
    return Path(DATA_DIR) / "today_insight" / _slug(owner) / f"{day}.json"


def load(owner: str | None, day: str) -> dict | None:
    from fork.common.state import read_json

    data = read_json(insight_path(owner, day))
    return data if isinstance(data, dict) else None


def save(owner: str | None, insight: dict) -> None:
    from fork.common.state import write_json

    write_json(insight_path(owner, insight["date"]), insight, sort_keys=False,
               ensure_ascii=False)


def _utc_iso(now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_insight(day: Date, answer: dict, hash_hex: str, *, votes: list | None = None,
                 now: datetime | None = None) -> dict:
    return {"date": day.isoformat(), "generated": _utc_iso(now),
            "headline": answer.get("headline") or "",
            **{s: list(answer.get(s) or []) for s in SECTIONS},
            "input_hash": hash_hex, "votes": list(votes or [])}


def public(insight: dict) -> dict:
    """What token clients get: no hash, no votes."""
    return {k: insight[k] for k in ("date", "generated", "headline", *SECTIONS) if k in insight}
