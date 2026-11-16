"""Fork: the agent's ``alarm`` tool (docs/fork/voice-assistant.md#alarms).

"Set an alarm for 7", "what alarms do I have", "cancel my alarm": Home Assistant has no
alarm intent, so these reach Odysseus. One tool with an ``op``: ``set``, ``cancel`` or
``list``, for one Echo: the one named in ``device`` (a room or device name,
``devices.match``), else the device the voice turn came from, else the only Echo the hub
knows. The alarms are kept by ``fork/voice/alarms.py``; the Echo rings them itself.

* **Voice** (``fork/voice/policy.py``): it runs at once, no spoken "yes": it only changes
  an alarm clock on the Echo, nothing leaves it. :func:`run_voice` is called by the
  ``VoiceGate`` with the asking device, and records each change on the turn's ledger
  (``kind: "alarm"``, ``created`` / ``cancelled``), so the Echo refetches its list.
* **Chat** (``fork/brain/tools.py``): ``list`` is a read (``toolset.is_read``); ``set``
  and ``cancel`` are writes, asked on an approval card like ``announce`` and run only
  with that exact approval (:func:`run_approved`).

Offered to an admin (or single-user) owner while the hub knows at least one Echo
(:func:`available`), in the brain's ``home`` tool group.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fork.voice import alarm_rules as rules

logger = logging.getLogger(__name__)

NAME = "alarm"
OPS = ("set", "cancel", "list")
MAX_DEVICE = 100

SCHEMA = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "Set, cancel or list alarm clocks on the home's Echo devices (they ring at that "
            "time on the device). Use it for \"wake me at 7\", \"set an alarm for 6:30 on "
            "weekdays\", \"what alarms do I have\", \"cancel my alarm\". Not for timers "
            "(a duration) and not for reminders (a to-do). By voice it acts on the Echo "
            "that was spoken to unless `device` names another; tell the owner the result's "
            "next ring time."),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": list(OPS),
                       "description": "set a new alarm, cancel alarms, or list them."},
                "time": {"type": "string",
                         "description": "set: the clock time, 24 h \"HH:MM\" (\"07:30\"). "
                                        "cancel: cancel every alarm at this time."},
                "days": {"type": "array", "items": {"type": "string"},
                         "description": "set, for a repeating alarm: weekday names "
                                        "(\"monday\"), \"weekdays\", \"weekends\" or "
                                        "\"every day\". Leave out for a one-off."},
                "date": {"type": "string",
                         "description": "set, a one-off on a given day: YYYY-MM-DD. Leave "
                                        "out for the next time that clock time comes."},
                "label": {"type": "string", "description": "set: an optional name (\"Gym\")."},
                "alarm_id": {"type": "string",
                             "description": "cancel: one alarm's id, from list."},
                "all": {"type": "boolean", "description": "cancel: every alarm of the device."},
                "device": {"type": "string",
                           "description": "A room or Echo name; leave out for the Echo that "
                                          "was spoken to."},
            },
            "required": ["op"],
        },
    },
}


def available(owner: Any) -> bool:
    """Offered to an admin or single-user owner while the hub knows an Echo."""
    try:
        from src.tool_security import owner_is_admin_or_single_user

        from fork.voice import devices

        return owner_is_admin_or_single_user(owner) and bool(devices.devices(owner))
    except Exception:  # noqa: BLE001
        return False


def _args(content: Any) -> dict:
    try:
        args = json.loads(content or "{}") if isinstance(content, str) else content
    except ValueError:
        return {}
    return args if isinstance(args, dict) else {}


def op_of(content: Any) -> str:
    op = str(_args(content).get("op") or "").strip().lower()
    return {"add": "set", "create": "set", "delete": "cancel", "remove": "cancel",
            "show": "list", "get": "list"}.get(op, op)


def is_read(content: Any) -> bool:
    """``list`` only reads; ``set`` and ``cancel`` change the device's alarms."""
    return op_of(content) == "list"


def _error(text: str) -> tuple[str, dict]:
    return f"{NAME}: failed", {"error": text, "exit_code": 1}


def _names(known: list) -> str:
    return ", ".join(sorted(d.name or d.device_id for d in known)) or "none yet"


