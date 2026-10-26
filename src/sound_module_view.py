"""Fork: data for the Athena page (conversations, "who is this?" cards).

Everything comes from the module's MCP tools (``search_conversations``,
``get_conversation``, ``get_transcript``, ``list_unnamed_speakers``, ``name_speaker``,
``reassign_line``, ``rename_speaker_everywhere``, action items) through Odysseus' MCP manager; Odysseus never
opens the module's database. The tools return compact text for models, so this file
parses their documented line shapes (see docs/fork/modules/odysseus-sound.md). Lines
that don't match are skipped, so a wording change degrades to fewer rows, not an error.
Action items are read as JSON (``list_action_items format="json"``); the text lines are
the fallback for a module that predates it.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

CallTool = Callable[[str, dict], Awaitable[str]]

_CONV = re.compile(
    r"^- (?P<start>\d{4}-\d{2}-\d{2} \d{2}:\d{2})–(?P<end>\d{2}:\d{2}) \"(?P<title>.*)\""
    r"(?P<note> \(voice note\))? id (?P<id>\S+); people: (?P<people>.*)$")
_UNNAMED = re.compile(
    r"^- (?P<label>.+?) in \"(?P<title>.*)\" (?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) "
    r"\(conversation id (?P<conv>[^)]+)\), (?P<lines>\d+) lines"
    r"(?:; sounds like (?P<guess>.+) \((?P<score>[0-9.]+)\))?$")
_ITEM = re.compile(
    r"^- #(?P<id>\d+) \[(?P<status>open|done|dismissed)\] (?P<rest>.*) — from "
    r"\"(?P<title>.*)\"(?P<note> \(voice note\))? (?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (?P<link>\S+)$")
_KIND_WORD = re.compile(r"^(?P<kind>event|reminder): ")
_UNCLEAR = " [time unclear]"
# "2026-10-21 12:00 (Wednesday at 12 pm)", "2026-10-23 (Friday)", "2026-10-23"
_DUE_PARTS = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?: (?P<time>\d{2}:\d{2}))?(?: \((?P<said>.*)\))?$")
KINDS = ("task", "event", "reminder")
# an item may also be a note ("create a note ..."), filed into Odysseus notes; it can't
# be moved to that kind
ITEM_KINDS = (*KINDS, "note")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
# the last " due ": the item text itself may say "due" ("Confirm the due date")
_DUE = re.compile(r" due (?P<due>(?:(?! due ).)+)$")
_OWNER = re.compile(r" \(owner (?P<owner>[^)]*)\)$")
_QUOTE = re.compile(r"^#(?P<id>\d+) \"(?P<text>.*)\" (?P<link>https?://\S+)$")
# get_conversation
_HEAD = re.compile(r"^\"(?P<title>.*)\"(?: \(voice note\))? — (?P<when>.+)$")
_SPEAKERS = re.compile(r"^Speakers: (?P<speakers>.*)\. People: (?P<people>.*)$")
_CONV_ITEM = re.compile(
    r"^Action #(?P<id>\d+) \[(?P<status>open|done|dismissed)\]: (?P<rest>.*) "
    r"(?P<link>https?://\S+)$")
_FACT = re.compile(r"^Fact \[(?P<subject>[^\]]*)\]: (?P<fact>.*) (?P<link>https?://\S+)$")
# get_transcript
_T_HEAD = re.compile(r"^\"(?P<title>.*)\" (?P<when>\S+(?: \S+)?) (?P<link>https?://\S+)$")
# "[8s 17:00:08]" (older modules) or "[8s]": the clock time is then the header's start
# (with seconds) plus the offset
_T_LINE = re.compile(
    r"^#(?P<id>\d+) \[(?P<offset>\d+)s(?: (?P<time>\S+))?\] (?P<speaker>.+?): (?P<text>.*)$")
_T_MORE = re.compile(r"^… \d+ more lines")
_CONV_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


@dataclass
class Conversation:
    id: str
    start: str
    end: str
    title: str
    people: list[str]
    summary: str = ""
    link: str = ""
    voice_note: bool = False


@dataclass
class Quote:
    line_id: int
    text: str
    link: str


@dataclass
class UnnamedSpeaker:
    label: str
    conversation_id: str
    conversation_title: str
    when: str
    lines: int
    guess: str | None = None
    guess_score: float | None = None
    quotes: list[Quote] = field(default_factory=list)


@dataclass
class ActionItem:
    id: int
    status: str
    text: str
    owner: str | None
    due: str | None  # "2026-10-23 (Friday)" or as said (text); as said (JSON)
    conversation_title: str
    when: str  # "YYYY-MM-DD HH:MM" of the conversation
    link: str
    kind: str = "task"  # task | event | reminder | note
    due_date: str | None = None  # YYYY-MM-DD, local to ``timezone``
    due_time: str | None = None  # HH:MM 24 h
    time_unclear: bool = False
    voice_note: bool = False
    conversation_id: str | None = None
    duration_minutes: int | None = None
    location: str | None = None
    timezone: str | None = None
    created_at: str | None = None
    list_name: str | None = None  # the to-do list (or, for a note, the notes) named
    note_title: str | None = None  # a note's title
    filed: dict | None = None  # {"target", "ref", "open_url", "list"?, "notes"?}


@dataclass
class ConversationItem:
    id: int
    status: str
    text: str
    owner: str | None
    due: str | None
    link: str


@dataclass
class Fact:
    subject: str
    fact: str
    link: str


@dataclass
class ConversationDetail:
    id: str
    title: str = ""
    when: str = ""  # "2026-10-14 17:00–17:12 (Europe/Berlin), device omi"
    speakers: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    link: str = ""
    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    action_items: list[ConversationItem] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)


@dataclass
class TranscriptLine:
    id: int
    offset: int
    time: str
    speaker: str
    text: str
    link: str


@dataclass
class Transcript:
    title: str = ""
    link: str = ""
    lines: list[TranscriptLine] = field(default_factory=list)
    more: bool = False
    next_start: int | None = None


def _names(text: str) -> list[str]:
    text = text.strip()
    return [] if text in {"", "—"} else [p.strip() for p in text.split(",") if p.strip()]


def _split_item(rest: str) -> tuple[str, str | None, str | None]:
    """"text (owner X) due D" -> (text, owner, due); see parse_action_items."""
    due = owner = None
    if d := _DUE.search(rest):
        due, rest = d["due"], rest[:d.start()]
    if o := _OWNER.search(rest):
        owner, rest = o["owner"], rest[:o.start()]
    return rest.strip(), owner, due


def parse_action_items(text: str) -> list[ActionItem]:
    """``list_action_items`` text lines (the fallback for modules without JSON)."""
    out = []
    for raw in (text or "").splitlines():
        m = _ITEM.match(raw.strip())
        if not m:
            continue
        rest, kind = m["rest"], "task"
        if k := _KIND_WORD.match(rest):
            kind, rest = k["kind"], rest[k.end():]
        unclear = rest.endswith(_UNCLEAR)
        if unclear:
            rest = rest[:-len(_UNCLEAR)]
        rest, owner, due = _split_item(rest)
        item = ActionItem(int(m["id"]), m["status"], rest, owner, due, m["title"], m["when"],
                          m["link"], kind=kind, time_unclear=unclear,
                          voice_note=bool(m["note"]))
        if due and (d := _DUE_PARTS.match(due)):
            item.due_date, item.due_time = d["date"], d["time"]
        out.append(item)
    return out


def _text(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _when(iso: Any, zone: Any = None) -> str:
    """ISO-8601 -> "YYYY-MM-DD HH:MM" in the module's timezone ``zone`` (as the text
    lines show it); "" when unparsable."""
    try:
        when = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return ""
    if when.tzinfo is not None and zone:
        try:
            from zoneinfo import ZoneInfo

            when = when.astimezone(ZoneInfo(str(zone)))
        except (KeyError, ValueError):
            pass
    return when.strftime("%Y-%m-%d %H:%M")


def item_from_json(row: dict) -> ActionItem | None:
    """One ``list_action_items format="json"`` row; None when it has no usable id."""
    try:
        item_id = int(row["id"])
    except (KeyError, TypeError, ValueError):
        return None
    kind = row.get("kind") if row.get("kind") in ITEM_KINDS else "task"
    status = row.get("status") if row.get("status") in {"open", "done", "dismissed"} else "open"
    due_date = _text(row.get("due_date"))
    due_time = _text(row.get("due_time"))
    try:
        duration = int(row["duration_minutes"]) if row.get("duration_minutes") else None
    except (TypeError, ValueError):
        duration = None
    return ActionItem(
        item_id, status, str(row.get("text") or ""), _text(row.get("owner")),
        _text(row.get("due")), str(row.get("conversation_title") or ""),
        _when(row.get("conversation_at"), row.get("timezone")), str(row.get("link") or ""), kind=kind,
        due_date=due_date if due_date and _DATE.match(due_date) else None,
        due_time=due_time if due_time and _TIME.match(due_time) else None,
        time_unclear=bool(row.get("time_unclear")),
        voice_note=row.get("conversation_kind") == "note",
        conversation_id=_text(row.get("conversation_id")), duration_minutes=duration,
        location=_text(row.get("location")), timezone=_text(row.get("timezone")),
        created_at=_text(row.get("created_at")), list_name=_text(row.get("list_name")),
        note_title=_text(row.get("note_title")))


def parse_action_items_result(text: str) -> list[ActionItem]:
    """JSON array from a current module, else the text lines of an older one."""
    try:
        rows = json.loads(text or "")
    except ValueError:
        return parse_action_items(text)
    if not isinstance(rows, list):
        return parse_action_items(text)
    # ``mine`` is false when the module says someone else owns the item
    return [i for r in rows if isinstance(r, dict) and r.get("mine") is not False
            and (i := item_from_json(r))]


def open_url(target: str, ref: Any) -> str | None:
    """Where Odysseus shows a filed item, in its own chat-link conventions."""
    ref = str(ref or "")
    if not ref or not re.match(r"^[A-Za-z0-9_.:@-]{1,200}$", ref):
        return None
    if target == "calendar":
        return f"/#event-{ref}"
    if target in ("todo", "note"):
        return f"/#open=notes&note={ref}"
    return None


def filed_for(filed: dict | None, item_id: int) -> dict | None:
    """``{"target", "ref", "open_url"}`` from module_action_items.filed_state, or None.

    Keys may be int or str (JSON state); an entry marked undone/removed isn't "filed"."""
    filed = filed or {}
    entry = filed.get(item_id, filed.get(str(item_id)))
    if (not isinstance(entry, dict) or entry.get("target") not in {"calendar", "todo", "note"}
            or entry.get("undone") or entry.get("removed")):
        return None
    target = entry["target"]
    out = {"target": target, "ref": entry.get("ref"),
           "open_url": entry.get("open_url") or open_url(target, entry.get("ref"))}
    if entry.get("list"):
        out["list"] = str(entry["list"])
    if entry.get("notes"):
        out["notes"] = str(entry["notes"])
    return out


