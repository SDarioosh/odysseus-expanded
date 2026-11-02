"""The fork's HTTP routes that are not Athena's (Athena's are in fork/athena/).

:func:`mount` includes every fork router, Athena's too, from the one hook in ``app.py``
(after the MCP routes).
"""
from __future__ import annotations

import importlib
from typing import Any

# (module, router factory), in mount order. Order matters: belief search goes before the
# review page's ``/api/beliefs/{id}``.
ROUTERS: tuple[tuple[str, str], ...] = (
    ("fork.athena.routes", "setup_sound_module_routes"),  # Athena conversations page
    ("fork.routes.today_insight_routes", "setup_today_insight_routes"),  # Today page, votes, token route
    ("fork.routes.belief_search_routes", "setup_belief_search_routes"),  # belief search
    ("fork.routes.beliefs_routes", "setup_beliefs_routes"),  # belief review page
    ("fork.athena.relay_routes", "setup_sound_relay_routes"),  # phone relay, pairing by QR
    ("fork.athena.summary_model_routes", "setup_summary_model_routes"),  # summary model setting
    ("fork.athena.whisper_vocabulary_routes", "setup_whisper_vocabulary_routes"),  # vocabulary
    ("fork.athena.voice_enroll_routes", "setup_voice_enroll_routes"),  # Enroll My Voice
    ("fork.routes.auto_mode_routes", "setup_auto_mode_routes"),  # auto mode default
    ("fork.routes.ink_routes", "setup_ink_routes"),  # handwritten notes
    ("fork.routes.news_routes", "setup_news_routes"),  # morning news brief
)


def mount(app: Any) -> None:
    """Include the fork's routers in ``app``, in :data:`ROUTERS` order, each module
    imported just before its router is built."""
    for module, factory in ROUTERS:
        app.include_router(getattr(importlib.import_module(module), factory)())
