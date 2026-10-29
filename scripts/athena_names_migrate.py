#!/usr/bin/env python3
"""Fork: rename Athena's stored names in a hub data folder (``convo`` / ``odysseus-sound`` -> ``athena``).

The hub stored Athena under former names (``fork/common/athena_names.py``): its MCP
server is registered as ``convo``, and ``"odysseus-sound"`` is the label on filed notes
and the source of Athena's beliefs and memory entries. The code reads both names; this
script changes the stored ones, once, when the owner runs it:

* ``app.db``: the MCP server's name in ``mcp_servers`` (``convo`` -> ``athena``; its id,
  and with it every tool name, approval and per-tool disable keyed by the id, stays),
  the label of filed notes (``notes.label``) and the source of rows in the old
  ``memories`` table;
* ``beliefs.db``: the source of Athena's evidence and person ids (``evidence``,
  ``subject_sources``) and the event lines that name it (``from odysseus-sound``,
  ``back in odysseus-sound``, ``odysseus-sound <fact id>``), one transaction;
* ``memory.json``: the source of Athena's memory entries;
* skills (``skills/**/SKILL.md``, the old ``skills.json``): ``mcp:convo`` in
  ``requires_toolsets`` (the ``/athena`` and ``/context`` skills) -> ``mcp:athena``;
* ``--env-file`` (any number, e.g. the deploy's ``.env``): ``convo`` in the server
  settings ``ODYSSEUS_TRUSTED_READONLY_MCP``, ``MODULE_ACTION_ITEMS_SERVER``,
  ``SOUND_MODULE_SERVER`` and their ``BARVIS_*`` names -> ``athena``.

Never changed: chat history (old chats keep their tool names), calendar events (their
uids hash ``odysseus-sound:item:convo:<id>`` whatever the server is called), and the
action-items state files (keyed by item id; nothing in them names the server).

``--dry-run`` (the default) lists every row and file it would change, with counts.
``--apply`` first copies every file it will touch (a database with its ``-wal`` and
``-shm``) to ``<data dir>/athena_names_backups/<UTC time>/`` with a ``manifest.json``,
then changes them; run again, it finds nothing to change. ``--rollback <backup dir>``
puts every file back byte for byte (and removes a ``-wal``/``-shm`` that wasn't there).
``--apply`` and ``--rollback`` need the hub stopped (``--i-stopped-the-hub``), and refuse
while another process has a file in the data folder open.

Usage:
    python scripts/athena_names_migrate.py --data-dir ~/odysseus-data [--env-file .env]
    python scripts/athena_names_migrate.py --data-dir ... --apply --i-stopped-the-hub
    python scripts/athena_names_migrate.py --data-dir ... --rollback <backup dir> \\
        --i-stopped-the-hub
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from fork.common.athena_names import OLD_SERVER, OLD_SOURCE, SERVER, SOURCE

BACKUPS = "athena_names_backups"
MANIFEST = "manifest.json"
SQLITE_SIDE = ("-wal", "-shm")
SERVER_SETTINGS = ("ODYSSEUS_TRUSTED_READONLY_MCP", "MODULE_ACTION_ITEMS_SERVER",
                   "BARVIS_ACTION_ITEMS_SERVER", "SOUND_MODULE_SERVER", "BARVIS_SOUND_SERVER")
# event lines the ledger writes with a source in them (fork/beliefs/evidence_intake.py)
EVENT_DETAILS = ((f"from {OLD_SOURCE}", f"from {SOURCE}"),
                 (f"back in {OLD_SOURCE}", f"back in {SOURCE}"))
_EVENT_FACT = re.compile(rf"^{re.escape(OLD_SOURCE)} (\S+)$")
_SKILL_TOKEN = re.compile(rf"(mcp:){re.escape(OLD_SERVER)}(?![\w.-])", re.IGNORECASE)
_ENV_LINE = re.compile(r"^(?P<head>\s*(?:-\s*)?(?:export\s+)?(?P<key>[A-Z_][A-Z0-9_]*)=)"
                       r"(?P<value>.*?)(?P<tail>\s*)$")


@dataclass
class Store:
    """One file and what changes in it: ``items`` are (what, ids) lines for the report."""

    path: Path
    kind: str  # sqlite, json, text
    items: list[tuple[str, list[str]]] = field(default_factory=list)
    count: int = 0
    warnings: list[str] = field(default_factory=list)
    new_text: str | None = None  # json / text stores: the whole new file

    def add(self, what: str, ids: list[str]) -> None:
        if ids:
            self.items.append((what, ids))
            self.count += len(ids)


# ------------------------------------------------------------------------ planning
def _table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                      (name,)).fetchone() is not None


class _CopyConnection(sqlite3.Connection):
    """A connection to a temporary copy; closing it removes the copy."""

    folder: str = ""

    def close(self) -> None:
        super().close()
        shutil.rmtree(self.folder, ignore_errors=True)


def _ro(path: Path) -> sqlite3.Connection:
    """Read a database without touching it: even a read-only open of a WAL database can
    leave ``-wal``/``-shm`` files beside it, so the dry run reads a copy (with its log)."""
    folder = tempfile.mkdtemp(prefix="athena-names-")
    for suffix in ("", "-wal"):  # SQLite rebuilds the -shm index from the log
        side = Path(str(path) + suffix)
        if side.is_file():
            shutil.copy2(side, Path(folder) / (path.name + suffix))
    db = sqlite3.connect(str(Path(folder) / path.name), factory=_CopyConnection)
    db.folder = folder
    db.row_factory = sqlite3.Row
    return db


def plan_app_db(path: Path) -> Store:
    store = Store(path, "sqlite")
    db = _ro(path)
    try:
        if _table(db, "mcp_servers"):
            rows = db.execute("SELECT id, name FROM mcp_servers").fetchall()
            old = [r["id"] for r in rows if (r["name"] or "").strip().casefold() == OLD_SERVER]
            new = [r["id"] for r in rows if (r["name"] or "").strip().casefold() == SERVER]
            if old and new:
                store.warnings.append(
                    f"mcp_servers: both a {OLD_SERVER!r} ({', '.join(old)}) and an {SERVER!r} "
                    f"server ({', '.join(new)}) are registered; left as they are")
            else:
                store.add(f"mcp_servers.name {OLD_SERVER} -> {SERVER}", old)
        if _table(db, "notes"):
            store.add(f"notes.label {OLD_SOURCE} -> {SOURCE}",
                      [r[0] for r in db.execute("SELECT id FROM notes WHERE label = ? "
                                                "ORDER BY id", (OLD_SOURCE,))])
        if _table(db, "memories"):
            store.add(f"memories.source {OLD_SOURCE} -> {SOURCE}",
                      [r[0] for r in db.execute("SELECT id FROM memories WHERE source = ? "
                                                "ORDER BY id", (OLD_SOURCE,))])
    finally:
        db.close()
    return store


def plan_beliefs_db(path: Path) -> Store:
    store = Store(path, "sqlite")
    db = _ro(path)
    try:
        if _table(db, "evidence"):
            clash = db.execute(
                "SELECT COUNT(*) FROM evidence o JOIN evidence n ON n.owner = o.owner AND "
                "n.source_id = o.source_id AND n.source = ? WHERE o.source = ?",
                (SOURCE, OLD_SOURCE)).fetchone()[0]
            if clash:
                store.warnings.append(f"evidence: {clash} Athena fact ids are stored under "
                                      "both names; beliefs.db left as it is")
                store.items, store.count = [], 0
                return store
            store.add(f"evidence.source {OLD_SOURCE} -> {SOURCE}",
                      [str(r[0]) for r in db.execute("SELECT id FROM evidence WHERE source = ? "
                                                     "ORDER BY id", (OLD_SOURCE,))])
        if _table(db, "subject_sources"):
            clash = db.execute(
                "SELECT COUNT(*) FROM subject_sources o JOIN subject_sources n ON "
                "n.owner = o.owner AND n.source_person_id = o.source_person_id AND "
                "n.source = ? WHERE o.source = ?", (SOURCE, OLD_SOURCE)).fetchone()[0]
            if clash:
                store.warnings.append(f"subject_sources: {clash} person ids are stored under "
                                      "both names; beliefs.db left as it is")
                store.items, store.count = [], 0
                return store
            store.add(f"subject_sources.source {OLD_SOURCE} -> {SOURCE}",
                      [str(r[0]) for r in db.execute(
                          "SELECT rowid FROM subject_sources WHERE source = ? ORDER BY rowid",
                          (OLD_SOURCE,))])
        if _table(db, "events"):
            ids = [str(r["id"]) for r in db.execute("SELECT id, detail FROM events ORDER BY id")
                   if _event_detail(r["detail"]) is not None]
            store.add(f"events.detail naming {OLD_SOURCE}", ids)
    finally:
        db.close()
    return store


def _event_detail(detail: str | None) -> str | None:
    """The event line with the new name, or None when it doesn't name the old one."""
    for old, new in EVENT_DETAILS:
        if detail == old:
            return new
    m = _EVENT_FACT.match(detail or "")
    return f"{SOURCE} {m[1]}" if m else None


