"""Fork: the Athena action-items job's names, settings, state and rows (``fork/athena/action_items.py``).

The values the job stores in the owner's data are defined here and nowhere else: the
task's action id (``ACTION``), the label on filed notes (``LABEL``), the state file's
name (``_state_path``) and the input of a filed calendar event's uid (``_event_uid``).
Also the job's settings, its state file, Athena's rows (the text list and the JSON rows)
and the item fields every part reads (dates, times, zones, the short title).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fork.athena import client as athena
from fork.common import settings

logger = logging.getLogger("fork.athena.action_items")  # the job's one logger


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


# One line of Athena's list_action_items result (the title may be followed by
# "(voice note)" or "(written by hand: <hub link>)"), e.g.
# - #12 [open] Send Alice the spreadsheet (owner Me) due 2026-09-19 (Friday) — from
#   "Q4 budget" 2026-09-15 10:00 http://host:8000/view/abc?sig=...#t=40
_LINE = re.compile(
    r"^- #(?P<id>\d+) \[(?P<status>open|done|dismissed)\] (?P<rest>.*) — from "
    r"\"(?P<title>.*)\"(?: \(voice note\)| \(written by hand: https?://[^\s)]+\))? "
    r"(?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (?P<link>\S+)$")
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
# Read from the one settings table (``MODULE_<name>``, else the former ``BARVIS_<name>``).
def server_key() -> str:
    return settings.athena_server()


def notify_channel() -> str:
    return settings.notify_channel()


def digest_hour() -> int | None:
    return settings.digest_hour()


# -------------------------------------------------------------------------- state
def _state_path(owner: str) -> Path:
    from fork.common.state import owner_file

    return owner_file("barvis_action_items", owner)


def load_state(owner: str) -> dict[str, Any]:
    from fork.common.state import read_json

    state = read_json(_state_path(owner), {})
    state.setdefault("items", {})
    return state


def save_state(owner: str, state: dict[str, Any]) -> None:
    from fork.common import state as files

    files.write_json(_state_path(owner), state)


# -------------------------------------------------------------------------- MCP
# Athena's tools through the one client (fork/athena/client.py); a failed tool raises
# AthenaError, a RuntimeError.
CallTool = athena.CallTool


def module_server_id(manager: Any, key: str | None = None) -> str | None:
    """Id of the connected module MCP server named ``key`` (name or id)."""
    return athena.server_id(manager, key or server_key())


def mcp_caller(manager: Any, server_id: str) -> CallTool:
    return athena.bound_caller(manager, server_id)


FILE_MODES = ("explicit", "auto", "off")
VOICE_LIST = "Voice notes"  # the one to-do list for undated voice-note tasks
TITLE_MAX = 60


def file_mode() -> str:
    """``MODULE_FILE_ITEMS``: ``explicit`` (default), ``auto`` (``all``) or ``off``."""
    return settings.file_items()


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


def _when_label(item: dict) -> str:
    day, clock = _ymd(item.get("due_date")), _hhmm(item.get("due_time"))
    if not day:
        return ""
    label = Date.fromisoformat(day).strftime("%a %d %b")
    return f"{label} {clock}" if clock else label
