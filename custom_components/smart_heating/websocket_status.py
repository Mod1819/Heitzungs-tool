"""WebSocket: Regelungsstatus (Fenster, effektive Werte, Thermostate, letzte Aktionen)."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_status)
    websocket_api.async_register_command(hass, ws_subscribe)


def _snapshot(data) -> dict:
    return {"config": data.store.data, "status": data.controller.status(),
            "targets": data.scheduler.targets}


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/status"})
@callback
def ws_status(hass, connection, msg):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], "not_loaded", "Smart Heating ist nicht geladen")
        return
    connection.send_result(msg["id"], entries[0].runtime_data.controller.status())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@callback
def ws_subscribe(hass, connection, msg):
    """Live-Abo für das Panel: sofortiger Stand, danach bei jeder Änderung (kein Polling)."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], "not_loaded", "Smart Heating ist nicht geladen")
        return
    data = entries[0].runtime_data

    @callback
    def forward() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], _snapshot(data)))

    unsub_status = data.controller.async_add_status_listener(forward)
    unsub_store = data.store.async_add_listener(lambda _cfg: forward())

    @callback
    def unsubscribe() -> None:
        unsub_status()
        unsub_store()

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    forward()
