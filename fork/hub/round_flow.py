"""Fork: a saved agent answer keeps each round's text where it was shown.

The chat shows an agent answer round by round: the text before a tool call, the tool
step, then the next round's text. The agent loop sometimes drops a round's text from
the ``round_texts`` it reports (text ending in "Let me check ..." counts as a
preamble) or puts the whole joined answer in the last round, but upstream's
completion gate (``src/agent_runtime/completion.py``) still releases every round's
text to the page, and the saved message is all of it joined. A reload then drew the
first round's text below the tool step, after the tool had run.

The chat route's ``_AgentRenderState`` sees every text delta with the round it
belongs to. ``note_delta`` collects them per round and ``round_texts`` saves the
text each round showed, so a reload shows the same flow as the live page
(``static/js/forkRoundFlow.js`` then leaves those rounds out of the last bubble).
Hooks: ``routes/chat_routes.py`` (``_AgentRenderState.consume`` and ``metadata``).
"""
from __future__ import annotations

from typing import Any

_ATTR = "_fork_round_text"


def note_delta(state: Any, event: dict) -> None:
    """Remember a visible text delta under its round (call after it is added)."""
    rounds = getattr(state, _ATTR, None)
    if rounds is None:
        rounds = {}
        setattr(state, _ATTR, rounds)
    if event.get("replacement_scope") == "turn":
        # A synthesis that replaces the turn starts the saved text again.
        rounds.clear()
    round_num = event.get("round")
    if isinstance(round_num, bool) or not isinstance(round_num, int) or round_num < 1:
        rounds[None] = True  # text without a round: keep upstream's round_texts
        return
    rounds[round_num] = rounds.get(round_num, "") + str(event.get("delta") or "")


def round_texts(state: Any, metadata: dict) -> dict:
    """Put the text each round showed into ``metadata["round_texts"]``."""
    rounds = getattr(state, _ATTR, None)
    if (
        not rounds
        or None in rounds
        or getattr(state, "replaced_turn", False)
        or not isinstance(metadata.get("round_texts"), list)
    ):
        return metadata
    shown = {r: t for r, t in rounds.items() if t.strip()}
    if len(shown) < 2:
        return metadata  # one round of text: nothing to put back in place
    texts = list(metadata["round_texts"])
    texts.extend([""] * (max(shown) - len(texts)))
    for round_num, text in shown.items():
        texts[round_num - 1] = text.strip()
    metadata["round_texts"] = texts
    return metadata
