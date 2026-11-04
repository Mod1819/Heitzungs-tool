"""Außentemperaturregelung im laufenden System – gegen HA-Core mit simuliertem Sensor."""
import copy

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState

from custom_components.smart_heating.const import DOMAIN
from tests.test_controller import CFG, Rig, _prepare

SENSOR = "sensor.aussen"


def _data(**settings):
    data = copy.deepcopy(CFG)
    data["settings"].update({"outdoor_enabled": True, "outdoor_sensor": SENSOR, **settings})
    return data


async def _start(hass, hass_storage, freezer, outdoor="16", unit="°C", data=None, running=True):
    calls, entry = await _prepare(hass, hass_storage, freezer, data or _data())
    hass.states.async_set(SENSOR, outdoor, {"unit_of_measurement": unit})
    if not running:
        hass.set_state(CoreState.not_running)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return Rig(hass, freezer, calls, entry)


async def sensor(rig, value, unit="°C"):
    rig.hass.states.async_set(SENSOR, value, {"unit_of_measurement": unit})
    await rig.hass.async_block_till_done()


def outdoor(rig):
    return rig.entry.runtime_data.controller.status()["outdoor"]


async def test_reduction_on_start(hass, hass_storage, freezer):
    rig = await _start(hass, hass_storage, freezer)
    # 16 °C außen -> 1,5 K: WZ 21->19,5 / EZ 20->18,5 / Flur 18->16,5 (min aus EZ, Flur)
    assert rig.new_calls() == {"climate.wz1": 19.5, "climate.wz2": 19.5, "climate.ez": 18.5,
                               "climate.flur": 16.5}
    assert rig.room("wz")["desired_temp"] == 21 and rig.room("wz")["outdoor_reduction"] == 1.5
    assert outdoor(rig)["status"] == "ok" and outdoor(rig)["used"] == 16


async def test_hysteresis_and_change(hass, hass_storage, freezer):
    rig = await _start(hass, hass_storage, freezer)
    await rig.ack()
    await sensor(rig, "16.4")  # < 0,5 K -> alter Wert bleibt
    assert outdoor(rig)["used"] == 16 and rig.new_calls() == {}
    await sensor(rig, "20")
    assert outdoor(rig)["used"] == 20
    assert rig.new_calls() == {"climate.wz1": 18, "climate.wz2": 18, "climate.ez": 17, "climate.flur": 15}


@pytest.mark.parametrize("bad", ["unavailable", "unknown", "abc", "99"])
async def test_invalid_sensor_means_no_reduction(hass, hass_storage, freezer, bad):
    rig = await _start(hass, hass_storage, freezer)
    await rig.ack()
    await sensor(rig, bad)
    assert rig.new_calls() == {"climate.wz1": 21, "climate.wz2": 21, "climate.ez": 20, "climate.flur": 18}
    assert outdoor(rig)["status"] in ("invalid", "implausible") and outdoor(rig)["used"] is None
    await rig.ack()
    await sensor(rig, "16")
    assert rig.new_calls()["climate.wz1"] == 19.5


async def test_fahrenheit_sensor(hass, hass_storage, freezer):
    rig = await _start(hass, hass_storage, freezer, outdoor="60.8", unit="°F")
    assert outdoor(rig)["used"] == 16
    assert rig.new_calls()["climate.wz1"] == 19.5


async def test_restart_with_unknown_sensor_no_uncontrolled_change(hass, hass_storage, freezer):
    rig = await _start(hass, hass_storage, freezer, outdoor="unknown", running=False)
    assert rig.new_calls() == {}
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert rig.new_calls()["climate.wz1"] == 21  # ohne gültigen Wert: normal nach Plan
    await rig.ack()
    await sensor(rig, "18")
    assert rig.new_calls()["climate.wz1"] == 19  # 21 - 2,2 = 18,8 -> Schritt 0,5


