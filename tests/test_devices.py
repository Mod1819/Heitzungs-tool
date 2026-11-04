"""Batterieüberwachung und Schutz vor falschen Fensterkontakten (z. B. Batterie-Meldung)."""
import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.smart_heating.const import DOMAIN, STORAGE_KEY
from custom_components.smart_heating.devices import battery_report, wrong_window_sensors


def _device(hass, src, name, area=None):
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=src.entry_id, identifiers={("hmip", name)}, name=name,
        suggested_area=area)


def _entity(hass, domain, uid, device, device_class=None, state=None, attrs=None):
    entry = er.async_get(hass).async_get_or_create(
        domain, "hmip", uid, device_id=device.id if device else None,
        original_device_class=device_class, suggested_object_id=uid)
    if state is not None:
        hass.states.async_set(entry.entity_id, state, attrs or {})
    return entry.entity_id


@pytest.fixture
def hmip(hass):
    """Nachbau Homematic IP: Heizgruppe (ohne Batterie) + 2 Heizkörperthermostate + Fensterkontakt."""
    src = MockConfigEntry(domain="homematicip_cloud")
    src.add_to_hass(hass)
    ar.async_get(hass).async_create("Wohnzimmer")
    ar.async_get(hass).async_create("Bad")
    group = _device(hass, src, "Heizgruppe Wohnzimmer", "Wohnzimmer")
    trv1 = _device(hass, src, "Heizkörper Fenster", "Wohnzimmer")
    trv2 = _device(hass, src, "Heizkörper Tür", "Wohnzimmer")
    swdo = _device(hass, src, "Fensterkontakt Wohnzimmer", "Wohnzimmer")
    bad_trv = _device(hass, src, "Heizkörper Bad", "Bad")
    ids = {
        "climate": _entity(hass, "climate", "wz_group", group, state="heat",
                           attrs={"temperature": 20, "min_temp": 5, "max_temp": 30}),
        "trv1_bat": _entity(hass, "binary_sensor", "trv1_low_battery", trv1, "battery", "on"),
        "trv2_bat": _entity(hass, "binary_sensor", "trv2_low_battery", trv2, "battery", "off"),
        "window": _entity(hass, "binary_sensor", "wz_fenster", swdo, "window", "off",
                          {"device_class": "window"}),
        "window_bat": _entity(hass, "sensor", "wz_fenster_battery", swdo, "battery", "85",
                              {"device_class": "battery", "unit_of_measurement": "%"}),
        "window_bat_bin": _entity(hass, "binary_sensor", "wz_fenster_low_battery", swdo, "battery", "off",
                                  {"device_class": "battery"}),
        "bad_bat": _entity(hass, "sensor", "bad_trv_battery", bad_trv, "battery", "10"),
        "sabotage": _entity(hass, "binary_sensor", "wz_fenster_sabotage", swdo, "tamper", "off"),
    }
    return ids


def test_wrong_window_sensors(hass, hmip):
    assert wrong_window_sensors(hass, [hmip["window"], hmip["window_bat_bin"], hmip["sabotage"],
                                       "binary_sensor.unbekannt", "climate.x"]) == [
        hmip["window_bat_bin"], hmip["sabotage"]]


def test_battery_report_group_area_and_window(hass, hmip):
    cfg = {"rooms": {"wz": {"thermostats": [hmip["climate"]], "window_sensors": [hmip["window"]]}}}
    report = battery_report(hass, cfg)
    by_entity = {b["entity_id"]: b for b in report}
    assert by_entity[hmip["trv1_bat"]]["low"] is True              # über Bereich gefunden
    assert by_entity[hmip["trv1_bat"]]["device"] == "Heizkörper Fenster"
    assert by_entity[hmip["trv2_bat"]]["low"] is False
    assert by_entity[hmip["window_bat"]]["level"] == 85 and not by_entity[hmip["window_bat"]]["low"]
    assert hmip["bad_bat"] not in by_entity                       # anderer Bereich
    assert report[0]["low"] is True                                # niedrige zuerst
    assert all(b["rooms"] == ["wz"] for b in report)


def test_battery_low_percent_and_unavailable(hass, hmip):
    hass.states.async_set(hmip["window_bat"], "15")
    hass.states.async_set(hmip["trv1_bat"], "unavailable")
    cfg = {"rooms": {"wz": {"thermostats": [hmip["climate"]], "window_sensors": [hmip["window"]]}}}
    by_entity = {b["entity_id"]: b for b in battery_report(hass, cfg)}
    assert by_entity[hmip["window_bat"]]["low"] is True
    assert hmip["trv1_bat"] not in by_entity


def test_entities_without_device_ignored(hass):
    cfg = {"rooms": {"x": {"thermostats": ["climate.ohne_geraet"], "window_sensors": []}}}
    assert battery_report(hass, cfg) == []


@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_api_rejects_battery_as_window_and_status(hass, hass_storage, hass_ws_client, hmip):
    hass_storage[STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": STORAGE_KEY, "data": {
        "rooms": {"wz": {"name": "Wohnzimmer", "thermostats": [hmip["climate"]]},
                  "kueche": {"name": "Küche"}}}}
    async_mock_service(hass, "climate", "set_temperature")
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ws = await hass_ws_client(hass)
    n = 0

    async def call(t, **kw):
        nonlocal n
        n += 1
        await ws.send_json({"id": n, "type": f"{DOMAIN}/{t}", **kw})
        return await ws.receive_json()

    r = await call("rooms/assign", room_id="wz", kind="window_sensor", entity_id=hmip["window_bat_bin"])
    assert r["error"]["code"] == "not_a_window_sensor"
    r = await call("rooms/update", room_id="kueche",
                   coupled_sensors=[{"entity_id": hmip["sabotage"], "delay": 60}])
    assert r["error"]["code"] == "not_a_window_sensor"
    r = await call("rooms/assign", room_id="wz", kind="window_sensor", entity_id=hmip["window"])
    assert r["success"]
    status = (await call("status"))["result"]
    low = [b for b in status["batteries"] if b["low"]]
    assert [b["device"] for b in low] == ["Heizkörper Fenster"] and low[0]["rooms"] == ["wz"]
