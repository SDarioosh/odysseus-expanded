"""Fork: Athena's names in the owner's data, the old and the new, in one place.

The hub stored Athena under former names: its MCP server is registered as ``convo``
(Settings → MCP, approval cards, the ``/athena`` skill's ``mcp:convo``,
``ODYSSEUS_TRUSTED_READONLY_MCP=convo``), and the string ``"odysseus-sound"`` is the
label on filed notes, the source of Athena's beliefs (Wisdom) and of their memory
entries. The owner sees these, so they read ``athena`` now:

* **The code reads both.** A server named ``athena`` or ``convo`` is Athena's, whichever
  of the two a setting, the trusted list or a skill names (:func:`server_aliases`); a
  label or source of either name is Athena's (:func:`is_source`).
* **New data is written as** ``athena``, with one exception: the belief ledger keeps
  writing the name an owner's ledger already holds (``fork/beliefs/ledger_db.py``,
  ``stored_module_source``), so one ledger never holds both and one Athena fact is never
  counted as two sources.
* **The stored values change only when the owner runs**
  ``scripts/athena_names_migrate.py`` (backup, dry run, rollback).
* **Calendar event ids never change.** A filed event's uid hashes
  ``odysseus-sound:item:convo:<item id>`` (:data:`EVENT_UID_PREFIX`), whatever the server
  is called now, so no event is filed twice.

Nothing here imports the rest of the hub: scripts use it as well.
"""

from __future__ import annotations

from collections.abc import Iterable

SERVER = "athena"
OLD_SERVER = "convo"
SERVER_NAMES = (SERVER, OLD_SERVER)

SOURCE = "athena"  # the label on filed notes, the belief and the memory source
OLD_SOURCE = "odysseus-sound"
SOURCES = (SOURCE, OLD_SOURCE)

# The input of a filed calendar event's uid, frozen: the server's name in it stays
# ``convo`` when the server is called ``athena`` (fork/athena/action_items_state.py).
EVENT_UID_PREFIX = f"{OLD_SOURCE}:item:{OLD_SERVER}:"


def is_source(value: object) -> bool:
    """Whether a stored label or source (as stored, exact) is Athena's."""
    return isinstance(value, str) and value in SOURCES


def server_aliases(key: object) -> tuple[str, ...]:
    """The casefolded MCP server names ``key`` stands for, ``key`` itself first:
    ``athena`` and ``convo`` stand for each other, any other name only for itself."""
    text = str(key or "").strip().casefold()
    if not text:
        return ()
    if text in SERVER_NAMES:
        return (text, *(n for n in SERVER_NAMES if n != text))
    return (text,)


def with_server_aliases(keys: Iterable[object]) -> frozenset[str]:
    """Casefolded ``keys`` plus the other name of Athena's server when one is in it."""
    return frozenset(alias for key in keys or () for alias in server_aliases(key))


def event_uid_server(server_key: str) -> str:
    """The server part of a filed event's uid input: ``convo`` for Athena's server under
    either name, any other configured server name or id as it is (as before)."""
    key = str(server_key or "").strip().casefold()
    return OLD_SERVER if key in SERVER_NAMES else key
