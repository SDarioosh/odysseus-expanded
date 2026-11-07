"""Fork: settings the hub manages per Echo (docs/fork/voice-assistant.md#device-settings).

The owner sets, per device of the registry (``fork/voice/devices.py``), some of the
Echo's own settings (contract C6: the clock home screen, Today, kiosk, recording,
answering without the wake word; C7: the Music page). A key that is absent means "the
device decides". The device fetches them
(``GET /api/chat/voice/v1/devices/{device_id}/settings``,
``fork/voice/device_settings_routes.py``), so the hub never reaches the device: at start,
after each hello, and when its announcements feed delivers an item of
``kind: "settings"``, which :func:`replace` puts there on every change.

One row per (owner, device id) in the voice SQLite file (``<DATA_DIR>/fork_voice.db``):
the settings as JSON and a ``version`` that goes up on every change. Rows stay when a
device leaves the registry, so a device that comes back finds its settings and a version
that never went backwards.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from fork.voice import devices

logger = logging.getLogger(__name__)

MAX_SECONDS = 86400  # an "after N seconds" setting longer than a day is a typo


@dataclass(frozen=True)
class Key:
    name: str
    kind: str  # "bool", "int" or "enum"
    label: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    hint: str = ""

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.name, "type": self.kind, "label": self.label}
        if self.kind == "int":
            out.update(min=self.minimum, max=self.maximum)
        if self.choices:
            out["choices"] = list(self.choices)
        if self.hint:
            out["hint"] = self.hint
        return out


# Contracts C6 and C7, in the order the Settings card shows them. Only these keys are
# stored.
KEYS: tuple[Key, ...] = (
    Key("home.enabled", "bool", "Clock home screen"),
    Key("home.idle_seconds", "int", "Back to the clock after (seconds)", 0, MAX_SECONDS,
        hint="0 = never"),
    Key("home.weather_enabled", "bool", "Weather on the clock"),
    Key("today.enabled", "bool", "Today page"),
    Key("kiosk.return_seconds", "int", "Back from other apps after (seconds)", 0,
        MAX_SECONDS, hint="0 = never"),
    Key("kiosk.auto_reload", "bool", "Reload a failed dashboard"),
    Key("record.enabled", "bool", "Recording"),
    Key("record.clock_button", "bool", "Record button on the clock"),
    Key("record.privacy_button", "enum", "Privacy button", choices=("record", "mute")),
    Key("followup.enabled", "bool", "Answer without the wake word"),
    Key("followup.seconds", "int", "Listen for an answer for (seconds)", 3, 15),
    # Contract C7 (the Music page).
    Key("music.enabled", "bool", "Music page"),
    Key("music.stay_while_playing", "bool", "Stay on the Music page while playing"),
)
BY_NAME = {k.name: k for k in KEYS}

FEED_KIND = "settings"
FEED_TEXT = "Settings changed in Odysseus."
FEED_SHOW_SECONDS = 600  # the device polls every 30 s; at start it fetches anyway

SCHEMA = """
CREATE TABLE IF NOT EXISTS device_settings (
    owner TEXT NOT NULL,
    device_id TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (owner, device_id)
);
"""


class BadSettings(ValueError):
    """A setting the contract doesn't have, or a value of the wrong type or range."""


@contextmanager
def _connect(write: bool = False) -> Iterator[sqlite3.Connection]:
    from fork.voice.ledger import db_path

    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)
        if write:
            db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            if write:
                db.execute("ROLLBACK")
            raise
        if write:
            db.execute("COMMIT")
    finally:
        db.close()


def _check(key: Key, value: Any) -> Any:
    if key.kind == "bool":
        if not isinstance(value, bool):
            raise BadSettings(f"{key.name} must be true or false")
        return value
    if key.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise BadSettings(f"{key.name} must be a whole number")
        if not key.minimum <= value <= key.maximum:  # type: ignore[operator]
            raise BadSettings(f"{key.name} must be from {key.minimum} to {key.maximum}")
        return value
    if not isinstance(value, str) or value not in key.choices:
        raise BadSettings(f"{key.name} must be one of: " + ", ".join(key.choices))
    return value


