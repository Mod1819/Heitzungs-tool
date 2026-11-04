"""Smart Heating Control – intelligente Raum-Heizungssteuerung."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from . import panel, websocket, websocket_profiles, websocket_settings, websocket_status
from .const import DOMAIN
from .controller import Controller
from .scheduler import Scheduler
from .store import ConfigStore

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class SmartHeatingData:
    store: ConfigStore
    scheduler: Scheduler
    controller: Controller


type SmartHeatingConfigEntry = ConfigEntry[SmartHeatingData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """WebSocket-Befehle einmalig registrieren (unabhängig vom Config-Entry-Zyklus)."""
    websocket.async_register(hass)
    websocket_profiles.async_register(hass)
    websocket_status.async_register(hass)
    websocket_settings.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SmartHeatingConfigEntry) -> bool:
    store = ConfigStore(hass)
    await store.async_load()
    scheduler = Scheduler(hass, store)
    controller = Controller(hass, store, scheduler)
    entry.runtime_data = SmartHeatingData(store=store, scheduler=scheduler, controller=controller)
    scheduler.async_start()
    controller.async_start()  # sendet erst, wenn HA vollständig gestartet ist
    await panel.async_register_panel(hass)
    cfg = store.data
    _LOGGER.info(
        "Smart Heating geladen: %d Räume, %d Profile",
        len(cfg["rooms"]), len(cfg["profiles"]),
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmartHeatingConfigEntry) -> bool:
    panel.async_remove_panel(hass)
    entry.runtime_data.controller.async_stop()
    entry.runtime_data.scheduler.async_stop()
    await entry.runtime_data.store.async_flush()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: SmartHeatingConfigEntry) -> None:
    """Integration gelöscht -> gespeicherte Konfiguration entfernen."""
    await ConfigStore(hass).async_remove()
