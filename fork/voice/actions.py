"""Fork: voice actions, the two writes a voice turn may make
(docs/fork/voice-assistant.md, "Voice actions").

A voice turn is read-only (``fork/brain/turn.py``, ``read_only_turn``) except for the two
tools here, offered only while the owner setting "Voice can add and complete items" is on
(:func:`enabled`):

* ``voice_add`` makes a to-do, reminder, calendar event, note, or a line on a named list
  or in named notes. It builds the item dict Athena's local model makes from a voice note
  and files it with the same code (``fork/athena/action_items_filing.py``,
  ``create_item``), so a spoken sentence and the same sentence in a voice note give the
  same result: titles, lists, the "Voice notes" list, due times, the 30-minute event
  reminder. A voice item isn't an Athena item: its calendar uid starts with ``voice:``
  (never one of Athena's ``odysseus-sound:item:convo:<id>`` uids), its notes carry the
  label ``voice``, and it is not written into the action-items state file.
* ``voice_complete`` marks one open item done: an unticked line of the owner's to-dos and
  lists, or an open Athena action item (``update_action_item``, status done). One clear
  match is marked; several change nothing and ask which one; none says so.

Nothing else is allowed: every other write stays refused (``fork/brain/tools.py``,
``read_only_refusal``). Each call must be grounded in what was said (:func:`grounded`):
the item's words, or the description, must come from the spoken request, so text in a
transcript or note the turn has read can't add or tick anything. A clarification's
options (kept for :data:`CLARIFY_SECONDS`) count as said for the next turn of that chat.

Each action is pushed to the owner's phone through the action-items job's reminder
channel (``fork/athena/action_items_push.py``) while :func:`push_enabled`.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

ADD = "voice_add"
COMPLETE = "voice_complete"
NAMES = frozenset({ADD, COMPLETE})
SETTING_KEY = "fork_voice_actions"
PUSH_KEY = "fork_voice_action_push"
LABEL = "voice"  # the label of notes voice makes (Athena's are "athena")
UID_PREFIX = "voice:"
SOURCE = "Added by voice"
KINDS = ("task", "reminder", "event", "note")
MAX_OPTIONS = 3
CLARIFY_SECONDS = 5 * 60
REPEAT_SECONDS = 120  # the same add again this soon is the same add (a retry)

ADD_SCHEMA = {
    "type": "function",
    "function": {
        "name": ADD,
        "description": (
            "Add something the owner asked for by voice: a to-do, a reminder, a calendar "
            "event, a note, or a line on a named list or in named notes. Call it for "
            "\"remind me to…\", \"add … to my … list\", \"make a note…\", \"put … on my "
            "calendar\". It acts at once; then say its result in one sentence."),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(KINDS),
                         "description": "task (a to-do or a list line), reminder, event "
                                        "(calendar), or note."},
                "text": {"type": "string",
                         "description": "What to do or note, in the owner's words, "
                                        "without the date, time or list "
                                        "(\"call the dentist\", \"milk\")."},
                "title": {"type": "string", "description": "Optional short title."},
                "due_date": {"type": "string",
                             "description": "YYYY-MM-DD, or today, tomorrow, or a weekday."},
                "due_time": {"type": "string",
                             "description": "HH:MM (24-hour), or \"9am\", \"noon\"."},
                "list_name": {"type": "string",
                              "description": "A named to-do list (\"shopping\") for a task, "
                                             "or named notes (\"hardware\") for a note."},
                "note_title": {"type": "string",
                               "description": "Optional title of a new note."},
            },
            "required": ["kind", "text"],
        },
    },
}
COMPLETE_SCHEMA = {
    "type": "function",
    "function": {
        "name": COMPLETE,
        "description": (
            "Mark one of the owner's open to-dos, list lines or Athena action items done, "
            "found by a description in the owner's words (\"the revised spreadsheet\"). "
            "Call it directly for \"mark … done\", \"I've done …\", \"tick off …\"; it "
            "searches by itself. It acts at once, or asks which one when several match."),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string",
                                "description": "The item, in the owner's words."},
            },
            "required": ["description"],
        },
    },
}
SCHEMAS = (ADD_SCHEMA, COMPLETE_SCHEMA)


# ------------------------------------------------------------------------ settings
def _setting(key: str) -> bool:
    try:
        from src.settings import get_setting
        return get_setting(key, True) is not False
    except Exception:  # noqa: BLE001 -- unreadable: the default
        return True


def enabled() -> bool:
    """"Voice can add and complete items" (default on)."""
    return _setting(SETTING_KEY)


def push_enabled() -> bool:
    """"Push voice actions to my phone" (default on)."""
    return _setting(PUSH_KEY)


def settings() -> dict:
    return {"actions": enabled(), "push": push_enabled()}


def save(*, actions: bool | None = None, push: bool | None = None) -> dict:
    from src.settings import load_settings, save_settings

    data = dict(load_settings())
    if actions is not None:
        data[SETTING_KEY] = bool(actions)
    if push is not None:
        data[PUSH_KEY] = bool(push)
    save_settings(data)
    return settings()


# ------------------------------------------------------------------------- words
_STOP = frozenset("""a an the my our your me i to of on in at for by with and or it its is
are be this that these those please can could would you mark marked as done complete
completed finish finished tick ticked off item items todo todos to-do to-dos task tasks
action reminder list one thing""".split())
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def _stem(word: str) -> str:
    word = word.replace("'s", "")
    for end in ("ing", "ed", "es", "s"):
        if len(word) > len(end) + 2 and word.endswith(end):
            return word[:-len(end)]
    return word


def words(text: Any) -> set[str]:
    """The content words of ``text``, lowercased and roughly stemmed."""
    return {_stem(w) for w in _WORD.findall(str(text or "").lower()) if w not in _STOP}


def _norm(text: Any) -> str:
    return " ".join(_WORD.findall(str(text or "").lower()))


def grounded(text: Any, said: str, *, share: float = 0.5) -> bool:
    """At least ``share`` of ``text``'s content words were said (and one at least)."""
    want = words(text)
    if not want:
        return False
    return len(want & words(said)) >= max(1, share * len(want))


