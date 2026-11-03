"""Fork: the preset a voice turn starts with, so its prompt starts like a chat's
(docs/fork/voice-assistant.md, "One prompt cache for chat and voice").

The owner's model server keeps one prompt cache, warm from ordinary chat. A voice turn
reuses it only when its prompt starts byte for byte like a chat turn's: the same system
prompt (the brain's, ``fork/brain/prompt.py``, plus the chat's preset text) and the same
tools in the same order. A new chat in the browser sends the Custom preset when it is on
and has settings (``static/js/presets.js``, "Auto-activate custom preset"), else no
preset; :func:`default_preset` resolves the same one, through upstream's own
``ChatHandler.validate_and_extract_preset``, from ``<DATA_DIR>/presets.json`` (which the
preset routes save on every change).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

EMPTY = SimpleNamespace(temperature=None, max_tokens=None, system_prompt="",
                        character_name=None)


def default_preset_id(presets: dict) -> str | None:
    """``custom`` when the browser would select it for a new chat, else None."""
    custom = (presets or {}).get("custom")
    if not isinstance(custom, dict) or custom.get("enabled") is False:
        return None
    temperature = custom.get("temperature", 1.0)
    has_settings = bool(
        custom.get("character_name") or custom.get("system_prompt")
        or custom.get("inject_prefix") or custom.get("inject_suffix")
        or custom.get("thinking_mode") or temperature != 1.0 or custom.get("max_tokens"))
    return "custom" if has_settings else None


def default_preset() -> Any:
    """The owner's default chat preset as ``routes.chat_helpers.extract_preset`` returns
    it (``system_prompt``, ``temperature``, ``max_tokens``, ``character_name``)."""
    try:
        from routes.chat_helpers import extract_preset
        from src.chat_handler import ChatHandler
        from src.constants import DATA_DIR
        from src.preset_manager import PresetManager

        manager = PresetManager(DATA_DIR)
        preset_id = default_preset_id(manager.presets)
        if preset_id is None:
            return EMPTY
        handler = SimpleNamespace(preset_manager=manager)
        handler.validate_and_extract_preset = (
            lambda pid: ChatHandler.validate_and_extract_preset(handler, pid))
        return extract_preset(handler, preset_id)
    except Exception:  # noqa: BLE001 -- no preset: the brain's own prompt
        logger.warning("voice: the chat preset is unavailable", exc_info=True)
        return EMPTY
