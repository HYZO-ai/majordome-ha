"""Majordome : assistant de maintenance 100 % local pour Home Assistant."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PANEL_ELEMENT, PANEL_ICON, PANEL_TITLE, PANEL_URL, STATIC_URL, VERSION
from .core import Majordome
from .websocket import async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CARD_URL = f"{STATIC_URL}/majordome-card.js?v={VERSION}"


async def _async_register_static(hass: HomeAssistant) -> None:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("static_registered"):
        return
    path = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(path), False)])
    async_register_websocket(hass)
    domain_data["static_registered"] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Démarre Majordome."""
    await _async_register_static(hass)
    md = Majordome(hass, entry)
    hass.data[DOMAIN]["core"] = md

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=f"{STATIC_URL}/majordome-panel.js?v={VERSION}",
        require_admin=True,
        config={},
    )
    frontend.add_extra_js_url(hass, CARD_URL)

    await md.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Arrête Majordome."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    md: Majordome | None = hass.data.get(DOMAIN, {}).pop("core", None)
    if md:
        await md.async_stop()
    frontend.async_remove_panel(hass, PANEL_URL)
    frontend.remove_extra_js_url(hass, CARD_URL)
    return unloaded
