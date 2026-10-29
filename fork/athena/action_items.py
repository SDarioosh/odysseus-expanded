"""Fork: phone notifications for Athena action items and the daily digest.

Action items stay in the module (Athena is the only source; the owner chose a live
view over copies). A built-in scheduled task ("Module Action Items", every 15 minutes)
reads them through the module's MCP tools and pushes, through Odysseus' reminder
dispatcher on the ntfy channel:

* new open action items (the first run only remembers the backlog, it doesn't push it);
* once a day after ``MODULE_DIGEST_HOUR``: items due today, and yesterday's digest.

Odysseus never opens the module's database and keeps no copy of the items: the only state
is which item ids and days were already pushed (``<DATA_DIR>/barvis_action_items_*.json``;
the file name is kept from the fork's former name so existing state carries over).
The live list (tick done / dismiss) is on the Athena page, ``/modules/athena``.

Settings (environment; the former ``BARVIS_*`` names are still read as a fallback):

* ``MODULE_ACTION_ITEMS_SERVER``: module MCP server name or id (default ``convo``).
* ``MODULE_NOTIFY_CHANNEL``: ``ntfy`` (default), ``browser`` or ``off``.
* ``MODULE_DIGEST_HOUR``: local hour after which the daily pushes go out (default 8;
  ``off`` = no daily pushes).
* ``MODULE_FILE_ITEMS``: ``explicit`` (default), ``auto`` or ``off``. ``off`` keeps the
  behaviour above exactly (text result, "new action item" pushes, nothing filed).
  ``auto`` files every new voice-note item. ``explicit`` files a voice-note item only
  when it is explicit (``is_explicit``: the module's ``explicit`` field, or for an older
  module a due date or time, a ``list_name``, kind reminder / event / note, or a command
  word such as "remind me", "to-do", "task", "add ... to my ... list"); the other
  voice-note items become suggestions like conversation items (one push per pass,
  "Suggested from your voice note", with their short titles; nothing filed).

Filing (every 5 minutes): the task reads the items as JSON
(``list_action_items {"status": "all", "format": "json"}``) and files new open items from
voice notes into Odysseus itself: events into the default calendar (``do_manage_calendar``,
idempotent uid, 30-minute reminder when timed), tasks and reminders into to-dos
(``do_manage_notes``, checklist note, due at the stated time or at ``MODULE_DIGEST_HOUR``).
One push per pass lists what went where; items from ordinary conversations are only
suggestions on ``/modules/athena`` (``file_item``) and are not pushed. Status then follows
both ways (to-do ticked/archived or event over -> done, event deleted -> dismissed; module
done/dismissed -> to-do ticked, module dismissed -> event deleted), each change once.
The state file then also holds ``filed`` (item id -> where it went) and ``file_since``.

Named lists: an item the module gives a ``list_name`` ("add milk to my shopping list" ->
``shopping``) goes onto that to-do list as one line instead of a to-do of its own: the
open checklist / to-do note whose title is that name ("Shopping", "shopping list"), or a
new one titled with it. The line carries the due date in its text (a line has no
reminder). Its status follows the line: ticked -> done, removed -> dismissed, list
archived -> done; Undo and moving the item remove the line only.
"Voice notes" list: a voice-note task without a due date and without a ``list_name`` is
one line (its short ``title``) on the to-do list "Voice notes" (made if missing), not a
to-do of its own; the named-list rules above apply. A dated task or a reminder keeps a
to-do of its own, titled with the short ``title`` (the module's, else the start of the
text), its one checklist line the full text. ``unfile_auto_todos`` (route
``POST /api/modules/athena/unfile-todos``, script ``scripts/unfile_voice_note_todos.py``)
removes the single undated to-dos filed per non-explicit voice-note item before this
(never a dated to-do or a reminder; entries record ``filed_mode``). A "Voice notes" list
emptied by Undo or a move is archived (``_archive_if_empty``).
Notes: an item of kind ``note`` (the owner said "create a note ..." in a voice note)
becomes an Odysseus note (type ``note``) titled with its ``note_title``, its text, the
voice note's ``key_points`` as a list (MCP 0.15.0, a note that is the whole voice note) and
the source line as content. With a ``list_name`` ("add to my hardware notes ..." ->
``hardware``) it is added as a line ("- <text>") to that note instead: the open note
whose title is that name ("Hardware notes", "hardware"), or a new one titled
"Hardware notes". A filed note is marked done in the module right away (a note is no
to-do; it leaves the open lists); status isn't synced after that. Undo / dismissing it
from the page removes the note or the line.
An older module that answers with text instead of JSON gets the old behaviour.

Code: this module is the task's passes (:func:`sync`), the page's actions (file, move,
undo, clean-up, :func:`filed_state`) and the task entry, and offers every name of the
job, so callers import ``fork.athena.action_items`` only. The rest is in
``action_items_state.py`` (the stored names, settings, state file and Athena's rows),
``action_items_lists.py`` (named to-do lists and notes), ``action_items_filing.py``
(filing and status sync) and ``action_items_push.py`` (the pushes), all in
``fork/athena/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from fork.athena import client as athena
from fork.athena.action_items_filing import (  # noqa: F401 -- re-exported
    _COMMAND,
    CLOSED,
    DEFAULT_EVENT_MINUTES,
    KINDS,
    MAX_FILE_ATTEMPTS,
    NOTE_KIND,
    REMINDER_MINUTES,
    TARGETS,
    _file_one,
    _note_content,
    _note_title,
    _on_voice_list,
    _placed,
    _remove_entry,
    _set_module_status,
    _set_note_done,
    _source_line,
    _sync_status,
    _warn_timezone,
    default_target,
    hub_status,
    is_explicit,
    open_url,
)
from fork.athena.action_items_lists import (  # noqa: F401 -- re-exported
    _LIST_WORDS,
    _NOTES_WORDS,
    LIST_TYPES,
    _add_to_list,
    _add_to_notes,
    _archive_if_empty,
    _block_index,
    _change_line,
    _event,
    _line_index,
    _line_text,
    _lists,
    _norm_line,
    _note,
    _note_items,
    _raw_items,
    _remove_block,
    _ticked,
    find_list,
    find_notes,
    list_key,
    list_title,
    notes_key,
    notes_title,
    todo_lists,
)
from fork.athena.action_items_push import (  # noqa: F401 -- re-exported
    MAX_PUSHED_ITEMS,
    _daily_pushes,
    _digest_message,
    _filed_line,
    _filed_message,
    _items_message,
    _suggested_message,
    push,
)
from fork.athena.action_items_state import (  # noqa: F401 -- re-exported
    _DUE_DATE,
    _DUE_TEXT,
    _LINE,
    _OWNER,
    ACTION,
    FILE_MODES,
    HOUSEKEEPING_DEFAULT,
    INFO,
    LABEL,
    OLD_LABEL,
    TASK_NAME,
    TITLE_MAX,
    VOICE_LIST,
    ActionItem,
    CallTool,
    _aware_utc,
    _event_uid,
    _hhmm,
    _row_item,
    _state_path,
    _valid_zone,
    _when_label,
    _ymd,
    _zone,
    digest_hour,
    file_mode,
    is_label,
    item_title,
    load_state,
    mcp_caller,
    module_server_id,
    notify_channel,
    parse_action_items,
    parse_items_json,
    save_state,
    server_key,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- sync
async def sync(owner: str, call: CallTool, *, now: datetime | None = None) -> dict[str, int]:
    """One pass. Returns counts: new (items first seen), pushed (notifications sent) and,
    when filing, filed (items put into Odysseus) and synced (status changes applied)."""
    if file_mode() == "off":
        return await _sync_text(owner, call, now=now)
    rows = parse_items_json(await call("list_action_items", {"status": "all", "format": "json"}))
    if rows is None:  # an older module: text only, so the old behaviour (nothing filed)
        logger.info("Athena answered list_action_items with text; not filing")
        return await _sync_text(owner, call, now=now)
    rows = [r for r in rows if r.get("mine") is not False]  # someone else's to-do isn't ours
    async with _lock(owner):
        return await _sync_json(owner, call, rows, now=now)


async def _sync_text(owner: str, call: CallTool, *, now: datetime | None = None
                     ) -> dict[str, int]:
    """The behaviour before filing (``MODULE_FILE_ITEMS=off``, or a text-only module)."""
    now = now or datetime.now()
    state = load_state(owner)
    items = parse_action_items(await call("list_action_items", {"status": "open"}))
    stats = {"new": 0, "pushed": 0}
    first_run = "seen" not in state
    seen = set(state.get("seen", []))
    new = [i for i in items if i.id not in seen]
    stats["new"] = len(new)
    # remember open ids only; a closed item never comes back as new
    state["seen"] = sorted(seen | {i.id for i in items})
    save_state(owner, state)
    if new and not first_run:
        title, body = _items_message(new)
        key = "module-items-" + "-".join(str(i.id) for i in new[:20])
        stats["pushed"] += int(await push(owner, title, body, key))
    await _daily_pushes(owner, call, state,
                        lambda today: [i for i in items if i.due_date == today], now, stats)
    return stats


_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(owner: str) -> asyncio.Lock:
    """One state writer per owner (the task and the page's buttons share the file)."""
    return _LOCKS.setdefault(owner or "", asyncio.Lock())


def _created_before(row: dict, since: str | None) -> bool:
    try:
        created = datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
        start = datetime.fromisoformat(str(since))
    except (TypeError, ValueError):
        return False
    if created.tzinfo is None or start.tzinfo is None:
        return False
    return created < start


async def _sync_json(owner: str, call: CallTool, rows: list[dict], *,
                     now: datetime | None = None) -> dict[str, int]:
    now_utc = _aware_utc(now)
    zone_name = next((r.get("timezone") for r in rows if r.get("timezone")), None)
    local_now = now if (now is not None and now.tzinfo is None) \
        else now_utc.astimezone(_zone(zone_name))
    state = load_state(owner)
    stats = {"new": 0, "filed": 0, "synced": 0, "pushed": 0}
    _warn_timezone(state, zone_name, now_utc)
    first_run = "file_since" not in state
    if first_run:  # remember the backlog; file only what comes after
        state["file_since"] = now_utc.isoformat(timespec="seconds")
    seen = set(state.get("seen", []))
    filed: dict[str, dict] = state.setdefault("filed", {})
    attempts: dict[str, int] = state.setdefault("file_attempts", {})
    by_id = {r["id"]: r for r in rows}
    new = [r for r in rows if r["id"] not in seen]
    stats["new"] = sum(1 for r in new if r.get("status") == "open")
    retry = set()
    newly: list[tuple[dict, str]] = []
    suggested: list[dict] = []
    explicit_only = file_mode() == "explicit"
    for row in new if not first_run else []:
        key = str(row["id"])
        if (row.get("status") != "open" or row.get("conversation_kind") != "note"
                or key in filed or _created_before(row, state.get("file_since"))):
            continue
        if explicit_only and not is_explicit(row):  # waits on the page, like conversations
            suggested.append(row)
            continue
        target = default_target(row)
        try:
            filed[key] = await _file_one(owner, row, target, now_utc=now_utc)
            filed[key]["filed_mode"] = file_mode()  # the clean-up leaves these alone
        except Exception as e:  # keep it unseen, try again next pass (a few times)
            attempts[key] = attempts.get(key, 0) + 1
            logger.warning("filing Athena item #%s failed (%s/%s): %s", key,
                           attempts[key], MAX_FILE_ATTEMPTS, e)
            if attempts[key] < MAX_FILE_ATTEMPTS:
                retry.add(row["id"])
            continue
        attempts.pop(key, None)
        newly.append((row, target))
        stats["filed"] += 1
        if target == "note":  # a note is no to-do: done in the module (retried below)
            try:
                await _set_module_status(call, row["id"], "done")
                filed[key]["module"] = "done"
            except Exception as e:
                logger.warning("marking Athena note #%s done failed: %s", key, e)
    state["seen"] = sorted((seen | set(by_id)) - retry)
    save_state(owner, state)

    for key, entry in filed.items():
        try:
            stats["synced"] += await _sync_status(owner, call, int(key), entry,
                                                  by_id.get(int(key)), now_utc)
        except Exception as e:  # the next pass sees the same difference and retries
            logger.warning("status sync for Athena item #%s failed: %s", key, e)
    save_state(owner, state)

    if newly:
        title, body = _filed_message(newly)
        key = "module-filed-" + "-".join(str(r["id"]) for r, _ in newly[:20])
        stats["pushed"] += int(await push(owner, title, body, key))
    if suggested:
        title, body = _suggested_message(suggested)
        key = "module-suggested-" + "-".join(str(r["id"]) for r in suggested[:20])
        stats["pushed"] += int(await push(owner, title, body, key))

    def due_today(today: str) -> list[ActionItem]:
        return [_row_item(r) for r in rows
                if r.get("status") == "open" and r.get("due_date") == today
                and str(r["id"]) not in filed]

    await _daily_pushes(owner, call, state, due_today, local_now, stats)
    return stats


# ------------------------------------------------------------------ page helpers
async def _find_row(call: CallTool, item_id: int) -> dict:
    rows = parse_items_json(await call("list_action_items", {"status": "all", "format": "json"}))
    if rows is None:
        raise RuntimeError("this Athena version can't list items as JSON; update it")
    for row in rows:
        if row["id"] == int(item_id):
            return row
    raise LookupError(f"No action item #{item_id}.")


def _public(entry: dict) -> dict:
    out = {"target": entry["target"], "ref": entry["ref"],
           "open_url": open_url(entry["target"], entry["ref"])}
    if entry.get("list"):
        out["list"] = entry["list"]
    if entry.get("notes"):
        out["notes"] = entry["notes"]
    return out


async def file_item(owner: str, item: dict, call: CallTool, *,
                    target: str | None = None) -> dict:
    """File one item now ("Add to calendar" / "Add to to-dos" / "Add to notes" on a
    suggestion).

    ``item`` is the module's JSON row. ``target`` defaults from its kind. An earlier
    Odysseus entry for the item is replaced; a closed item is reopened in the module, and
    an item filed as a note is marked done there. Returns ``{"target", "ref",
    "open_url"}``.
    """
    item = {**item, "id": int(item["id"])}
    target = target or default_target(item)
    if target not in TARGETS:
        raise ValueError("target must be calendar, todo or note")
    async with _lock(owner):
        state = load_state(owner)
        entry = await _refile(owner, state, item, target)
        status = "done" if target == "note" else "open"
        if str(item.get("status") or "open") != status:
            await _set_module_status(call, item["id"], status)
        entry["module"] = status
        save_state(owner, state)
        return _public(entry)


async def _refile(owner: str, state: dict, item: dict, target: str) -> dict:
    """Replace the item's Odysseus entry (if any) with a new one at ``target``; saves."""
    filed = state.setdefault("filed", {})
    key = str(item["id"])
    old = filed.get(key)
    if old and not old.get("undone"):
        # first, so a to-do with the same title and due time isn't taken for a duplicate
        await _remove_entry(owner, old, archive=False)
        old.update(undone=True, module="dismissed", hub="dismissed")
        save_state(owner, state)
    entry = await _file_one(owner, item, target, rev=(old.get("rev", 0) + 1) if old else 0)
    entry["filed_mode"] = "page"  # the owner's tap (Add / Move): never cleaned up
    filed[key] = entry
    state["seen"] = sorted(set(state.get("seen", [])) | {item["id"]})
    save_state(owner, state)
    return entry


async def unfile_item(owner: str, item_id: int, call: CallTool) -> dict:
    """Undo: remove the event / archive the to-do or note (or take its line off the list or
    notes) and dismiss the item in the module.
    The item stays recorded as filed, so the task never files it again."""
    async with _lock(owner):
        state = load_state(owner)
        entry = state.get("filed", {}).get(str(int(item_id)))
        if not entry or entry.get("undone"):
            raise LookupError(f"Action item #{item_id} isn't filed in Odysseus.")
        await _remove_entry(owner, entry, archive=True)
        await _set_module_status(call, int(item_id), "dismissed")
        entry.update(undone=True, module="dismissed", hub="dismissed",
                     undone_at=_aware_utc(None).isoformat(timespec="seconds"))
        save_state(owner, state)
        return {"ok": True, "target": entry["target"], "ref": entry["ref"]}


def _cleanup_entry(entry: dict, row: dict | None, include_all: bool) -> str | None:
    """How a filed entry came to be one of the to-dos filed one per voice-note item, or
    None. Never a list line or a reminder, nor an item that is explicit now; entries filed
    in ``explicit`` mode or from the page only with ``include_all``."""
    if (entry.get("undone") or entry.get("target") != "todo" or "line" in entry
            or "block" in entry or not entry.get("ref") or row is None
            or row.get("conversation_kind") != "note" or entry.get("kind") == "reminder"
            or is_explicit(row)):
        return None
    mode = entry.get("filed_mode")
    if mode in {"explicit", "page"} and not include_all:
        return None
    return {None: "filed before explicit filing", "auto": "filed in auto mode"}.get(
        mode, f"filed in {mode} mode (all)")


def _single_undated_todo(note) -> bool:
    """A filed to-do of its own still in Odysseus: one line, no due date, no reminder."""
    return (note is not None and not note.archived and is_label(note.label)
            and note.note_type == "todo" and len(_note_items(note)) <= 1
            and not note.due_date and (note.repeat or "none") == "none")


async def unfile_auto_todos(owner: str, call: CallTool, *, apply: bool = False,
                            only: set[int] | None = None, include_all: bool = False) -> dict:
    """Clean-up for the to-dos filed one per voice-note item before ``explicit`` filing
    and the "Voice notes" list: filed to-dos of their own (label ``athena``, or
    ``odysseus-sound`` before the rename; one checklist line, no due date, no reminder) of
    voice-note items that are not explicit now. Entries filed in ``explicit`` mode or from the page are left out unless
    ``include_all``. With ``apply`` each is deleted and its item, when still open,
    dismissed in the module; the item stays recorded as filed (undone), so it is never
    filed again. ``only``: just these item ids. Returns ``{"apply", "todos": [{"id",
    "title", "text", "status", "ref", "why"}], "removed"}``; ``why`` lists the reasons
    it qualifies."""
    from src.tools.notes import do_manage_notes

    rows = parse_items_json(await call("list_action_items",
                                       {"status": "all", "format": "json"}))
    if rows is None:
        raise RuntimeError("this Athena version can't list items as JSON; update it")
    by_id = {r["id"]: r for r in rows}
    async with _lock(owner):
        state = load_state(owner)
        found = []
        for key, entry in sorted(state.get("filed", {}).items(), key=lambda kv: int(kv[0])):
            if only is not None and int(key) not in only:
                continue
            row = by_id.get(int(key))
            how = _cleanup_entry(entry, row, include_all)
            if how is None:
                continue
            note = _note(owner, entry["ref"])
            if _single_undated_todo(note):
                why = [how, "not explicit now", "a to-do of its own",
                       "no due date or reminder"]
                found.append((int(key), entry, row, note, why))
        out = {"apply": bool(apply), "removed": 0, "todos": [
            {"id": i, "title": note.title or "", "text": str(row.get("text") or ""),
             "status": str(row.get("status") or "open"), "ref": entry["ref"], "why": why}
            for i, entry, row, note, why in found]}
        if not apply:
            return out
        for item_id, entry, row, _, _ in found:
            res = await do_manage_notes(json.dumps({"action": "delete", "id": entry["ref"]}),
                                        owner=owner or None)
            if res.get("error"):
                raise RuntimeError(f"to-dos: {res['error']}")
            module = str(row.get("status") or "open")
            if module == "open":
                await _set_module_status(call, item_id, "dismissed")
                module = "dismissed"
            entry.update(undone=True, module=module, hub="dismissed",
                         undone_at=_aware_utc(None).isoformat(timespec="seconds"))
            save_state(owner, state)
            out["removed"] += 1
        return out


async def move_item(owner: str, item_id: int, call: CallTool, *, kind: str,
                    due_date: str | None = None, due_time: str | None = None,
                    list_name: str | None = None) -> dict:
    """Move to calendar / to-dos / a to-do list or change the time: update the module
    item (kind, date, time, list), remove the old Odysseus entry and file the item again
    where it belongs. ``due_date`` / ``due_time`` / ``list_name``: None = unchanged,
    ``"none"`` = clear (a to-do of its own)."""
    if kind not in KINDS:
        raise ValueError("kind must be task, event or reminder")
    if due_date not in (None, "none") and not _ymd(due_date):
        raise ValueError("due_date must be YYYY-MM-DD or none")
    if due_time not in (None, "none") and not _hhmm(due_time):
        raise ValueError("due_time must be HH:MM or none")
    if list_name not in (None, "none") and not list_title(list_name):
        raise ValueError("list_name must be a list name or none")
    row = await _find_row(call, item_id)
    day = row.get("due_date") if due_date is None else (None if due_date == "none" else due_date)
    if kind == "event" and not _ymd(day):
        raise ValueError("an event needs a date")
    args: dict[str, Any] = {"item_id": int(item_id), "kind": kind}
    if due_date is not None:
        args["due_date"] = due_date
    if due_time is not None:
        args["due_time"] = due_time
    if list_name is not None:
        args["list_name"] = list_name
    res = await call("update_action_item", args)
    if not str(res).startswith("Action item"):
        raise ValueError(str(res))
    row = await _find_row(call, item_id)
    target = "calendar" if kind == "event" else "todo"
    async with _lock(owner):
        entry = await _refile(owner, load_state(owner), row, target)
        if str(row.get("status") or "open") != "open":
            await _set_module_status(call, int(item_id), "open")
        return _public(entry)


async def filed_state(owner: str) -> dict[int, dict]:
    """Where each item went: ``{id: {"target", "ref", "open_url", "kind", "at", "undone",
    "removed"}}``. ``undone``: the owner pressed Undo; ``removed``: the event or to-do is
    gone from Odysseus (deleted there). Either way it is no longer filed, and the task
    never files it again. ``owner`` "" and None are the same (auth off)."""
    filed = load_state(owner).get("filed", {})
    out = {}
    for key, entry in filed.items():
        undone = bool(entry.get("undone"))
        try:
            removed = not undone and hub_status(owner, entry) == "dismissed"
        except Exception:  # the page still lists the item
            removed = False
        out[int(key)] = {**_public(entry), "kind": entry.get("kind"), "at": entry.get("at"),
                         "undone": undone, "removed": removed}
    return out


async def action_sync_action_items(owner: str, **kwargs) -> tuple[str, bool]:
    """Built-in task entry point (see ``register_builtin_action``)."""
    from src.builtin_actions import TaskNoop

    call = athena.ATHENA.caller()
    if call is None:
        raise TaskNoop(f"module MCP server {server_key()!r} is not connected")
    try:
        stats = await sync(owner, call)
    except RuntimeError as e:
        return f"Action item sync failed: {e}", False
    if not any(stats.values()):
        raise TaskNoop("no changes")
    filed = (f"{stats['filed']} filed, {stats['synced']} status changes, "
             if "filed" in stats else "")
    return (f"Action items: {stats['new']} new, {filed}"
            f"{stats['pushed']} notifications sent."), True


def register_builtin_action(actions: dict, info: dict) -> None:
    actions[ACTION] = action_sync_action_items
    info[ACTION] = INFO


def register_housekeeping(defaults: dict) -> None:
    defaults.update(HOUSEKEEPING_DEFAULT)
