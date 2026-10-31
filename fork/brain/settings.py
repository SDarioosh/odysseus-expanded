"""Fork: the brain's settings, read from the table in ``fork/common/settings.py``.

``FORK_BRAIN`` (on unless set to 0, false, no or off), ``FORK_BRAIN_MAX_ROUNDS`` (8, clamped to 1-20) and
``FORK_BRAIN_TOOL_GROUPS`` (``web,athena,workspace,documents,beliefs,chats,email``). Read at call
time, so a changed environment counts at once.
"""
from __future__ import annotations

from fork.common import settings

DEFAULT_GROUPS = ("web", "athena", "workspace", "documents", "beliefs", "chats", "email")


def enabled() -> bool:
    """True when the brain answers chat turns (``FORK_BRAIN``)."""
    return settings.brain()


def max_rounds() -> int:
    """Tool rounds per turn (``FORK_BRAIN_MAX_ROUNDS``)."""
    return settings.brain_max_rounds()


def tool_groups() -> tuple[str, ...]:
    """The tool groups offered (``FORK_BRAIN_TOOL_GROUPS``), in the order given."""
    return settings.brain_tool_groups()
