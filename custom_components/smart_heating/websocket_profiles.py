"""WebSocket-API für Profile, Wochenpläne, Handbetrieb und aktuelle Sollwerte.

Berechtigungen wie Modul 2: Lesen alle angemeldeten Benutzer, Ändern nur Admins.
Raum-Profilüberschreibung: smart_heating/rooms/update {room_id, profile_override_id}.

  smart_heating/profiles/list
  smart_heating/profiles/create          {name, copy_from?}
  smart_heating/profiles/create_presets  (Winter, Frühling, Sommer, Herbst)
  smart_heating/profiles/rename          {profile_id, name}
  smart_heating/profiles/copy            {profile_id, name}
  smart_heating/profiles/delete          {profile_id}
  smart_heating/profiles/set_active      {profile_id | null}
  smart_heating/schedule/get             {profile_id, room_id}
  smart_heating/schedule/set_week        {profile_id, room_id, week}
  smart_heating/schedule/set_day         {profile_id, room_id, day, slots}
  smart_heating/schedule/copy_day        {profile_id, room_id, source_day, target_days}
  smart_heating/schedule/copy_room       {profile_id, source_room_id, target_room_ids}
  smart_heating/manual/set               {room_id, temperature, mode?}
  smart_heating/manual/clear             {room_id}
  smart_heating/targets                  aktuelle Sollwerte aller Räume
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .models import MANUAL_PERMANENT, MANUAL_UNTIL_NEXT
from .profiles import (
    SEASON_PRESETS, copy_day, copy_profile, copy_room_schedule, create_profile,
    create_season_presets, delete_profile, get_week, rename_profile, set_active_profile,
    set_day, set_week,
)
from .rooms import RoomError
from .websocket import _mutate, _store

P = f"{DOMAIN}/profiles"
S = f"{DOMAIN}/schedule"


@callback
def async_register(hass: HomeAssistant) -> None:
    for handler in (
        ws_list, ws_create, ws_presets, ws_rename, ws_copy, ws_delete, ws_set_active,
        ws_sched_get, ws_set_week, ws_set_day, ws_copy_day, ws_copy_room,
        ws_manual_set, ws_manual_clear, ws_targets,
    ):
        websocket_api.async_register_command(hass, handler)


def _entry_data(hass: HomeAssistant):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


def _not_loaded(connection, msg) -> None:
    connection.send_error(msg["id"], "not_loaded", "Smart Heating ist nicht geladen")


def _profiles_view(cfg: dict) -> dict:
    return {
        "active_profile_id": cfg["settings"]["active_profile_id"],
        "presets": list(SEASON_PRESETS),
        "profiles": [
            {"profile_id": pid, "name": p["name"], "rooms": sorted(p["schedules"]),
             "used_by_rooms": [rid for rid, r in cfg["rooms"].items() if r["profile_override_id"] == pid]}
            for pid, p in cfg["profiles"].items()
        ],
    }


async def _run(hass, connection, msg, mutator, result=_profiles_view):
    await _mutate(hass, connection, msg, mutator, [], result)


# --- Profile -----------------------------------------------------------------

@websocket_api.websocket_command({vol.Required("type"): f"{P}/list"})
@callback
def ws_list(hass, connection, msg):
    store = _store(hass)
    if store is None:
        return _not_loaded(connection, msg)
    connection.send_result(msg["id"], _profiles_view(store.data))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{P}/create", vol.Required("name"): str, vol.Optional("copy_from"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_create(hass, connection, msg):
    def mutator(cfg):
        if "copy_from" in msg:
            return copy_profile(cfg, msg["copy_from"], msg["name"])[0]
        return create_profile(cfg, msg["name"])[0]
    await _run(hass, connection, msg, mutator)


@websocket_api.websocket_command({vol.Required("type"): f"{P}/create_presets"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_presets(hass, connection, msg):
    await _run(hass, connection, msg, lambda cfg: create_season_presets(cfg)[0])


@websocket_api.websocket_command(
    {vol.Required("type"): f"{P}/rename", vol.Required("profile_id"): str, vol.Required("name"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_rename(hass, connection, msg):
    await _run(hass, connection, msg, lambda cfg: rename_profile(cfg, msg["profile_id"], msg["name"]))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{P}/copy", vol.Required("profile_id"): str, vol.Required("name"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_copy(hass, connection, msg):
    await _run(hass, connection, msg, lambda cfg: copy_profile(cfg, msg["profile_id"], msg["name"])[0])


@websocket_api.websocket_command({vol.Required("type"): f"{P}/delete", vol.Required("profile_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete(hass, connection, msg):
    await _run(hass, connection, msg, lambda cfg: delete_profile(cfg, msg["profile_id"]))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{P}/set_active", vol.Required("profile_id"): vol.Any(None, str)}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_active(hass, connection, msg):
    await _run(hass, connection, msg, lambda cfg: set_active_profile(cfg, msg["profile_id"]))


# --- Wochenplan ----------------------------------------------------------------

_PR = {vol.Required("profile_id"): str, vol.Required("room_id"): str}


def _week_result(msg):
    return lambda cfg: {"profile_id": msg["profile_id"], "room_id": msg["room_id"],
                        "week": get_week(cfg, msg["profile_id"], msg["room_id"])}


@websocket_api.websocket_command({vol.Required("type"): f"{S}/get", **_PR})
@callback
def ws_sched_get(hass, connection, msg):
    store = _store(hass)
    if store is None:
        return _not_loaded(connection, msg)
    try:
        connection.send_result(msg["id"], _week_result(msg)(store.data))
    except RoomError as err:
        connection.send_error(msg["id"], err.code, err.message)


@websocket_api.websocket_command({vol.Required("type"): f"{S}/set_week", **_PR, vol.Required("week"): dict})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_week(hass, connection, msg):
    await _run(hass, connection, msg,
               lambda cfg: set_week(cfg, msg["profile_id"], msg["room_id"], msg["week"]), _week_result(msg))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{S}/set_day", **_PR, vol.Required("day"): str, vol.Required("slots"): list}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_day(hass, connection, msg):
    await _run(hass, connection, msg,
               lambda cfg: set_day(cfg, msg["profile_id"], msg["room_id"], msg["day"], msg["slots"]),
               _week_result(msg))


@websocket_api.websocket_command({
    vol.Required("type"): f"{S}/copy_day", **_PR,
    vol.Required("source_day"): str, vol.Required("target_days"): [str],
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_copy_day(hass, connection, msg):
    await _run(hass, connection, msg,
               lambda cfg: copy_day(cfg, msg["profile_id"], msg["room_id"], msg["source_day"],
                                    msg["target_days"]),
               _week_result(msg))


@websocket_api.websocket_command({
    vol.Required("type"): f"{S}/copy_room", vol.Required("profile_id"): str,
    vol.Required("source_room_id"): str, vol.Required("target_room_ids"): [str],
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_copy_room(hass, connection, msg):
    await _run(hass, connection, msg,
               lambda cfg: copy_room_schedule(cfg, msg["profile_id"], msg["source_room_id"],
                                              msg["target_room_ids"]))


# --- Handbetrieb & aktuelle Sollwerte ------------------------------------------

@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/manual/set", vol.Required("room_id"): str,
    vol.Required("temperature"): vol.Coerce(float),
    vol.Optional("mode", default=MANUAL_UNTIL_NEXT): vol.In((MANUAL_UNTIL_NEXT, MANUAL_PERMANENT)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_manual_set(hass, connection, msg):
    data = _entry_data(hass)
    if data is None:
        return _not_loaded(connection, msg)
    try:
        result = await data.scheduler.async_set_manual(msg["room_id"], msg["temperature"], msg["mode"])
    except RoomError as err:
        return connection.send_error(msg["id"], err.code, err.message)
    except vol.Invalid as err:
        return connection.send_error(msg["id"], "invalid_format", str(err))
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/manual/clear", vol.Required("room_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_manual_clear(hass, connection, msg):
    data = _entry_data(hass)
    if data is None:
        return _not_loaded(connection, msg)
    try:
        result = await data.scheduler.async_clear_manual(msg["room_id"])
    except RoomError as err:
        return connection.send_error(msg["id"], err.code, err.message)
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/targets"})
@callback
def ws_targets(hass, connection, msg):
    data = _entry_data(hass)
    if data is None:
        return _not_loaded(connection, msg)
    sch = data.scheduler
    connection.send_result(msg["id"], {
        "targets": sch.targets,
        "next_wakeup": sch.next_wakeup.isoformat() if sch.next_wakeup else None,
    })
