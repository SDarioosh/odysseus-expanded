"""Fork: a saved agent answer keeps each round's text where it was shown (fork/hub/round_flow.py).

Feeds the chat route's ``_AgentRenderState`` the events upstream's completion gate
releases at the end of a run: every text delta tagged with its round. The texts are
fictional.
"""
from routes.chat_routes import _AgentRenderState

A1 = "The moon has eight phases:\n\n- New Moon\n- Full Moon\n\nLet me check the phase for today."
A2 = "Today the moon is a **Waning Gibbous**."
B1 = "First I'll look up the boiling point of water at altitude."
B2 = "Water boils at about 90 C at 3,000 m. Now let me check the cooking time."
B3 = "Pasta needs about **14 minutes**."


def _saved(deltas, reported):
    state = _AgentRenderState()
    for round_num, text in deltas:
        event = {"delta": text}
        if round_num is not None:
            event["round"] = round_num
        state.consume(event)
    return state, state.metadata({"round_texts": list(reported), "tool_events": [{"round": 1}]})


def _pieces(round_num, text, size=7):
    return [(round_num, text[i:i + size]) for i in range(0, len(text), size)]


def test_a_round_the_loop_dropped_is_saved_where_it_was_shown():
    # The loop reported round 1 as empty ("Let me check ..." counts as a preamble),
    # but the page showed it before the tool step.
    _, meta = _saved([(1, A1), (2, A2)], ["", A2])
    assert meta["round_texts"] == [A1, A2]


def test_two_tool_calls_with_text_each():
    # The loop put the joined answer in the last round.
    joined = "\n\n".join([B1, B2, B3])
    _, meta = _saved([(1, B1), (2, B2), (3, B3)], [B1, B2, joined])
    assert meta["round_texts"] == [B1, B2, B3]


def test_deltas_streamed_in_pieces():
    _, meta = _saved(_pieces(1, A1) + _pieces(2, A2), ["", A2])
    assert meta["round_texts"] == [A1, A2]


def test_no_text_before_the_tool_is_left_alone():
    _, meta = _saved([(2, A2)], ["", A2])
    assert meta["round_texts"] == ["", A2]


def test_text_without_a_round_keeps_upstreams_round_texts():
    _, meta = _saved([(1, A1), (None, "\n\n*[Stream error]*"), (2, A2)], ["", A2])
    assert meta["round_texts"] == ["", A2]


def test_a_replaced_turn_keeps_upstreams_round_texts():
    state = _AgentRenderState()
    state.consume({"delta": A1, "round": 1})
    state.consume({"type": "final_response", "content": "Done."})
    meta = state.metadata({"round_texts": ["Done."]})
    assert meta["round_texts"] == ["Done."]
    assert meta["replacement_scope"] == "turn"


def test_a_synthesis_that_replaces_the_turn_starts_again():
    state = _AgentRenderState()
    state.consume({"type": "final_response", "content": "Draft list."})
    # A later model synthesis replaces the structured answer (replacement_scope turn).
    state.consume({"delta": B1, "round": 1})
    state.consume({"delta": B3, "round": 2})
    assert state.replaced_turn  # upstream keeps it scoped to the turn
    meta = state.metadata({"round_texts": ["", B3]})
    assert meta["round_texts"] == ["", B3]


def test_metadata_without_round_texts_is_unchanged():
    state, _ = _saved([(1, A1), (2, A2)], ["", A2])
    assert state.message_saved(7) == {"type": "message_saved", "id": 7, "render_owner": "streamed"}