def merge_filed(items: list[ActionItem], filed: dict | None) -> list[ActionItem]:
    for item in items:
        item.filed = filed_for(filed, item.id)
    return items


def parse_conversations(text: str) -> list[Conversation]:
    out: list[Conversation] = []
    for raw in (text or "").splitlines():
        m = _CONV.match(raw)
        if m:
            people = [] if m["people"].strip() in {"", "—"} else [
                p.strip() for p in m["people"].split(",") if p.strip()]
            out.append(Conversation(m["id"], m["start"], m["end"], m["title"], people,
                                    voice_note=bool(m["note"])))
            continue
        if not out or not raw.startswith("  "):
            continue
        line = raw.strip()
        if line.startswith(("http://", "https://")) and not out[-1].link:
            out[-1].link = line
        elif line and not out[-1].summary and not out[-1].link:
            out[-1].summary = line
    return out


def parse_unnamed(text: str) -> list[UnnamedSpeaker]:
    out: list[UnnamedSpeaker] = []
    for raw in (text or "").splitlines():
        m = _UNNAMED.match(raw)
        if m:
            out.append(UnnamedSpeaker(
                m["label"], m["conv"], m["title"], m["when"], int(m["lines"]),
                m["guess"], float(m["score"]) if m["score"] else None))
            continue
        q = _QUOTE.match(raw.strip())
        if q and out and raw.startswith("  "):
            out[-1].quotes.append(Quote(int(q["id"]), q["text"], q["link"]))
    return out