# --------------------------------------------------------------------- dates, times
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _today(now: datetime | None = None) -> datetime:
    from src.user_time import user_timezone

    zone = user_timezone()
    return now.astimezone(zone) if now is not None else datetime.now(zone)


def parse_date(value: Any, now: datetime) -> str | None:
    """YYYY-MM-DD, "today", "tomorrow", or a weekday (the next one; today's is a week on
    only when said "next"). None for anything else."""
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if not text or text in {"none", "null"}:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    day = now.date()
    text = re.sub(r"^(?:on|this|coming)\s+", "", text)
    if text == "today" or text == "tonight":
        return day.isoformat()
    if text == "tomorrow":
        return (day + timedelta(days=1)).isoformat()
    later = text.startswith("next ")
    name = text.removeprefix("next ").strip()
    if name in _DAYS:
        ahead = (_DAYS.index(name) - day.weekday()) % 7
        if ahead == 0 and later:
            ahead = 7
        return (day + timedelta(days=ahead)).isoformat()
    return None


def parse_time(value: Any) -> str | None:
    """HH:MM from "09:00", "9", "9am", "9:30 pm", "21:15", "noon", "midnight"."""
    text = re.sub(r"[\s.]+", "", str(value or "").strip().lower())
    if not text or text in {"none", "null"}:
        return None
    if text == "noon" or text == "midday":
        return "12:00"
    if text == "midnight":
        return "00:00"
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(am|pm)?", text)
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def spoken_when(day: str | None, clock: str | None, now: datetime) -> str:
    """"tomorrow at 9 a.m.", "Friday at noon", "on 14 November": for the reply."""
    out = ""
    if day:
        d = date.fromisoformat(day)
        ahead = (d - now.date()).days
        if ahead == 0:
            out = "today"
        elif ahead == 1:
            out = "tomorrow"
        elif 1 < ahead < 7:
            out = _DAYS[d.weekday()].capitalize()
        else:
            out = f"on {d.day} {d.strftime('%B')}"
    if clock:
        hour, minute = (int(p) for p in clock.split(":"))
        if (hour, minute) == (12, 0):
            at = "noon"
        elif (hour, minute) == (0, 0):
            at = "midnight"
        else:
            half = "a.m." if hour < 12 else "p.m."
            h = hour % 12 or 12
            at = f"{h} {half}" if not minute else f"{h}:{minute:02d} {half}"
        out = f"{out} at {at}".strip()
    return out


