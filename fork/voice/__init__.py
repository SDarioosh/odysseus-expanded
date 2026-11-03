"""Fork: voice requests from a Home Assistant voice satellite (docs/fork/voice-assistant.md).

* ``speech.py``: an answer turned into text to be read aloud (no markdown, links kept
  apart), sentence by sentence while it streams; only a confirmation ends with "?".
* ``sessions.py``: which Odysseus chat a voice conversation continues.
* ``prompt.py``: the voice instructions the model gets, and the fixed spoken replies.
* ``chat_prefix.py``: the owner's default chat preset, so a voice prompt starts like a
  chat's (one prompt cache for both).
* ``policy.py``: what a voice turn's tool calls may run (no email, private answers).
* ``confirm.py``: the changes voice may make after a spoken "yes" (the allow-list).
* ``ledger.py``: one record per voice turn and the changes it made (the screen's feeds).
* ``openai_api.py``: the OpenAI chat-completions shapes Home Assistant sends and reads.
* ``routes.py``: ``POST /api/chat/voice/v1/chat/completions``, ``GET …/v1/models``,
  ``GET …/v1/actions`` and ``GET …/v1/turns/latest`` for API tokens with the ``voice``
  scope; each question is a brain turn (``fork/brain/turn.py``, ``read_only_turn``).
"""
