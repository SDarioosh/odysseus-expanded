"""Fork: changes by voice after a spoken "yes" (docs/fork/voice-assistant.md,
"Changes by voice").

A voice turn can't be approved on a card, so a write the model calls is never run in its
turn. If it is one of :data:`WRITABLE` (a to-do, reminder, list item, note or calendar
event made, an Athena action item marked done), the exact call is kept as the voice
conversation's :class:`Proposal` (:data:`PROPOSAL_SECONDS`, one per conversation: a new
turn's proposal replaces the old) and the model is told :data:`PROPOSED`: not run yet,
ask the owner in one short question. Every other write is refused as before
(``fork/brain/tools.py``, ``read_only_refusal``).

The next question of that conversation decides (:func:`reply_kind`): an affirmation runs
the kept calls exactly (:func:`run`, no model call) through the brain's executor with an
exact approval filed and consumed in upstream's store, the same path an approved chat
call takes, so owner scoping and upstream's checks still apply; a refusal drops it;
anything else drops it and is answered as a new question.

A new writable tool is one entry in :data:`WRITABLE`: its tool, the actions allowed and
a function that describes the call (:class:`Change`: what the ledger and the reply say)
or returns None to refuse it.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

logger = logging.getLogger(__name__)

PROPOSAL_SECONDS = 5 * 60
ATHENA_UPDATE = "update_action_item"
PROPOSED = ("Not run yet: changes by voice need the owner's spoken yes. Ask the owner to "
            "confirm in one short question that names the change and ends with a question "
            "mark (for example \"Add Dentist on Tuesday at three to your calendar?\"). "
            "Don't say it is done.")
DONE = "Done."
DECLINED = "Okay, I won't."
FAILED = "Sorry, that didn't work. Nothing was changed."
CHANGED = "That has changed since, so I didn't do it. Nothing was changed."


@dataclass(frozen=True)
class Change:
    """What a kept call does, as the ledger and the spoken reply say it."""
    action: str  # created | completed
    kind: str  # todo | reminder | list_item | note | event | action_item
    title: str
    where: str
    phrase: str  # "Added Dentist to your calendar."
    detail: str | None = None
    due: str | None = None
    all_day: bool = False
    source: str = "odysseus"
    question: str = ""  # the confirmation question spoken (it replaces any the model asked)
    check: Callable[[], bool] | None = None  # still true when run? (a list's items)


@dataclass
class Call:
    name: str
    content: str
    change: Change


@dataclass
class Proposal:
    owner: str
    session_id: str
    turn_id: str
    run: Any  # the voice turn's ToolRun (fork/brain/tools.py)
    calls: list[Call] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    def question(self) -> str:
        """The spoken confirmation: built from the kept calls only, naming each one, so a
        "yes" only ever approves what the owner just heard."""
        if len(self.calls) == 1:
            return self.calls[0].change.question
        parts = [c.change.question.rstrip("?").strip() for c in self.calls]
        parts = [parts[0]] + [p[:1].lower() + p[1:] for p in parts[1:]]
        return ", and ".join(parts) + "?"


_PROPOSALS: dict[tuple[str, str], Proposal] = {}
_LOCK = threading.Lock()


def _key(owner: Any, session_id: Any) -> tuple[str, str]:
    return str(owner or "").strip().casefold(), str(session_id or "")


def pending(owner: Any, session_id: Any, *, now: float | None = None) -> Proposal | None:
    now = time.time() if now is None else now
    with _LOCK:
        p = _PROPOSALS.get(_key(owner, session_id))
        if p is not None and now - p.created > PROPOSAL_SECONDS:
            _PROPOSALS.pop(_key(owner, session_id), None)
            return None
        return p


def take(owner: Any, session_id: Any) -> Proposal | None:
    p = pending(owner, session_id)
    with _LOCK:
        _PROPOSALS.pop(_key(owner, session_id), None)
    return p


def drop(owner: Any, session_id: Any) -> None:
    with _LOCK:
        _PROPOSALS.pop(_key(owner, session_id), None)


def forget_all() -> None:
    with _LOCK:
        _PROPOSALS.clear()


def keep(owner: Any, session_id: str, turn_id: str, run: Any, name: str, content: str,
         change: Change) -> None:
    """Add this call to the conversation's proposal of ``turn_id``; a proposal of an
    earlier turn is replaced. The same call twice is kept once."""
    with _LOCK:
        key = _key(owner, session_id)
        p = _PROPOSALS.get(key)
        if p is None or p.turn_id != turn_id:
            p = Proposal(owner=str(owner), session_id=str(session_id), turn_id=turn_id, run=run)
            _PROPOSALS[key] = p
        if not any(c.name == name and c.content == content for c in p.calls):
            p.calls.append(Call(name, content, change))


# ------------------------------------------------------------------- yes, no, other
_PUNCT = re.compile(r"[^\w\s']+")
YES = ("yes", "yeah", "yep", "yup", "sure", "okay", "ok", "correct", "do it", "please do",
       "go ahead", "that's right", "right", "absolutely", "of course", "go for it",
       "sounds good", "sure thing", "yes please")
NO = ("no", "nope", "cancel", "never mind", "nevermind", "don't", "dont", "do not",
      "stop", "forget it", "not now", "please don't")
POLITE = ("please", "thanks", "thank you", "do it", "do that", "that", "it")


def _phrases(words) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


def _only(text: str, words: tuple[str, ...]) -> bool:
    """True when ``text`` is one of these phrases, then more of them or polite words."""
    first, rest = _phrases(words), _phrases(words + POLITE)
    return re.fullmatch(rf"(?:{first})(?:\s+(?:{rest}))*", text) is not None


def reply_kind(text: str) -> str:
    """``"yes"``, ``"no"`` or ``"other"`` for the answer to a confirmation question."""
    norm = " ".join(_PUNCT.sub(" ", (text or "").casefold().replace("’", "'")).split())
    if not norm:
        return "other"
    if _only(norm, YES):
        return "yes"
    if _only(norm, NO):
        return "no"
    return "other"


# ------------------------------------------------------------------- the allow-list
def _args(content: str) -> dict:
    try:
        args = json.loads(content or "{}")
    except ValueError:
        return {}
    return args if isinstance(args, dict) else {}


_NUM = ("one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        "[0-9]{1,2}(?::[0-9]{2})?")
# Words that say when. A due date is kept only when the owner said one of them: the model
# sometimes adds a due date to a plain to-do (upstream's tool example shows one).
WHEN = re.compile(
    r"\b(?:today|tonight|tomorrow|tmrw|yesterday|morning|afternoon|evening|night|noon|"
    r"midday|midnight|weekend|later|eod|end of (?:the )?day|every|daily|weekly|monthly|"
    r"yearly|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|"
    r"mon|tues?|wed|thur?s?|fri|sat|sun|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)days?|"
    r"jan(?:uary)?|feb(?:ruary)?|april|june|july|aug(?:ust)?|sept?(?:ember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|(?:march|may) (?:the )?[0-9]|"
    r"christmas|thanksgiving|easter|new year'?s?|halloween|"
    r"o'?clock|half past|quarter (?:past|to)|"
    rf"(?:at|by|until|till|before|after|around) (?:{_NUM})|"
    r"[0-9]{1,2}(?::[0-9]{2})? ?(?:am|pm|a ?m|p ?m)|[0-9]{1,2}:[0-9]{2}|"
    r"[0-9]{1,2}(?:st|nd|rd|th)|[0-9]{1,2}/[0-9]{1,2}|[0-9]{4}-[0-9]{2}-[0-9]{2})\b")


def says_when(text: Any) -> bool:
    """True when the owner's words name a date or time."""
    norm = " ".join(str(text or "").casefold().replace("’", "'").replace(".", " ").split())
    return WHEN.search(norm) is not None


