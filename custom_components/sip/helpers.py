"""Helper utilities for the SIP Client integration."""
from __future__ import annotations

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant


def get_ffmpeg_bin(hass: HomeAssistant) -> str:
    """Return the ffmpeg binary path configured by the HA ffmpeg integration.

    ``ffmpeg`` is a hard dependency of this integration (see manifest), so the
    manager is always available here.
    """
    return get_ffmpeg_manager(hass).binary