def plan_memory(path: Path) -> Store:
    store = Store(path, "json")
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        store.warnings.append(f"can't read it ({type(e).__name__}); left as it is")
        return store
    if not isinstance(entries, list):
        return store
    ids = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("source") == OLD_SOURCE:
            entry["source"] = SOURCE
            ids.append(str(entry.get("id") or "?"))
    store.add(f"source {OLD_SOURCE} -> {SOURCE}", ids)
    if ids:  # as src/memory.py MemoryManager.save writes it
        store.new_text = json.dumps(entries, ensure_ascii=False, indent=2)
    return store


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    return text[:end], text[end:]


def plan_skill_md(path: Path) -> Store:
    store = Store(path, "text")
    text = path.read_text(encoding="utf-8")
    parts = _split_frontmatter(text)
    if parts is None:
        return store
    head, rest = parts
    lines = head.splitlines(keepends=True)
    changed = []
    in_toolsets = False
    for i, line in enumerate(lines):
        key = re.match(r"^([A-Za-z_]+)\s*:", line)
        if key:
            in_toolsets = key[1] in ("requires_toolsets", "fallback_for_toolsets")
        if in_toolsets and _SKILL_TOKEN.search(line):
            lines[i] = _SKILL_TOKEN.sub(lambda m: m[1] + SERVER, line)
            changed.append(f"line {i + 1}")
    store.add(f"requires_toolsets mcp:{OLD_SERVER} -> mcp:{SERVER}", changed)
    if changed:
        store.new_text = "".join(lines) + rest
    return store


