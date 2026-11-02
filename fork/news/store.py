"""Fork: the news brief's files (``fork/news/brief.py``).

Per owner, under ``<DATA_DIR>/news_brief/<owner>/`` (owner folder names as the Today
insight's, ``fork.common.state.owner_slug``):

* ``config.json``: the categories (name, optional query, enabled, optional stories,
  optional RSS/Atom ``feeds``) and the delivery time (``H:MM``, the owner's local time);
* ``<YYYY-MM-DD>.json``: one brief per day, deleted after ``NEWS_BRIEF_KEEP_DAYS``;
* ``push-<YYYY-MM-DD>.json``: that day's phone push (``pushed`` stamp, ``tries``;
  ``fork/news/push.py``), deleted with the brief.

:func:`latest_brief` and :func:`recent_briefs` are the read side (the "seen in the last
briefs" filter; phase 3: the chat block and the Today window).
"""

from __future__ import annotations

import re
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

FOLDER = "news_brief"
CONFIG = "config.json"
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
TIME = re.compile(r"(\d{1,2}):(\d{2})")

DEFAULT_TIME = "7:00"
MAX_CATEGORIES = 20
MAX_NAME = 60
MAX_QUERY = 200
MIN_STORIES, MAX_STORIES = 1, 10
MAX_FEEDS = 10
MAX_FEED_URL = 500
PUSH_PREFIX = "push-"


def owner_dir(owner: str | None) -> Path:
    from fork.common.state import owner_slug
    from src.constants import DATA_DIR

    return Path(DATA_DIR) / FOLDER / owner_slug(owner)


def brief_path(owner: str | None, day: str) -> Path:
    if not DAY.fullmatch(day or ""):
        raise ValueError("day must be YYYY-MM-DD")
    return owner_dir(owner) / f"{day}.json"


def config_path(owner: str | None) -> Path:
    return owner_dir(owner) / CONFIG


# ------------------------------------------------------------------------- config
def default_config() -> dict:
    return {"categories": [], "delivery_time": DEFAULT_TIME}


def parse_time(text: Any) -> tuple[int, int]:
    """``"H:MM"`` (or ``"HH:MM"``) -> (hour, minute); ValueError otherwise."""
    m = TIME.fullmatch(str(text or "").strip())
    if not m or not (0 <= int(m[1]) <= 23 and 0 <= int(m[2]) <= 59):
        raise ValueError("delivery time must be H:MM (0:00 to 23:59)")
    return int(m[1]), int(m[2])


def format_time(hour: int, minute: int) -> str:
    return f"{hour}:{minute:02d}"


