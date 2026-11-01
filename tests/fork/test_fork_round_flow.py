"""Fork: an agent answer reads as text, tool step, then the rest (static/js/forkRoundFlow.js).

Runs the module through `node --input-type=module`; skips without node.
The texts are fictional.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_MODULE = (_REPO / "static" / "js" / "forkRoundFlow.js").as_posix()
_HAS_NODE = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(not _HAS_NODE, reason="node not installed")

_ROUND_1 = (
    "The moon goes through eight phases in about 29.5 days:\n\n"
    "- New Moon\n- Waxing Crescent\n- First Quarter\n- Full Moon\n\n"
    "Let me check the phase for today."
)
_ROUND_2 = "Today the moon is a **Waning Gibbous**, about 64% lit."


def _strip(final_text, earlier):
    js = f"""
    import {{ withoutShownRounds }} from {json.dumps(_MODULE)};
    process.stdout.write(JSON.stringify(withoutShownRounds({json.dumps(final_text)}, {json.dumps(earlier)})));
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", js],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(out.stdout)


def test_final_answer_keeps_only_the_new_round():
    assert _strip(_ROUND_1 + "\n\n" + _ROUND_2, [_ROUND_1]) == _ROUND_2


def test_whitespace_differences_still_match():
    streamed = _ROUND_1.replace("\n\n", "\n") + "  "
    assert _strip(_ROUND_1 + "\n\n" + _ROUND_2, [streamed]) == _ROUND_2


def test_thinking_in_the_streamed_round_is_ignored():
    streamed = "<think>The user wants moon phases.</think>" + _ROUND_1
    assert _strip(_ROUND_1 + "\n\n" + _ROUND_2, [streamed]) == _ROUND_2


def test_several_rounds_and_a_dropped_preamble():
    # The server dropped round 2 ("I'll search...") from the final text.
    rounds = [_ROUND_1, "I'll search the web.", "Found two sources."]
    final = _ROUND_1 + "\n\nFound two sources.\n\n" + _ROUND_2
    assert _strip(final, rounds) == _ROUND_2


def test_a_fresh_summary_is_left_alone():
    assert _strip(_ROUND_2, [_ROUND_1]) == _ROUND_2


def test_no_earlier_rounds_changes_nothing():
    assert _strip(_ROUND_1, []) == _ROUND_1
    assert _strip(_ROUND_1, ["", "  "]) == _ROUND_1


def test_never_leaves_the_bubble_empty():
    # The final text is only what was already shown: keep it as it is.
    assert _strip(_ROUND_1, [_ROUND_1]) == _ROUND_1


def test_hooks_are_in_place():
    chat = (_REPO / "static" / "js" / "chat.js").read_text()
    renderer = (_REPO / "static" / "js" / "chatRenderer.js").read_text()
    assert "import { withoutShownRounds } from './forkRoundFlow.js';" in chat
    assert "_forkShownRounds.push(roundText);" in chat
    assert chat.count("withoutShownRounds(") == 2
    assert "import { withoutShownRounds } from './forkRoundFlow.js';" in renderer
    assert "withoutShownRounds(textRaw, roundTexts.slice(0, lastTextIdx))" in renderer