def parse_conversation(text: str, conversation_id: str) -> ConversationDetail:
    """``get_conversation`` output; unknown lines are skipped."""
    c = ConversationDetail(conversation_id)
    in_summary = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not c.title and (m := _HEAD.match(line)):
            c.title, c.when = m["title"], m["when"]
        elif m := _SPEAKERS.match(line):
            c.speakers, c.people = _names(m["speakers"]), _names(m["people"])
        elif line.startswith("Link: ") and not c.link:
            c.link = line[len("Link: "):].strip()
        elif line.startswith("Summary: ") and not c.summary:
            c.summary, in_summary = line[len("Summary: "):].strip(), True
            continue
        elif line.startswith("* "):
            c.key_points.append(line[2:].strip())
        elif m := _CONV_ITEM.match(line):
            rest, owner, due = _split_item(m["rest"])
            c.action_items.append(ConversationItem(int(m["id"]), m["status"], rest, owner,
                                                   due, m["link"]))
        elif m := _FACT.match(line):
            c.facts.append(Fact(m["subject"], m["fact"], m["link"]))
        elif in_summary and line and not line.startswith("When answering"):
            c.summary += "\n" + line  # a summary that spans lines
            continue
        in_summary = False
    return c


def parse_transcript(text: str) -> Transcript:
    """``get_transcript`` output; each line links to its moment in the module's view."""
    t = Transcript()
    start: datetime | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if m := _T_LINE.match(line):
            off = int(m["offset"])
            link = (t.link.split("#", 1)[0] + f"#t={off}") if t.link else ""
            clock = m["time"] or (
                (start + timedelta(seconds=off)).strftime("%H:%M:%S") if start else "")
            t.lines.append(TranscriptLine(int(m["id"]), off, clock, m["speaker"],
                                          m["text"], link))
        elif not t.title and not t.lines and (m := _T_HEAD.match(line)):
            t.title, t.link = m["title"], m["link"]
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    start = datetime.strptime(m["when"], fmt)
                    break
                except ValueError:
                    pass
        elif _T_MORE.match(line):
            t.more = True
    if t.more and t.lines:
        t.next_start = t.lines[-1].offset
    else:
        t.more = False  # nothing to continue from
    return t


