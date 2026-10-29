"""Fork: fork/common/state.py, the per-owner JSON helpers."""
from __future__ import annotations

import os

import pytest

from fork.common import state

SLUGS = [
    (None, "default", "default"), ("", "default", "default"), ("alice", "alice", "alice"),
    ("a.b@x.org", "a.b@x.org", "a_b@x_org"), ("a/b", "a_b", "a_b"), (".", "_cdb4ee2aea69", "_"),
    ("..", "_5ec1f7e700f3", "__"), ("...", "...", "___"), ("../x", ".._x", "___x"),
    ("a b", "a_b", "a_b"), ("\u00e4\u540d", "\u00e4\u540d", "\u00e4\u540d"),
    ("x:y\\z", "x_y_z", "x_y_z"), ("-_.@", "-_.@", "-__@"), ("\t", "_", "_"),
]


@pytest.mark.parametrize(("owner", "slug", "no_dots"), SLUGS)
def test_owner_slug(owner, slug, no_dots):
    assert state.owner_slug(owner) == slug
    assert state.owner_slug(owner, safe="-_@") == no_dots


@pytest.mark.parametrize("safe", [state.SAFE, "-_@", ".", ""])
def test_owner_slug_is_never_a_folder_or_empty(safe):
    for owner in [None, "", ".", "..", "...", "/", "./", "/..", "\x00", "a"]:
        slug = state.owner_slug(owner, safe=safe)
        assert slug not in ("", ".", "..") and "/" not in slug and "\\" not in slug


def test_owner_file(data_dir):
    assert state.owner_file("module_facts", "a/b") == data_dir / "module_facts_a_b.json"


def test_read_json_keeps_null_and_defaults_on_errors(tmp_path):
    path = tmp_path / "s.json"
    assert state.read_json(path, {}) == {}
    path.write_text("null", encoding="utf-8")
    assert state.read_json(path, {}) is None
    path.write_text("{", encoding="utf-8")
    assert state.read_json(path, []) == []


@pytest.mark.parametrize("clean_up", [True, False])
def test_write_atomic_temp_file_on_failure(tmp_path, monkeypatch, clean_up):
    target = tmp_path / "d" / "s.json"
    state.write_json(target, {"b": 1, "a": "\u00e9"})
    assert target.read_text(encoding="utf-8") == '{\n "a": "\\u00e9",\n "b": 1\n}'

    def refuse(*a, **k):
        raise OSError("no")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        state.write_atomic(target, b"x", clean_up=clean_up)
    assert state.temp_path(target).exists() is not clean_up
    assert target.read_text(encoding="utf-8").startswith("{")  # the old file stays