# -------------------------------------------------------------------------- adding
def build_item(args: dict, now: datetime) -> dict:
    """The item dict Athena's model makes from a voice note, from ``voice_add``'s
    arguments. A time without a date is today's, or tomorrow's once it has passed."""
    kind = str(args.get("kind") or "task").strip().lower()
    kind = {"todo": "task", "to-do": "task", "calendar": "event"}.get(kind, kind)
    if kind not in KINDS:
        kind = "task"
    text = re.sub(r"\s+", " ", str(args.get("text") or "")).strip()
    day, clock = parse_date(args.get("due_date"), now), parse_time(args.get("due_time"))
    if clock and not day:
        passed = clock <= now.strftime("%H:%M")
        day = (now.date() + timedelta(days=1 if passed else 0)).isoformat()
    item = {"id": None, "kind": kind, "text": text, "due_date": day, "due_time": clock,
            "conversation_kind": "note", "explicit": True, "status": "open"}
    for key in ("title", "list_name", "note_title"):
        value = re.sub(r"\s+", " ", str(args.get(key) or "")).strip()
        if value and value.lower() not in {"none", "null"}:
            item[key] = value
    return item


def _sentence(item: dict, entry: dict, now: datetime) -> str:
    from fork.athena.action_items_lists import list_key
    from fork.athena.action_items_state import VOICE_LIST, item_title

    when = spoken_when(item.get("due_date"), item.get("due_time"), now)
    due = f", due {when}" if when else ""
    if entry["target"] == "calendar":
        return f"Put '{item['text']}' on your calendar for {when.removeprefix('on ')}." \
            .replace("..", ".")
    if "line" in entry:
        voice_list = list_key(entry.get("list")) == list_key(VOICE_LIST)
        return (f"Added '{item_title(item) if voice_list else item['text']}' to your "
                f"{entry['list']} list{due}.").replace("..", ".")
    if "block" in entry:
        return f"Added '{item['text']}' to your {entry['notes']}."
    if entry["target"] == "note":
        return f"Made a note: '{item_title(item)}'."
    return f"Added '{item_title(item)}' to your to-dos{due}.".replace("..", ".")


_RECENT: dict[tuple[str, str], tuple[float, str]] = {}
_LOCK = threading.Lock()


async def add(owner: str, args: dict, *, said: str, now: datetime | None = None) -> dict:
    """File one spoken item; the tool result (``output`` is the sentence to say)."""
    from fork.athena.action_items_filing import create_item, default_target

    now = _today(now)
    item = build_item(args, now)
    if not item["text"]:
        return _failed("Say what to add.")
    if not grounded(item["text"], said) or (
            item.get("list_name") and not grounded(item["list_name"], said)):
        return _failed("Not run: the item's words must be the ones the owner just said.")
    target = default_target(item)
    key = (owner, json.dumps([target, _norm(item["text"]), item.get("due_date"),
                              item.get("due_time"), _norm(item.get("list_name"))]))
    with _LOCK:
        recent = _RECENT.get(key)
    if recent and time.time() - recent[0] < REPEAT_SECONDS:
        return _done(recent[1])  # the model, or Home Assistant, sent it again
    try:
        entry = await create_item(
            owner, item, target, source=SOURCE, label=LABEL, now_utc=now,
            event_uid=UID_PREFIX + uuid.uuid4().hex if target == "calendar" else None)
    except (RuntimeError, ValueError) as exc:
        logger.warning("voice: could not add an item: %s", exc)
        return _failed(f"Not added: {exc}")
    sentence = _sentence(item, entry, now)
    with _LOCK:
        _RECENT[key] = (time.time(), sentence)
        for k in [k for k, (t, _) in _RECENT.items() if time.time() - t > REPEAT_SECONDS]:
            _RECENT.pop(k, None)
    await notify(owner, sentence)
    return _done(sentence, entry=entry)


# ---------------------------------------------------------------------- completing
@dataclass
class Candidate:
    text: str
    store: str  # "hub" (a to-do line) or "athena" (an action item)
    ref: str = ""  # hub: the note id
    line: str = ""  # hub: the line's text as stored
    list: str = ""  # hub: the list's title
    item_id: int | None = None  # athena, or the Athena item a hub line was filed from

    def label(self) -> str:
        return f"'{self.text}' on {self.list}" if self.list else f"'{self.text}'"