def as_dicts(rows: list[Any]) -> list[dict]:
    return [asdict(r) for r in rows]


def server_id(manager: Any, key: str) -> str | None:
    """Id of the connected MCP server called ``key`` (name or id)."""
    key = key.casefold()
    for sid, conn in (getattr(manager, "_connections", {}) or {}).items():
        conn = conn or {}
        if conn.get("status") != "connected":
            continue
        if str(sid).casefold() == key or str(conn.get("name") or "").strip().casefold() == key:
            return sid
    return None


async def conversations(call: CallTool, *, query: str = "", person: str = "",
                        since: str = "", until: str = "", kind: str = "",
                        limit: int = 30) -> list[dict]:
    args: dict[str, Any] = {"query": query, "person": person, "since": since,
                            "until": until, "limit": max(1, min(int(limit), 50))}
    if kind in ("note", "conversation"):  # Athena >= kind filter; "" = both
        args["kind"] = kind
    text = await call("search_conversations", args)
    return as_dicts(parse_conversations(text))


async def unnamed_speakers(call: CallTool, limit: int = 10) -> list[dict]:
    text = await call("list_unnamed_speakers", {"limit": max(1, min(int(limit), 50))})
    return as_dicts(parse_unnamed(text))


def check_conversation_id(conversation_id: str) -> str:
    if not _CONV_ID.match(conversation_id or ""):
        raise ValueError("conversation id must be 1-200 characters of A-Z a-z 0-9 _ . : -")
    return conversation_id


def check_name(name: str, what: str = "name") -> str:
    name = (name or "").strip()
    if not name or len(name) > 100:
        raise ValueError(f"{what} must be 1-100 characters")
    return name


def _missing(text: str) -> bool:
    return (text or "").startswith("No conversation")


async def conversation(call: CallTool, conversation_id: str) -> dict:
    """Raises LookupError when the module doesn't know the id."""
    conversation_id = check_conversation_id(conversation_id)
    text = await call("get_conversation", {"conversation_id": conversation_id})
    if _missing(text):
        raise LookupError(text.strip())
    return asdict(parse_conversation(text, conversation_id))


