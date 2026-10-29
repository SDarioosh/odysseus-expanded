"""Fork: the action-items job's phone pushes (``fork/athena/action_items.py``).

Through Odysseus' reminder dispatcher (ntfy by default), verbatim: new items, what was
filed, suggestions, items due today and yesterday's digest (once a day after the
digest hour).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from fork.athena.action_items_filing import _placed
from fork.athena.action_items_lists import list_key, list_title, notes_title
from fork.athena.action_items_state import (
    VOICE_LIST,
    ActionItem,
    CallTool,
    _when_label,
    digest_hour,
    item_title,
    notify_channel,
    save_state,
)

MAX_PUSHED_ITEMS = 5


# --------------------------------------------------------------------------- push
async def push(owner: str, title: str, body: str, key: str) -> bool:
    """Phone notification through Odysseus' reminder dispatcher (ntfy by default)."""
    channel = notify_channel()
    if channel == "off":
        return False
    from routes.note_routes import dispatch_reminder

    # Verbatim text: no LLM rewrite of transcript-derived content.
    result = await dispatch_reminder(
        title, body, key, owner=owner,
        settings_override={"reminder_channel": channel, "reminder_llm_synthesis": False})
    return bool(result.get("ntfy_sent") or result.get("browser_sent"))


def _items_message(items: list[ActionItem], what: str = "new action item"
                   ) -> tuple[str, str]:
    title = what[:1].upper() + what[1:] if len(items) == 1 else f"{len(items)} {what}s"
    lines = []
    for it in items[:MAX_PUSHED_ITEMS]:
        due = f" (due {it.due_date or it.due})" if (it.due_date or it.due) else ""
        lines.append(f"- {it.text}{due}")
    if len(items) > MAX_PUSHED_ITEMS:
        lines.append(f"... and {len(items) - MAX_PUSHED_ITEMS} more on the Records page")
    return title, "\n".join(lines)


def _digest_message(text: str) -> tuple[str, str] | None:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines or lines[0].startswith("No digest"):
        return None
    # drop the citation rule meant for the model
    lines = [ln for ln in lines if not ln.startswith("When answering")]
    head = lines[0]
    title = head.split(": ", 1)[1] if ": " in head else head
    return f"Yesterday: {title}"[:200], "\n".join(lines[1:])[:1500]


def _filed_line(item: dict, target: str) -> str:
    item = _placed(item, target)
    text = str(item.get("text") or "").strip()
    if target == "todo" and not (list_title(item.get("list_name"))
                                 and list_key(item["list_name"]) != list_key(VOICE_LIST)):
        text = item_title(item)  # a to-do of its own, or a line on "Voice notes"
    when = _when_label(item)
    if target == "calendar":
        line = f"📅 {text}" + (f" · {when}" if when else "") + " → Calendar"
    elif target == "note":
        name = notes_title(item.get("list_name"))
        line = f"📝 {text}" + (f" → {name}" if name else " → Notes")
    else:
        icon = "⏰" if item.get("kind") == "reminder" else "☑️"
        name = list_title(item.get("list_name"))
        line = f"{icon} {text}" + (f" · due {when}" if when else "") + (
            f" → {name} list" if name else " → To-dos")
    if item.get("time_unclear"):
        line += " ⚠️ check the time"
    return line


def _filed_message(filed: list[tuple[dict, str]]) -> tuple[str, str]:
    title = ("Filed from your voice note" if len(filed) == 1
             else f"Filed {len(filed)} items from your voice notes")
    lines = [_filed_line(item, target) for item, target in filed[:MAX_PUSHED_ITEMS]]
    if len(filed) > MAX_PUSHED_ITEMS:
        lines.append(f"... and {len(filed) - MAX_PUSHED_ITEMS} more on the Athena page")
    return title, "\n".join(lines)


def _suggested_message(items: list[dict]) -> tuple[str, str]:
    title = ("Suggested from your voice note" if len(items) == 1
             else f"{len(items)} suggestions from your voice notes")
    lines = [f"💡 {item_title(item)}" for item in items[:MAX_PUSHED_ITEMS]]
    if len(items) > MAX_PUSHED_ITEMS:
        lines.append(f"... and {len(items) - MAX_PUSHED_ITEMS} more")
    lines.append("Add or dismiss them on the Athena page.")
    return title, "\n".join(lines)


async def _daily_pushes(owner: str, call: CallTool, state: dict, due_today: Callable[[str], list],
                        now: datetime, stats: dict[str, int]) -> None:
    """Items due today and yesterday's digest, once a day after the digest hour."""
    hour = digest_hour()
    today = now.date().isoformat()
    if hour is None or now.hour < hour:
        return
    if state.get("due_pushed") != today:
        due = due_today(today)
        if due:
            title, body = _items_message(due, "action item due today")
            stats["pushed"] += int(await push(owner, title, body, f"module-due-{today}"))
        state["due_pushed"] = today
        save_state(owner, state)
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    if state.get("digest_pushed") != yesterday:
        msg = _digest_message(await call("get_daily_digest", {"date": yesterday}))
        if msg:  # not ready yet: try again next pass
            stats["pushed"] += int(await push(owner, msg[0], msg[1],
                                              f"module-digest-{yesterday}"))
            state["digest_pushed"] = yesterday
            save_state(owner, state)