def hub_candidates(owner: str) -> list[Candidate]:
    """Every unticked line of the owner's open to-dos and lists."""
    from fork.athena.action_items_lists import _lists, _note_items, _ticked

    out = []
    for note in _lists(owner):
        items = _note_items(note)
        title = (note.title or "").strip()
        for it in items:
            text = str(it.get("text") or "").strip()
            if text and not _ticked(it):
                # a to-do of its own is titled with its line (or its start): no list
                own = _norm(text).startswith(_norm(title.rstrip("…")))
                out.append(Candidate(text=text, store="hub", ref=note.id, line=text,
                                     list="" if own else title))
    return out


def _filed_lines(owner: str) -> dict[tuple[str, str], int]:
    """(note id, line or "") -> the Athena item id filed there (the job's state)."""
    from fork.athena.action_items_state import load_state

    out = {}
    try:
        filed = load_state(owner).get("filed") or {}
    except Exception:  # noqa: BLE001 -- no state: no links
        return {}
    for key, entry in filed.items():
        if not isinstance(entry, dict) or entry.get("undone") or entry.get("target") != "todo":
            continue
        try:
            out[(str(entry.get("ref")), str(entry.get("line") or ""))] = int(key)
        except (TypeError, ValueError):
            continue
    return out


async def athena_candidates(call: Any) -> list[Candidate]:
    from fork.athena.action_items_state import parse_items_json

    if call is None:
        return []
    try:
        rows = parse_items_json(await call("list_action_items",
                                           {"status": "open", "format": "json"})) or []
    except Exception:  # noqa: BLE001 -- Athena away: the hub's to-dos only
        logger.warning("voice: Athena's action items unavailable", exc_info=True)
        return []
    return [Candidate(text=str(r.get("text") or "").strip(), store="athena", item_id=r["id"])
            for r in rows if str(r.get("status") or "open") == "open"
            and str(r.get("text") or "").strip()]


def _score(want: set[str], cand: Candidate) -> float:
    have = words(cand.text) | words(cand.list)
    return len(want & have) / len(want) if want else 0.0


def match(description: str, candidates: list[Candidate]) -> list[Candidate]:
    """The candidates ``description`` means: an exact text first; else those with the
    best share of its words (at least half), the full share beating a partial one."""
    exact = [c for c in candidates if _norm(c.text) == _norm(description)
             or _norm(c.label()) == _norm(description)]
    if exact:
        return exact
    want = words(description)
    scored = [(s, c) for c in candidates if (s := _score(want, c)) >= 0.5]
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return [c for s, c in scored if s == best]


_CLARIFY: dict[tuple[str, str], tuple[float, list[str]]] = {}


def _remember_options(owner: str, chat: str, options: list[Candidate]) -> None:
    with _LOCK:
        _CLARIFY[(owner, chat)] = (time.time(), [c.label() for c in options]
                                   + [c.text for c in options])


def _offered(owner: str, chat: str, description: str) -> bool:
    with _LOCK:
        kept = _CLARIFY.get((owner, chat))
    if not kept or time.time() - kept[0] > CLARIFY_SECONDS:
        return False
    return any(grounded(description, option, share=1.0) for option in kept[1])


def _same(cands: list[Candidate]) -> bool:
    """One item seen twice: the same text (a to-do and the Athena item it came from)."""
    return len({_norm(c.text) for c in cands}) == 1 and len({c.store for c in cands}) == len(cands)


