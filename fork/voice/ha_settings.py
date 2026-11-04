"""Fork: the Home Assistant address the hub saved (docs/fork/voice-assistant.md).

"Set up Home Assistant" (``fork/voice/ha_routes.py``, ``scripts/ha_voice_setup.py``)
saves the address it set up, and the Echo link (``fork/voice/echo_link.py``) hands it to
each new Echo. It is kept in upstream's settings file (``data/settings.json``, through
``src.settings``, as ``fork/routing/auto_mode.py`` keeps its setting) under
:data:`SETTING_KEY`: a runtime choice made in the app, not an environment setting.

:func:`home_assistant_url` is the one address every voice feature uses (the Echo link,
spoken announcements): ``FORK_HA_URL`` when set, else the saved one. Only a host on the
owner's network is used (``fork.voice.announce.check_url``).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SETTING_KEY = "fork_voice_ha_url"


def get_ha_url() -> str:
    """The saved Home Assistant address (``scheme://host[:port]``), or "" when unset."""
    try:
        from src.settings import get_setting

        value = get_setting(SETTING_KEY, "")
    except Exception:
        logger.warning("voice: couldn't read the Home Assistant address", exc_info=True)
        return ""
    return value.strip() if isinstance(value, str) else ""


def set_ha_url(url: str) -> str:
    """Save ``url`` (checked by ``fork.voice.ha_setup.check_ha_url``; "" clears it)."""
    from fork.voice.ha_setup import check_ha_url
    from src.settings import load_settings, save_settings

    value = check_ha_url(url) if (url or "").strip() else ""
    settings = dict(load_settings())
    settings[SETTING_KEY] = value
    save_settings(settings)
    return value


def home_assistant_url() -> str:
    """The Home Assistant address voice features use: ``FORK_HA_URL`` (an override) when
    set, else the one "Set up Home Assistant" saved; "" when neither is."""
    from fork.common import settings

    return settings.ha_url() or get_ha_url()
