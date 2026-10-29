"""Fork: the daily "Today" insight (personal-context Phase 0, stage 0.3).

A built-in task ("Today Insight", every 15 minutes per owner) gathers, in code:

* calendar events from today 00:00 local to +7 days (not cancelled, not ended);
* the open items of to-do notes (``checklist``, ``todo``, ``goal``; not archived);
* memory: preference / contact / fact entries and everything from Athena;
* Athena's digest for today and its open action items, over its MCP server,
  when that server is connected (otherwise they are simply left out).

Code buckets every to-do item and event (overdue, due_today, due_soon, undated; items due
later than a week are ``later``), drops done items, ended events and shopping-type lists
without a due date, and keeps at most 30 candidates, each with a ref
(``todo:<note id>:<item index>`` or ``event:<uid>``). The local model only picks refs and
writes a short reason for each; refs it invents are dropped. It runs only when there is no
insight for today yet or the inputs changed (SHA-256 of candidates, memory and digest).

Stored per owner and day: ``<DATA_DIR>/today_insight/<owner>/<YYYY-MM-DD>.json``.
Read by ``routes/today_insight_routes.py`` (web page, votes, refresh, the token route
``GET /api/codex/insight/today``) and by the chat memory context (``context_block``).

Settings (environment):

* ``TODAY_INSIGHT_ENABLED``: ``on`` (default) or ``off``.
* ``TODAY_INSIGHT_SKIP_LISTS``: comma-separated list titles left out unless the note has a
  due date (default ``Shopping,Groceries,Grocery``; any case, with or without "list").
* ``MODULE_ACTION_ITEMS_SERVER``: the Athena MCP server (default ``convo``).

Docs: ``docs/fork/today-insight.md``.

Code: this module is the pass (:func:`gather`, :func:`run`, :func:`generate_now`), the
owner's zone, votes and the task entry, and offers every name of the insight, so callers
import ``fork.insight.today`` only; the loaders and the model's prompt and checks are
called through it, so tests and the eval can replace them here. The rest is in
``today_candidates.py`` (candidates, memory lines, the input hash), ``today_gather.py``
(reading notes, calendar, memory and Athena), ``today_prompt.py`` (prompt, labels,
answer checks), ``today_store.py`` (the files) and ``today_context.py`` (the chat
context), all in ``fork/insight/``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, tzinfo
from typing import Any, Awaitable, Callable

from fork.common import settings
from fork.insight.today_candidates import (  # noqa: F401 -- re-exported
    _SOURCE_SUFFIX,
    DEFAULT_SKIP_LISTS,
    MAX_CANDIDATES,
    MAX_MEMORY,
    MEMORY_CATEGORIES,
    MEMORY_SOURCE,
    TODO_TYPES,
    _by_use,
    _item_done,
    _item_text,
    _items,
    _list_key,
    _memory_text,
    _occurrence_time,
    bucket_for,
    event_candidates,
    input_hash,
    is_skipped_list,
    local_date,
    memory_query,
    notes_state,
    order_candidates,
    select_memory,
    skip_lists,
    todo_candidates,
)
from fork.insight.today_context import (  # noqa: F401 -- re-exported
    _SHORT_WHAT_TO_DO,
    _WANTS_INSIGHT,
    CONTEXT_LIMIT,
    _insight_block,
    context_block,
    wants_insight,
)
from fork.insight.today_gather import (  # noqa: F401 -- re-exported
    MAX_ACTION_ITEMS,
    MAX_DIGEST,
    CallTool,
    _action_item_lines,
    _digest_text,
    _load_events,
    _load_memory,
    _load_notes,
    _module_caller,
    _module_inputs,
)
from fork.insight.today_prompt import (  # noqa: F401 -- re-exported
    _DATE_LIKE,
    _LABEL_KINDS,
    _WHY_STOP,
    MAX_HEADLINE,
    MAX_PER_SECTION,
    MAX_WHY,
    SECTIONS,
    SYSTEM_PROMPT,
    _candidate_line,
    _clip,
    _content,
    _extra_line,
    build_messages,
    labels,
    parse_answer,
    repeats_title,
    resolve_ref,
    validate,
)
from fork.insight.today_store import (  # noqa: F401 -- re-exported
    _slug,
    _utc_iso,
    insight_path,
    load,
    make_insight,
    public,
    save,
)

logger = logging.getLogger(__name__)


ACTION = "today_insight"
TASK_NAME = "Today Insight"
HOUSEKEEPING_DEFAULT = {
    ACTION: {"name": TASK_NAME, "schedule": "cron", "scheduled_time": None,
             "cron_expression": "*/15 * * * *"},
}
INFO = ("Write the daily Today insight (what to do first, what is coming up, what else is "
        "worth a look) from the calendar, to-dos, memory and Athena. The local model "
        "runs only when the inputs changed.")

VOTES = ("up", "down")

Llm = Callable[[list[dict]], Awaitable[str]]


# ----------------------------------------------------------------------- settings
def enabled() -> bool:
    return settings.today_insight()


# --------------------------------------------------------------------------- time
def owner_timezone(owner: str | None) -> tzinfo:
    """The owner's zone: the insight task's crew timezone (``_resolve_task_timezone``),
    else the owner's personal assistant's, else the server's (``user_timezone``)."""
    from zoneinfo import ZoneInfo

    name = None
    try:
        from core.database import CrewMember, ScheduledTask, SessionLocal
        from fork.upstream import resolve_task_timezone as _resolve_task_timezone

        db = SessionLocal()
        try:
            task = db.query(ScheduledTask).filter(ScheduledTask.owner == owner,
                                                  ScheduledTask.action == ACTION).first()
            name = _resolve_task_timezone(db, task) if task is not None else None
            if not name:
                cm = db.query(CrewMember).filter(CrewMember.owner == owner,
                                                 CrewMember.is_default_assistant == True  # noqa: E712
                                                 ).first()
                name = getattr(cm, "timezone", None)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 -- fall back to the server zone
        logger.debug("today insight: owner timezone lookup failed: %s", e)
    if name:
        try:
            return ZoneInfo(str(name))
        except (ValueError, KeyError, OSError):
            logger.warning("today insight: unknown timezone %r", name)
    from src.user_time import user_timezone

    return user_timezone()


