"""Fork: the DOM and fetch helpers of the fork's page scripts (`el`, `$`, `api`).

Each script used to carry its own copy, and the copies differed. They now share
static/js/forkDom.js. tests/helpers/fork_dom_cases.mjs runs the helper each script uses
(its own copy, or what it imports from forkDom.js) over a table of inputs: `el` and `$`
in Chromium, `api` in node with a fake fetch. ``fork_dom_golden.json`` holds what each
script's own copy did, recorded before the copies were replaced; every script must still
do the same, except for the inputs listed below with the reason.
Regenerate the record (only on purpose): ``UPDATE_FORK_DOM=1 pytest tests/fork/test_fork_dom.py``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tests" / "helpers" / "fork_dom_cases.mjs"
GOLDEN = Path(__file__).with_name("fork_dom_golden.json")

_SETTINGS_CARDS = ("forkAutoMode.js", "forkBackgroundModels.js", "forkPhonePairing.js",
                   "forkVoiceEnroll.js", "forkWhisperVocabulary.js")

# Inputs where the shared helper differs from a script's old copy, and the script never
# passes them: each script's el() calls were read for the keys and values they pass (only
# string attribute values or values from `cond ? a : null`; children are nodes, strings,
# numbers or null, never false).
NEVER_PASSED: dict[tuple[str, str], dict[str, str]] = {
    **{(f, "el"): {
        "className_key": "uses `class`",
        "html_key": "no `html` key",
        "text_key": "text is the third argument",
        "on_handler": "adds listeners itself",
        "attr_true": "attribute values are strings",
        "attr_false": "attribute values are strings",
        "attr_null": "attribute values are strings",
        "attr_undefined": "attribute values are strings",
        "child_false": "the text argument is a string",
        "child_node": "the text argument is a string",
        "children_mixed": "one text argument at most",
    } for f in _SETTINGS_CARDS},
    ("forkInk.js", "el"): {
        "className_key": "uses `class`",
        "html_key": "no `html` key",
        "child_false": "children are nodes or strings",
    },
    ("soundModule.js", "el"): {
        "text_key": "no `text` key",
        "attr_true": "attribute values are strings (`hidden: ''`)",
        "attr_false": "attribute values are strings",
        "child_false": "children are nodes, strings or null",
    },
    ("beliefs.js", "el"): {"text_key": "no `text` key"},
    ("today.js", "el"): {
        "text_key": "no `text` key",
        "child_false": "children are nodes, strings or null",
    },
    ("todayTrends.js", "el"): {
        "html_key": "no `html` key",
        "text_key": "no `text` key",
        "on_handler": "no listeners",
        "child_false": "children are nodes, strings, numbers or null",
    },
    ("todayWeights.js", "el"): {
        "html_key": "no `html` key",
        "text_key": "no `text` key",
        "child_false": "children are nodes, strings, numbers or null",
    },
}

# Inputs a script can meet where it now does what the other pages did already.
CHANGED: dict[tuple[str, str], dict[str, str]] = {
    ("soundModule.js", "api"): {
        "not_found_error_key": "an error answer with `error` and no `detail` shows that text "
                               "instead of `HTTP <status>`; the Athena routes answer `detail`",
    },
    ("phonePairing.js", "api"): {
        "unauthorized_json": "the 401 error says 'not logged in' (was 'not signed in'); the "
                             "page goes to the login page either way",
        "unauthorized_text": "same",
    },
}


def _run() -> dict:
    proc = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True,
                          cwd=str(REPO), timeout=180, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _same(old: dict, new: dict) -> bool:
    """`new` does what `old` did; an error may carry `status` and `data` it didn't have."""
    if "error" in old and "error" in new:
        new = {**new, "error": {k: v for k, v in new["error"].items()
                                if k in old["error"] or k not in ("status", "data")}}
    return old == new


def _dump(record: dict) -> str:
    """One line per case, so a diff of the record shows the cases that changed."""
    lines = ["{"]
    for i, (file, by_helper) in enumerate(sorted(record.items())):
        lines.append(f" {json.dumps(file)}: {{")
        for j, (helper, cases) in enumerate(sorted(by_helper.items())):
            lines.append(f"  {json.dumps(helper)}: {{")
            items = sorted(cases.items())
            for k, (case, outcome) in enumerate(items):
                comma = "," if k < len(items) - 1 else ""
                lines.append(f"   {json.dumps(case)}: {json.dumps(outcome, sort_keys=True)}{comma}")
            lines.append("  }" + ("," if j < len(by_helper) - 1 else ""))
        lines.append(" }" + ("," if i < len(record) - 1 else ""))
    return "\n".join(lines + ["}"]) + "\n"


@pytest.fixture(scope="module")
def got() -> dict:
    out = _run()
    if os.environ.get("UPDATE_FORK_DOM"):
        assert "__skip" not in out, out["__skip"]
        GOLDEN.write_text(_dump({f: v for f, v in out.items() if f != "forkDom.js"}))
    return out


def _differences(got: dict, helpers: tuple[str, ...]) -> list:
    golden = json.loads(GOLDEN.read_text())
    differ = []
    for file, by_helper in golden.items():
        for helper, cases in by_helper.items():
            if helper not in helpers:
                continue
            skip = {**NEVER_PASSED.get((file, helper), {}), **CHANGED.get((file, helper), {})}
            for case, old in cases.items():
                if case not in skip and not _same(old, got[file][helper][case]):
                    differ.append((file, helper, case, old, got[file][helper][case]))
    return differ


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_each_script_keeps_what_its_api_did(got):
    assert not _differences(got, ("api",))


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_each_script_keeps_what_its_el_and_dollar_did(got):
    if "__skip" in got:
        pytest.skip(got["__skip"])
    assert not _differences(got, ("el", "$"))


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_the_shared_helpers_are_the_widest_copies(got):
    # forkDom.js: api is the copy of beliefs.js and today.js (status and data on the error,
    # `detail` or `error` as the message, 401 to the login page unless redirect401 is
    # false), request is Write's, and el is beliefs.js's plus the `text` key of Write's.
    golden = json.loads(GOLDEN.read_text())
    shared = got["forkDom.js"]
    assert shared["api"] == golden["beliefs.js"]["api"] == golden["today.js"]["api"]
    assert shared["request"] == golden["forkInk.js"]["api"]
    if "__skip" in got:
        pytest.skip(got["__skip"])
    assert shared["$"] == golden["beliefs.js"]["$"]
    assert shared["el"] == {**golden["beliefs.js"]["el"],
                            "text_key": golden["forkInk.js"]["el"]["text_key"]}
