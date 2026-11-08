"""WebSocket: globale Einstellungen (Außentemperaturregelung, Fensterverhalten, Standardtemperatur).

  smart_heating/settings/get
  smart_heating/settings/update {outdoor_sensor?, outdoor_enabled?, outdoor_curve?,
                                 unknown_window_policy?, default_temp?, recipients?}
  smart_heating/notify/test {recipient_id? | room_id? + stage?}
Das aktive Profil wird über profiles/set_active geändert, raumweise Außentemperatur-
Einstellungen über rooms/update (outdoor_mode, outdoor_curve).
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .rooms import RoomError
from .websocket import _mutate, _store

SETTING_FIELDS = (
    "outdoor_sensor", "outdoor_enabled", "outdoor_curve", "unknown_window_policy", "default_temp",
    "notify_service", "notify_entity_id",
    "thermostat_changes", "recipients",
)


def update_settings(cfg: dict, changes: dict[str, Any]) -> dict:
    """Teiländerung der Einstellungen; Kurve wird zusammengeführt.

    Validierung erfolgt anschließend vollständig in validate_config (ConfigStore).
    """
    unknown = set(changes) - set(SETTING_FIELDS)
    if unknown:
        raise RoomError("invalid_field", f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    settings = cfg["settings"]
    for key, value in changes.items():
        if key == "outdoor_curve":
            if not isinstance(value, dict):
                raise vol.Invalid("outdoor_curve muss ein Objekt sein")
            merged = {**settings["outdoor_curve"], **value}
            if value.get("points") in (None, []):
                merged.pop("points", None)  # zurück zur linearen Kurve
            settings["outdoor_curve"] = merged
        else:
            settings[key] = value
    if settings.get("outdoor_enabled") and not settings.get("outdoor_sensor"):
        raise RoomError("sensor_required", "Für die Außentemperaturregelung ist ein Sensor nötig")
    return cfg


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_get)
    websocket_api.async_register_command(hass, ws_update)
    websocket_api.async_register_command(hass, ws_notify_test)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/settings/get"})
@callback
def ws_get(hass, connection, msg):
    store = _store(hass)
    if store is None:
        connection.send_error(msg["id"], "not_loaded", "Smart Heating ist nicht geladen")
        return
    connection.send_result(msg["id"], store.data["settings"])


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/settings/update", **{vol.Optional(f): object for f in SETTING_FIELDS}}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_update(hass, connection, msg):
    changes = {k: v for k, v in msg.items() if k in SETTING_FIELDS}
    store = _store(hass)
    current = store.data["settings"]["outdoor_sensor"] if store else None
    sensor = changes.get("outdoor_sensor")
    new = [sensor] if isinstance(sensor, str) and sensor != current else []
    service = changes.get("notify_service")
    if isinstance(service, str) and "." in service and not hass.services.has_service(*service.split(".", 1)):
        connection.send_error(msg["id"], "service_not_found",
                              f"Dienst {service} existiert nicht (z. B. notify.telegram)")
        return
    recipients = changes.get("recipients")
    if isinstance(recipients, list):
        for rec in recipients:
            svc = rec.get("service") if isinstance(rec, dict) else None
            if isinstance(svc, str) and "." in svc and not hass.services.has_service(*svc.split(".", 1)):
                connection.send_error(msg["id"], "service_not_found",
                                      f"Dienst {svc} existiert nicht (z. B. notify.telegram)")
                return
    if isinstance(changes.get("notify_entity_id"), str):
        new.append(changes["notify_entity_id"])
    await _mutate(hass, connection, msg, lambda cfg: update_settings(cfg, changes), new,
                  lambda cfg: cfg["settings"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/notify/test",
        vol.Optional("recipient_id"): str,
        vol.Optional("room_id"): str,
        vol.Optional("stage"): vol.All(int, vol.Range(min=0, max=100)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_notify_test(hass, connection, msg):
    """Testnachricht senden: an einen Empfänger, über die Meldung eines Raums oder (ohne Angabe) alt."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], "not_loaded", "Smart Heating ist nicht geladen")
        return
    data = entries[0].runtime_data
    cfg = data.store.data
    settings = cfg["settings"]
    notifier = data.controller.notifier
    if "recipient_id" in msg:
        rec = next((r for r in settings["recipients"] if r["id"] == msg["recipient_id"]), None)
        if rec is None:
            connection.send_error(msg["id"], "recipient_not_found", "Empfänger existiert nicht")
            return
        service, payload = notifier.recipient_call(
            rec, f"🔔 Testnachricht von Smart Heating Control an {rec['name']}")
        ok = int(await notifier.async_send(service, payload, []))
        total = 1
    elif "room_id" in msg:
        rid = msg["room_id"]
        if rid not in cfg["rooms"]:
            connection.send_error(msg["id"], "room_not_found", f"Raum {rid!r} existiert nicht")
            return
        text = notifier.sample_text(cfg, rid, msg.get("stage"))
        ok, total = await notifier.async_send_to_room(cfg, rid, text)
        if not total:
            connection.send_error(msg["id"], "no_recipient",
                                  "Kein Empfänger für diesen Raum – bitte Empfänger auswählen und speichern")
            return
    else:
        if not settings["notify_service"]:
            connection.send_error(msg["id"], "no_service", "Kein Benachrichtigungsdienst eingestellt")
            return
        payload = {"message": "🔔 Testnachricht von Smart Heating Control", "title": "Heizung"}
        if settings["notify_entity_id"]:
            payload["entity_id"] = settings["notify_entity_id"]
        if await notifier.async_send(settings["notify_service"], payload, []):
            connection.send_result(msg["id"], {"sent": True})
        else:
            connection.send_error(msg["id"], "send_failed", "Senden fehlgeschlagen – Details im Log")
        return
    if ok:
        connection.send_result(msg["id"], {"sent": ok})
    else:
        connection.send_error(msg["id"], "send_failed", "Senden fehlgeschlagen – Details im Log")
