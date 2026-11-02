"""Fork: the old ``src/`` and ``routes/`` paths of fork modules alias ``fork/``.

The fork's Python files moved into the ``fork`` package. Each old path is a shim that
replaces itself in ``sys.modules`` with the new module, so old imports, string patches
(``monkeypatch.setattr("src.x.attr", ...)``) and the new path all reach one module
object. The shims go once nothing imports the old paths.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHIM = re.compile(
    r"\A# Shim: moved to (fork/[a-z_/]+)\.py; this old path is the same module object\.\n"
    r"import sys\n\nfrom (fork(?:\.[a-z_]+)+) import ([a-z_]+) as _module\n\n"
    r"sys\.modules\[__name__\] = _module\n\Z"
)


def _shims() -> dict[str, str]:
    """Old dotted name -> new dotted name, for every shim in src/ and routes/."""
    found = {}
    for folder in ("src", "routes"):
        for path in sorted((REPO / folder).glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if not text.startswith("# Shim: moved to fork/"):
                continue
            m = SHIM.match(text)
            assert m, f"{path.relative_to(REPO)} is not a plain shim"
            new = f"{m[2]}.{m[3]}"
            assert m[1].replace("/", ".") == new, f"{path.relative_to(REPO)} names another path"
            found[f"{folder}.{path.stem}"] = new
    return found


SHIMS = _shims()


def test_every_moved_module_has_a_shim() -> None:
    assert len(SHIMS) == 59
    for new in SHIMS.values():
        assert (REPO / (new.replace(".", "/") + ".py")).is_file(), new
    assert len(set(SHIMS.values())) == len(SHIMS), "two shims point at one module"


@pytest.mark.parametrize("old,new", sorted(SHIMS.items()))
def test_old_path_is_the_same_module_object(old: str, new: str) -> None:
    assert importlib.import_module(old) is importlib.import_module(new)
    parent, _, leaf = old.rpartition(".")
    assert getattr(importlib.import_module(parent), leaf) is importlib.import_module(new)


def test_a_string_patch_on_the_old_path_reaches_the_new_module(monkeypatch) -> None:
    from fork.mcp import trust

    monkeypatch.setattr("src.mcp_trust.trusted_readonly_mcp_tools", lambda *a, **k: "patched")
    assert trust.trusted_readonly_mcp_tools() == "patched"


def test_app_mounts_the_fork_routers_from_the_package() -> None:
    from fork.routes import ROUTERS

    app = (REPO / "app.py").read_text(encoding="utf-8")
    assert "from fork.routes import mount as _fork_mount_routes  # noqa: E402" in app
    hooks = [module for module, _ in ROUTERS]
    assert len(hooks) == 11 and all(h.startswith("fork.") for h in hooks)
    assert not [h for h in hooks if h in SHIMS], "a fork router is mounted by its old path"