def plan_skills_json(path: Path) -> Store:
    store = Store(path, "json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return store
    rows = data if isinstance(data, list) else data.get("skills") if isinstance(data, dict) \
        else None
    names = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for key in ("requires_toolsets", "fallback_for_toolsets"):
            values = row.get(key)
            if isinstance(values, list) and any(
                    isinstance(v, str) and _SKILL_TOKEN.fullmatch(v.strip()) for v in values):
                row[key] = [f"mcp:{SERVER}" if isinstance(v, str)
                            and _SKILL_TOKEN.fullmatch(v.strip()) else v for v in values]
                names.append(str(row.get("name") or row.get("id") or "?"))
    store.add(f"requires_toolsets mcp:{OLD_SERVER} -> mcp:{SERVER}", names)
    if names:
        store.new_text = json.dumps(data, ensure_ascii=False, indent=2)
    return store


def plan_env_file(path: Path) -> Store:
    store = Store(path, "text")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = []
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        m = _ENV_LINE.match(body)
        if not m or m["key"] not in SERVER_SETTINGS:
            continue
        value = m["value"]
        quote = value[:1] if value[:1] in "\"'" and value[-1:] == value[:1] and len(value) > 1 \
            else ""
        inner = value[1:-1] if quote else value
        parts = inner.split(",")
        new = [p.replace(p.strip(), SERVER) if p.strip().casefold() == OLD_SERVER else p
               for p in parts]
        if new != parts:
            lines[i] = (m["head"] + quote + ",".join(new) + quote + m["tail"]
                        + line[len(body):])
            changed.append(f"line {i + 1} {m['key']}")
    store.add(f"{OLD_SERVER} -> {SERVER}", changed)
    if changed:
        store.new_text = "".join(lines)
    return store


def plan(data_dir: Path, env_files: list[Path]) -> list[Store]:
    stores = []
    if (data_dir / "app.db").is_file():
        stores.append(plan_app_db(data_dir / "app.db"))
    if (data_dir / "beliefs.db").is_file():
        stores.append(plan_beliefs_db(data_dir / "beliefs.db"))
    if (data_dir / "memory.json").is_file():
        stores.append(plan_memory(data_dir / "memory.json"))
    skills = data_dir / "skills"
    if skills.is_dir():
        stores += [plan_skill_md(p) for p in sorted(skills.rglob("SKILL.md"))]
    if (data_dir / "skills.json").is_file():
        stores.append(plan_skills_json(data_dir / "skills.json"))
    stores += [plan_env_file(p) for p in env_files]
    return stores


# ------------------------------------------------------------------------- report
def report(stores: list[Store], data_dir: Path) -> str:
    out = []
    for store in stores:
        if not store.items and not store.warnings:
            continue
        try:
            name = str(store.path.relative_to(data_dir))
        except ValueError:
            name = str(store.path)
        out.append(f"{name}: {store.count} to change")
        for what, ids in store.items:
            out.append(f"  {what}: {len(ids)} ({', '.join(ids)})")
        for warning in store.warnings:
            out.append(f"  warning: {warning}")
    total = sum(s.count for s in stores)
    files = sum(1 for s in stores if s.count)
    out.append(f"Total: {total} changes in {files} files." if total
               else "Nothing to change: the stored names already read athena.")
    return "\n".join(out)


# -------------------------------------------------------------------------- apply
def _files_of(store: Store) -> list[Path]:
    if store.kind == "sqlite":
        return [store.path, *(Path(str(store.path) + s) for s in SQLITE_SIDE)]
    return [store.path]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backup(stores: list[Store], data_dir: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S.%fZ")
    root = data_dir / BACKUPS / stamp
    root.mkdir(parents=True)
    files = []
    for n, path in enumerate(p for s in stores if s.count for p in _files_of(s)):
        entry = {"path": str(path.resolve()), "existed": path.is_file()}
        if entry["existed"]:
            name = f"{n:03d}-{path.name}"
            shutil.copy2(path, root / name)
            entry.update(backup=name, sha256=_sha(path))
        files.append(entry)
    (root / MANIFEST).write_text(json.dumps({"created": stamp, "data_dir": str(data_dir.resolve()),
                                             "files": files}, indent=1) + "\n", encoding="utf-8")
    return root


def _write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.athena-names.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _apply_sqlite(store: Store) -> None:
    db = sqlite3.connect(str(store.path), timeout=30, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            if store.path.name == "app.db":
                for what, ids in store.items:
                    table = what.split(".", 1)[0]
                    marks = ", ".join("?" * len(ids))
                    if table == "mcp_servers":
                        db.execute(f"UPDATE mcp_servers SET name = ? WHERE id IN ({marks})",
                                   (SERVER, *ids))
                    elif table == "notes":
                        db.execute("UPDATE notes SET label = ? WHERE label = ?",
                                   (SOURCE, OLD_SOURCE))
                    elif table == "memories":
                        db.execute("UPDATE memories SET source = ? WHERE source = ?",
                                   (SOURCE, OLD_SOURCE))
            else:
                db.execute("UPDATE evidence SET source = ? WHERE source = ?",
                           (SOURCE, OLD_SOURCE))
                if _table(db, "subject_sources"):
                    db.execute("UPDATE subject_sources SET source = ? WHERE source = ?",
                               (SOURCE, OLD_SOURCE))
                if _table(db, "events"):
                    for row in db.execute("SELECT id, detail FROM events").fetchall():
                        new = _event_detail(row[1])
                        if new is not None:
                            db.execute("UPDATE events SET detail = ? WHERE id = ?", (new, row[0]))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    finally:
        db.close()


def apply(stores: list[Store]) -> None:
    for store in stores:
        if not store.count:
            continue
        if store.kind == "sqlite":
            _apply_sqlite(store)
        else:
            _write_text(store.path, store.new_text or "")


def rollback(backup_dir: Path) -> list[str]:
    manifest = json.loads((backup_dir / MANIFEST).read_text(encoding="utf-8"))
    done = []
    for entry in manifest["files"]:
        path = Path(entry["path"])
        if entry["existed"]:
            source = backup_dir / entry["backup"]
            if _sha(source) != entry["sha256"]:
                raise SystemExit(f"backup file {source} doesn't match its manifest; "
                                 "nothing more restored")
            tmp = path.with_name(f".{path.name}.athena-names.tmp")
            shutil.copy2(source, tmp)
            os.replace(tmp, path)
            done.append(f"restored {path}")
        elif path.exists():
            path.unlink()
            done.append(f"removed {path} (it wasn't there before)")
    return done


# --------------------------------------------------------------------- the guard
def open_by_others(data_dir: Path) -> list[int]:
    """Processes (other than this one) with a file in ``data_dir`` open, as far as
    ``/proc`` lets us see (Linux; another user's processes may be hidden)."""
    root = str(data_dir.resolve()) + os.sep
    pids = []
    proc = Path("/proc")
    if not proc.is_dir():
        return pids
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith(root):
                pids.append(int(entry.name))
                break
    return sorted(pids)


def _guard(args: argparse.Namespace, data_dir: Path) -> None:
    if not args.i_stopped_the_hub:
        raise SystemExit("stop the hub that uses this data folder first (a running hub "
                         "writes these files), then add --i-stopped-the-hub")
    pids = open_by_others(data_dir)
    if pids:
        raise SystemExit(f"processes {', '.join(map(str, pids))} have files in {data_dir} "
                         "open; stop the hub first")


# ---------------------------------------------------------------------------- main
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", required=True, type=Path,
                    help="the hub's data folder (ODYSSEUS_DATA_DIR), or a copy of it")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="only list (the default)")
    mode.add_argument("--apply", action="store_true", help="back up, then change")
    mode.add_argument("--rollback", type=Path, metavar="BACKUP_DIR",
                      help="put back the files of one backup")
    ap.add_argument("--env-file", action="append", type=Path, default=[],
                    help="an env file whose server settings name convo (repeatable)")
    ap.add_argument("--i-stopped-the-hub", action="store_true",
                    help="confirm the hub using this data folder is stopped")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_dir = args.data_dir.expanduser()
    if not data_dir.is_dir():
        print(f"no data folder at {data_dir}", file=sys.stderr)
        return 2
    if args.rollback is not None:
        _guard(args, data_dir)
        for line in rollback(args.rollback.expanduser()):
            print(line)
        return 0
    env_files = [p.expanduser() for p in args.env_file]
    for p in env_files:
        if not p.is_file():
            print(f"no env file at {p}", file=sys.stderr)
            return 2
    stores = plan(data_dir, env_files)
    print(f"Athena names in {data_dir} ({'apply' if args.apply else 'dry run: nothing is changed'})")
    print(report(stores, data_dir))
    if not args.apply or not any(s.count for s in stores):
        if not args.apply and any(s.count for s in stores):
            print("Run again with --apply --i-stopped-the-hub to change them "
                  "(a backup is made first).")
        return 0
    _guard(args, data_dir)
    where = backup(stores, data_dir)
    print(f"backup: {where}")
    apply(stores)
    print(f"done. To undo: python scripts/athena_names_migrate.py --data-dir {data_dir} "
          f"--rollback {where} --i-stopped-the-hub")
    return 0


if __name__ == "__main__":
    sys.exit(main())
