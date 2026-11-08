"""fork/common/sqlite.py: the schema runs once per file and process; writes roll back on
an error; fork/common/owner.py: the owner key."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from fork.common import sqlite as common_sqlite
from fork.common.owner import owner_key

SCHEMA = "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL);"


def _count_scripts(monkeypatch) -> list[str]:
    runs: list[str] = []
    real = common_sqlite._set_up

    def counted(db, key, schema, columns=()):
        runs.append(key[0])
        real(db, key, schema, columns)

    monkeypatch.setattr(common_sqlite, "_set_up", counted)
    return runs


def test_schema_runs_once_per_path(tmp_path, monkeypatch):
    runs = _count_scripts(monkeypatch)
    path = tmp_path / "sub" / "a.db"
    for _ in range(3):
        with common_sqlite.connect(path, SCHEMA) as db:
            assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
    with common_sqlite.connect(path, SCHEMA, write=True) as db:
        db.execute("INSERT INTO notes (text) VALUES ('x')")
    assert len(runs) == 1
    with sqlite3.connect(path) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    other = tmp_path / "b.db"  # another file (another temp DATA_DIR): its own run
    with common_sqlite.connect(other, SCHEMA) as db:
        db.execute("SELECT 1 FROM notes")
    assert len(runs) == 2


def test_schema_runs_again_when_the_file_is_gone(tmp_path, monkeypatch):
    runs = _count_scripts(monkeypatch)
    path = tmp_path / "a.db"
    with common_sqlite.connect(path, SCHEMA):
        pass
    for name in ("a.db", "a.db-wal", "a.db-shm"):
        (tmp_path / name).unlink(missing_ok=True)
    with common_sqlite.connect(path, SCHEMA) as db:
        assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
    assert len(runs) == 2


def test_schema_runs_once_across_threads(tmp_path, monkeypatch):
    runs = _count_scripts(monkeypatch)
    path = tmp_path / "a.db"
    with common_sqlite.connect(path, ""):  # the file exists before the threads race
        pass
    runs.clear()

    def open_once():
        with common_sqlite.connect(path, SCHEMA) as db:
            db.execute("SELECT 1 FROM notes")

    threads = [threading.Thread(target=open_once) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(runs) == 1


def test_a_write_rolls_back_on_error(tmp_path):
    path = tmp_path / "a.db"
    with common_sqlite.connect(path, SCHEMA, write=True) as db:
        db.execute("INSERT INTO notes (text) VALUES ('kept')")
    with pytest.raises(RuntimeError):
        with common_sqlite.connect(path, SCHEMA, write=True) as db:
            db.execute("INSERT INTO notes (text) VALUES ('dropped')")
            raise RuntimeError("boom")
    with common_sqlite.connect(path, SCHEMA) as db:
        rows = [r["text"] for r in db.execute("SELECT text FROM notes")]
    assert rows == ["kept"]


def test_columns_are_added_to_an_older_file(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as raw:  # a file from before the column existed
        raw.executescript(SCHEMA)
        raw.execute("INSERT INTO notes (text) VALUES ('kept')")
    cols = (("notes", "tag", "TEXT"),)
    with common_sqlite.connect(path, SCHEMA, write=True, columns=cols) as db:
        db.execute("UPDATE notes SET tag = 'x'")
    with common_sqlite.connect(path, SCHEMA, columns=cols) as db:
        assert [tuple(r) for r in db.execute("SELECT text, tag FROM notes")] == [("kept", "x")]
    common_sqlite._ready.clear()  # another process opening it again: no duplicate column
    with common_sqlite.connect(path, SCHEMA, columns=cols) as db:
        assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1


def test_owner_key():
    assert owner_key(" Alice ") == "alice"
    assert owner_key(None) == ""
    assert owner_key("ÄRGER") == "ärger"