def _text(value: Any, limit: int, what: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        raise ValueError(f"{what} is longer than {limit} characters")
    return text


def normalize_feeds(raw: Any, name: str) -> list[str]:
    """The category's feed URLs: http(s) with a host, no user or password, at most
    ``MAX_FEEDS``, each once. Whether the address is public is checked when it is read
    (``fork/news/gather.fetch_feed``)."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError(f"feeds of {name!r} must be a list of URLs")
    out: list[str] = []
    for item in raw:
        url = str(item or "").strip() if isinstance(item, str) else None
        if url is None:
            raise ValueError(f"feeds of {name!r} must be a list of URLs")
        if not url:
            continue
        if len(url) > MAX_FEED_URL:
            raise ValueError(f"a feed of {name!r} is longer than {MAX_FEED_URL} characters")
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError:
            parts, host = None, None
        if (parts is None or parts.scheme.lower() not in ("http", "https") or not host
                or parts.username is not None or any(c.isspace() for c in url)):
            raise ValueError(f"the feed {url[:80]!r} of {name!r} must be an http(s) URL")
        if url not in out:
            out.append(url)
    if len(out) > MAX_FEEDS:
        raise ValueError(f"at most {MAX_FEEDS} feeds per category ({name!r} has {len(out)})")
    return out


def normalize_category(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("each category must be an object")
    name = _text(raw.get("name"), MAX_NAME, "a category name")
    if not name:
        raise ValueError("every category needs a name")
    stories = raw.get("stories")
    if stories in (None, ""):
        stories = None
    else:
        try:
            stories = int(stories)
        except (TypeError, ValueError):
            raise ValueError(f"stories of {name!r} must be a number") from None
        if not MIN_STORIES <= stories <= MAX_STORIES:
            raise ValueError(f"stories of {name!r} must be {MIN_STORIES} to {MAX_STORIES}")
    return {"name": name, "query": _text(raw.get("query"), MAX_QUERY, "a query"),
            "enabled": raw.get("enabled", True) is not False, "stories": stories,
            "feeds": normalize_feeds(raw.get("feeds"), name)}


def normalize_config(raw: Any) -> dict:
    """A config as stored; ValueError (with a message for the page) when invalid."""
    if not isinstance(raw, dict):
        raise ValueError("config must be an object")
    cats = raw.get("categories") or []
    if not isinstance(cats, list):
        raise ValueError("categories must be a list")
    if len(cats) > MAX_CATEGORIES:
        raise ValueError(f"at most {MAX_CATEGORIES} categories")
    out, seen = [], set()
    for c in cats:
        cat = normalize_category(c)
        key = cat["name"].casefold()
        if key in seen:
            raise ValueError(f"the category {cat['name']!r} is listed twice")
        seen.add(key)
        out.append(cat)
    hour, minute = parse_time(raw.get("delivery_time") or DEFAULT_TIME)
    return {"categories": out, "delivery_time": format_time(hour, minute)}


def load_config(owner: str | None) -> dict:
    """The owner's config; the default (no categories, 7:00) when missing or unreadable."""
    from fork.common.state import read_json

    data = read_json(config_path(owner))
    try:
        return normalize_config(data) if isinstance(data, dict) else default_config()
    except ValueError:
        return default_config()


def save_config(owner: str | None, raw: Any) -> dict:
    from fork.common.state import write_json

    config = normalize_config(raw)
    write_json(config_path(owner), config, sort_keys=False, ensure_ascii=False)
    return config


# ------------------------------------------------------------------------ briefs
def utc_iso(now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_brief(day: Date, categories: list[dict], errors: list[dict], *,
               now: datetime | None = None) -> dict:
    return {"date": day.isoformat(), "generated": utc_iso(now),
            "categories": list(categories), "errors": list(errors)}


def load(owner: str | None, day: str) -> dict | None:
    from fork.common.state import read_json

    data = read_json(brief_path(owner, day))
    return data if isinstance(data, dict) else None


def save(owner: str | None, brief: dict) -> None:
    from fork.common.state import write_json

    write_json(brief_path(owner, brief["date"]), brief, sort_keys=False, ensure_ascii=False)


def brief_days(owner: str | None) -> list[str]:
    """The days with a stored brief, newest first."""
    folder = owner_dir(owner)
    if not folder.is_dir():
        return []
    return sorted((p.stem for p in folder.glob("*.json") if DAY.fullmatch(p.stem)),
                  reverse=True)


def recent_briefs(owner: str | None, count: int, *, before: str | None = None) -> list[dict]:
    """Up to ``count`` stored briefs, newest first (only days before ``before`` if given)."""
    out = []
    for day in brief_days(owner):
        if before and day >= before:
            continue
        brief = load(owner, day)
        if brief is not None:
            out.append(brief)
        if len(out) >= count:
            break
    return out


def latest_brief(owner: str | None) -> dict | None:
    """The newest stored brief, or None (phase 3: chat block, Today window)."""
    found = recent_briefs(owner, 1)
    return found[0] if found else None


def push_path(owner: str | None, day: str) -> Path:
    if not DAY.fullmatch(day or ""):
        raise ValueError("day must be YYYY-MM-DD")
    return owner_dir(owner) / f"{PUSH_PREFIX}{day}.json"


def load_push_state(owner: str | None, day: str) -> dict:
    """That day's push state: ``{"pushed": <UTC stamp or null>, "tries": n}``."""
    from fork.common.state import read_json

    data = read_json(push_path(owner, day))
    data = data if isinstance(data, dict) else {}
    try:
        tries = max(0, int(data.get("tries") or 0))
    except (TypeError, ValueError):
        tries = 0
    return {"pushed": data.get("pushed") or None, "tries": tries}


def save_push_state(owner: str | None, day: str, state: dict) -> None:
    from fork.common.state import write_json

    write_json(push_path(owner, day), {"pushed": state.get("pushed") or None,
                                       "tries": int(state.get("tries") or 0)})


def prune(owner: str | None, keep_days: int, today: Date) -> list[str]:
    """Delete briefs (and their push files) older than ``keep_days`` days before
    ``today``; the days of the briefs deleted."""
    cutoff = (today - timedelta(days=max(1, keep_days))).isoformat()
    gone = []
    for day in brief_days(owner):
        if day < cutoff:
            try:
                brief_path(owner, day).unlink()
                gone.append(day)
            except OSError:
                pass
    folder = owner_dir(owner)
    if folder.is_dir():
        for path in folder.glob(f"{PUSH_PREFIX}*.json"):
            day = path.stem.removeprefix(PUSH_PREFIX)
            if DAY.fullmatch(day) and day < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass
    return gone
