"""Fork: the Today insight in the chat context (``fork/insight/today.py``).

:func:`context_block` is what the chat memory context gets from the fork: today's
insight as a short text when the message is about today's plan (:func:`wants_insight`),
the ledger's "What I know (confirmed)" block when it asks about someone, and the stored
news brief when it asks about the news (``fork/news/context.py``: it rides this hook, so
no upstream line of its own).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from fork.insight.today_prompt import MAX_HEADLINE, _clip
from fork.insight.today_store import insight_path

logger = logging.getLogger("fork.insight.today")  # the insight's one logger


CONTEXT_LIMIT = 800


_WANTS_INSIGHT = re.compile(
    r"\b(?:today|today's|tonight|this (?:morning|afternoon|evening)|first|"
    r"priorit(?:y|ies|ise|ize)|plans?|focus|agenda|schedule)\b"
    r"|\bwhat(?:'s| is) next\b|\bwhat now\b|\bwhat (?:should|shall|can|do) i do\b|\bwhat to do\b"
    r"|\bwhere (?:do|should) i (?:start|begin)\b",
    re.IGNORECASE)
# a short question that is only "what do I do (now)?", nothing else ("what do I need to
# do before October 31?" is a module question, not today's plan)
_SHORT_WHAT_TO_DO = re.compile(
    r"(?:(?:ok(?:ay)?|so|and|now),?\s+)?(?:what|which)(?:\s+(?:should|shall|can|do)\s+i)?"
    r"\s+(?:do|tackle|work on|start with|do now|do next)(?:\s+(?:now|next))?\s*[?.!]*",
    re.IGNORECASE)


def wants_insight(message: Any) -> bool:
    """Whether a chat message is about today's plan (then the insight joins the context).
    Slash commands and slash-invoked skills (``/sound``, ``/context``) never are: the
    insight block made a small model answer from it instead of calling the module."""
    text = str(message or "").strip()
    if not text or text.startswith("/"):
        return False
    try:
        from fork.routing.skill_invocation import INVOKE_PREFIX

        if text.replace("\r\n", "\n").startswith(INVOKE_PREFIX):
            return False
    except Exception:  # noqa: BLE001
        pass
    if _WANTS_INSIGHT.search(text):
        return True
    return len(text) <= 60 and bool(_SHORT_WHAT_TO_DO.fullmatch(text))


def context_block(owner: str | None, now: datetime | None = None,
                  limit: int = CONTEXT_LIMIT, message: Any = None) -> str:
    """Chat memory context from the fork: today's insight as a short text (at most
    ``limit``; with ``message``, only when it is about today's plan, see
    ``wants_insight``) and, when ``message`` asks what the hub knows about someone, the
    "What I know (confirmed)" block of the belief ledger (``belief_search.chat_block``,
    at most 1200 characters) and, when it asks about the news, the stored news brief
    (``fork.news.context.context_block``, at most 1200 characters). "" when none
    applies."""
    parts = []
    if message is None or wants_insight(message):
        parts.append(_insight_block(owner, now, limit))
    if message is not None:
        try:
            from fork.beliefs import search as belief_search

            parts.append(belief_search.chat_block(owner, message))
        except Exception as e:  # noqa: BLE001 -- chat must never fail on this
            logger.debug("today insight: belief block unavailable: %s", e)
        try:
            from fork.news.context import context_block as news_block

            parts.append(news_block(owner, message=message, now=now))
        except Exception as e:  # noqa: BLE001 -- chat must never fail on this
            logger.debug("today insight: news block unavailable: %s", e)
    return "\n\n".join(p for p in parts if p)


def _insight_block(owner: str | None, now: datetime | None, limit: int) -> str:
    from fork.insight import today as ti  # today's file and its patch points (the zone)

    try:
        if not insight_path(owner, "2000-01-01").parent.is_dir():
            return ""  # never written for this owner: skip the timezone lookup
        insight = ti.load_today(owner, now)
    except Exception as e:  # noqa: BLE001 -- chat must never fail on this
        logger.debug("today insight: context unavailable: %s", e)
        return ""
    if not insight:
        return ""
    # the owner's "↑ first" / "not today" (src/insight_outcomes.py)
    insight = ti._learning("insight_outcomes", "ordered", insight, owner, insight)
    text = ("Today's plan, for questions about today, priorities or what to do first. "
            f"For anything else use your tools as usual. ({insight.get('date')})")
    if insight.get("headline"):
        text += "\n" + _clip(insight["headline"], MAX_HEADLINE)
    for section, label in (("focus", "Do first"), ("coming_up", "Coming up"), ("also", "Also")):
        rows = [r for r in insight.get(section) or [] if isinstance(r, dict)]
        if not rows:
            continue
        line = f"\n{label}:"
        for i, r in enumerate(rows, 1):
            part = f" {i}. {r.get('title') or r.get('ref')}"
            part += f": {r['why']}" if r.get("why") else ""
            if len(text) + len(line) + len(part) > limit:
                break
            line += part
        if line.endswith(":") or len(text) + len(line) > limit:
            break
        text += line
    return text[:limit]
