"""Fork: filing Athena action items into Odysseus (``fork/athena/action_items.py``).

Which items are explicit and where they go, making the calendar event, to-do, list line
or note (``_file_one``), where a filed copy stands (:func:`hub_status`) and carrying a
status change across to the other side, once (``_sync_status``).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fork.athena.action_items_lists import (
    _add_to_list,
    _add_to_notes,
    _block_index,
    _change_line,
    _event,
    _line_index,
    _note,
    _note_items,
    _remove_block,
    _ticked,
    list_key,
    list_title,
    notes_title,
)
from fork.athena.action_items_state import (
    LABEL,
    VOICE_LIST,
    CallTool,
    _aware_utc,
    _event_uid,
    _hhmm,
    _valid_zone,
    _ymd,
    _zone,
    digest_hour,
    item_title,
)

logger = logging.getLogger("fork.athena.action_items")  # the job's one logger


# ------------------------------------------------------------------------- filing
KINDS = {"task", "event", "reminder"}
NOTE_KIND = "note"  # "create a note ..." in a voice note: filed into Odysseus notes
TARGETS = {"calendar", "todo", "note"}
CLOSED = {"done", "dismissed"}
REMINDER_MINUTES = 30
DEFAULT_EVENT_MINUTES = 60
MAX_FILE_ATTEMPTS = 3


# An older module sends no ``explicit``: these words in the item's text make it explicit.
_COMMAND = re.compile(
    r"\bremind\s+me\b|\bto-?dos?\b|\btasks?\b|\badd\b.*\bto\s+(?:my|the|our)\b.*\blist\b",
    re.IGNORECASE)


def is_explicit(item: dict) -> bool:
    """Did the owner clearly ask for this to be filed? The module's ``explicit`` field;
    an older module's row: a due date or time, a list, kind reminder / event, or a
    command word. Notes are made only on a command, so they always are."""
    if item.get("kind") == NOTE_KIND:
        return True
    if isinstance(item.get("explicit"), bool):
        return item["explicit"]
    return bool(_ymd(item.get("due_date")) or _hhmm(item.get("due_time"))
                or list_title(item.get("list_name"))
                or item.get("kind") in {"reminder", "event"}
                or _COMMAND.search(str(item.get("text") or "")))


def _on_voice_list(item: dict) -> bool:
    """A voice-note task with no date and no list: a line on the "Voice notes" list."""
    return (item.get("conversation_kind") == "note"
            and str(item.get("kind") or "task") == "task"
            and not _ymd(item.get("due_date")) and not list_title(item.get("list_name")))


def _placed(item: dict, target: str) -> dict:
    """``item`` as it is filed: an undated voice-note task gets the "Voice notes" list."""
    if target == "todo" and _on_voice_list(item):
        return {**item, "list_name": VOICE_LIST}
    return item


def _warn_timezone(state: dict, name: str | None, now_utc: datetime) -> None:
    """Log once when the module's local time isn't the Odysseus user's."""
    if not _valid_zone(name):
        return
    from src.user_time import user_timezone

    hub = user_timezone()
    if ZoneInfo(name).utcoffset(now_utc.replace(tzinfo=None)) == \
            now_utc.astimezone(hub).utcoffset():
        return
    key = f"{name}|{hub}"
    if state.get("tz_warned") != key:
        logger.warning("Athena times are in %s but Odysseus runs in %s; filed items "
                       "keep the module's wall time (converted). Set CONVO_TIMEZONE to match.",
                       name, hub)
        state["tz_warned"] = key


def default_target(item: dict) -> str:
    """Notes go to notes, events with a date to the calendar, everything else to to-dos."""
    if item.get("kind") == NOTE_KIND:
        return "note"
    return "calendar" if item.get("kind") == "event" and _ymd(item.get("due_date")) else "todo"


def _source_line(item: dict) -> str:
    where = "voice note" if item.get("conversation_kind") == "note" else "conversation"
    title = str(item.get("conversation_title") or "").strip()
    link = str(item.get("link") or "").strip()
    return " — ".join(p for p in (f"From your {where}" + (f' "{title}"' if title else ""),
                                  link) if p)


def open_url(target: str, ref: str) -> str:
    """Calendar events by uid; to-dos, lists and notes are all Odysseus notes."""
    return f"/#event-{ref}" if target == "calendar" else f"/#open=notes&note={ref}"


