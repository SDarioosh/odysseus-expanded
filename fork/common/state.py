"""Fork: per-owner JSON files, read and written the one way.

* :func:`owner_slug`: an owner name as a file-name part (``None`` or "" is ``default``;
  every character that is not a letter, digit or one of ``safe`` becomes ``_``).
* :func:`owner_file`: ``<DATA_DIR>/<prefix>_<slug>.json``, the per-owner state files.
* :func:`read_json`: the parsed file, or ``default`` when it is missing or not JSON.
* :func:`write_json` / :func:`write_atomic`: write a temp file next to the target
  (``.<name>.<pid>.<thread>.tmp``, unique per process and thread, so two writers never
  share one) and rename it over the target, so a reader sees the old file or the new
  one, never half of one. With ``clean_up`` (the default) a failed write removes its
  temp file; the few writers that never did keep doing without.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

SAFE = "-_.@"


def owner_slug(owner: str | None, *, safe: str = SAFE) -> str:
    return "".join(c if (c.isalnum() or c in safe) else "_" for c in (owner or "default"))


def owner_file(prefix: str, owner: str | None) -> Path:
    from src.constants import DATA_DIR

    return Path(DATA_DIR) / f"{prefix}_{owner_slug(owner)}.json"


def read_json(path: Path, default: Any = None) -> Any:
    """The file's JSON (``null`` included), or ``default`` when it can't be read or
    parsed."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def temp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def write_atomic(path: Path, data: str | bytes, *, clean_up: bool = True) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path(path)
    try:
        if isinstance(data, bytes):
            tmp.write_bytes(data)
        else:
            tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        if clean_up:
            tmp.unlink(missing_ok=True)
        raise


def write_json(path: Path, data: Any, *, indent: int | None = 1, sort_keys: bool = True,
               ensure_ascii: bool = True, separators: tuple[str, str] | None = None,
               clean_up: bool = True) -> None:
    """``data`` as JSON, written with :func:`write_atomic`. The text is made first, so
    data that isn't JSON leaves no file behind."""
    text = json.dumps(data, indent=indent, sort_keys=sort_keys, ensure_ascii=ensure_ascii,
                      separators=separators)
    write_atomic(path, text, clean_up=clean_up)