async def test_window_lock_beats_outdoor_and_restores_adjusted(hass, hass_storage, freezer):
    rig = await _start(hass, hass_storage, freezer)
    await rig.ack()
    await rig.window("binary_sensor.wz_f1", "on")
    assert rig.new_calls() == {"climate.wz1": 5, "climate.wz2": 5}
    await rig.ack()
    await sensor(rig, "20")  # Außentemperatur ändert sich bei offenem Fenster
    assert "climate.wz1" not in rig.new_calls()
    await rig.ack()
    await rig.window("binary_sensor.wz_f1", "off")
    assert rig.new_calls() == {"climate.wz1": 18, "climate.wz2": 18}


async def test_manual_not_reduced(hass, hass_storage, freezer):
    rig = await _start(hass, hass_storage, freezer)
    await rig.ack()
    await rig.device("climate.wz1", 22)
    assert rig.new_calls() == {"climate.wz2": 22}
    assert rig.room("wz")["effective_temp"] == 22


@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_room_mode_off_and_custom_via_api(hass, hass_storage, freezer, hass_ws_client):
    rig = await _start(hass, hass_storage, freezer)
    await rig.ack()
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/rooms/update", "room_id": "wz", "outdoor_mode": "off"})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert rig.new_calls() == {"climate.wz1": 21, "climate.wz2": 21}
    await rig.ack()
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/rooms/update", "room_id": "wz", "outdoor_mode": "custom",
                        "outdoor_curve": {"start_at": 10, "full_at": 16, "max_reduction": 4}})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert rig.new_calls() == {"climate.wz1": 17, "climate.wz2": 17}


@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_settings_api(hass, hass_storage, freezer, hass_ws_client):
    rig = await _start(hass, hass_storage, freezer, data=_data(outdoor_enabled=False, outdoor_sensor=None))
    await rig.ack()
    ws = await hass_ws_client(hass)
    n = 0

    async def call(t, **kw):
        nonlocal n
        n += 1
        await ws.send_json({"id": n, "type": f"{DOMAIN}/settings/{t}", **kw})
        r = await ws.receive_json()
        await hass.async_block_till_done()
        return r

    assert (await call("get"))["result"]["outdoor_enabled"] is False
    assert (await call("update", outdoor_enabled=True))["error"]["code"] == "sensor_required"
    assert (await call("update", outdoor_sensor="sensor.gibtsnicht"))["error"]["code"] == "entity_not_found"
    assert (await call("update", outdoor_sensor="climate.ez"))["error"]["code"] == "invalid_format"
    assert (await call("update", active_profile_id=None))["error"]["code"] == "invalid_format"
    r = await call("update", outdoor_curve={"start_at": 20, "full_at": 12})
    assert r["error"]["code"] == "invalid_format"
    r = await call("update", outdoor_sensor=SENSOR, outdoor_enabled=True,
                   outdoor_curve={"points": [[12, 0], [14, 0.5], [16, 1.0], [18, 2.0], [20, 3.0]]})
    assert r["success"] and r["result"]["outdoor_curve"]["points"][1] == [14, 0.5]
    assert rig.new_calls() == {"climate.wz1": 20, "climate.wz2": 20, "climate.ez": 19, "climate.flur": 17}
    await rig.ack()
    r = await call("update", outdoor_curve={"points": None})  # zurück zu linear (1,5 K)
    assert "points" not in r["result"]["outdoor_curve"]
    assert rig.new_calls()["climate.wz1"] == 19.5
    await rig.ack()
    assert (await call("update", outdoor_enabled=False))["success"]
    assert rig.new_calls()["climate.wz1"] == 21
    assert (await call("update", unknown_window_policy="closed", default_temp=19.5))["success"]
    assert (await call("update", unknown_window_policy="egal"))["error"]["code"] == "invalid_format"


@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_settings_non_admin(hass, hass_storage, freezer, hass_ws_client, hass_read_only_access_token):
    await _start(hass, hass_storage, freezer)
    ws = await hass_ws_client(hass, hass_read_only_access_token)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/settings/update", "outdoor_enabled": False})
    assert (await ws.receive_json())["error"]["code"] == "unauthorized"
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/settings/get"})
    assert (await ws.receive_json())["success"]
