"""Fork: the per-owner JSON state files, pinned for many owners and file contents.

For each feature that keeps a JSON file per owner, records the file's path for a table
of owner names (the owner slug), what loading returns for a table of file contents, the
exact bytes a save writes, and what a save that fails leaves behind (the temp file's
name, with the process and thread ids replaced). Compared with
``fixtures/state_files.json``, recorded before the copies were replaced by
``fork/common/state.py``.
"""
from __future__ import annotations

import itertools
import json
import os
import threading
from pathlib import Path

import pytest

FIXTURE = Path(__file__).with_name("fixtures") / "state_files.json"
DAY = "2026-10-29"

OWNERS = [None, "", "alice", "Alice.Smith@example.org", "a/b", "../x", ".", "..",
          "a b", "äöü", "名前", "a\\b", "x:y", "-_.@", "t\tab",
          "a.b.c", "o'neil", "#1", "a" * 70]

CONTENTS = [None, "", "{", "null", "[]", "[1]", '"s"', "1", "{}", '{"items": []}',
            '{"items": {"7": {"status": "open"}}}', '{"notes": 5}', '{"notes": {"n": 1}}',
            '{"tried": []}', '{"tried": {"b1": "x"}}', '{"events": {"1": {}, "2": 3}}',
            '{"events": [1]}', '{"x": "é", "date": "2026-10-29"}', "﻿{}",
            "{} trailing", b"\xff\xfe".decode("latin-1")]

DATA = {"date": DAY, "zeta": 1, "alpha": "été ☃", "items": {"b": [1, 2],
                                                                        "a": None}}


def _features():
    from fork.athena import action_items, facts, meeting_auto_join
    from fork.beliefs import notes_evidence, structure
    from fork.insight import outcomes, routines, today, weights

    return {
        "notes_evidence": (notes_evidence._state_path, notes_evidence.load_state,
                           notes_evidence.save_state),
        "structure": (structure._state_path, structure.load_state, structure.save_state),
        "action_items": (action_items._state_path, action_items.load_state,
                         action_items.save_state),
        "facts": (facts._state_path, facts.load_state, facts.save_state),
        "today": (lambda o: today.insight_path(o, DAY), lambda o: today.load(o, DAY),
                  today.save),
        "outcomes": (lambda o: outcomes.outcome_path(o, DAY), lambda o: outcomes.load(o, DAY),
                     outcomes.save),
        "routines": (routines.cache_path, routines._load_cache, routines._save_cache),
        "weights": (weights.weights_path, lambda o: weights._read_json(weights.weights_path(o)),
                    lambda o, d: weights._write_json(weights.weights_path(o), d)),
        "meeting_auto_join": (meeting_auto_join.state_path, meeting_auto_join.load,
                              meeting_auto_join.save),
    }


def _writers():
    from fork.ink import notes as ink

    return {"ink_json": ink._write_json,
            "ink_bytes": lambda path, data: ink._write_bytes(path, json.dumps(data).encode())}


def _listing(root: Path) -> list[str]:
    ids = (str(os.getpid()), str(threading.get_ident()))
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            name = str(p.relative_to(root))
            out.append(name.replace(ids[0], "<pid>").replace(ids[1], "<tid>"))
    return out


def _call(fn, *args):
    try:
        return {"result": fn(*args)}
    except Exception as exc:  # noqa: BLE001 -- a raise is pinned too
        return {"raises": type(exc).__name__}


def _clear(root: Path) -> None:
    for p in sorted(root.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()


def observe(root: Path, monkeypatch) -> dict:
    out: dict = {}
    for label, (path_of, load, save) in _features().items():
        paths = {repr(o): str(path_of(o).relative_to(root)) for o in OWNERS}
        loads = {}
        for content in CONTENTS:
            _clear(root)
            path = path_of("alice")
            if content is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            loads[repr(content)] = _call(load, "alice")
        _clear(root)
        save("alice", json.loads(json.dumps(DATA)))
        saved = path_of("alice").read_bytes().decode("utf-8")
        listing = _listing(root)
        # A save whose data can't be written as JSON.
        _clear(root)
        bad = _call(save, "alice", {**DATA, "x": {1, 2}})
        bad_listing = _listing(root)
        # A save whose final rename fails.
        _clear(root)
        with monkeypatch.context() as m:
            m.setattr(os, "replace", _fail)
            m.setattr(Path, "replace", _fail)
            failed = _call(save, "alice", dict(DATA))
            failed_listing = _listing(root)
        _clear(root)
        out[label] = {"paths": paths, "loads": loads, "saved": saved, "listing": listing,
                      "unserializable": [bad.get("raises"), bad_listing],
                      "rename_fails": [failed.get("raises"), failed_listing]}
    for label, write in _writers().items():
        _clear(root)
        path = root / "w" / "page.json"
        write(path, json.loads(json.dumps(DATA)))
        saved = path.read_bytes().decode("utf-8")
        listing = _listing(root)
        _clear(root)
        with monkeypatch.context() as m:
            m.setattr(os, "replace", _fail)
            m.setattr(Path, "replace", _fail)
            failed = _call(write, path, dict(DATA))
            failed_listing = _listing(root)
        _clear(root)
        out[label] = {"saved": saved, "listing": listing,
                      "rename_fails": [failed.get("raises"), failed_listing]}
    return out


def _fail(*args, **kwargs):
    raise OSError("rename refused")


# Every name up to four characters long made of these, plus a few longer ones.
TRICKY = ["".join(t) for n in range(5) for t in itertools.product("./\\a\x00", repeat=n)]
TRICKY += [None, "../../etc/passwd", "/etc/passwd", "..\\..\\x", "~", "~/x", "%2e%2e",
           "\u2025", "\uff0e\uff0e", "." * 300]


def test_no_owner_name_leaves_its_feature_folder(data_dir):
    root = data_dir.resolve()
    for label, (path_of, _load, _save) in _features().items():
        mine = path_of("alice").resolve().relative_to(root)
        folder = root / mine.parts[0] if len(mine.parts) > 1 else root
        for owner in TRICKY:
            path = path_of(owner)
            assert ".." not in path.parts and "." not in path.parts, (label, owner, path)
            got = path.resolve()
            assert got.is_relative_to(folder), (label, owner, path)
            assert len(got.relative_to(root).parts) == len(mine.parts), (label, owner, path)


def test_state_files_behave_as_recorded(data_dir, monkeypatch):
    seen = json.loads(json.dumps(observe(data_dir, monkeypatch)))
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert seen.keys() == expected.keys()
    for label, rows in expected.items():
        for part, value in rows.items():
            assert seen[label][part] == value, (label, part)


if __name__ == "__main__":  # pragma: no cover -- records the fixture (old code only)
    import tempfile

    mp = pytest.MonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mp.setattr("src.constants.DATA_DIR", tmp)
            data = observe(Path(tmp), mp)
        FIXTURE.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=True)
                           + "\n", encoding="utf-8")
    finally:
        mp.undo()
