"""Fork: the Wisdom (beliefs) and Meetings views look the same as windows and as pages.

tests/helpers/fork_views_look.mjs opens them in Chromium (the app's windows from
static/index.html, and the standalone pages /beliefs, /modules/sound/record, plus the phone
pairing page) at desktop and phone widths, in the default dark theme and in the light
theme, with fictional data. It records every element of each view (tag, id, classes,
attributes, own text) with a fixed list of computed style properties, and the theme each
standalone page applies. ``fork_views_look_golden.json`` holds that record, taken before
the views moved to one mount pattern; every view must still give the same.
Regenerate (only on purpose): ``UPDATE_FORK_VIEWS_LOOK=1 pytest tests/fork/test_fork_views_look.py``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tests" / "helpers" / "fork_views_look.mjs"
GOLDEN = Path(__file__).with_name("fork_views_look_golden.json")

THEMES = ("dark", "light")
WIDTHS = (1300, 420)

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _split(key: str) -> tuple[str, str]:
    """'window beliefs dark 1300' -> ('window beliefs', 'dark 1300')."""
    words = key.split()
    return " ".join(words[:-2]), " ".join(words[-2:])


def _is_view(value) -> bool:
    return isinstance(value, list) and bool(value) and isinstance(value[0], dict) and "style" in value[0]


def _compact(record: dict) -> dict:
    """Each view's elements once (they are the same in every theme and width), and per
    theme and width the index of each element's style in `styles`."""
    styles: dict[str, int] = {}
    views: dict = {}
    other: dict = {}
    for key, value in sorted(record.items()):
        if key == "props":
            continue
        if not _is_view(value):
            other[key] = value
            continue
        base, config = _split(key)
        dom = [{k: v for k, v in e.items() if k != "style"} for e in value]
        view = views.setdefault(base, {"elements": dom, "styles": {}})
        assert view["elements"] == dom, f"{key}: the elements differ between themes or widths"
        view["styles"][config] = [styles.setdefault(e["style"], len(styles)) for e in value]
    return {"props": record["props"], "styles": list(styles), "views": views, "other": other}


def _expand(compact: dict) -> dict:
    styles = compact["styles"]
    out = {"props": compact["props"], **compact["other"]}
    for base, view in compact["views"].items():
        for config, ids in view["styles"].items():
            out[f"{base} {config}"] = [{**e, "style": styles[i]}
                                       for e, i in zip(view["elements"], ids)]
    return out


def _dump(compact: dict) -> str:
    """One line per element and per style, so a diff of the record shows what changed."""
    lines = ["{", f' "props": {json.dumps(compact["props"])},', ' "styles": [']
    lines += [f"  {json.dumps(s)}," for s in compact["styles"]]
    lines[-1] = lines[-1].rstrip(",")
    lines += [" ],", ' "views": {']
    views = list(compact["views"].items())
    for i, (base, view) in enumerate(views):
        lines += [f"  {json.dumps(base)}: {{", '   "elements": [']
        lines += [f"    {json.dumps(e, sort_keys=True)}," for e in view["elements"]]
        lines[-1] = lines[-1].rstrip(",")
        lines += ["   ],", '   "styles": {']
        configs = list(view["styles"].items())
        lines += [f"    {json.dumps(c)}: {json.dumps(ids)}" + ("," if j < len(configs) - 1 else "")
                  for j, (c, ids) in enumerate(configs)]
        lines += ["   }", "  }" + ("," if i < len(views) - 1 else "")]
    lines += [" },", f' "other": {json.dumps(compact["other"], sort_keys=True, indent=1)}', "}"]
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def got() -> dict:
    proc = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True,
                          cwd=str(REPO), timeout=600, check=False)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    if "__skip" in out:
        pytest.skip(out["__skip"])
    if os.environ.get("UPDATE_FORK_VIEWS_LOOK"):
        GOLDEN.write_text(_dump(_compact(out)))
    return out


def _golden() -> dict:
    return _expand(json.loads(GOLDEN.read_text()))


def _differences(old: list, new: list) -> list:
    if len(old) != len(new):
        return [("element count", len(old), len(new))]
    return [(o["path"], o, n) for o, n in zip(old, new) if o != n]


def test_the_record_covers_every_view(got):
    golden = _golden()
    assert set(got) == set(golden)
    assert got["props"] == golden["props"]
    for key, value in golden.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            assert len(value) > 10, key


@pytest.mark.parametrize("view", ["window beliefs", "window beliefs subjects", "window meetings"])
def test_windows_look_the_same(got, view):
    golden = _golden()
    for key in (f"{view} {theme} {width}" for theme in THEMES for width in WIDTHS):
        assert not _differences(golden[key], got[key]), key


@pytest.mark.parametrize("page", ["beliefs.html", "beliefs.html subjects", "call-recorder.html",
                                  "phone-pairing.html"])
def test_pages_look_the_same(got, page):
    golden = _golden()
    for key in (f"page {page} {theme} {width}" for theme in THEMES for width in WIDTHS):
        assert not _differences(golden[key], got[key]), key


def test_pages_apply_the_saved_theme_the_same_way(got):
    golden = _golden()
    keys = [k for k in golden if " theme " in k]
    assert len(keys) == 20  # five pages, two themes, two widths
    for key in keys:
        assert got[key] == golden[key], key
    light = golden["page beliefs.html theme light 420"]
    assert light["themeColor"] == "#f3f5f7" and light["rootStyle"].startswith("--bg: #f3f5f7;")


def test_no_script_errors(got):
    for key, value in got.items():
        if " errors " in key:
            assert value == [], key
