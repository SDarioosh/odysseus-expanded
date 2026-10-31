"""Forget reaches the brain's stored tool exchanges (fork/brain/history.py)."""
from fork.brain import history

FACT = "Wren Hale is allergic to hazelnuts"
LINE = "Wren said she's allergic to hazelnuts, so no praline cake"


def _exchange(text: str) -> list[dict]:
    return [{"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
             "function": {"name": "list_facts", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": text}]


def test_forget_drops_exchanges_and_summaries_that_quote_the_fact(data_dir):
    history.save_turn("chat-a", "m1", _exchange(f"- {FACT}. (link)"), owner="alice")
    history.save_turn("chat-a", "m2", _exchange("Lanternfall Festival opens on Friday"),
                      owner="alice")
    history.save_turn("chat-b", "m3", _exchange(f"transcript: {LINE}"), owner="alice")
    history.save_summary("chat-b", "m3", f"Earlier: {LINE.upper()}!")
    history.save_summary("chat-a", "m2", "Earlier: the festival dates.")

    assert history.forget_texts("alice", [FACT + ".", LINE]) == 2

    assert set(history.turns("chat-a")) == {"m2"}
    assert history.turns("chat-b") == {}
    assert history.summary("chat-b") is None
    assert history.summary("chat-a") is not None


def test_forget_leaves_other_owners_and_short_texts_alone(data_dir):
    history.save_turn("chat-c", "m1", _exchange(FACT), owner="bob")
    history.save_turn("chat-d", "m2", _exchange("Wren called"), owner="alice")

    assert history.forget_texts("alice", [FACT]) == 0
    assert history.forget_texts("alice", ["Wren"]) == 0  # under three words: ignored
    assert set(history.turns("chat-c")) == {"m1"}
    assert set(history.turns("chat-d")) == {"m2"}


def test_forget_without_a_brain_db_creates_nothing(data_dir):
    assert history.forget_texts("alice", [FACT]) == 0
    assert not (data_dir / history.DB_NAME).exists()
