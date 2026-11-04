"""WebSocket-API für Raumverwaltung (Grundlage für das spätere Panel).

Befehle (Lesen: jeder angemeldete Benutzer, Ändern: nur Admins):
  smart_heating/rooms/list
  smart_heating/rooms/create   {name, thermostats?, window_sensors?, ...}
  smart_heating/rooms/update   {room_id, <Teiländerung>}
  smart_heating/rooms/delete   {room_id}
  smart_heating/rooms/assign   {room_id, kind: thermostat|window_sensor, entity_id}
  smart_heating/rooms/unassign {room_id, kind, entity_id}
  smart_heating/devices/map
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .rooms import (
    KIND_FIELDS,
    ROOM_FIELDS,
    RoomError,
    assign_entity,
    create_room,
    delete_room,
    device_map,
    unassign_entity,
    update_room,
)

_LOGGER = logging.getLogger(__name__)

ROOM_INPUT = {vol.Optional(f): object for f in ROOM_FIELDS}


@callback
def async_register(hass: HomeAssistant) -> None:
    for handler in (
        ws_list, ws_create, ws_update, ws_delete, ws_assign, ws_unassign, ws_device_map,
    ):
        websocket_api.async_register_command(hass, handler)


def _store(hass: HomeAssistant):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data.store if entries else None


def missing_entities(hass: HomeAssistant, entity_ids: list[str]) -> list[str]:
    """Entities, die weder im State-Machine noch in der Entity-Registry bekannt sind."""
    reg = er.async_get(hass)
    return [e for e in entity_ids if hass.states.get(e) is None and reg.async_get(e) is None]


def _entity_status(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    state = hass.states.get(entity_id)
    if state is None:
        return {"entity_id": entity_id, "state": None, "available": False, "missing": True}
    return {
        "entity_id": entity_id,
        "state": state.state,
        "available": state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN),
        "missing": False,
        "name": state.name,
    }


def _room_view(hass: HomeAssistant, cfg: dict, room_id: str) -> dict[str, Any]:
    room = cfg["rooms"][room_id]
    shared = device_map(cfg)
    return {
        "room_id": room_id,
        **room,
        "status": {
            field: [
                {**_entity_status(hass, e), "rooms": shared[field].get(e, [])}
                for e in room[field]
            ]
            for field in KIND_FIELDS.values()
        },
    }


async def _mutate(
    hass: HomeAssistant, connection, msg: dict, mutator: Callable[[dict], dict],
    new_entities: list[str], result: Callable[[dict], Any],
) -> None:
    store = _store(hass)
    if store is None:
        connection.send_error(msg["id"], "not_loaded", "Smart Heating ist nicht geladen")
        return
    missing = missing_entities(hass, new_entities)
    if missing:
        connection.send_error(
            msg["id"], "entity_not_found", f"Unbekannte Entities: {', '.join(missing)}"
        )
        return
    try:
        cfg = await store.async_update(mutator)
    except RoomError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    except vol.Invalid as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], result(cfg))


def _new_ids(data: dict[str, Any], current: dict | None = None) -> list[str]:
    """Neu hinzukommende Entity-IDs – nur diese werden auf Existenz geprüft."""
    ids: list[str] = []
    for field in (*KIND_FIELDS.values(), "temperature_sensor"):
        val = data.get(field)
        vals = val if isinstance(val, list) else [val] if isinstance(val, str) else []
        old = (current or {}).get(field) or []
        old = old if isinstance(old, list) else [old]
        ids += [v for v in vals if isinstance(v, str) and v not in old]
    # Entitäten in gekoppelten Kontakten und Meldestufen-Aktionen
    for field, key in (("coupled_sensors", "entity_id"), ("notify_stages", "action_entity")):
        old_ids = {i.get(key) for i in (current or {}).get(field) or [] if isinstance(i, dict)}
        for item in data.get(field) or []:
            if isinstance(item, dict) and isinstance(item.get(key), str) and item[key] not in old_ids:
                ids.append(item[key])
    return ids


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/rooms/list"})
@callback
def ws_list(hass, connection, msg):
    store = _store(hass)
    if store is None:
        connection.send_error(msg["id"], "not_loaded", "Smart Heating ist nicht geladen")
        return
    cfg = store.data
    connection.send_result(msg["id"], {"rooms": [_room_view(hass, cfg, rid) for rid in cfg["rooms"]]})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/rooms/create", **ROOM_INPUT})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_create(hass, connection, msg):
    data = {k: v for k, v in msg.items() if k in ROOM_FIELDS}
    created: dict[str, str] = {}

    def mutator(cfg):
        cfg, rid = create_room(cfg, data)
        created["id"] = rid
        return cfg

    await _mutate(hass, connection, msg, mutator, _new_ids(data),
                  lambda cfg: _room_view(hass, cfg, created["id"]))
    if created:
        _LOGGER.info("Raum angelegt: %s (%s)", data.get("name"), created["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/rooms/update", vol.Required("room_id"): str, **ROOM_INPUT}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_update(hass, connection, msg):
    rid = msg["room_id"]
    changes = {k: v for k, v in msg.items() if k in ROOM_FIELDS}
    store = _store(hass)
    current = store.data["rooms"].get(rid) if store else None
    await _mutate(hass, connection, msg, lambda cfg: update_room(cfg, rid, changes),
                  _new_ids(changes, current), lambda cfg: _room_view(hass, cfg, rid))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/rooms/delete", vol.Required("room_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete(hass, connection, msg):
    rid = msg["room_id"]
    await _mutate(hass, connection, msg, lambda cfg: delete_room(cfg, rid), [],
                  lambda cfg: {"room_id": rid, "deleted": True})


_ASSIGN_SCHEMA = {
    vol.Required("room_id"): str,
    vol.Required("kind"): vol.In(list(KIND_FIELDS)),
    vol.Required("entity_id"): str,
}


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/rooms/assign", **_ASSIGN_SCHEMA})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_assign(hass, connection, msg):
    rid, kind, eid = msg["room_id"], msg["kind"], msg["entity_id"]
    await _mutate(hass, connection, msg, lambda cfg: assign_entity(cfg, rid, kind, eid), [eid],
                  lambda cfg: _room_view(hass, cfg, rid))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/rooms/unassign", **_ASSIGN_SCHEMA})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_unassign(hass, connection, msg):
    rid, kind, eid = msg["room_id"], msg["kind"], msg["entity_id"]
    await _mutate(hass, connection, msg, lambda cfg: unassign_entity(cfg, rid, kind, eid), [],
                  lambda cfg: _room_view(hass, cfg, rid))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/devices/map"})
@callback
def ws_device_map(hass, connection, msg):
    store = _store(hass)
    if store is None:
        connection.send_error(msg["id"], "not_loaded", "Smart Heating ist nicht geladen")
        return
    connection.send_result(msg["id"], device_map(store.data))