def without_unsaid_due(name: str, content: str, said: str) -> str:
    """``content`` of a ``manage_notes`` ``add`` without a ``due_date`` the owner never
    said (a plain to-do then, not a reminder); any other call unchanged. The confirmation
    question is built from the result, so the owner hears what will be saved."""
    if name != "manage_notes":
        return content
    args = _args(content)
    if str(args.get("action") or "").strip().lower() != "add" \
            or not str(args.get("due_date") or "").strip() or says_when(said):
        return content
    args.pop("due_date", None)
    items = args.get("checklist_items") or args.get("items")
    if not items and not str(args.get("content") or "").strip() and not args.get("note_type"):
        args["note_type"] = "checklist"  # a title alone is a to-do
    return json.dumps(args)


def _due(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        from src.user_time import parse_due_for_user
        return parse_due_for_user(text)
    except Exception:  # noqa: BLE001 -- shown as said
        return text


def _spoken_when(due: str | None, all_day: bool = False) -> str:
    """" on Wednesday, November 4 at 9:00 AM" for a due or start time, so the question
    says when; "" when there is none, the raw text when it can't be read."""
    text = str(due or "").strip()
    if not text:
        return ""
    from datetime import datetime
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return f" ({text})"
    day = f"{when.strftime('%A, %B')} {when.day}"
    if all_day or len(text) == 10:
        return f" on {day}"
    clock = when.strftime("%I:%M %p").lstrip("0")
    return f" on {day} at {clock}"


def _clean(text: Any, limit: int = 120) -> str:
    out = " ".join(str(text or "").split())
    return out if len(out) <= limit else out[:limit - 1].rstrip() + "…"


async def _note_add(owner: Any, args: dict) -> Change | None:
    items = args.get("checklist_items")
    if items is None:
        items = args.get("items")
    texts = [_clean(i.get("text")) for i in items or () if isinstance(i, dict) and i.get("text")]
    title = _clean(args.get("title") or args.get("text") or args.get("content")
                   or ", ".join(texts))
    due = _due(args.get("due_date"))
    if due:
        name = title or (texts[0] if texts else "a reminder")
        return Change("created", "reminder", name, "To-dos", f"I'll remind you: {name}.",
                      detail=", ".join(texts) or _clean(args.get("content"), 500) or None,
                      due=due, question=f"Set a reminder: {name}{_spoken_when(due)}?")
    checklist = bool(items) or str(args.get("note_type") or "") in {"checklist", "todo"}
    if checklist:
        name = title or (texts[0] if texts else "")
        if not name:
            return None
        return Change("created", "todo", name, "To-dos", f"Added {name} to your to-dos.",
                      detail=", ".join(texts) or None,
                      question=f"Add {name} to your to-dos?")
    if not title:
        return None
    return Change("created", "note", title, "Notes", f"Saved the note {title}.",
                  detail=_clean(args.get("content"), 500) or None,
                  question=f"Save a note: {title}?")


LIST_KEYS = frozenset({"action", "id", "note_id", "title", "checklist_items", "items",
                       "note_type"})


def _items_of(raw: Any) -> list[tuple[str, bool]]:
    try:
        rows = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return [(" ".join(str(r.get("text") or "").split()), bool(r.get("done")))
            for r in rows or () if isinstance(r, dict)]


def _find_list(owner: Any, args: dict):
    """The owner's checklist note this update names (id prefix, or an exact title)."""
    from core.database import Note, SessionLocal

    note_id = str(args.get("id") or args.get("note_id") or "").strip()
    title = " ".join(str(args.get("title") or "").split()).casefold()
    db = SessionLocal()
    try:
        q = db.query(Note)
        if owner:
            q = q.filter(Note.owner == owner)
        if note_id:
            note = q.filter(Note.id.startswith(note_id)).first()
            if note is not None and title and " ".join((note.title or "").split()).casefold() != title:
                return None  # would rename the list
        elif title:
            found = [n for n in q.filter(Note.archived == False).all()  # noqa: E712
                     if " ".join((n.title or "").split()).casefold() == title]
            note = found[0] if len(found) == 1 else None
        else:
            return None
        if note is None:
            return None
        return note.id, note.title or "", _items_of(note.items)
    finally:
        db.close()


def _keeps(old: list[tuple[str, bool]], new: list[tuple[str, bool]]) -> list[str] | None:
    """The added lines when ``new`` keeps every line of ``old`` (text and tick, in
    order) and only adds; else None."""
    i = 0
    added = []
    for row in new:
        if i < len(old) and row == old[i]:
            i += 1
        else:
            added.append(row[0])
    if i != len(old) or not added or any(not t for t in added):
        return None
    return added


async def _list_items(owner: Any, args: dict) -> Change | None:
    """``update`` that only adds lines to one of the owner's lists."""
    if set(args) - LIST_KEYS:
        return None
    new_raw = args.get("checklist_items") if args.get("checklist_items") is not None \
        else args.get("items")
    if not isinstance(new_raw, list):
        return None
    new = _items_of(new_raw)
    try:
        found = _find_list(owner, args)
    except Exception:  # noqa: BLE001 -- can't check: refused
        logger.warning("voice: list lookup failed", exc_info=True)
        return None
    if found is None:
        return None
    note_id, list_title, old = found
    added = _keeps(old, new)
    if added is None:
        return None

    def still() -> bool:
        try:
            now = _find_list(owner, {"id": note_id})
        except Exception:  # noqa: BLE001
            return False
        return now is not None and now[2] == old

    name = ", ".join(added)
    where = list_title or "your list"
    return Change("created", "list_item", name, where, f"Added {name} to {where}.",
                  question=f"Add {name} to {where}?", check=still)


async def _event(owner: Any, args: dict) -> Change | None:
    title = _clean(args.get("summary") or args.get("title"))
    if not title:
        return None
    all_day = bool(args.get("all_day"))
    start = args.get("local_start") if isinstance(args.get("local_start"), dict) else None
    due = None
    if start and start.get("date"):
        date, clock = str(start["date"]), str(start.get("time") or "")
        all_day = all_day or not clock
        due = date if all_day else _due(f"{date}T{clock}")
    elif args.get("dtstart"):
        raw = str(args["dtstart"])
        due = raw if all_day or len(raw) == 10 else _due(raw)
        all_day = all_day or len(raw) == 10
    return Change("created", "event", title, "Calendar", f"Added {title} to your calendar.",
                  detail=_clean(args.get("description"), 500) or None, due=due,
                  all_day=all_day,
                  question=f"Add {title} to your calendar{_spoken_when(due, all_day)}?")


async def athena_item_title(item_id: int) -> str | None:
    """The action item's short title, read through Athena's MCP tool; None if unknown."""
    from fork.athena.action_items_state import item_title, mcp_caller, parse_items_json
    from fork.brain import toolset

    manager = toolset._manager()
    sid = toolset.athena_server(manager)
    if manager is None or not sid:
        return None
    try:
        out = await mcp_caller(manager, sid)("list_action_items",
                                             {"status": "all", "format": "json"})
    except Exception:  # noqa: BLE001
        return None
    for row in parse_items_json(out) or ():
        if row.get("id") == item_id:
            return item_title(row)
    return None


async def _item_done(owner: Any, args: dict) -> Change | None:
    """Athena's ``update_action_item`` with ``status: done`` and nothing else."""
    if str(args.get("status") or "").strip().lower() != "done":
        return None
    if any(str(args.get(k) or "").strip() for k in ("kind", "due_date", "due_time",
                                                     "list_name", "title")):
        return None
    try:
        item_id = int(args.get("item_id", args.get("id")))
    except (TypeError, ValueError):
        return None
    title = await athena_item_title(item_id) or f"action item {item_id}"
    return Change("completed", "action_item", title, "Athena", f"Marked {title} done.",
                  source="athena", question=f"Mark {title} as done?")


@dataclass(frozen=True)
class Writable:
    tool: str  # a built-in tool's name, or ATHENA_UPDATE for Athena's tool
    actions: frozenset[str] | None  # the canonical actions allowed; None: no action arg
    describe: Callable[[Any, dict], Awaitable[Change | None]]


WRITABLE: tuple[Writable, ...] = (
    Writable("manage_notes", frozenset({"add"}), _note_add),
    Writable("manage_notes", frozenset({"update"}), _list_items),
    Writable("manage_calendar", frozenset({"create_event"}), _event),
    Writable(ATHENA_UPDATE, None, _item_done),
)


def _athena_tool(name: str) -> str | None:
    """The bare tool name of an Athena MCP tool, else None."""
    m = re.fullmatch(r"mcp__(.+?)__(\w+)", name or "")
    if not m:
        return None
    try:
        from fork.brain import toolset
        sid = toolset.athena_server()
    except Exception:  # noqa: BLE001
        sid = None
    return m.group(2) if sid and m.group(1) == sid else None


async def describe(owner: Any, name: str, content: str) -> Change | None:
    """The :class:`Change` this call would make when it may be proposed, else None."""
    from fork.brain import toolset

    tool = _athena_tool(name) if name.startswith("mcp__") else name
    if not tool:
        return None
    action = toolset._action(name, content) if name in toolset.ACTION_TOOLS else None
    args = _args(content)
    for entry in WRITABLE:
        if entry.tool != tool:
            continue
        if entry.actions is not None and action not in entry.actions:
            continue
        try:
            return await entry.describe(owner, args)
        except Exception:  # noqa: BLE001 -- can't tell: refused
            logger.warning("voice: describing %s failed", name, exc_info=True)
            return None
    return None


# ------------------------------------------------------------------------ running
@dataclass
class Outcome:
    change: Change
    ok: bool
    reason: str = ""


def _ok(result: dict, desc: str) -> bool:
    code = result.get("exit_code")
    return not (result.get("blocked") or result.get("error") or result.get("approval_required")
                or "BLOCKED" in desc or "NOT RUN" in desc) \
        and (code is None or code == 0)


async def run(proposal: Proposal) -> list[Outcome]:
    """Run the kept calls exactly, each with an exact approval filed and consumed in
    upstream's store (the path of an approved chat call), on the voice turn's run with
    its read-only flag lifted for that call only."""
    from src.tool_approvals import tool_approval_store

    from fork.brain import tools as brain_tools

    out: list[Outcome] = []
    for call in proposal.calls:
        change = call.change
        if change.check is not None and not change.check():
            out.append(Outcome(change, False, CHANGED))
            continue
        base = proposal.run
        try:
            pending_approval = brain_tools.create_approval(
                base, call.name, call.content, continuation_query=base.request_text)
            exact = tool_approval_store.consume(
                pending_approval.approval_id, decision="approve", owner=base.owner,
                session_id=base.session_id, allow_continuation=False)
            if exact is None:
                out.append(Outcome(change, False, "not approved"))
                continue
            # execute_tool_block takes an exact approval only for an armed run
            base.security.external_untrusted_context_seen = True
            approved = replace(base, read_only=False, gate=None)
            _ran, desc, result = await brain_tools.execute(approved, call.name, call.content,
                                                           exact_approval=exact)
        except Exception:  # noqa: BLE001
            logger.exception("voice: running a confirmed call failed")
            out.append(Outcome(change, False, "failed"))
            continue
        out.append(Outcome(change, _ok(result, desc),
                           "" if _ok(result, desc) else brain_tools.result_text(result)))
    return out


def reply(outcomes: list[Outcome]) -> str:
    """What is said after the calls ran."""
    done = [o for o in outcomes if o.ok]
    failed = [o for o in outcomes if not o.ok]
    if not done:
        if failed and all(o.reason == CHANGED for o in failed):
            return CHANGED
        return FAILED
    text = DONE + " " + " ".join(o.change.phrase for o in done)
    if failed:
        text += " I couldn't do the rest."
    return text
