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
The live list (tick done / dismiss) is on the Athena page, ``/modules/sound``.

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
suggestions on ``/modules/sound`` (``file_item``) and are not pushed. Status then follows
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
``POST /api/modules/sound/unfile-todos``, script ``scripts/unfile_voice_note_todos.py``)
removes the single undated to-dos filed per non-explicit voice-note item before this
(never a dated to-do or a reminder; entries record ``filed_mode``). A "Voice notes" list
emptied by Undo or a move is archived (``_archive_if_empty``).
Notes: an item of kind ``note`` (the owner said "create a note ..." in a voice note)
becomes an Odysseus note (type ``note``) titled with its ``note_title``, its text and the
source line as content. With a ``list_name`` ("add to my hardware notes ..." ->
``hardware``) it is added as a line ("- <text>") to that note instead: the open note
whose title is that name ("Hardware notes", "hardware"), or a new one titled
"Hardware notes". A filed note is marked done in the module right away (a note is no
to-do; it leaves the open lists); status isn't synced after that. Undo / dismissing it
from the page removes the note or the line.
An older module that answers with text instead of JSON gets the old behaviour.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# The action id is stored in each user's scheduled task, so it keeps the fork's former name.
ACTION = "barvis_sync_action_items"
LABEL = "odysseus-sound"
TASK_NAME = "Module Action Items"
HOUSEKEEPING_DEFAULT = {
    ACTION: {"name": TASK_NAME, "schedule": "cron", "scheduled_time": None,
             "cron_expression": "*/5 * * * *", "ship_paused": True,
             "old_cron_expressions": ["*/15 * * * *"],
             "legacy_names": ["Barvis Action Items"]},
}
INFO = ("File Athena action items from voice notes into the calendar, to-dos and "
        "notes, "
        "keep their status in step, and push what was filed, items due today and the "
        "daily digest to the phone via ntfy. Reads them through the module's MCP tools.")
MAX_PUSHED_ITEMS = 5

# One line of Athena's list_action_items result, e.g.
# - #12 [open] Send Alice the spreadsheet (owner Me) due 2026-09-19 (Friday) — from
#   "Q4 budget" 2026-09-15 10:00 http://host:8000/view/abc?sig=...#t=40
_LINE = re.compile(
    r"^- #(?P<id>\d+) \[(?P<status>open|done|dismissed)\] (?P<rest>.*) — from "
    r"\"(?P<title>.*)\"(?: \(voice note\))? (?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (?P<link>\S+)$")
_DUE_DATE = re.compile(r" due (?P<date>\d{4}-\d{2}-\d{2})(?: \((?P<said>[^)]*)\))?$")
# the last " due ": the item text itself may say "due" ("Check the due date")
_DUE_TEXT = re.compile(r" due (?P<said>(?:(?! due ).)+)$")
_OWNER = re.compile(r" \(owner (?P<owner>[^)]*)\)$")


@dataclass(frozen=True)
class ActionItem:
    id: int
    status: str
    text: str
    owner: str | None
    due_date: str | None  # YYYY-MM-DD
    due: str | None  # as said, e.g. "Friday"
    conversation: str
    when: str
    link: str


def parse_action_items(result: str) -> list[ActionItem]:
    """Parse the module's list_action_items text (lines that don't match are skipped)."""
    items = []
    for line in (result or "").splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        rest = m["rest"]
        due_date = due = owner = None
        if d := _DUE_DATE.search(rest):
            due_date, due, rest = d["date"], d["said"], rest[:d.start()]
        elif d := _DUE_TEXT.search(rest):
            due, rest = d["said"], rest[:d.start()]
        if o := _OWNER.search(rest):
            owner, rest = o["owner"], rest[:o.start()]
        items.append(ActionItem(int(m["id"]), m["status"], rest.strip(), owner, due_date, due,
                                m["title"], m["when"], m["link"]))
    return items


# ----------------------------------------------------------------------- settings
def _env(name: str, default: str) -> str:
    """``MODULE_<name>``, else the former ``BARVIS_<name>``, else ``default``."""
    return (os.environ.get(f"MODULE_{name}") or os.environ.get(f"BARVIS_{name}")
            or default).strip()


def server_key() -> str:
    return _env("ACTION_ITEMS_SERVER", "convo").casefold()


def notify_channel() -> str:
    value = _env("NOTIFY_CHANNEL", "ntfy").lower()
    return value if value in {"ntfy", "browser", "off"} else "ntfy"


