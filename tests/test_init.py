"""Integration: Setup, Config Flow, Persistenz, Neustart – gegen echten HA-Core (Testharness)."""
from datetime import timedelta

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry, async_fire_time_changed,
)

from custom_components.smart_heating.const import DOMAIN, STORAGE_KEY

ROOM_CFG = {
    "rooms": {"wz": {"name": "Wohnzimmer", "thermostats": ["climate.wz_1", "climate.wz_2"],
                     "window_sensors": ["binary_sensor.wz_fenster"]}},
    "profiles": {"winter": {"name": "Winter", "schedules": {"wz": {
        "mon": [{"start": "00:00", "end": "06:00", "temp": 17},
                {"start": "06:00", "end": "24:00", "temp": 21}]}}}},
    "settings": {"active_profile_id": "winter"},
}


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="Smart Heating Control", data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow_single_instance(hass):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.FORM
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.ABORT


async def test_setup_empty_and_unload(hass):
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.store.data["rooms"] == {}
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_delayed_save_written(hass, hass_storage):
    entry = await _setup(hass)
    await entry.runtime_data.store.async_replace(ROOM_CFG)
    assert STORAGE_KEY not in hass_storage  # noch verzögert
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert hass_storage[STORAGE_KEY]["data"]["rooms"]["wz"]["thermostats"] == ["climate.wz_1", "climate.wz_2"]


async def test_restart_restores_config(hass, hass_storage):
    entry = await _setup(hass)
    await entry.runtime_data.store.async_replace(ROOM_CFG)
    # Neustart simulieren: Entry entladen (flush) und neu laden
    assert await hass.config_entries.async_unload(entry.entry_id)
    saved = hass_storage[STORAGE_KEY]["data"]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    cfg = entry.runtime_data.store.data
    assert cfg == saved
    assert cfg["settings"]["active_profile_id"] == "winter"
    assert cfg["profiles"]["winter"]["schedules"]["wz"]["mon"][1]["temp"] == 21.0


async def test_preexisting_storage_loaded(hass, hass_storage):
    hass_storage[STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": STORAGE_KEY, "data": ROOM_CFG}
    entry = await _setup(hass)
    assert entry.runtime_data.store.data["rooms"]["wz"]["name"] == "Wohnzimmer"


async def test_invalid_update_keeps_old_state(hass, hass_storage):
    entry = await _setup(hass)
    store = entry.runtime_data.store
    await store.async_replace(ROOM_CFG, immediate=True)
    with pytest.raises(vol.Invalid):
        await store.async_replace({"rooms": {"x": {"name": "X", "thermostats": ["light.falsch"]}}})
    assert store.data["rooms"]["wz"]["name"] == "Wohnzimmer"
    assert hass_storage[STORAGE_KEY]["data"]["rooms"]["wz"]["name"] == "Wohnzimmer"


async def test_corrupt_storage_backed_up_not_lost(hass, hass_storage):
    bad = {"rooms": {"x": {"name": "X", "thermostats": ["kein-entity"]}}}
    hass_storage[STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": STORAGE_KEY, "data": bad}
    entry = await _setup(hass)
    store = entry.runtime_data.store
    assert entry.state is ConfigEntryState.LOADED
    assert store.load_error
    assert hass_storage[f"{STORAGE_KEY}.invalid_backup"]["data"] == bad


async def test_listener_notified_and_removable(hass):
    entry = await _setup(hass)
    store = entry.runtime_data.store
    calls = []
    remove = store.async_add_listener(calls.append)
    await store.async_replace(ROOM_CFG)
    await store.async_replace(ROOM_CFG)  # unverändert -> kein Event, kein Schreiben
    assert len(calls) == 1
    remove()
    await store.async_replace({})
    assert len(calls) == 1


async def test_remove_entry_deletes_storage(hass, hass_storage):
    entry = await _setup(hass)
    await entry.runtime_data.store.async_replace(ROOM_CFG, immediate=True)
    assert STORAGE_KEY in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert STORAGE_KEY not in hass_storage
