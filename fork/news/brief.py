"""Fork: the morning news brief (docs/fork/news-brief.md).

A built-in task ("News Brief", action ``news_brief``) writes, once a day per owner, a
short brief: for each category the owner set, the day's main stories with a 2-3 sentence
summary and links to the articles.

* The task runs every 15 minutes; a pass writes the brief once the owner's local time
  (the task's crew timezone, else the owner's assistant's, else the server's) has reached
  the delivery time from the News page and there is no brief for today yet. Every other
  pass is a no-op (no Activity entry). With ``NEWS_BRIEF_ENABLED=off``, or no enabled
  category, every pass is a no-op.
* Gathering is code (``gather.py``: SearXNG only, clean-up, dedupe, grouping, reading the
  top pages); the hub's local background model writes one section per category
  (``prompt.py``: labels, validation, the headlines-only fallback); the files are
  ``store.py``.
* After a brief is written, each function in :data:`AFTER_RUN` is called (phase 2: the
  phone push), and old briefs are pruned (``NEWS_BRIEF_KEEP_DAYS``).

Settings: ``NEWS_BRIEF_ENABLED`` (on), ``NEWS_BRIEF_STORIES`` (5, 1-10; per category it
can be overridden on the page), ``NEWS_BRIEF_KEEP_DAYS`` (30).

Read by ``fork/routes/news_routes.py`` (page, Refresh, config).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone, tzinfo
from typing import Any

from fork.common import settings
from fork.news import gather as news_gather
from fork.news import prompt as news_prompt
from fork.news import store

logger = logging.getLogger(__name__)

ACTION = "news_brief"
TASK_NAME = "News Brief"
CRON = "*/15 * * * *"
HOUSEKEEPING_DEFAULT = {
    ACTION: {"name": TASK_NAME, "schedule": "cron", "scheduled_time": None,
             "cron_expression": CRON},
}
INFO = ("Write the morning news brief: for each category on the News page, the day's main "
        "stories from the hub's SearXNG, summarised by the local model. Runs once a day "
        "after the delivery time set on the News page.")

Llm = Callable[[list[dict]], Awaitable[str]]
# After a brief is written: hook(owner, brief, now=now); sync or async; failures are logged.
AfterRun = Callable[..., Any]
AFTER_RUN: list[AfterRun] = []


# ----------------------------------------------------------------------- settings
def enabled() -> bool:
    return settings.news_brief()


def default_stories() -> int:
    return settings.news_brief_stories()


def keep_days() -> int:
    return settings.news_brief_keep_days()


# --------------------------------------------------------------------------- time
def owner_timezone(owner: str | None) -> tzinfo:
    """The News Brief task's crew timezone, else the owner's assistant's, else the
    server's (the Today insight's rule, ``fork.insight.today.owner_timezone``)."""
    from zoneinfo import ZoneInfo

    name = None
    try:
        from core.database import ScheduledTask, SessionLocal
        from fork.upstream import resolve_task_timezone

        db = SessionLocal()
        try:
            task = db.query(ScheduledTask).filter(ScheduledTask.owner == owner,
                                                  ScheduledTask.action == ACTION).first()
            name = resolve_task_timezone(db, task) if task is not None else None
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 -- fall back below
        logger.debug("news brief: task timezone lookup failed: %s", e)
    if name:
        try:
            return ZoneInfo(str(name))
        except (ValueError, KeyError, OSError):
            logger.warning("news brief: unknown timezone %r", name)
    from fork.insight.today import owner_timezone as assistant_or_server

    return assistant_or_server(owner)


def local_now(owner: str | None, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(owner_timezone(owner))


def due(config: dict, now_local: datetime) -> bool:
    """The owner's local time has reached today's delivery time."""
    hour, minute = store.parse_time(config.get("delivery_time") or store.DEFAULT_TIME)
    return (now_local.hour, now_local.minute) >= (hour, minute)


def load_today(owner: str | None, now: datetime | None = None) -> dict | None:
    return store.load(owner, local_now(owner, now).date().isoformat())


# --------------------------------------------------------------------------- LLM
async def default_llm(owner: str | None, *, wait_quiet: bool = True) -> Llm:
    """The background-task model chain, thinking off (as the Today insight)."""
    from fork.common.llm import background_model

    return await background_model(owner, "news brief", temperature=0.2, max_tokens=1500,
                                  timeout=180, wait_quiet=wait_quiet)


async def summarise(llm: Llm | None, name: str, stories: list[dict], count: int) -> dict:
    """One category's section: the validated answer (one retry), else the headlines."""
    if not stories:
        return {"overview": "", "summarised": False, "stories": []}
    if llm is None:
        return news_prompt.fallback(stories, count)
    from fork.common.llm import ask_with_retry

    def check(raw):
        answer = news_prompt.parse_answer(raw)
        if answer is None:
            return None, ("was not usable JSON", ("That was not valid JSON in the required "
                                                  "shape. Reply with the JSON object only."))
        checked = news_prompt.validate(answer, stories, count)
        if checked is None:
            return None, ("named no candidate", ("Use the labels from CANDIDATES (S1, S2, "
                                                 "...) as ref and reply with the JSON object "
                                                 "only."))
        return checked, None

    answer = await ask_with_retry(llm, news_prompt.build_messages(name, stories, count),
                                  check, label=f"news brief ({name})", log=logger)
    if answer is None:
        return news_prompt.fallback(stories, count)
    return {**answer, "summarised": True}


