"""Fork: every setting the fork's code reads from the environment, in one table.

Each row of ``SETTINGS`` gives a variable's name, its former names (still read, after
the current one), its type, the text used when it is unset, the fork files that use it,
how the production override passes it (``docker/fork.override.yml``) and one line of
documentation. The accessors below are the only fork code that reads these variables;
the features call them instead of ``os.environ``.

Reading rules, the same for every row unless the row says otherwise:

* Read at call time, never cached, so a changed environment (and a test's
  ``monkeypatch.setenv``) counts at once.
* The current name first, then each former name; an empty value counts as unset
  (``empty_is_unset``), so ``${NAME:-}`` in the override, which passes "" when ``.env``
  doesn't set it, means the code's default. ``TODAY_INSIGHT_SKIP_LISTS`` is the one
  exception: there "" means "skip no lists".
* :func:`flag` is the one on/off parser. A setting that is on by default is off only for
  ``0``, ``false``, ``no`` or ``off``; one that is off by default is on only for ``1``,
  ``true``, ``yes`` or ``on``; case and surrounding whitespace never matter.

Two settings name the Athena MCP server, and they stay two (each feature reads the one
it always read): :func:`athena_server` (to-dos, facts, today insight, the beliefs page,
MCP session recovery) and :func:`athena_recorder_server` (uploads, the Athena page,
meeting bot, voice enrollment and the other module HTTP routes). Both default to
``convo``.

``scripts/generate_env_reference.py`` finds the ``ODYSSEUS_*`` rows (each row is a
``_setting("NAME", "default", ...)`` call, which it reads as an environment read with
that default), and ``tests/fork/test_fork_settings.py`` checks the table against the
production override and ``docs/fork/settings.md``, which is generated from it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

FALSY = frozenset({"0", "false", "no", "off"})
TRUTHY = frozenset({"1", "true", "yes", "on"})

# How docker/fork.override.yml passes a setting into the production container.
PASS_EMPTY = "empty"  # - NAME=${NAME:-}  ("" when .env doesn't set it)
PASS_BARE = "bare"    # - NAME            (absent when .env doesn't set it)


@dataclass(frozen=True)
class Setting:
    name: str
    default: str | None
    type: str
    doc: str
    used_in: tuple[str, ...]
    old: tuple[str, ...] = ()
    empty_is_unset: bool = True
    passed: str = PASS_EMPTY  # PASS_EMPTY, PASS_BARE, or the fixed value the override sets

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.old)

    def value(self) -> str | None:
        """The raw text: the first name that is set (and not empty, unless empty is a
        value of its own here), else ``default``."""
        for name in self.names:
            text = os.environ.get(name)
            if text is None or (text == "" and self.empty_is_unset):
                continue
            return text
        return self.default


SETTINGS: dict[str, Setting] = {}


def _setting(name: str, default: str | None, type: str, doc: str, *,
             used_in: tuple[str, ...], old: tuple[str, ...] = (),
             empty_is_unset: bool = True, passed: str = PASS_EMPTY) -> Setting:
    if SETTINGS.get(name) is not None:
        raise ValueError(f"{name} is listed twice")
    row = Setting(name, default, type, doc, tuple(used_in), tuple(old), empty_is_unset,
                  passed)
    SETTINGS[name] = row
    return row


def flag(value: str | None, default: bool) -> bool:
    """The one on/off parser (see the module doc)."""
    text = (value or "").strip().lower()
    return text not in FALSY if default else text in TRUTHY


# ---------------------------------------------------------------------- the table
# Athena (docs/fork/modules/athena.md)
ACTION_ITEMS_SERVER = _setting(
    "MODULE_ACTION_ITEMS_SERVER", "convo", "text",
    "The Athena MCP server (name or id) for to-dos, facts, today insight and the "
    "beliefs page; compared case-insensitively.",
    used_in=("fork/athena/action_items.py", "fork/athena/facts.py", "fork/insight/today.py",
             "fork/mcp/session_recovery.py", "fork/routes/beliefs_routes.py"),
    old=("BARVIS_ACTION_ITEMS_SERVER",))
NOTIFY_CHANNEL = _setting(
    "MODULE_NOTIFY_CHANNEL", "ntfy", "ntfy, browser or off",
    "Where Athena's to-do pushes go; anything else means ntfy.",
    used_in=("fork/athena/action_items.py",), old=("BARVIS_NOTIFY_CHANNEL",))
DIGEST_HOUR = _setting(
    "MODULE_DIGEST_HOUR", "8", "hour 0-23 or off",
    "Local hour after which the daily pushes go out, and the time of to-dos with a date "
    "only; off or none: no daily pushes. Out of range is clamped; not a number: 8.",
    used_in=("fork/athena/action_items.py",), old=("BARVIS_DIGEST_HOUR",))
FILE_ITEMS = _setting(
    "MODULE_FILE_ITEMS", "explicit", "explicit, auto or off",
    "Which voice-note items are filed as to-dos and events: explicit ones, all (auto "
    "or all) or none (off, no, false, 0).",
    used_in=("fork/athena/action_items.py",), old=("BARVIS_FILE_ITEMS",))
FACTS_SYNC = _setting(
    "MODULE_FACTS_SYNC", "on", "on/off",
    "Copy Athena's confirmed facts into the belief ledger.",
    used_in=("fork/athena/facts.py",), old=("BARVIS_FACTS_SYNC",))
RECORDER_SERVER = _setting(
    "SOUND_MODULE_SERVER", "convo", "text",
    "The Athena MCP server (name or id) for uploads, the Athena page, the meeting bot "
    "and the module's other HTTP routes.",
    used_in=("fork/athena/call_recorder.py", "fork/athena/routes.py"),
    old=("BARVIS_SOUND_SERVER",))
CALL_DEVICE = _setting(
    "SOUND_MODULE_CALL_DEVICE", "computer", "device name",
    "The device name recordings from the hub are uploaded under; an invalid name "
    "means computer.",
    used_in=("fork/athena/call_recorder.py",))
# Beliefs (docs/fork/beliefs.md)
CONFIRM_SOURCES = _setting(
    "BELIEF_CONFIRM_SOURCES", "2", "integer >= 1",
    "Distinct sources that confirm a belief.", used_in=("fork/beliefs/evidence_intake.py",))
CONFIRM_TIMES = _setting(
    "BELIEF_CONFIRM_TIMES", "3", "integer >= 1",
    "Sightings that confirm a belief (with BELIEF_CONFIRM_DAYS).",
    used_in=("fork/beliefs/evidence_intake.py",))
CONFIRM_DAYS = _setting(
    "BELIEF_CONFIRM_DAYS", "2", "integer >= 1",
    "Distinct days those sightings must span.", used_in=("fork/beliefs/evidence_intake.py",))
STALE_DAYS = _setting(
    "BELIEF_STALE_DAYS", "180", "integer >= 1",
    "Days without a sighting after which a belief goes stale.",
    used_in=("fork/beliefs/evidence_intake.py",))
CONSOLIDATE = _setting(
    "BELIEF_CONSOLIDATE", "on", "on/off",
    "Merge beliefs that say the same thing.", used_in=("fork/beliefs/consolidation.py",))
STRUCTURE = _setting(
    "BELIEF_STRUCTURE", "on", "on/off",
    "Give beliefs an attribute, value and validity with the local model.",
    used_in=("fork/beliefs/structure.py",))
STRUCTURE_BATCH = _setting(
    "BELIEF_STRUCTURE_BATCH", "5", "integer 1-10",
    "Beliefs per model call when structuring; clamped to 1-10.",
    used_in=("fork/beliefs/structure.py",))
EVIDENCE_NOTES = _setting(
    "EVIDENCE_NOTES", "off", "on/off",
    "Read facts from the owner's notes into the belief ledger.",
    used_in=("fork/beliefs/notes_evidence.py",))
# Today insight (docs/fork/today-insight.md)
TODAY_INSIGHT = _setting(
    "TODAY_INSIGHT_ENABLED", "on", "on/off",
    "Write the daily insight.", used_in=("fork/insight/today.py",))
SKIP_LISTS = _setting(
    "TODAY_INSIGHT_SKIP_LISTS", "Shopping,Groceries,Grocery", "comma-separated list titles",
    "Lists left out of the insight unless the note has a due date; empty: none.",
    used_in=("fork/insight/today.py",), empty_is_unset=False, passed=PASS_BARE)
INSIGHT_PUSH = _setting(
    "TODAY_INSIGHT_PUSH", "off", "on/off",
    "Push the daily insight to the phone.", used_in=("fork/insight/push.py",))
# Agent turns (docs/fork/personal-context.md, docs/fork/features/notes-and-tasks.md)
PERSONAL_READ_GRANTS = _setting(
    "ODYSSEUS_PERSONAL_READ_GRANTS", "1", "on/off",
    "Grant read-only access to the owner's personal stores on every turn.",
    used_in=("fork/routing/personal_read_grants.py",))
PERSONAL_STORE_ESCALATION = _setting(
    "ODYSSEUS_PERSONAL_STORE_ESCALATION", "1", "on/off",
    "Run chat-mode lookups in the owner's personal stores in agent mode.",
    used_in=("fork/routing/personal_store_escalation.py",))
TRUSTED_READONLY_MCP = _setting(
    "ODYSSEUS_TRUSTED_READONLY_MCP", "", "comma-separated server names or ids",
    "MCP servers whose readOnlyHint tools run without approval.",
    used_in=("fork/mcp/trust.py",), passed="convo")
CHECKLIST_ARCHIVE_MINUTES = _setting(
    "FORK_CHECKLIST_ARCHIVE_MINUTES", "", "minutes or off",
    "Minutes a finished checklist waits before it is archived (default 10); 0, off, "
    "no, false or none: never.",
    used_in=("fork/hub/checklist_auto_archive.py",))
# Handwritten notes (docs/fork/handwritten-notes.md)
INK_AUTO_SEND = _setting(
    "INK_AUTO_SEND", "", "on/off",
    "Send pages the model read without [?] to Athena without review.",
    used_in=("fork/ink/transcribe.py",))
APP_PUBLIC_URL = _setting(
    "APP_PUBLIC_URL", "", "URL",
    "The hub's address for links in notes sent to Athena when Settings has no Public "
    "URL (upstream's MCP OAuth redirect reads it too).",
    used_in=("fork/ink/athena.py",))


# --------------------------------------------------------------------- accessors
def athena_server() -> str:
    """``MODULE_ACTION_ITEMS_SERVER``, else ``BARVIS_ACTION_ITEMS_SERVER``, else
    ``convo``; stripped and case-folded."""
    return (ACTION_ITEMS_SERVER.value() or "").strip().casefold()


def athena_recorder_server() -> str:
    """``SOUND_MODULE_SERVER``, else ``BARVIS_SOUND_SERVER``, else ``convo``; stripped
    (case kept)."""
    return (RECORDER_SERVER.value() or "").strip()


def notify_channel() -> str:
    value = (NOTIFY_CHANNEL.value() or "").strip().lower()
    return value if value in {"ntfy", "browser", "off"} else "ntfy"


def digest_hour() -> int | None:
    value = (DIGEST_HOUR.value() or "").strip().lower()
    if value in {"off", "none", ""}:
        return None
    try:
        return max(0, min(23, int(value)))
    except ValueError:
        return 8


def file_items() -> str:
    value = (FILE_ITEMS.value() or "").strip().lower()
    if value in FALSY:
        return "off"
    return "auto" if value in {"auto", "all"} else "explicit"


def facts_sync() -> bool:
    return flag(FACTS_SYNC.value(), True)


def call_device(log: logging.Logger = logger, *, valid=None) -> str:
    """The upload device name; ``valid(name)`` false logs a warning on ``log`` and
    gives the default."""
    name = (CALL_DEVICE.value() or "").strip() or CALL_DEVICE.default
    if valid is not None and not valid(name):
        log.warning("%s is not a valid device name; using %r", CALL_DEVICE.name,
                    CALL_DEVICE.default)
        return CALL_DEVICE.default
    return name


def _int(row: Setting, low: int, high: int | None = None) -> int:
    default = int(row.default)
    try:
        value = int((row.value() or "").strip() or default)
    except ValueError:
        value = default
    value = max(low, value)
    return value if high is None else min(high, value)


def belief_confirm_sources() -> int:
    return _int(CONFIRM_SOURCES, 1)


def belief_confirm_times() -> int:
    return _int(CONFIRM_TIMES, 1)


def belief_confirm_days() -> int:
    return _int(CONFIRM_DAYS, 1)


def belief_stale_days() -> int:
    return _int(STALE_DAYS, 1)


def belief_consolidate() -> bool:
    return flag(CONSOLIDATE.value(), True)


def belief_structure() -> bool:
    return flag(STRUCTURE.value(), True)


def belief_structure_batch() -> int:
    return _int(STRUCTURE_BATCH, 1, 10)


def evidence_notes() -> bool:
    return flag(EVIDENCE_NOTES.value(), False)


def today_insight() -> bool:
    return flag(TODAY_INSIGHT.value(), True)


def today_insight_skip_lists() -> str:
    """The raw comma-separated titles ("" when set empty: skip no lists)."""
    return SKIP_LISTS.value() or ""


def today_insight_push() -> bool:
    return flag(INSIGHT_PUSH.value(), False)


def personal_read_grants() -> bool:
    return flag(PERSONAL_READ_GRANTS.value(), True)


def personal_store_escalation() -> bool:
    return flag(PERSONAL_STORE_ESCALATION.value(), True)


def trusted_readonly_mcp() -> frozenset[str]:
    raw = TRUSTED_READONLY_MCP.value() or ""
    return frozenset(part.strip().casefold() for part in raw.split(",") if part.strip())


CHECKLIST_ARCHIVE_DEFAULT = 10.0


def checklist_archive_minutes(log: logging.Logger = logger) -> float | None:
    """Minutes a finished checklist waits, or ``None`` when turned off; a value that is
    not a number logs a warning on ``log`` and gives the default."""
    raw = (CHECKLIST_ARCHIVE_MINUTES.value() or "").strip().lower()
    if not raw:
        return CHECKLIST_ARCHIVE_DEFAULT
    if raw in {"off", "no", "false", "none"}:
        return None
    try:
        minutes = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", CHECKLIST_ARCHIVE_MINUTES.name, raw,
                    CHECKLIST_ARCHIVE_DEFAULT)
        return CHECKLIST_ARCHIVE_DEFAULT
    return minutes if minutes > 0 else None


def ink_auto_send() -> bool:
    return flag(INK_AUTO_SEND.value(), False)


def app_public_url() -> str:
    return APP_PUBLIC_URL.value() or ""
