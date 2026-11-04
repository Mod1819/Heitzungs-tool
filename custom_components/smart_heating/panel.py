"""Seitenleisten-Panel (Web-Component ohne Build-Schritt).

Die Datei www/smart-heating-panel.js wird über einen statischen Pfad ausgeliefert
und als Custom Panel registriert (nur für Admins sichtbar).
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "smart-heating"
STATIC_URL = f"/{DOMAIN}_static"
WEBCOMPONENT = "smart-heating-panel"
_STATIC_FLAG = f"{DOMAIN}_static_registered"


def _version() -> str:
    import json

    manifest = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))
    return manifest["version"]


async def async_register_panel(hass: HomeAssistant) -> bool:
    if not hass.data.get(_STATIC_FLAG):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "www"), cache_headers=False)]
        )
        hass.data[_STATIC_FLAG] = True
    if "frontend" not in hass.config.components:
        _LOGGER.warning("Frontend nicht geladen – Smart-Heating-Panel wird nicht angezeigt")
        return False
    from homeassistant.components import panel_custom

    version = await hass.async_add_executor_job(_version)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=WEBCOMPONENT,
        sidebar_title="Heizung",
        sidebar_icon="mdi:radiator",
        module_url=f"{STATIC_URL}/{WEBCOMPONENT}.js?v={version}",
        require_admin=True,
        config={},
    )
    return True


def async_remove_panel(hass: HomeAssistant) -> None:
    if "frontend" not in hass.config.components:
        return
    from homeassistant.components import frontend

    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