# ------------------------------------------------------------------------ the run
_RUN_LOCKS: dict[str, asyncio.Lock] = {}


def _run_lock(owner: str | None) -> asyncio.Lock:
    return _RUN_LOCKS.setdefault(owner or "", asyncio.Lock())


async def _model(owner: str | None, llm: Llm | None, wait_quiet: bool) -> Llm | None:
    if llm is not None:
        return llm
    try:
        return await default_llm(owner, wait_quiet=wait_quiet)
    except RuntimeError as e:  # no model endpoint: headlines only
        logger.warning("news brief: %s; writing headlines only", e)
        return None


async def build(owner: str | None, config: dict, day, *, llm: Llm | None = None,
                now: datetime | None = None, wait_quiet: bool = True,
                sources: tuple | None = None, fetch: Callable | None = None) -> dict | None:
    """Gather and summarise every enabled category; the brief, or None when no category
    found any story (nothing is stored then; the next pass tries again)."""
    rows = await news_gather.gather(config["categories"], now=now, sources=sources,
                                    fetch=fetch)
    if not any(r["stories"] for r in rows):
        return None
    model = await _model(owner, llm, wait_quiet)
    categories, errors = [], []
    for row in rows:
        cat = row["category"]
        if row["error"]:
            errors.append({"category": cat["name"], "error": row["error"]})
        if not row["stories"]:
            continue
        count = cat.get("stories") or default_stories()
        section = await summarise(model, cat["name"], row["stories"], count)
        categories.append({"name": cat["name"], **section})
    return store.make_brief(day, categories, errors, now=now)


async def _after_run(owner: str | None, brief: dict, now: datetime | None) -> None:
    for hook in list(AFTER_RUN):
        try:
            result = hook(owner, brief, now=now)
            if inspect.isawaitable(result):
                await result
        except Exception as e:  # noqa: BLE001
            logger.warning("news brief: after-run hook %s failed: %s",
                           getattr(hook, "__name__", hook), e)


async def run(owner: str | None, *, force: bool = False, llm: Llm | None = None,
              now: datetime | None = None, wait_quiet: bool = True,
              sources: tuple | None = None, fetch: Callable | None = None,
              ) -> tuple[str, dict | None]:
    """One pass. Returns (status, brief): ``disabled``, ``no_categories``, ``not_yet``
    (before the delivery time), ``exists`` (today's brief is written), ``empty`` (no
    category found a story; nothing stored) or ``written``. ``force`` (Refresh) skips the
    time and "exists" checks."""
    if not enabled():
        return "disabled", None
    config = store.load_config(owner)
    if not any(c.get("enabled", True) for c in config["categories"]):
        return "no_categories", None
    async with _run_lock(owner):
        now_local = local_now(owner, now)
        day = now_local.date()
        existing = store.load(owner, day.isoformat())
        if not force:
            if existing is not None:
                return "exists", existing
            if not due(config, now_local):
                return "not_yet", None
        brief = await build(owner, config, day, llm=llm, now=now, wait_quiet=wait_quiet,
                            sources=sources, fetch=fetch)
        if brief is None:
            return "empty", existing
        store.save(owner, brief)
        try:
            store.prune(owner, keep_days(), day)
        except Exception as e:  # noqa: BLE001
            logger.warning("news brief: pruning failed: %s", e)
    await _after_run(owner, brief, now)
    return "written", brief


async def generate_now(owner: str | None, *, llm: Llm | None = None,
                       now: datetime | None = None) -> tuple[str, dict | None]:
    """Refresh: write today's brief now, without waiting for chat to go quiet."""
    return await run(owner, force=True, llm=llm, now=now, wait_quiet=False)


NOOP_REASONS = {
    "disabled": "NEWS_BRIEF_ENABLED is off",
    "no_categories": "no categories",
    "not_yet": "before the delivery time",
    "exists": "today's brief is written",
}


async def action_news_brief(owner: str, **kwargs) -> tuple[str, bool]:
    """Built-in task entry point (``fork/tasks.py``)."""
    from src.builtin_actions import TaskNoop

    try:
        status, brief = await run(owner)
    except Exception as e:
        logger.exception("news brief failed for %s", owner)
        return f"News brief failed: {e}", False
    if status in NOOP_REASONS:
        raise TaskNoop(NOOP_REASONS[status])
    if status == "empty":
        return "News brief: no category found any story; trying again at the next pass.", False
    cats = (brief or {}).get("categories") or []
    count = sum(len(c.get("stories") or []) for c in cats)
    return f"News brief written ({len(cats)} categories, {count} stories).", True
