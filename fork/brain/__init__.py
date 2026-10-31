"""Fork: the brain, a small agent loop that answers Odysseus chat turns.

On when ``FORK_BRAIN`` is on (``fork/brain/settings.py``). One hook in ``chat_stream``
(``routes/chat_routes.py``) hands a normal chat turn, or the reply to one of the brain's
own approval cards, to :func:`fork.brain.turn.brain_response`; every other turn, and
every turn while the setting is off, stays upstream's.

- ``turn.py``: the hook's entry point, the loop and the chat window's events.
- ``prompt.py``: the stable system prompt, the history and the context message.
- ``toolset.py``: the fixed tool set (names, groups, read or write).
- ``tools.py``: runs each call through upstream's ``execute_tool_block``.
- ``history.py``: tool calls and trimmed results per message (``data/brain.db``).
- ``query_guard.py``: removes private details from outgoing web search queries.

Design: ``docs/fork/brain.md``.
"""
