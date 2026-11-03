"""Fork: voice requests from a Home Assistant voice satellite (docs/fork/voice-assistant.md).

* ``speech.py``: an answer turned into text to be read aloud (no markdown, links kept
  apart), sentence by sentence while it streams.
* ``sessions.py``: which Odysseus chat a voice conversation continues.
* ``prompt.py``: the voice instructions the model gets, and the fixed spoken replies.
* ``openai_api.py``: the OpenAI chat-completions shapes Home Assistant sends and reads.
* ``routes.py``: ``POST /v1/chat/completions`` and ``GET /v1/models`` for API tokens with
  the ``voice`` scope; each question is a read-only brain turn
  (``fork/brain/turn.py``, ``read_only_turn``).
"""