def _now_local(tz: tzinfo, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(tz)


# ---------------------------------------------------------------------- gathering
async def gather(owner: str | None, *, now: datetime | None = None,
                 call: CallTool | None | bool = True) -> dict:
    """Everything the insight is built from (``call=True``: the module if connected).
    Phase 3 additions (docs/fork/today-insight.md): ``weights`` (the day's rule weights),
    ``notes_state``/``occurrences``/``action_rows``/``action_rows_done``/``gathered_at``
    (what the outcome log observes, and when it was read),
    ``extra`` (open loops and this week's plans, refs ``loop:``/``belief:``) and
    ``routines`` (a short text)."""
    tz = owner_timezone(owner)
    now_local = _now_local(tz, now)
    today = now_local.date()
    gathered_at = _utc_iso(now)  # the outcome log keeps page changes made after this
    notes = _load_notes(owner)
    occurrences = _load_events(owner, now_local)
    weights = _learning("insight_weights", "current", {}, owner, today, now=now)
    marks = _learning("insight_outcomes", "owner_marks", {}, owner, today)
    todos = todo_candidates(notes, today, tz)
    events = event_candidates(occurrences, now_local)
    candidates = order_candidates(todos, events, weights=weights,
                                  dismissed=(marks or {}).get("dismissed") or ())
    memory = _load_memory(owner, memory_query(candidates))
    caller = _module_caller() if call is True else (call or None)
    loops = _learning("insight_outcomes", "open_loop_ids", set(), owner, today) or set()
    digest, action_items, action_rows, action_rows_done = await _module_inputs(
        owner, today, caller, loops)
    extra = _learning("insight_inputs", "extra_candidates", [], owner, today, action_rows,
                      now=now) or []
    loop_ids = [c["ref"].split(":", 1)[1] for c in extra
                if str(c.get("ref") or "").startswith("loop:")]
    if loop_ids:  # open loops are candidates now, not context lines
        action_items = _action_item_lines(owner, action_rows, skip=loop_ids)
    gone = set((marks or {}).get("dismissed") or ())
    extra = [c for c in extra if c.get("ref") not in gone]
    routines = _learning("insight_routines", "summary", "", owner, today, now=now) or ""
    return {"now": now_local, "today": today, "candidates": candidates, "memory": memory,
            "digest": digest, "action_items": action_items, "extra": extra,
            "routines": routines, "weights": weights or {},
            "notes_state": notes_state(notes), "occurrences": occurrences,
            "action_rows": action_rows, "action_rows_done": action_rows_done,
            "gathered_at": gathered_at,
            "hash": input_hash(candidates, memory, digest,
                               [action_items, extra, routines])}


def _learning(module: str, func: str, default: Any, *args, **kwargs) -> Any:
    """Call a Phase 3 helper (``fork/insight/<name>.py``); its failure never stops the insight."""
    try:
        import importlib

        return getattr(importlib.import_module(f"fork.insight.{module.removeprefix('insight_')}"),
                       func)(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.warning("today insight: %s.%s failed: %s", module, func, e)
        return default


async def _after_run(owner: str | None, inputs: dict | None, insight: dict | None,
                     now: datetime | None) -> None:
    """After every pass (also a no-op): the outcome log observes the day and finalizes
    earlier days, then the once-a-day push. Failures are logged, never raised."""
    if insight is not None and inputs is not None:
        _learning("insight_outcomes", "observe", None, owner, insight, inputs, now=now)
    if insight is not None:
        try:
            from fork.insight import push as insight_push

            await insight_push.maybe_push(owner, insight, now=now)
        except Exception as e:  # noqa: BLE001
            logger.warning("today insight: push failed: %s", e)


# --------------------------------------------------------------------------- LLM
async def default_llm(owner: str | None, *, wait_quiet: bool = True) -> Llm:
    """The background-task model chain. ``wait_quiet=False`` (the page's Refresh, which the
    owner is waiting for) skips the wait for chat to go quiet: the refresh request itself
    counts as interactive activity, so it would wait for itself."""
    from fork.common.llm import background_model

    return await background_model(owner, "today insight", temperature=0.2, max_tokens=1200,
                                  timeout=180, wait_quiet=wait_quiet)


async def ask_model(llm: Llm, inputs: dict) -> dict | None:
    """The validated answer; one retry on unusable JSON; None when both fail."""
    from fork.common.llm import ask_with_retry

    def check(raw):
        answer = parse_answer(raw)
        if answer is None:
            return None, ("was not usable JSON", "That was not valid JSON in the required "
                          "shape. Reply with the JSON object only.")
        checked = validate(answer, inputs["candidates"], inputs.get("extra"))
        if any(checked[s] for s in SECTIONS) or not (inputs["candidates"]
                                                     or inputs.get("extra")):
            return checked, None
        return None, ("named no candidate", "Use the labels from CANDIDATES (T1, E1, …) "
                      "as refs and reply with the JSON object only.")

    return await ask_with_retry(llm, build_messages(inputs), check, label="today insight",
                                log=logger)


# ------------------------------------------------------------------------ storage
_FILE_LOCKS: dict[str, asyncio.Lock] = {}
_RUN_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(owner: str | None) -> asyncio.Lock:
    """One writer of an owner's insight file at a time (the task, Refresh, votes)."""
    return _FILE_LOCKS.setdefault(owner or "", asyncio.Lock())


def _run_lock(owner: str | None) -> asyncio.Lock:
    """One generation per owner at a time; votes don't wait for the model."""
    return _RUN_LOCKS.setdefault(owner or "", asyncio.Lock())


def today_str(owner: str | None, now: datetime | None = None) -> str:
    return _now_local(owner_timezone(owner), now).date().isoformat()


def load_today(owner: str | None, now: datetime | None = None) -> dict | None:
    return load(owner, today_str(owner, now))


async def add_vote(owner: str | None, vote: str, now: datetime | None = None) -> dict | None:
    """Record 👍/👎 on today's insight; None when there is none."""
    if vote not in VOTES:
        raise ValueError("vote must be up or down")
    async with _lock(owner):
        insight = load_today(owner, now)
        if insight is None:
            return None
        votes = insight.get("votes") if isinstance(insight.get("votes"), list) else []
        insight["votes"] = [*votes, {"vote": vote, "at": _utc_iso(now)}]
        save(owner, insight)
        return insight


# ---------------------------------------------------------------------------- run
EMPTY_HEADLINE = "Nothing is due and nothing is on the calendar for the next days."


async def run(owner: str | None, *, force: bool = False, llm: Llm | None = None,
              now: datetime | None = None, call: CallTool | None | bool = True,
              wait_quiet: bool = True) -> tuple[str, dict | None]:
    """One pass. Returns (status, insight): ``noop`` (inputs unchanged), ``generated``,
    ``empty`` (no candidates; written without the model) or ``kept`` (model failed; the
    previous insight, if any, stays). ``wait_quiet``: see ``default_llm``."""
    async with _run_lock(owner):
        inputs = await gather(owner, now=now, call=call)
        status, insight = await _write(owner, inputs, force=force, llm=llm, now=now,
                                       wait_quiet=wait_quiet)
        await _after_run(owner, inputs, insight, now)
        return status, insight


async def _write(owner: str | None, inputs: dict, *, force: bool, llm: Llm | None,
                 now: datetime | None, wait_quiet: bool) -> tuple[str, dict | None]:
    day = inputs["today"].isoformat()
    previous = load(owner, day)
    if not force and previous and previous.get("input_hash") == inputs["hash"]:
        return "noop", previous
    if not inputs["candidates"] and not inputs.get("extra"):
        status, answer = "empty", {"headline": EMPTY_HEADLINE}
    else:
        model = llm or await default_llm(owner, wait_quiet=wait_quiet)
        status, answer = "generated", await ask_model(model, inputs)
        if answer is None:
            logger.warning("today insight for %s: no usable model answer; keeping the "
                           "previous insight", owner)
            return "kept", previous
    async with _lock(owner):
        votes = (load(owner, day) or {}).get("votes") or []  # votes cast meanwhile
        insight = make_insight(inputs["today"], answer, inputs["hash"], votes=votes,
                               now=now)
        save(owner, insight)
    return status, insight


async def generate_now(owner: str | None, *, llm: Llm | None = None,
                       now: datetime | None = None, call: CallTool | None | bool = True,
                       wait_quiet: bool = False) -> dict | None:
    """Rebuild today's insight now, even when the inputs didn't change (Refresh). Doesn't
    wait for chat to go quiet unless asked. None when the model's answer wasn't usable
    (the previous insight stays)."""
    status, insight = await run(owner, force=True, llm=llm, now=now, call=call,
                                wait_quiet=wait_quiet)
    return None if status == "kept" else insight


async def action_today_insight(owner: str, **kwargs) -> tuple[str, bool]:
    """Built-in task entry point (see ``register_builtin_action``)."""
    from src.builtin_actions import TaskNoop

    if not enabled():
        raise TaskNoop("TODAY_INSIGHT_ENABLED is off")
    try:
        status, insight = await run(owner)
    except Exception as e:  # noqa: BLE001
        logger.exception("today insight failed for %s", owner)
        return f"Today insight failed: {e}", False
    if status == "noop":
        raise TaskNoop("inputs unchanged")
    if status == "kept":
        return "Today insight: the model's answer wasn't usable; kept the previous one.", False
    count = sum(len((insight or {}).get(s) or []) for s in SECTIONS)
    return f"Today insight written ({count} items).", True


def register_builtin_action(actions: dict, info: dict) -> None:
    actions[ACTION] = action_today_insight
    info[ACTION] = INFO


def register_housekeeping(defaults: dict) -> None:
    defaults.update(HOUSEKEEPING_DEFAULT)