def digest_hour() -> int | None:
    value = _env("DIGEST_HOUR", "8").lower()
    if value in {"off", "none", ""}:
        return None
    try:
        return max(0, min(23, int(value)))
    except ValueError:
        return 8


# -------------------------------------------------------------------------- state
def _state_path(owner: str) -> Path:
    from src.constants import DATA_DIR

    slug = "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in (owner or "default"))
    return Path(DATA_DIR) / f"barvis_action_items_{slug}.json"


def load_state(owner: str) -> dict[str, Any]:
    try:
        state = json.loads(_state_path(owner).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("items", {})
    return state


def save_state(owner: str, state: dict[str, Any]) -> None:
    path = _state_path(owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# -------------------------------------------------------------------------- MCP
CallTool = Callable[[str, dict], Awaitable[str]]


def module_server_id(manager: Any, key: str | None = None) -> str | None:
    """Id of the connected module MCP server named ``key`` (name or id)."""
    key = (key or server_key()).casefold()
    connections = getattr(manager, "_connections", {}) or {}
    for server_id, conn in connections.items():
        if (conn or {}).get("status") != "connected":
            continue
        name = str((conn or {}).get("name") or "").strip().casefold()
        if str(server_id).casefold() == key or name == key:
            return server_id
    return None


def mcp_caller(manager: Any, server_id: str) -> CallTool:
    async def call(tool: str, arguments: dict) -> str:
        res = await manager.call_tool(f"mcp__{server_id}__{tool}", arguments)
        if res.get("exit_code") or res.get("error"):
            raise RuntimeError(f"{tool}: {res.get('error') or res.get('stderr') or 'failed'}")
        return res.get("stdout") or ""
    return call


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
        lines.append(f"... and {len(items) - MAX_PUSHED_ITEMS} more on the Conversations page")
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


# ------------------------------------------------------------------------- filing
KINDS = {"task", "event", "reminder"}
NOTE_KIND = "note"  # "create a note ..." in a voice note: filed into Odysseus notes
TARGETS = {"calendar", "todo", "note"}
CLOSED = {"done", "dismissed"}
REMINDER_MINUTES = 30
DEFAULT_EVENT_MINUTES = 60
MAX_FILE_ATTEMPTS = 3
_LOCKS: dict[str, asyncio.Lock] = {}


FILE_MODES = ("explicit", "auto", "off")
VOICE_LIST = "Voice notes"  # the one to-do list for undated voice-note tasks
TITLE_MAX = 60
# An older module sends no ``explicit``: these words in the item's text make it explicit.
_COMMAND = re.compile(
    r"\bremind\s+me\b|\bto-?dos?\b|\btasks?\b|\badd\b.*\bto\s+(?:my|the|our)\b.*\blist\b",
    re.IGNORECASE)


def file_mode() -> str:
    """``MODULE_FILE_ITEMS``: ``explicit`` (default), ``auto`` (``all``) or ``off``."""
    value = _env("FILE_ITEMS", "explicit").lower()
    if value in {"off", "no", "false", "0"}:
        return "off"
    return "auto" if value in {"auto", "all"} else "explicit"


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


def item_title(item: dict) -> str:
    """The short title (at most 60 characters): the module's ``title``, else the start of
    the text, cut at a word."""
    for value in (item.get("title"), item.get("text")):
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if text:
            break
    else:
        return f"Action item #{item.get('id')}"
    if len(text) <= TITLE_MAX:
        return text
    cut = text[:TITLE_MAX - 1]
    if " " in cut and text[TITLE_MAX - 1] != " ":
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-–—") + "…"


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


def _lock(owner: str) -> asyncio.Lock:
    """One state writer per owner (the task and the page's buttons share the file)."""
    return _LOCKS.setdefault(owner or "", asyncio.Lock())


def parse_items_json(result: str) -> list[dict] | None:
    """The module's ``format="json"`` list, or None when it answered with text."""
    try:
        rows = json.loads(result or "")
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            row = {**row, "id": int(row["id"])}
        except (KeyError, TypeError, ValueError):
            continue
        out.append(row)
    return out


def _row_item(row: dict) -> ActionItem:
    """A JSON row as the text parser's ActionItem (for the shared push messages)."""
    return ActionItem(row["id"], str(row.get("status") or "open"), str(row.get("text") or ""),
                      row.get("owner"), row.get("due_date"), row.get("due"),
                      str(row.get("conversation_title") or ""),
                      str(row.get("conversation_at") or ""), str(row.get("link") or ""))


def _zone(name: str | None) -> tzinfo:
    """The item's zone (CONVO_TIMEZONE), else the Odysseus user's zone."""
    if name:
        try:
            return ZoneInfo(str(name))
        except (ValueError, KeyError, OSError):
            logger.warning("Athena timezone %r is unknown; using Odysseus'", name)
    from src.user_time import user_timezone

    return user_timezone()


def _valid_zone(name: str | None) -> str | None:
    if not name:
        return None
    try:
        ZoneInfo(str(name))
    except (ValueError, KeyError, OSError):
        return None
    return str(name)


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


def _aware_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now.astimezone(timezone.utc)  # naive = server local time


def _event_uid(item_id: int, rev: int = 0) -> str:
    base = f"odysseus-sound:item:{server_key()}:{item_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, base + (f":{rev}" if rev else "")))


def _hhmm(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{2}:\d{2}", text) else None


def _ymd(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


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
            "content": f"{text}\n\n{_source_line(item)}"}), owner=owner or None)
        if res.get("error") or not res.get("note_id"):
            raise RuntimeError(f"notes: {res.get('error') or 'no note created'}")
        ref = res["note_id"]
    else:
        raise ValueError("target must be calendar, todo or note")
    return {"kind": str(item.get("kind") or "task"), "target": target, "ref": ref,
            "at": _aware_utc(now_utc).isoformat(timespec="seconds"), "rev": rev,
            "module": "open", "hub": "open"}


# ------------------------------------------------------------------------ notes
_NOTES_WORDS = re.compile(r"^(?:my|the|our)\s+|\s+notes?$", re.IGNORECASE)


def _note_title(item: dict, text: str) -> str:
    """The module's title for the note, else the start of its text."""
    title = re.sub(r"\s+", " ", str(item.get("note_title") or "").strip())
    if title:
        return title[:80]
    words = text.split()
    return " ".join(words[:8]) + (" …" if len(words) > 8 else "")


def notes_key(name: Any) -> str:
    """"My Hardware notes" and "hardware" are the same notes."""
    key = re.sub(r"\s+", " ", str(name or "").strip().lower())
    for _ in range(2):
        key = _NOTES_WORDS.sub("", key).strip()
    return "" if key in {"note", "notes", "none"} else key


def notes_title(name: Any) -> str | None:
    """The title new notes get ("hardware" -> "Hardware notes"); None for no usable name."""
    key = notes_key(name)
    if not key or len(key) > 60:
        return None
    text = re.sub(r"\s+", " ", str(name).strip())
    for _ in range(2):
        text = _NOTES_WORDS.sub("", text).strip()
    return f"{text[:1].upper()}{text[1:]} notes"


def find_notes(owner: str, name: Any):
    """The owner's open note (type note) called ``name``, with or without "notes"."""
    from core.database import Note, SessionLocal

    key = notes_key(name)
    if not key:
        return None
    db = SessionLocal()
    try:
        q = db.query(Note).filter(Note.archived == False,  # noqa: E712
                                  Note.note_type == "note")
        if owner:
            q = q.filter(Note.owner == owner)
        for n in q.order_by(Note.updated_at.desc()).limit(500).all():
            if notes_key(n.title) == key:
                db.expunge(n)
                return n
        return None
    finally:
        db.close()


def _block_index(content: Any, block: str) -> int | None:
    lines = str(content or "").split("\n")
    want = _norm_line(block)
    return next((i for i, ln in enumerate(lines) if _norm_line(ln) == want), None)


async def _add_to_notes(owner: str, item: dict, text: str) -> dict:
    """Add ``item`` as a line to its named notes (made if missing); the entry's ``ref``
    (note id), ``block`` (the line) and ``notes`` (title). The same line isn't added
    twice."""
    from src.tools.notes import do_manage_notes

    name = notes_title(item.get("list_name"))
    block = f"- {text}"
    note = find_notes(owner, name)
    if note is None:
        res = await do_manage_notes(json.dumps({
            "action": "add", "note_type": "note", "title": name, "label": LABEL,
            "content": block}), owner=owner or None)
        if res.get("error") or not res.get("note_id"):
            raise RuntimeError(f"notes: {res.get('error') or 'no note created'}")
        return {"ref": res["note_id"], "block": block, "notes": name}
    if _block_index(note.content, block) is None:
        content = str(note.content or "").rstrip()
        res = await do_manage_notes(json.dumps({
            "action": "update", "id": note.id,
            "content": f"{content}\n{block}" if content else block}), owner=owner or None)
        if res.get("error"):
            raise RuntimeError(f"notes: {res['error']}")
    return {"ref": note.id, "block": block, "notes": (note.title or name).strip()}


async def _remove_block(owner: str, ref: str, block: str) -> None:
    """Take one line out of a note, keeping the rest as it is."""
    from src.tools.notes import do_manage_notes

    note = _note(owner, ref)
    if note is None:
        return
    lines = str(note.content or "").split("\n")
    i = _block_index(note.content, block)
    if i is None:
        return
    lines.pop(i)
    res = await do_manage_notes(json.dumps({"action": "update", "id": ref,
                                            "content": "\n".join(lines)}), owner=owner or None)
    if res.get("error"):
        raise RuntimeError(f"notes: {res['error']}")


# ------------------------------------------------------------------ named to-do lists
LIST_TYPES = ("checklist", "todo")
_LIST_WORDS = re.compile(r"^(?:my|the|our)\s+|\s+(?:to-?do\s+|to\s+do\s+)?(?:list|to-?dos)$",
                         re.IGNORECASE)


def list_key(name: Any) -> str:
    """"My Shopping list" and "shopping" are the same list."""
    key = re.sub(r"\s+", " ", str(name or "").strip().lower())
    for _ in range(2):
        key = _LIST_WORDS.sub("", key).strip()
    return "" if key in {"list", "to-do", "todo", "to do", "to-dos", "todos"} else key


def list_title(name: Any) -> str | None:
    """The title a new list gets ("shopping" -> "Shopping"); None for no usable name."""
    text = re.sub(r"\s+", " ", str(name or "").strip())
    if not list_key(text) or len(text) > 60 or text.lower() == "none":
        return None
    return text[:1].upper() + text[1:]


def _lists(owner: str) -> list:
    """The owner's open checklists and to-do notes, most recently changed first."""
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(Note).filter(Note.archived == False,  # noqa: E712
                                  Note.note_type.in_(LIST_TYPES))
        if owner:
            q = q.filter(Note.owner == owner)
        notes = q.order_by(Note.updated_at.desc()).limit(500).all()
        for n in notes:
            db.expunge(n)
        return notes
    finally:
        db.close()


def find_list(owner: str, name: Any):
    """The to-do list called ``name`` (any case, with or without "list"), or None."""
    key = list_key(name)
    return next((n for n in _lists(owner) if key and list_key(n.title) == key), None)


def todo_lists(owner: str) -> list[str]:
    """Titles of the owner's to-do lists, for the page's picker. A to-do of its own (its
    only line is its title, as filed for an item without a list) isn't a list."""
    out, seen = [], set()
    for n in _lists(owner):
        key, items = list_key(n.title), _note_items(n)
        # a filed dated to-do's title is the short title, its line the full text
        single = len(items) <= 1 and (
            all(_norm_line(i.get("text")) == _norm_line(n.title) for i in items)
            or (n.label == LABEL and len(items) == 1 and (
                bool(n.due_date)
                or _norm_line(n.title) == _norm_line(item_title(items[0])))))
        if key and key not in seen and not single:
            seen.add(key)
            out.append(n.title.strip())
    return sorted(out, key=str.lower)


def _norm_line(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def _raw_items(note) -> list:
    """A list's lines exactly as stored (also lines that aren't objects), so changing one
    line writes the others back unchanged."""
    try:
        items = json.loads(note.items or "[]")
    except (TypeError, ValueError):
        return []
    return items if isinstance(items, list) else []


def _ticked(item: Any) -> bool:
    return isinstance(item, dict) and bool(item.get("done") or item.get("checked"))


def _line_index(items: list, line: str) -> int | None:
    """The line's index: an open one with that text first, else a ticked one."""
    want = _norm_line(line)
    same = [i for i, it in enumerate(items)
            if isinstance(it, dict) and _norm_line(it.get("text")) == want]
    return next((i for i in same if not _ticked(items[i])), same[0] if same else None)


def _line_text(item: dict, text: str) -> str:
    when = _when_label(item)
    return f"{text} (due {when})" if when else text


async def _add_to_list(owner: str, item: dict, text: str) -> dict:
    """Add ``item`` as a line to its named list (made if missing); the entry's
    ``ref`` (note id), ``line`` and ``list`` (title). An open line with the same text
    is reused, not repeated."""
    from src.tools.notes import do_manage_notes

    name = list_title(item.get("list_name"))
    line = _line_text(item, text)
    note = find_list(owner, name)
    if note is None:
        res = await do_manage_notes(json.dumps({
            "action": "add", "note_type": "todo", "title": name, "label": LABEL,
            "checklist_items": [{"text": line, "done": False}]}), owner=owner or None)
        if res.get("error") or not res.get("note_id"):
            raise RuntimeError(f"to-dos: {res.get('error') or 'no list created'}")
        return {"ref": res["note_id"], "line": line, "list": name}
    items = _raw_items(note)
    i = _line_index(items, line)
    if i is None or _ticked(items[i]):
        res = await do_manage_notes(json.dumps({
            "action": "update", "id": note.id,
            "checklist_items": items + [{"text": line, "done": False}]}), owner=owner or None)
        if res.get("error"):
            raise RuntimeError(f"to-dos: {res['error']}")
    return {"ref": note.id, "line": line, "list": (note.title or name).strip()}


async def _change_line(owner: str, ref: str, line: str, *, done: bool | None = None,
                       remove: bool = False) -> bool:
    """Tick / untick or remove one line of a list, keeping the others as they are."""
    from src.tools.notes import do_manage_notes

    note = _note(owner, ref)
    if note is None:
        return False
    items = _raw_items(note)
    i = _line_index(items, line)
    if i is None:
        return False
    if remove:
        items.pop(i)
    else:
        items[i] = {**items[i], "done": bool(done)}
        items[i].pop("checked", None)
    res = await do_manage_notes(json.dumps({"action": "update", "id": ref,
                                            "checklist_items": items}), owner=owner or None)
    if res.get("error"):
        raise RuntimeError(f"to-dos: {res['error']}")
    if remove and not items and list_key(note.title) == list_key(VOICE_LIST):
        await _archive_if_empty(owner, ref)
    return True


async def _archive_if_empty(owner: str, ref: str) -> None:
    """The "Voice notes" list whose last line was taken off is archived (unless pinned);
    the next undated task starts a new one. Finished lists wait for
    src/checklist_auto_archive.py; an empty one would stay forever."""
    from src.checklist_auto_archive import is_empty
    from src.tools.notes import do_manage_notes

    note = _note(owner, ref)
    if note is None or not is_empty(note):
        return
    res = await do_manage_notes(json.dumps({"action": "update", "id": ref, "archived": True}),
                                owner=owner or None)
    if res.get("error"):
        raise RuntimeError(f"to-dos: {res['error']}")


def _note(owner: str, ref: str):
    from core.database import Note, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(Note).filter(Note.id == ref)
        if owner:
            q = q.filter(Note.owner == owner)
        note = q.first()
        if note is not None:
            db.expunge(note)
        return note
    finally:
        db.close()


def _event(owner: str, ref: str):
    from core.database import CalendarCal, CalendarEvent, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(CalendarEvent).join(CalendarCal).filter(CalendarEvent.uid == ref)
        if owner:
            q = q.filter(CalendarCal.owner == owner)
        ev = q.first()
        if ev is not None:
            db.expunge(ev)
        return ev
    finally:
        db.close()


def _note_items(note) -> list[dict]:
    try:
        items = json.loads(note.items or "[]")
    except (TypeError, ValueError):
        return []
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


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


def _when_label(item: dict) -> str:
    day, clock = _ymd(item.get("due_date")), _hhmm(item.get("due_time"))
    if not day:
        return ""
    label = Date.fromisoformat(day).strftime("%a %d %b")
    return f"{label} {clock}" if clock else label


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


def _created_before(row: dict, since: str | None) -> bool:
    try:
        created = datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
        start = datetime.fromisoformat(str(since))
    except (TypeError, ValueError):
        return False
    if created.tzinfo is None or start.tzinfo is None:
        return False
    return created < start


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
    return (note is not None and not note.archived and note.label == LABEL
            and note.note_type == "todo" and len(_note_items(note)) <= 1
            and not note.due_date and (note.repeat or "none") == "none")


async def unfile_auto_todos(owner: str, call: CallTool, *, apply: bool = False,
                            only: set[int] | None = None, include_all: bool = False) -> dict:
    """Clean-up for the to-dos filed one per voice-note item before ``explicit`` filing
    and the "Voice notes" list: filed to-dos of their own (label ``odysseus-sound``, one
    checklist line, no due date, no reminder) of voice-note items that are not explicit
    now. Entries filed in ``explicit`` mode or from the page are left out unless
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
    from src.tool_utils import get_mcp_manager

    manager = get_mcp_manager()
    server_id = module_server_id(manager) if manager is not None else None
    if server_id is None:
        raise TaskNoop(f"module MCP server {server_key()!r} is not connected")
    try:
        stats = await sync(owner, mcp_caller(manager, server_id))
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
