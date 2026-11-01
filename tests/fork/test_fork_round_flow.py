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


def _flow(steps):
    """Run createRoundFlow over `steps` with a stub DOM and markdown."""
    js = f"""
    import {{ createRoundFlow }} from {json.dumps(_MODULE)};
    const bubble = (name) => {{
      const body = {{ innerHTML: '' }};
      return {{ name, style: {{ display: '' }}, isConnected: true, querySelector: () => body, body }};
    }};
    const markdown = {{ normalizeThinkingMarkup: (t) => t, squashOutsideCode: (t) => t, processWithThinking: (t) => t }};
    const flow = createRoundFlow({{ markdown, displayText: (t) => t }});
    const bubbles = {{}};
    const out = [];
    for (const [kind, a, b] of {json.dumps(steps)}) {{
      if (kind === 'end') {{ bubbles[a] = bubbles[a] || bubble(a); flow.endRound(bubbles[a], b.text, b.next); }}
      if (kind === 'delta') out.push(flow.place({{ delta: a, round: b }}));
      if (kind === 'reset') flow.reset();
    }}
    const shown = Object.fromEntries(Object.entries(bubbles).map(([k, v]) => [k, [v.body.innerHTML, v.style.display]]));
    process.stdout.write(JSON.stringify({{ placed: out, shown, rounds: flow.shown() }}));
    """
    out = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_late_text_goes_back_to_its_round():
    # The completion gate releases round 1's text after round 2 started.
    r = _flow([
        ["end", "bubble1", {"text": "", "next": 2}],
        ["delta", "The moon has eight phases. ", 1],
        ["delta", "Let me check today.", 1],
        ["delta", "Today it is a Waning Gibbous.", 2],
    ])
    assert r["placed"] == [True, True, False]  # round 2's text stays in the current bubble
    assert r["shown"]["bubble1"] == ["The moon has eight phases. Let me check today.", ""]
    assert r["rounds"] == ["The moon has eight phases. Let me check today."]


def test_two_tool_calls_with_text_each():
    r = _flow([
        ["end", "bubble1", {"text": "", "next": 2}],
        ["end", "bubble2", {"text": "", "next": 3}],
        ["delta", "First I'll look it up.", 1],
        ["delta", "Now let me check the time.", 2],
        ["delta", "Pasta needs 14 minutes.", 3],
    ])
    assert r["placed"] == [True, True, False]
    assert r["shown"]["bubble1"][0] == "First I'll look it up."
    assert r["shown"]["bubble2"][0] == "Now let me check the time."
    assert r["rounds"] == ["First I'll look it up.", "Now let me check the time."]


def test_text_already_streamed_live_is_kept():
    r = _flow([
        ["end", "bubble1", {"text": "Let me check", "next": 2}],
        ["delta", " today.", 1],
    ])
    assert r["shown"]["bubble1"][0] == "Let me check today."


def test_an_empty_round_stays_hidden_and_unknown_rounds_are_not_placed():
    r = _flow([
        ["end", "bubble1", {"text": "", "next": 2}],
        ["delta", "", 1],
        ["delta", "no such round", 0],
        ["delta", "no round", None],
    ])
    assert r["placed"] == [True, False, False]
    assert r["shown"]["bubble1"] == ["", "none"]


def test_a_teacher_takeover_starts_its_own_rounds():
    r = _flow([
        ["end", "bubble1", {"text": "Student text.", "next": 2}],
        ["reset"],
        ["delta", "Teacher text.", 1],
    ])
    assert r["placed"] == [False]
    assert r["rounds"] == []


def test_hooks_are_in_place():
    chat = (_REPO / "static" / "js" / "chat.js").read_text()
    renderer = (_REPO / "static" / "js" / "chatRenderer.js").read_text()
    assert "import { withoutShownRounds, createRoundFlow } from './forkRoundFlow.js';" in chat
    assert "_forkRounds.endRound(roundHolder, roundText, json.round);" in chat
    assert "_forkRounds.place(json)" in chat
    assert "_forkRounds.reset();" in chat
    assert chat.count("withoutShownRounds(") == 2
    assert chat.count("_forkRounds.shown()") == 2
    assert "import { withoutShownRounds } from './forkRoundFlow.js';" in renderer
    assert "withoutShownRounds(textRaw, roundTexts.slice(0, lastTextIdx))" in renderer