def find_device(owner: Any, said: Any, asked_from: str | None) -> tuple[Any, str | None]:
    """``(Device, None)`` for the device meant, or ``(None, why not)``."""
    from fork.voice import devices

    known = devices.devices(owner)
    said = " ".join(str(said or "").split())[:MAX_DEVICE]
    if said:
        found = devices.match(owner, said)
        if isinstance(found, dict) and found.get("device_id"):
            device = devices.get(owner, found["device_id"])
            if device is not None:
                return device, None
        if isinstance(found, dict) and found.get("area"):
            room = devices.slug(found["area"])
            there = [d for d in known if d.area and devices.slug(d.area) == room]
            if len(there) == 1:
                return there[0], None
            if there:
                return None, (f"There are {len(there)} Echos in the {found['area']} "
                              f"({_names(there)}); say which one.")
        if found == "all":
            return None, f"Name one Echo for the alarm. Known: {_names(known)}."
        return None, f"No Echo called \"{said}\". Known: {_names(known)}."
    if asked_from:
        device = devices.get(owner, asked_from)
        if device is not None:
            return device, None
    if len(known) == 1:
        return known[0], None
    if not known:
        return None, "No Echo device is known to Odysseus yet."
    return None, f"Say which Echo the alarm is for. Known: {_names(known)}."


def _on(device: Any) -> str:
    name = device.name or "this Echo"
    return name if name.lower().startswith("the ") else f"the {name}"


def _where(device: Any) -> str:
    return device.name or device.area or "Echo"


def _record(owner: Any, turn_id: str | None, action: str, alarm: Any, device: Any,
            local: datetime) -> None:
    """One ledger action on the voice turn (the Echo refetches on seeing it)."""
    if not turn_id:
        return
    from fork.voice import ledger

    at = alarm.next_ring(local)
    try:
        ledger.add_action(turn_id, owner, action=action, kind="alarm",
                          title=alarm.label or "Alarm", where=_where(device),
                          detail=rules.days_detail(alarm.days),
                          due=at.isoformat(timespec="seconds") if at is not None else None)
    except Exception:  # noqa: BLE001 -- the alarm is set; only the screen's line is lost
        logger.warning("alarm: the ledger action was not kept", exc_info=True)


def _describe(alarm: Any, local: datetime) -> str:
    at = alarm.next_ring(local)
    repeat = rules.days_phrase(alarm.days)
    when = rules.spoken_when(at, local) if at is not None else "never"
    label = f" ({alarm.label})" if alarm.label else ""
    if repeat:
        return f"{rules.spoken_time(alarm.time)} {repeat}{label}, next at {when}"
    return f"{when}{label}"


def _set(owner: Any, args: dict, device: Any, turn_id: str | None, now: float | None,
         tz: Any) -> tuple[str, dict]:
    from fork.voice import alarms

    if not str(args.get("time") or "").strip():
        return _error("Give the alarm's time (HH:MM, 24 h).")
    hhmm = rules.parse_time(args.get("time"))
    days = rules.parse_days(args.get("days"))
    local = alarms.now_local(owner, now, tz)
    day = None
    if not days:
        day = rules.parse_date(args.get("date")) or rules.default_date(hhmm, local)
        if rules.next_ring(hhmm, (), day, local) is None:
            return _error(f"{day.isoformat()} at {hhmm} has already passed.")
    try:
        alarm = alarms.add(owner, device.device_id, hhmm, days=days, day=day,
                           label=args.get("label"), now=now, tz=tz)
    except alarms.TooMany as exc:
        return _error(str(exc))
    _record(owner, turn_id, "created", alarm, device, local)
    at = alarm.next_ring(local)
    when = rules.spoken_when(at, local)  # type: ignore[arg-type]
    repeat = rules.days_phrase(alarm.days)
    label = f" ({alarm.label})" if alarm.label else ""
    if repeat:
        text = (f"Alarm{label} set for {rules.spoken_time(hhmm)} {repeat} on {_on(device)}; "
                f"it next rings at {when}.")
    else:
        text = f"Alarm{label} set for {when} on {_on(device)}."
    return f"{NAME}: set", {"output": text, "exit_code": 0}