async def _file_one(owner: str, item: dict, target: str, *, rev: int = 0,
                    now_utc: datetime | None = None) -> dict:
    """Create the Odysseus calendar event, to-do or note for ``item``; the ``filed``
    entry."""
    from src.tools.calendar import do_manage_calendar
    from src.tools.notes import do_manage_notes

    item = _placed(item, target)
    text = str(item.get("text") or "").strip() or f"Action item #{item['id']}"
    day, clock = _ymd(item.get("due_date")), _hhmm(item.get("due_time"))
    zone_name = _valid_zone(item.get("timezone"))
    if target == "calendar":
        if not day:
            raise ValueError("an event needs a date; add it to to-dos or set a date first")
        args: dict[str, Any] = {"action": "create_event", "summary": text,
                                "description": _source_line(item)}
        if item.get("location"):
            args["location"] = str(item["location"])
        if clock:
            minutes = item.get("duration_minutes")
            minutes = int(minutes) if isinstance(minutes, int) and minutes > 0 \
                else DEFAULT_EVENT_MINUTES
            args.update(local_start={"date": day, "time": clock}, duration=f"{minutes}m",
                        reminder_minutes=REMINDER_MINUTES)
            if zone_name:
                args["timezone"] = zone_name
        else:
            args.update(all_day=True, local_start={"date": day})
        res = await do_manage_calendar(json.dumps(args), owner=owner or None,
                                       import_event_uid=_event_uid(item["id"], rev))
        if res.get("error") or not res.get("uid"):
            raise RuntimeError(f"calendar: {res.get('error') or 'no event created'}")
        ref = res["uid"]
    elif target == "todo" and list_title(item.get("list_name")):
        line = item_title(item) if list_key(item["list_name"]) == list_key(VOICE_LIST) \
            else text
        return {"kind": str(item.get("kind") or "task"), "target": target,
                **await _add_to_list(owner, item, line),
                "at": _aware_utc(now_utc).isoformat(timespec="seconds"), "rev": rev,
                "module": "open", "hub": "open"}
    elif target == "todo":
        # short title, the full text as its one line
        args = {"action": "add", "note_type": "todo", "title": item_title(item),
                "checklist_items": [{"text": text, "done": False}], "label": LABEL,
                "content": _source_line(item)}
        if day:
            hour = digest_hour()
            at = clock or f"{8 if hour is None else hour:02d}:00"
            local = datetime.fromisoformat(f"{day}T{at}").replace(tzinfo=_zone(zone_name))
            args["due_date"] = local.isoformat()
        res = await do_manage_notes(json.dumps(args), owner=owner or None)
        if res.get("error") or not res.get("note_id"):
            raise RuntimeError(f"to-dos: {res.get('error') or 'no to-do created'}")
        ref = res["note_id"]
    elif target == "note" and notes_title(item.get("list_name")):
        return {"kind": str(item.get("kind") or NOTE_KIND), "target": target,
                **await _add_to_notes(owner, item, text),
                "at": _aware_utc(now_utc).isoformat(timespec="seconds"), "rev": rev,
                "module": "open", "hub": "open"}
    elif target == "note":
        title = _note_title(item, text)
        res = await do_manage_notes(json.dumps({
            "action": "add", "note_type": "note", "title": title, "label": LABEL,
            "content": _note_content(item, text)}), owner=owner or None)
        if res.get("error") or not res.get("note_id"):
            raise RuntimeError(f"notes: {res.get('error') or 'no note created'}")
        ref = res["note_id"]
    else:
        raise ValueError("target must be calendar, todo or note")
    return {"kind": str(item.get("kind") or "task"), "target": target, "ref": ref,
            "at": _aware_utc(now_utc).isoformat(timespec="seconds"), "rev": rev,
            "module": "open", "hub": "open"}


def _note_content(item: dict, text: str) -> str:
    """A filed note: its text, the voice note's key points as a list when the module sends
    them (MCP 0.15.0: the note is the whole voice note, its text the summary), and the
    source line with the transcript link last."""
    points = item.get("key_points")
    points = [str(p).strip() for p in points if str(p).strip()] \
        if isinstance(points, list) else []
    parts = [text] + (["\n".join(f"- {p}" for p in points)] if points else [])
    return "\n\n".join(parts + [_source_line(item)])


def _note_title(item: dict, text: str) -> str:
    """The module's title for the note, else the start of its text."""
    title = re.sub(r"\s+", " ", str(item.get("note_title") or "").strip())
    if title:
        return title[:80]
    words = text.split()
    return " ".join(words[:8]) + (" …" if len(words) > 8 else "")