async def complete(owner: str, args: dict, *, said: str, chat: str = "",
                   call: Any = None) -> dict:
    """Mark the one item ``args["description"]`` means done; the tool result."""
    from fork.athena.action_items_lists import _change_line

    description = re.sub(r"\s+", " ", str(args.get("description") or "")).strip()
    if not words(description):
        return _failed("Say which item to mark done.")
    if not (grounded(description, said, share=0.01) or _offered(owner, chat, description)):
        return _failed("Not run: the description must be in the owner's words.")
    hub = hub_candidates(owner)
    filed = _filed_lines(owner)
    filed_ids = set()
    for c in hub:
        c.item_id = filed.get((c.ref, c.line)) or filed.get((c.ref, ""))
        if c.item_id is not None:
            filed_ids.add(c.item_id)
    athena = [c for c in await athena_candidates(call) if c.item_id not in filed_ids]
    found = match(description, hub + athena)
    if not found:
        return _done(f"I couldn't find an open to-do matching '{description}'.", changed=False)
    if len(found) > 1 and not _same(found):
        options = found[:MAX_OPTIONS]
        _remember_options(owner, chat, options)
        labels = [c.label() for c in options]
        ask = ", ".join(labels[:-1]) + " or " + labels[-1]
        more = f" I found {len(found)}." if len(found) > MAX_OPTIONS else ""
        return _done(f"Which one: {ask}?{more}", changed=False, ask=True)
    done = []
    for c in found:
        try:
            if c.store == "hub":
                if not await _change_line(owner, c.ref, c.line, done=True):
                    continue
                if c.item_id is not None and call is not None:  # at once, not next pass
                    await _athena_done(call, c.item_id, quiet=True)
            elif not await _athena_done(call, c.item_id):
                continue
        except Exception as exc:  # noqa: BLE001 -- said, not raised
            logger.warning("voice: could not mark an item done: %s", exc)
            return _failed(f"Not marked: {exc}")
        done.append(c)
    if not done:
        return _failed("Not marked: the item changed meanwhile.")
    sentence = f"Marked '{done[0].text}' done."
    with _LOCK:
        _CLARIFY.pop((owner, chat), None)
    await notify(owner, sentence)
    return _done(sentence)


async def _athena_done(call: Any, item_id: int | None, *, quiet: bool = False) -> bool:
    if call is None or item_id is None:
        return False
    try:
        res = await call("update_action_item", {"item_id": item_id, "status": "done"})
    except Exception:  # noqa: BLE001
        if quiet:  # the job carries the to-do's status across on its next pass
            logger.info("voice: Athena item %s left to the job", item_id, exc_info=True)
            return False
        raise
    return str(res).startswith("Action item")


# ------------------------------------------------------------------------ results
def _done(sentence: str, *, changed: bool = True, ask: bool = False, entry=None) -> dict:
    out = {"output": sentence, "exit_code": 0, "changed": changed}
    if ask:
        out["ask"] = True
    if entry:
        out["filed"] = {k: v for k, v in entry.items() if k in ("target", "ref", "list",
                                                                "notes")}
    return out


def _failed(reason: str) -> dict:
    return {"error": reason, "exit_code": 1, "blocked": True}


async def notify(owner: str, sentence: str) -> bool:
    """One phone push per action ("Voice: Marked … done."), when the setting is on."""
    if not push_enabled():
        return False
    try:
        from fork.athena.action_items_push import push
        return await push(owner, "Voice assistant", f"Voice: {sentence}",
                          f"voice-action-{uuid.uuid4().hex[:12]}")
    except Exception:  # noqa: BLE001 -- the action stands without its push
        logger.warning("voice: the push failed", exc_info=True)
        return False


# ----------------------------------------------------------------- for the brain
def athena_caller(owner: str) -> Any:
    """``call(tool, args)`` on Athena's MCP server, or None (not connected, not allowed)."""
    try:
        from fork.athena.client import bound_caller
        from fork.brain import toolset

        manager = toolset._manager()
        sid = toolset.athena_server(manager)
        if manager is None or not sid or toolset.owner_blocks_mcp(owner):
            return None
        return bound_caller(manager, sid)
    except Exception:  # noqa: BLE001
        logger.debug("voice: no Athena caller", exc_info=True)
        return None


class VoiceActions:
    """What ``read_only_turn`` is given for a voice turn with actions on: the schemas to
    offer, the names ``read_only_refusal`` lets through, and :meth:`run`."""

    schemas = SCHEMAS
    names = NAMES

    def __init__(self, *, call: Any = None, now: datetime | None = None):
        self._call, self._now = call, now

    async def run(self, run: Any, name: str, content: str) -> tuple[str, dict]:
        try:
            args = json.loads(content or "{}")
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        owner, said = str(run.owner or ""), str(run.request_text or "")
        if name == ADD:
            result = await add(owner, args, said=said, now=self._now)
        elif name == COMPLETE:
            call = self._call if self._call is not None else athena_caller(owner)
            result = await complete(owner, args, said=said, chat=str(run.session_id or ""),
                                    call=call)
        else:
            result = _failed(f"Tool '{name}' is not available here.")
        status = "ok" if not result.get("error") else "NOT RUN"
        return f"{name}: {status}", result


def for_turn() -> VoiceActions | None:
    """The actions a voice turn gets: None while the setting is off."""
    return VoiceActions() if enabled() else None
