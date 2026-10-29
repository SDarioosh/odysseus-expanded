"""Fork: what every settings reader of the fork returns, pinned for many values.

Each reader (``enabled()``, ``server_key()``, ``digest_hour()`` …) is called with its
environment variable unset, empty, and set to each value in ``VALUES``; former names
are tried against the current ones in ``PAIRS``. The results and the log lines each
call writes are compared with ``fixtures/settings_readers.json``, recorded from the
readers as they were before the settings moved into ``fork/common/settings.py``.
The readers must keep giving the same answers wherever they read their settings.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
from pathlib import Path

import pytest

FIXTURE = Path(__file__).with_name("fixtures") / "settings_readers.json"

UNSET = None
VALUES = [
    UNSET, "", " ", "\t", "\n", "1", "0", "00", "true", "True", "TRUE", " yes ", "yes", "Y",
    "no", "NO", "off", " Off\n", "on", "ON", "On ", "false", "False", "none", "None", "nil",
    "maybe", "2", "-1", "+3", "0.5", " 7 ", "10", "11", "23", "24", "25", "3_0", "1e3",
    "inf", "nan", "abc", "auto", "ALL", "all", "explicit", "ntfy", "Browser", "browser",
    "convo", " Convo ", "CONVO", "Straße", "a.b", "a b", "phone-1", "x" * 129,
    "https://hub.example.org", "HTTPS://Hub.Example.org/", "http://10.0.0.5:7000/x?y=1#z",
    "ftp://x", "x,y", " a , B ,, c ", ",", "Shopping", "Groceries list,Work",
]

# (current name, former name) read as one setting.
PAIRS = [
    ("MODULE_ACTION_ITEMS_SERVER", "BARVIS_ACTION_ITEMS_SERVER"),
    ("MODULE_NOTIFY_CHANNEL", "BARVIS_NOTIFY_CHANNEL"),
    ("MODULE_DIGEST_HOUR", "BARVIS_DIGEST_HOUR"),
    ("MODULE_FILE_ITEMS", "BARVIS_FILE_ITEMS"),
    ("MODULE_FACTS_SYNC", "BARVIS_FACTS_SYNC"),
    ("SOUND_MODULE_SERVER", "BARVIS_SOUND_SERVER"),
]
PAIR_VALUES = [UNSET, "", " ", "off", "Browser", "3", "Convo"]

ALL_NAMES = sorted({n for pair in PAIRS for n in pair} | {
    "ODYSSEUS_PERSONAL_READ_GRANTS", "ODYSSEUS_PERSONAL_STORE_ESCALATION",
    "ODYSSEUS_TRUSTED_READONLY_MCP", "FORK_CHECKLIST_ARCHIVE_MINUTES", "EVIDENCE_NOTES",
    "BELIEF_STRUCTURE", "BELIEF_STRUCTURE_BATCH", "BELIEF_CONSOLIDATE",
    "BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS",
    "BELIEF_STALE_DAYS", "INK_AUTO_SEND", "APP_PUBLIC_URL", "TODAY_INSIGHT_ENABLED",
    "TODAY_INSIGHT_SKIP_LISTS", "TODAY_INSIGHT_PUSH", "SOUND_MODULE_CALL_DEVICE",
})


def _readers(monkeypatch) -> dict[str, list[tuple[str, object]]]:
    """Variable name -> [(reader label, zero-argument reader)]."""
    from fastapi import HTTPException

    import src.settings
    import src.tool_utils
    from fork.athena import action_items, call_recorder, facts
    from fork.athena import routes as athena_routes
    from fork.beliefs import consolidation, evidence_intake, notes_evidence, structure
    from fork.hub import checklist_auto_archive
    from fork.ink import athena as ink_athena
    from fork.ink import transcribe
    from fork.insight import push, today
    from fork.mcp import trust
    from fork.routing import personal_read_grants, personal_store_escalation

    # hub_base reads the Settings page first; keep it empty so the variable decides.
    monkeypatch.setattr(src.settings, "get_setting", lambda key, default=None: "")
    # The Athena page's caller names the server it looked for when none is connected.
    monkeypatch.setattr(src.tool_utils, "get_mcp_manager", lambda: None)

    def routes_server():
        try:
            athena_routes._caller()
        except HTTPException as exc:
            return exc.detail
        return "connected"

    confirm = evidence_intake.settings
    return {
        "ODYSSEUS_PERSONAL_READ_GRANTS": [("personal_read_grants.enabled",
                                           personal_read_grants.enabled)],
        "ODYSSEUS_PERSONAL_STORE_ESCALATION": [("personal_store_escalation.enabled",
                                                personal_store_escalation.enabled)],
        "ODYSSEUS_TRUSTED_READONLY_MCP": [("trust.trusted_readonly_server_keys",
                                           trust.trusted_readonly_server_keys)],
        "FORK_CHECKLIST_ARCHIVE_MINUTES": [("checklist_auto_archive.archive_minutes",
                                            checklist_auto_archive.archive_minutes)],
        "EVIDENCE_NOTES": [("notes_evidence.enabled", notes_evidence.enabled)],
        "BELIEF_STRUCTURE": [("structure.enabled", structure.enabled)],
        "BELIEF_STRUCTURE_BATCH": [("structure.batch_size", structure.batch_size)],
        "BELIEF_CONSOLIDATE": [("consolidation.enabled", consolidation.enabled)],
        "BELIEF_CONFIRM_SOURCES": [("evidence_intake.settings", confirm)],
        "BELIEF_CONFIRM_TIMES": [("evidence_intake.settings", confirm)],
        "BELIEF_CONFIRM_DAYS": [("evidence_intake.settings", confirm)],
        "BELIEF_STALE_DAYS": [("evidence_intake.settings", confirm)],
        "INK_AUTO_SEND": [("transcribe.auto_send_enabled", transcribe.auto_send_enabled)],
        "APP_PUBLIC_URL": [("ink_athena.hub_base", ink_athena.hub_base)],
        "TODAY_INSIGHT_ENABLED": [("today.enabled", today.enabled)],
        "TODAY_INSIGHT_SKIP_LISTS": [("today.skip_lists", today.skip_lists)],
        "TODAY_INSIGHT_PUSH": [("push.enabled", push.enabled)],
        "MODULE_ACTION_ITEMS_SERVER": [("action_items.server_key", action_items.server_key)],
        "MODULE_NOTIFY_CHANNEL": [("action_items.notify_channel",
                                   action_items.notify_channel)],
        "MODULE_DIGEST_HOUR": [("action_items.digest_hour", action_items.digest_hour)],
        "MODULE_FILE_ITEMS": [("action_items.file_mode", action_items.file_mode)],
        "MODULE_FACTS_SYNC": [("facts.enabled", facts.enabled)],
        "SOUND_MODULE_SERVER": [("call_recorder.server_key", call_recorder.server_key),
                                ("athena_routes._caller", routes_server)],
        "SOUND_MODULE_CALL_DEVICE": [("call_recorder.device", call_recorder.device)],
    }


def _jsonable(value):
    if dataclasses.is_dataclass(value):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    return value


class _Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.name}: {record.getMessage()}")


def _call(read, logs: _Logs) -> dict:
    logs.lines.clear()
    try:
        out = {"result": _jsonable(read())}
    except Exception as exc:  # noqa: BLE001 -- a reader that raises is pinned too
        out = {"raises": type(exc).__name__}
    if logs.lines:
        out["logs"] = list(logs.lines)
    return out


def observe(monkeypatch) -> dict:
    readers = _readers(monkeypatch)
    logs = _Logs()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(logs)
    root.setLevel(logging.DEBUG)
    try:
        for name in ALL_NAMES:
            monkeypatch.delenv(name, raising=False)
        single: dict[str, dict] = {}
        for name, entries in readers.items():
            for label, read in entries:
                rows = {}
                for value in VALUES:
                    if value is UNSET:
                        monkeypatch.delenv(name, raising=False)
                    else:
                        monkeypatch.setenv(name, value)
                    rows["<unset>" if value is UNSET else repr(value)] = _call(read, logs)
                monkeypatch.delenv(name, raising=False)
                single[f"{name} {label}"] = rows
        pairs: dict[str, dict] = {}
        for new, old in PAIRS:
            for label, read in readers[new]:
                rows = {}
                for a in PAIR_VALUES:
                    for b in PAIR_VALUES:
                        for n, v in ((new, a), (old, b)):
                            if v is UNSET:
                                monkeypatch.delenv(n, raising=False)
                            else:
                                monkeypatch.setenv(n, v)
                        key = f"{new}={'<unset>' if a is UNSET else repr(a)} " \
                              f"{old}={'<unset>' if b is UNSET else repr(b)}"
                        rows[key] = _call(read, logs)
                monkeypatch.delenv(new, raising=False)
                monkeypatch.delenv(old, raising=False)
                pairs[f"{new}+{old} {label}"] = rows
        # Former names of one feature never answer for the other's setting.
        cross = {}
        for name, value in (("BARVIS_SOUND_SERVER", "other"),
                            ("SOUND_MODULE_SERVER", "other"),
                            ("BARVIS_ACTION_ITEMS_SERVER", "other"),
                            ("MODULE_ACTION_ITEMS_SERVER", "other")):
            monkeypatch.setenv(name, value)
            from fork.athena import action_items, call_recorder

            cross[name] = {"action_items.server_key": _call(action_items.server_key, logs),
                           "call_recorder.server_key": _call(call_recorder.server_key, logs)}
            monkeypatch.delenv(name)
        return {"single": single, "pairs": pairs, "cross": cross}
    finally:
        root.removeHandler(logs)
        root.setLevel(old_level)


def test_every_settings_reader_answers_as_recorded(monkeypatch):
    seen = observe(monkeypatch)
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for section in ("single", "pairs", "cross"):
        assert seen[section].keys() == expected[section].keys(), section
        for key, rows in expected[section].items():
            assert seen[section][key] == rows, key


if __name__ == "__main__":  # pragma: no cover -- records the fixture (old readers only)
    mp = pytest.MonkeyPatch()
    try:
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(json.dumps(observe(mp), indent=1, sort_keys=True,
                                      ensure_ascii=True) + "\n", encoding="utf-8")
    finally:
        mp.undo()