async def transcript(call: CallTool, conversation_id: str, start_seconds: int = 0) -> dict:
    """Up to 200 lines from ``start_seconds``; raises LookupError for an unknown id."""
    conversation_id = check_conversation_id(conversation_id)
    text = await call("get_transcript", {"conversation_id": conversation_id,
                                         "start_seconds": max(0, int(start_seconds)),
                                         "max_lines": 200})
    if _missing(text):
        raise LookupError(text.strip())
    return asdict(parse_transcript(text))


async def reassign_line(call: CallTool, line_id: int, name: str) -> dict:
    if int(line_id) < 1:
        raise ValueError("line id must be positive")
    message = await call("reassign_line", {"line_id": int(line_id), "name": check_name(name)})
    ok = message.startswith("Line #") and ("moved" in message or "is already" in message)
    return {"ok": ok, "message": message}


async def rename_speaker(call: CallTool, old_name: str, new_name: str) -> dict:
    """The module's rename_speaker_everywhere: a merge if ``new_name`` exists already."""
    message = await call("rename_speaker_everywhere", {
        "old_name": check_name(old_name, "old_name"),
        "new_name": check_name(new_name, "new_name")})
    return {"ok": message.startswith("Renamed"), "message": message}


async def name_speaker(call: CallTool, conversation_id: str, speaker_label: str,
                       name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 100:
        raise ValueError("name must be 1-100 characters")
    return await call("name_speaker", {"conversation_id": conversation_id,
                                       "speaker_label": speaker_label, "name": name,
                                       "remember_voice": True})


async def action_items(call: CallTool, status: str = "open",
                       filed: dict | None = None) -> list[dict]:
    """The module's items (JSON, or text from an older module) with where each was filed."""
    if status not in {"open", "done", "dismissed", "all"}:
        raise ValueError("status must be open, done, dismissed or all")
    text = await call("list_action_items", {"status": status, "format": "json"})
    return as_dicts(merge_filed(parse_action_items_result(text), filed))


async def find_item(call: CallTool, item_id: int, status: str = "all") -> dict:
    """One item as the module lists it (raw JSON row when available); LookupError if none."""
    text = await call("list_action_items", {"status": status, "format": "json"})
    try:
        rows = json.loads(text or "")
    except ValueError:
        rows = None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and str(row.get("id")) == str(item_id):
                return row
    else:
        for item in parse_action_items(text):
            if item.id == item_id:
                return asdict(item)
    raise LookupError(f"No action item #{item_id}.")


def check_item_id(item_id: int) -> int:
    if int(item_id) < 1:
        raise ValueError("item id must be positive")
    return int(item_id)


def check_target(target: str) -> str:
    if target not in {"calendar", "todo", "note"}:
        raise ValueError("target must be calendar, todo or note")
    return target


def check_list_name(list_name: str | None) -> str | None:
    """A to-do list name for move: None = unchanged, "none" = no list (a to-do of its own)."""
    name = " ".join((list_name or "").split())
    if not name:
        return None
    if name.lower() == "none":
        return "none"
    if len(name) > 40 or any(not ch.isprintable() for ch in name):
        raise ValueError("list_name must be a short list name (up to 40 characters)")
    return name


def check_move(kind: str, due_date: str | None, due_time: str | None
               ) -> tuple[str, str | None, str | None]:
    """kind task/event/reminder; date YYYY-MM-DD, time HH:MM; "" = unchanged, "none" = clear."""
    if kind not in KINDS:
        raise ValueError("kind must be task, event or reminder")
    due_date = (due_date or "").strip() or None
    due_time = (due_time or "").strip() or None
    if due_date and due_date != "none":
        try:
            if not _DATE.match(due_date):
                raise ValueError
            date.fromisoformat(due_date)
        except ValueError:
            raise ValueError("due_date must be YYYY-MM-DD") from None
    if due_time and due_time != "none" and not _TIME.match(due_time):
        raise ValueError("due_time must be HH:MM (24 h)")
    return kind, due_date, due_time


async def set_action_item(call: CallTool, item_id: int, status: str) -> str:
    """The module stays the source of truth: this is its own update_action_item."""
    if status not in {"open", "done", "dismissed"}:
        raise ValueError("status must be open, done or dismissed")
    return await call("update_action_item", {"item_id": int(item_id), "status": status})