def hub_status(owner: str, entry: dict, *, now_utc: datetime | None = None,
               zone: str | None = None) -> str:
    """Where the filed copy stands: open, done (ticked/archived/over) or dismissed (gone)."""
    now_utc = _aware_utc(now_utc)
    if entry["target"] == "note":
        note = _note(owner, entry["ref"])
        if note is None:
            return "dismissed"
        if note.archived:
            return "done"
        if "block" in entry and _block_index(note.content, entry["block"]) is None:
            return "dismissed"
        return "open"
    if entry["target"] == "todo":
        note = _note(owner, entry["ref"])
        if note is None:
            return "dismissed"
        items = _note_items(note)
        if "line" in entry:  # one line on a named list
            if note.archived:
                return "done"
            i = _line_index(items, entry["line"])
            if i is None:
                return "dismissed"
            return "done" if _ticked(items[i]) else "open"
        if note.archived or (items and all(i.get("done") or i.get("checked") for i in items)):
            return "done"
        return "open"
    ev = _event(owner, entry["ref"])
    if ev is None or (ev.status or "") == "cancelled":
        return "dismissed"
    if ev.all_day:
        return "done" if now_utc.astimezone(_zone(zone)).date() >= ev.dtend.date() else "open"
    if ev.is_utc:
        ended = ev.dtend <= now_utc.replace(tzinfo=None)
    else:  # naive local wall time (older events)
        ended = ev.dtend <= now_utc.astimezone(_zone(zone)).replace(tzinfo=None)
    return "done" if ended else "open"


async def _set_note_done(owner: str, ref: str, done: bool, line: str | None = None) -> bool:
    from src.tools.notes import do_manage_notes

    if line is not None:
        return await _change_line(owner, ref, line, done=done)
    note = _note(owner, ref)
    if note is None:
        return False
    items = [{"text": str(i.get("text") or ""), "done": done}
             for i in _note_items(note) if str(i.get("text") or "").strip()]
    if not items:
        return False
    res = await do_manage_notes(json.dumps({"action": "update", "id": ref,
                                            "checklist_items": items}), owner=owner or None)
    if res.get("error"):
        raise RuntimeError(f"to-dos: {res['error']}")
    return True


async def _remove_entry(owner: str, entry: dict, *, archive: bool) -> None:
    """Delete the event; archive (Undo) or delete (move) the to-do. Gone already is fine."""
    from src.tools.calendar import do_manage_calendar
    from src.tools.notes import do_manage_notes

    if entry["target"] == "calendar":
        if _event(owner, entry["ref"]) is None:
            return
        res = await do_manage_calendar(json.dumps({"action": "delete_event",
                                                   "uid": entry["ref"]}), owner=owner or None)
        if res.get("error"):
            raise RuntimeError(f"calendar: {res['error']}")
        return
    if "block" in entry:  # take the line out of the notes; the note itself stays
        await _remove_block(owner, entry["ref"], entry["block"])
        return
    if "line" in entry:  # take the line off the list; the list itself stays
        await _change_line(owner, entry["ref"], entry["line"], remove=True)
        return
    if _note(owner, entry["ref"]) is None:
        return
    args = ({"action": "update", "id": entry["ref"], "archived": True} if archive
            else {"action": "delete", "id": entry["ref"]})
    res = await do_manage_notes(json.dumps(args), owner=owner or None)
    if res.get("error"):
        raise RuntimeError(f"to-dos: {res['error']}")


async def _set_module_status(call: CallTool, item_id: int, status: str) -> None:
    res = await call("update_action_item", {"item_id": item_id, "status": status})
    if not str(res).startswith("Action item"):
        raise RuntimeError(f"update_action_item: {res}")


async def _sync_status(owner: str, call: CallTool, item_id: int, entry: dict,
                       row: dict | None, now_utc: datetime) -> int:
    """Carry one status change across, once. Edge-triggered on both sides: a change is
    a status that differs from the one recorded last pass, so nothing ping-pongs."""
    if entry.get("undone") or row is None:
        return 0
    module = str(row.get("status") or "open")
    if entry["target"] == "note":  # a filed note is done in the module, nothing more
        if module == "open" and entry.get("module") != "done":  # marking it failed
            await _set_module_status(call, item_id, "done")
            entry["module"] = "done"
            return 1
        if module != "open":  # the row of this pass may predate the "done" just set
            entry["module"] = module
        return 0
    zone = row.get("timezone")
    last_module, last_hub = entry.get("module", "open"), entry.get("hub", "open")
    hub = hub_status(owner, entry, now_utc=now_utc, zone=zone)
    changes = 0
    if module != last_module:  # changed in the module (page, chat): follow it here
        if entry["target"] == "todo" and hub != "dismissed" and (hub in CLOSED) != (
                module in CLOSED):
            changes += int(await _set_note_done(owner, entry["ref"], module in CLOSED,
                                                entry.get("line")))
        elif entry["target"] == "calendar" and module == "dismissed" and hub == "open":
            await _remove_entry(owner, entry, archive=False)
            changes += 1
        hub = hub_status(owner, entry, now_utc=now_utc, zone=zone)
    elif hub != last_hub:  # changed in Odysseus: tell the module
        if hub in CLOSED and module == "open":
            await _set_module_status(call, item_id, hub)
            module, changes = hub, 1
        elif hub == "open" and module in CLOSED:
            await _set_module_status(call, item_id, "open")
            module, changes = "open", 1
    entry["module"], entry["hub"] = module, hub
    return changes
