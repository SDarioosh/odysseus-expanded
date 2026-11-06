"""Fork: the agent's ``announce`` tool (docs/fork/voice-assistant.md#the-announce-tool).

"Tell the kitchen dinner is ready": the model calls ``announce`` with the message and
the room or device (``to``), matched against the devices the hub knows
(``fork/voice/devices.py``) and Home Assistant's rooms with a satellite. It runs
:func:`fork.voice.announcements.deliver`: spoken there through Home Assistant and shown on
that room's Echo screens. A room or device Home Assistant has no satellite for is not
spoken anywhere (never in every room instead); the result says so.

Speaking aloud in the house is a write, so the tool never runs on the model's word:

* **Chat** (the brain, ``fork/brain/tools.py``): it is not a read (``toolset.is_read``),
  so ``needs_approval`` asks on a card; the call runs only with the exact approval the
  owner gave, which :func:`run` claims for this exact call (owner, chat, arguments).
* **Voice** (``fork/voice/policy.py``): it is one of the writes kept for a spoken "yes"
  (``fork/voice/confirm.py`` ``WRITABLE``, :func:`describe`): "Announce “Dinner is ready”
  in the Kitchen?"; a "yes" runs it through the same exact approval.

Offered only to an admin (or single-user) owner, while Home Assistant is set up
(:func:`available`), in the brain's ``home`` tool group.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

NAME = "announce"
MAX_TO = 100

SCHEMA = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "Say a message aloud on the home's voice satellites (Echo devices) through "
            "Home Assistant, and show it on their screens. Use it when the owner asks to "
            "tell, announce or call out something to a room or the house (\"tell the "
            "kitchen dinner is ready\"). Give the words to say exactly as they should be "
            "heard, and the room or device; leave `to` empty for every room."),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string",
                            "description": "What to say aloud, one or two short sentences."},
                "to": {"type": "string",
                       "description": "A room (\"kitchen\") or device name (\"Bedroom "
                                      "Echo\"); empty or \"everywhere\" for all rooms."},
            },
            "required": ["message"],
        },
    },
}


def available(owner: Any) -> bool:
    """Offered to an admin or single-user owner while Home Assistant is set up."""
    try:
        from src.tool_security import owner_is_admin_or_single_user

        from fork.voice.announcements import can_speak

        return owner_is_admin_or_single_user(owner) and can_speak()
    except Exception:  # noqa: BLE001
        return False


def _args(content: Any) -> dict:
    try:
        args = json.loads(content or "{}") if isinstance(content, str) else content
    except ValueError:
        return {}
    return args if isinstance(args, dict) else {}


def _message(args: dict) -> str:
    from fork.voice.announce import MAX_MESSAGE

    return " ".join(str(args.get("message") or args.get("text") or "").split())[:MAX_MESSAGE]


def _to(args: dict) -> str:
    return " ".join(str(args.get("to") or args.get("room") or args.get("device") or "")
                    .split())[:MAX_TO]


async def _ha_areas() -> tuple[str, ...]:
    """Home Assistant's rooms that have a satellite; () when it can't say."""
    from fork.voice import announcements

    try:
        sats = await announcements.satellites()
    except Exception:  # noqa: BLE001
        return ()
    return tuple(dict.fromkeys(s.area or s.area_id for s in sats if s.area or s.area_id))


async def target_for(owner: Any, to: str) -> dict | str | None:
    """The target ``to`` names (``devices.match``), asking Home Assistant for its rooms
    only when the hub's own devices don't match."""
    from fork.voice import devices

    found = devices.match(owner, to)
    if found is None:
        found = devices.match(owner, to, areas=await _ha_areas())
    return found


def _known(owner: Any) -> str:
    from fork.voice import devices

    rooms = sorted({d.area for d in devices.devices(owner) if d.area})
    return ", ".join(rooms) if rooms else "none yet"


async def run(owner: Any, content: str) -> tuple[str, dict]:
    """Announce; returns ``(description, result)`` the way the brain's tools do."""
    from fork.voice import announcements, devices

    args = _args(content)
    message = _message(args)
    if not message:
        return f"{NAME}: no message", {"error": "Give the message to say.", "exit_code": 1}
    to = _to(args)
    target = await target_for(owner, to)
    if target is None:
        return f"{NAME}: unknown room", {
            "error": f"No room or device called \"{to}\". Known rooms: {_known(owner)}.",
            "exit_code": 1}
    where = devices.label(owner, target)
    out = await announcements.deliver(owner, message, target)
    if out.spoken:
        return f"{NAME}: spoken", {"output": f"Announced in {where}: {message}", "exit_code": 0}
    if out.no_satellite:
        shown = (f" It is shown on the screens of the devices in {where}."
                 if out.item is not None else "")
        return f"{NAME}: not spoken", {
            "output": (f"Not spoken aloud anywhere: {out.problem}. Tell the owner; nothing "
                       f"was said in other rooms.{shown} Message: {message}"),
            "exit_code": 0}
    if out.item is not None:
        return f"{NAME}: shown only", {
            "output": (f"Not spoken aloud ({out.problem}); shown on the screens of the "
                       f"devices in {where}: {message}"), "exit_code": 0}
    return f"{NAME}: failed", {"error": f"Not announced: {out.problem or 'failed'}",
                               "exit_code": 1}


async def run_approved(run_: Any, content: str, exact_approval: Any) -> tuple[str, dict]:
    """:func:`run` only with the owner's exact approval of this very call (claimed here,
    once); anything else is refused without speaking."""
    claimed = False
    if exact_approval is not None:
        try:
            claimed = exact_approval.claim(owner=run_.owner, session_id=run_.session_id,
                                           tool_name=NAME, content=content, workspace=None)
        except Exception:  # noqa: BLE001 -- can't tell: refused
            logger.warning("announce: approval check failed", exc_info=True)
    if not claimed:
        return f"{NAME}: BLOCKED", {"error": "Announcing needs the owner's approval.",
                                    "exit_code": 1, "blocked": True}
    return await run(run_.owner, content)


async def describe(owner: Any, args: dict):
    """The voice confirmation's :class:`fork.voice.confirm.Change`, or None (refused)."""
    from fork.voice import devices
    from fork.voice.confirm import Change

    message = _message(args)
    if not message:
        return None
    target = await target_for(owner, _to(args))
    if target is None:
        return None
    where = devices.label(owner, target)
    place = "everywhere" if where == "everywhere" else f"in {where}"
    return Change("created", "announcement", message, where.removeprefix("the "),
                  f"Announced it {place}.", question=f"Announce “{message}” {place}?")
