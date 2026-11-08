"""Fork: an owner name as the key the fork's per-owner stores use.

:func:`owner_key`: the name trimmed and case-folded (``None`` is ``""``), so "Alice" and
"alice " are one owner. The voice stores (``fork/voice/``) key their rows by it.
"""

from __future__ import annotations

from typing import Any


def owner_key(owner: Any) -> str:
    return str(owner or "").strip().casefold()