def _cancel(owner: Any, args: dict, device: Any, turn_id: str | None, now: float | None,
            tz: Any) -> tuple[str, dict]:
    from fork.voice import alarms

    local = alarms.now_local(owner, now, tz)
    have = alarms.alarms(owner, device.device_id, now=now, tz=tz)
    alarm_id = str(args.get("alarm_id") or args.get("id") or "").strip()
    if alarm_id:
        wanted = [a for a in have if a.id == alarm_id]
        what = f"alarm {alarm_id}"
    elif str(args.get("time") or "").strip():
        hhmm = rules.parse_time(args.get("time"))
        wanted = [a for a in have if a.time == hhmm]
        what = f"alarm at {rules.spoken_time(hhmm)}"
    elif args.get("all") in (True, 1, "true", "yes"):
        wanted, what = list(have), "alarm"
    elif len(have) == 1:  # "cancel my alarm" with only one
        wanted, what = list(have), "alarm"
    elif not have:
        return f"{NAME}: nothing", {"output": f"There are no alarms on {_on(device)}.",
                                    "exit_code": 0}
    else:
        return _error("Say which alarm to cancel (its time, its id from list, or all): "
                      + "; ".join(f"{_describe(a, local)} [id {a.id}]" for a in have))
    if not wanted:
        return f"{NAME}: nothing", {"output": f"There is no {what} on {_on(device)}; "
                                              "nothing was cancelled.", "exit_code": 0}
    gone = alarms.remove(owner, device.device_id, [a.id for a in wanted], now=now)
    for a in gone:
        _record(owner, turn_id, "cancelled", a, device, local)
    listed = "; ".join(_describe(a, local) for a in gone)
    plural = "alarm" if len(gone) == 1 else f"{len(gone)} alarms"
    return f"{NAME}: cancelled", {"output": f"Cancelled the {plural} on {_on(device)}: "
                                            f"{listed}.", "exit_code": 0}


def _list(owner: Any, device: Any, now: float | None, tz: Any) -> tuple[str, dict]:
    from fork.voice import alarms

    local = alarms.now_local(owner, now, tz)
    have = alarms.alarms(owner, device.device_id, now=now, tz=tz)
    if not have:
        return f"{NAME}: list", {"output": f"There are no alarms on {_on(device)}.",
                                 "exit_code": 0}
    rows = "\n".join(f"- {_describe(a, local)} [id {a.id}]" for a in have)
    count = "1 alarm" if len(have) == 1 else f"{len(have)} alarms"
    return f"{NAME}: list", {"output": f"{count} on {_on(device)}, next first:\n{rows}",
                             "exit_code": 0}


async def run(owner: Any, content: Any, *, asked_from: str | None = None,
              turn_id: str | None = None, now: float | None = None,
              tz: Any = None) -> tuple[str, dict]:
    """Run one call; returns ``(description, result)`` the way the brain's tools do.
    ``asked_from``: the device id of the voice turn; ``turn_id``: its ledger turn."""
    import asyncio

    return await asyncio.to_thread(run_sync, owner, content, asked_from=asked_from,
                                   turn_id=turn_id, now=now, tz=tz)


def run_sync(owner: Any, content: Any, *, asked_from: str | None = None,
             turn_id: str | None = None, now: float | None = None,
             tz: Any = None) -> tuple[str, dict]:
    args = _args(content)
    op = op_of(args)
    if op not in OPS:
        return _error("op must be set, cancel or list.")
    device, why = find_device(owner, args.get("device") or args.get("room"), asked_from)
    if device is None:
        return _error(why or "No such Echo.")
    try:
        if op == "set":
            return _set(owner, args, device, turn_id, now, tz)
        if op == "cancel":
            return _cancel(owner, args, device, turn_id, now, tz)
        return _list(owner, device, now, tz)
    except rules.BadAlarm as exc:
        return _error(str(exc))


async def run_approved(run_: Any, content: str, exact_approval: Any) -> tuple[str, dict]:
    """A chat call: ``list`` runs; ``set`` and ``cancel`` only with the owner's exact
    approval of this very call (claimed here, once)."""
    if is_read(content):
        return await run(run_.owner, content)
    claimed = False
    if exact_approval is not None:
        try:
            claimed = exact_approval.claim(owner=run_.owner, session_id=run_.session_id,
                                           tool_name=NAME, content=content, workspace=None)
        except Exception:  # noqa: BLE001 -- can't tell: refused
            logger.warning("alarm: approval check failed", exc_info=True)
    if not claimed:
        return f"{NAME}: BLOCKED", {"error": "Changing alarms needs the owner's approval.",
                                    "exit_code": 1, "blocked": True}
    return await run(run_.owner, content)


async def run_voice(owner: Any, content: str, *, device_id: str | None,
                    turn_id: str | None) -> dict:
    """A voice call (the ``VoiceGate``): runs at once on the asking device; the result
    stands for the call."""
    desc, result = await run(owner, content, asked_from=device_id or None, turn_id=turn_id)
    return {**result, "description": desc}
