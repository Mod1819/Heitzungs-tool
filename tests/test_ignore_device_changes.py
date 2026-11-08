"""v1.4: Änderungen direkt am Thermostat werden standardmäßig ignoriert (Plan/Panel gilt)."""
import copy
from datetime import timedelta

import pytest
import voluptuous as vol

from custom_components.smart_heating.const import DOMAIN
from custom_components.smart_heating.models import validate_config
from tests.test_controller import CFG, Rig, _prepare


def _cfg(mode=None):
    data = copy.deepcopy(CFG)
    data["settings"].pop("thermostat_changes", None)
    if mode:
        data["settings"]["thermostat_changes"] = mode
    return data


async def _rig(hass, hass_storage, freezer, data=None):
    calls, entry = await _prepare(hass, hass_storage, freezer, data or _cfg())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rig = Rig(hass, freezer, calls, entry)
    assert rig.new_calls() == {"climate.wz1": 21, "climate.wz2": 21, "climate.ez": 20, "climate.flur": 18}
    await rig.ack()
    return rig


def _log(rig, rid="wz"):
    return [a["message"] for a in rig.entry.runtime_data.controller.status()["actions"][rid]]


async def test_device_change_is_ignored_and_target_resent(hass, hass_storage, freezer):
    rig = await _rig(hass, hass_storage, freezer)
    await rig.device("climate.wz1", 23)
    assert rig.new_calls() == {"climate.wz1": 21}
    assert rig.room("wz")["desired_temp"] == 21
    assert "wz" not in rig.entry.runtime_data.store.data["overrides"]
    assert any("ignoriert" in m and "23" in m for m in _log(rig))
    assert not any("manuell" in m for m in _log(rig))
    await rig.ack()
    assert rig.thermo("climate.wz1")["status"] == "ok"
    assert rig.new_calls() == {}


async def test_device_change_during_open_window_keeps_lock(hass, hass_storage, freezer):
    rig = await _rig(hass, hass_storage, freezer)
    await rig.window("binary_sensor.wz_f1", "on")
    assert rig.new_calls() == {"climate.wz1": 5, "climate.wz2": 5}
    await rig.ack()
    await rig.device("climate.wz1", 22)
    assert rig.new_calls() == {"climate.wz1": 5}
    assert rig.room("wz")["desired_temp"] == 21
    assert "wz" not in rig.entry.runtime_data.store.data["overrides"]
    await rig.ack()
    await rig.window("binary_sensor.wz_f1", "off")
    assert rig.new_calls() == {"climate.wz1": 21, "climate.wz2": 21}  # kein Override gespeichert


async def test_reverting_device_gives_up_after_three_attempts(hass, hass_storage, freezer):
    rig = await _rig(hass, hass_storage, freezer)
    for _ in range(3):
        await rig.device("climate.wz1", 23)
        assert rig.new_calls() == {"climate.wz1": 21}
        await rig.ack()
    await rig.device("climate.wz1", 23)               # 4. Rücksetzung
    assert rig.new_calls() == {}
    assert rig.thermo("climate.wz1")["status"] == "not_confirmed"
    assert sum("setzt eigenen Wert dauerhaft" in m for m in _log(rig)) == 1
    await rig.device("climate.wz1", 24)               # kein weiterer Aufruf, kein zweites Log
    assert rig.new_calls() == {}
    assert sum("setzt eigenen Wert dauerhaft" in m for m in _log(rig)) == 1
    # neues Ziel (Zeitplan 08:30 -> 18 °C) wird wieder gesendet
    await rig.tick(timedelta(hours=1, minutes=20))
    assert rig.new_calls().get("climate.wz1") == 18


async def test_revert_window_expires(hass, hass_storage, freezer):
    rig = await _rig(hass, hass_storage, freezer)
    for _ in range(3):
        await rig.device("climate.wz1", 23)
        await rig.ack()
    await rig.tick(timedelta(minutes=16))
    await rig.device("climate.wz1", 23)
    assert rig.new_calls() == {"climate.wz1": 21}


async def test_adopt_mode_stores_manual(hass, hass_storage, freezer):
    rig = await _rig(hass, hass_storage, freezer, _cfg("adopt"))
    await rig.device("climate.wz1", 23)
    assert rig.new_calls() == {"climate.wz2": 23}      # Gerätewert wird als manuell übernommen
    assert rig.entry.runtime_data.store.data["overrides"]["wz"]["temp"] == 23


@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_ws_setting_roundtrip(hass, hass_storage, freezer, hass_ws_client):
    await _rig(hass, hass_storage, freezer)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/settings/update", "thermostat_changes": "adopt"})
    r = await ws.receive_json()
    assert r["success"] and r["result"]["thermostat_changes"] == "adopt"
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/settings/update", "thermostat_changes": "egal"})
    r = await ws.receive_json()
    assert r["error"]["code"] == "invalid_format"
    await ws.send_json({"id": 3, "type": f"{DOMAIN}/settings/get"})
    assert (await ws.receive_json())["result"]["thermostat_changes"] == "adopt"


def test_default_is_ignore():
    assert validate_config({})["settings"]["thermostat_changes"] == "ignore"
    with pytest.raises(vol.Invalid):
        validate_config({"settings": {"thermostat_changes": "x"}})