def validate(settings: Any) -> dict[str, Any]:
    """The settings to store: only C6's and C7's keys, each of its type and range; a key
    that is null is left out (the device decides). Raises :class:`BadSettings`."""
    if not isinstance(settings, dict):
        raise BadSettings("settings must be an object")
    unknown = sorted(str(k) for k in settings if k not in BY_NAME)
    if unknown:
        raise BadSettings("unknown setting(s): " + ", ".join(unknown))
    return {k.name: _check(k, settings[k.name]) for k in KEYS
            if settings.get(k.name) is not None}


def _clean_id(device_id: Any) -> str:
    return devices.clean(device_id, devices.FIELD_LIMITS["device_id"])


def _stored(text: Any) -> dict[str, Any]:
    """A row's settings JSON; ``{}`` when it isn't an object (a damaged row)."""
    try:
        stored = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def get(owner: Any, device_id: Any) -> dict[str, Any]:
    """``{"version": n, "settings": {...}}`` for this owner's device; version 0 and no
    settings when the hub has never set any."""
    with _connect() as db:
        row = db.execute("SELECT settings, version FROM device_settings"
                         " WHERE owner = ? AND device_id = ?",
                         (devices.owner_key(owner), _clean_id(device_id))).fetchone()
    if row is None:
        return {"version": 0, "settings": {}}
    stored = _stored(row["settings"])
    # Only known keys of the right type, even if the row was written by an older build.
    out = {}
    for k in KEYS:
        if k.name in stored and stored[k.name] is not None:
            try:
                out[k.name] = _check(k, stored[k.name])
            except BadSettings:
                pass
    return {"version": int(row["version"]), "settings": out}


def all_for(owner: Any) -> dict[str, dict[str, Any]]:
    """Every device's ``get`` answer for this owner, by device id."""
    with _connect() as db:
        ids = [r["device_id"] for r in db.execute(
            "SELECT device_id FROM device_settings WHERE owner = ?",
            (devices.owner_key(owner),)).fetchall()]
    return {i: get(owner, i) for i in ids}


def replace(owner: Any, device_id: Any, settings: Any, *, now: float | None = None,
            notify: bool = True) -> dict[str, Any]:
    """Make ``settings`` this device's whole set (a key left out or null: the device
    decides). Validated first (:class:`BadSettings`). When anything changed, the version
    goes up, the change is logged (owner, device, keys; never values) and the device's
    feed gets a ``kind: "settings"`` item. Answers :func:`get`'s shape."""
    key = devices.owner_key(owner)
    device_id = _clean_id(device_id)
    if not key or not device_id:
        raise BadSettings("an owner and a device id are needed")
    wanted = validate(settings)
    now = time.time() if now is None else float(now)
    with _connect(write=True) as db:
        row = db.execute("SELECT settings, version FROM device_settings"
                         " WHERE owner = ? AND device_id = ?", (key, device_id)).fetchone()
        before = _stored(row["settings"]) if row is not None else {}
        version = int(row["version"]) if row is not None else 0
        changed = sorted(k for k in set(before) | set(wanted) if before.get(k) != wanted.get(k))
        if changed:
            version += 1
            db.execute(
                "INSERT INTO device_settings (owner, device_id, settings, version, updated_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (owner, device_id) DO UPDATE SET"
                " settings = excluded.settings, version = excluded.version,"
                " updated_at = excluded.updated_at",
                (key, device_id, json.dumps(wanted, sort_keys=True), version, now))
    if changed:
        logger.info("voice: device settings changed (owner %s, device %s, keys %s, version %d)",
                    key, device_id, ", ".join(changed), version)
        if notify:
            _notify(key, device_id, version, now)
    return {"version": version, "settings": dict(wanted)}


def _notify(owner: str, device_id: str, version: int, now: float) -> None:
    """A ``kind: "settings"`` item in that device's feed, so it fetches within 30 s. The
    device shows no card for it. A failure is logged: the device still fetches hourly."""
    from fork.voice import announcements

    try:
        announcements.store(owner, FEED_TEXT, {"device_id": device_id}, kind=FEED_KIND,
                            title="Settings", ref=f"settings:{version}", now=now,
                            show_seconds=FEED_SHOW_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("voice: the settings feed item was not kept", exc_info=True)
