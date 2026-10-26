"""Fork: one phone push a day for the Today insight (Phase 3.3).

After every Today Insight pass (``today_insight._after_run``), :func:`maybe_push` sends
today's insight to the phone once, through the module notify channel
(``module_action_items.push``: Odysseus' reminder dispatcher, ntfy or browser; nothing
else). It pushes when all hold:

* ``TODAY_INSIGHT_PUSH=on`` (default ``off``);
* ``MODULE_NOTIFY_CHANNEL`` isn't ``off``;
* the insight is today's (the owner's local date) and not the fixed "nothing is due" one;
* the local hour is at least ``MODULE_DIGEST_HOUR`` (``off``: never);
* today's outcome file (``src/insight_outcomes.py``) has no ``pushed`` stamp yet, and
  fewer than ``MAX_TRIES`` sends were tried today.

Title ``Today``; body the headline, then ``First: <title>`` of the first focus row after
the owner's own order (``apply_owner_order``); key ``today-insight:<date>``. The day is
marked pushed only when the send succeeded, so a failed send is tried again on the next
pass, at most ``MAX_TRIES`` times a day.

Docs: ``docs/fork/today-insight.md#push``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

TITLE = "Today"
MAX_TRIES = 3
MAX_BODY = 600

Push = Callable[[str, str, str, str], Awaitable[bool]]


def enabled() -> bool:
    """``TODAY_INSIGHT_PUSH``: ``on`` or ``off`` (default)."""
    value = (os.environ.get("TODAY_INSIGHT_PUSH") or "off").strip().lower()
    return value in {"on", "yes", "true", "1"}


def is_empty(insight: dict) -> bool:
    """The fixed insight written without the model when nothing is due."""
    from src.today_insight import EMPTY_HEADLINE, SECTIONS

    return (str(insight.get("headline") or "").strip() == EMPTY_HEADLINE
            and not any(insight.get(s) for s in SECTIONS))


def message(insight: dict, outcome: dict | None = None) -> tuple[str, str]:
    """(title, body): the headline, then ``First: <title>`` when focus has a row."""
    ordered = insight
    try:
        from src import insight_outcomes

        ordered = insight_outcomes.apply_owner_order(insight, outcome) or insight
    except Exception as e:  # noqa: BLE001 -- the model's order then
        logger.debug("insight push: owner order unavailable: %s", e)
    body = " ".join(str(ordered.get("headline") or "").split())
    first = next((r for r in ordered.get("focus") or [] if isinstance(r, dict)
                  and str(r.get("title") or "").strip()), None)
    if first is not None:
        line = "First: " + " ".join(str(first["title"]).split())
        body = f"{body}\n{line}" if body else line
    return TITLE, body[:MAX_BODY]


# ------------------------------------------------------------ pushed / tries state
# The outcome log keeps the ``pushed`` stamp and ``push_tries`` (src/insight_outcomes.py).
def _pushed(owner: str | None, day: str) -> Any:
    from src import insight_outcomes

    return insight_outcomes.pushed(owner, day)


def _note_try(owner: str | None, day: str) -> int:
    """Count one try today; returns today's count including this one."""
    from src import insight_outcomes

    return int(insight_outcomes.note_push_try(owner, day) or 0)


def _mark_pushed(owner: str | None, day: str, now: datetime) -> None:
    from src import insight_outcomes

    insight_outcomes.mark_pushed(owner, day, now=now)


def _outcome(owner: str | None, day: str) -> dict | None:
    from src import insight_outcomes

    try:
        return insight_outcomes.load(owner, day)
    except Exception as e:  # noqa: BLE001
        logger.debug("insight push: outcome log unavailable: %s", e)
        return None


# ----------------------------------------------------------------------------- push
async def maybe_push(owner: str | None, insight: dict, *, now: datetime | None = None,
                     push: Push | None = None) -> bool:
    """Push today's insight once a day when the conditions above hold. True when sent."""
    if not enabled() or not isinstance(insight, dict):
        return False
    from src import module_action_items as mai
    from src import today_insight

    if mai.notify_channel() == "off":
        return False
    hour = mai.digest_hour()
    if hour is None:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(today_insight.owner_timezone(owner))
    day = local.date().isoformat()
    if insight.get("date") != day or is_empty(insight) or local.hour < hour:
        return False
    if _pushed(owner, day):
        return False
    title, body = message(insight, _outcome(owner, day))
    from src import insight_outcomes

    if not body or insight_outcomes.push_tries(owner, day) >= MAX_TRIES:
        return False
    _note_try(owner, day)
    send = push or mai.push
    try:
        ok = bool(await send(owner or "", title, body, f"today-insight:{day}"))
    except Exception as e:  # noqa: BLE001 -- tried again on the next pass
        logger.warning("insight push for %s failed: %s", owner, e)
        ok = False
    if ok:
        _mark_pushed(owner, day, now)
    return ok
