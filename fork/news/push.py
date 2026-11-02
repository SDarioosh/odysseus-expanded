"""Fork: one phone push a day for the morning news brief (docs/fork/news-brief.md#push).

Called after a brief is written (``fork/news/brief.py`` ``AFTER_RUN``) and on every later
pass with today's brief already written (``ON_EXISTING``), so a send that failed is tried
again. :func:`maybe_push` sends through the module notify channel
(``fork.athena.action_items.push``: Odysseus' reminder dispatcher, ntfy or browser;
nothing else) when all hold:

* ``NEWS_BRIEF_PUSH=on`` (default ``off``);
* ``MODULE_NOTIFY_CHANNEL`` isn't ``off``;
* the brief is today's (the owner's local date) and has at least one story;
* today's push file (``store.load_push_state``) has no ``pushed`` stamp yet, and fewer
  than ``MAX_TRIES`` sends were tried today.

Unlike the Today push it doesn't wait for ``MODULE_DIGEST_HOUR``: the brief's delivery
time is its time. Title ``News``; body the top headline of each category, one line each
(``<category>: <title>``, at most ``MAX_BODY`` characters), plain text from the stored
titles (no model rewrite); key ``news-brief:<date>``. The day is marked pushed only when
the send succeeded.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from fork.news import store

logger = logging.getLogger(__name__)

TITLE = "News"
MAX_TRIES = 3
MAX_BODY = 600

Push = Callable[[str, str, str, str], Awaitable[bool]]


def enabled() -> bool:
    """``NEWS_BRIEF_PUSH``: ``on`` or ``off`` (default)."""
    from fork.common import settings

    return settings.news_brief_push()


def _line(text: object) -> str:
    return " ".join(str(text or "").split())


def message(brief: dict) -> str:
    """The top headline of each category with a story, ``<category>: <title>``, whole
    lines up to ``MAX_BODY`` characters (a first line that is longer is cut)."""
    lines = []
    for cat in brief.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        top = next((s for s in cat.get("stories") or []
                    if isinstance(s, dict) and _line(s.get("title"))), None)
        if top is None:
            continue
        name = _line(cat.get("name"))
        title = _line(top.get("title"))
        lines.append(f"{name}: {title}" if name else title)
    body = ""
    for line in lines:
        joined = f"{body}\n{line}" if body else line
        if len(joined) > MAX_BODY:
            break
        body = joined
    return body or (lines[0][:MAX_BODY] if lines else "")


async def maybe_push(owner: str | None, brief: dict, *, now: datetime | None = None,
                     push: Push | None = None) -> bool:
    """Push today's brief once a day when the conditions above hold. True when sent."""
    if not enabled() or not isinstance(brief, dict):
        return False
    from fork.athena import action_items as mai
    from fork.news import brief as news_brief

    if mai.notify_channel() == "off":
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    day = news_brief.local_now(owner, now).date().isoformat()
    if brief.get("date") != day:
        return False
    body = message(brief)
    if not body:
        return False
    state = store.load_push_state(owner, day)
    if state["pushed"] or state["tries"] >= MAX_TRIES:
        return False
    state["tries"] += 1
    store.save_push_state(owner, day, state)
    send = push or mai.push
    try:
        ok = bool(await send(owner or "", TITLE, body, f"news-brief:{day}"))
    except Exception as e:  # noqa: BLE001 -- tried again on the next pass
        logger.warning("news brief push for %s failed: %s", owner, e)
        ok = False
    if ok:
        state["pushed"] = store.utc_iso(now)
        store.save_push_state(owner, day, state)
    return ok
