"""Manuell zugeordnete Batterien, Batterie-Attribute und flexible Zeiteingabe."""
import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smart_heating.const import DOMAIN
from custom_components.smart_heating.devices import battery_report
from custom_components.smart_heating.models import validate_day


@pytest.fixture
async def ws(hass, hass_ws_client):
    hass.states.async_set("climate.wz", "heat")
    hass.states.async_set("binary_sensor.fenster_bat", "off", {"device_class": "battery"})
    hass.states.async_set("sensor.fenster_bat_pct", "80", {"device_class": "battery"})
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    n = 0

    async def call(type_, **kw):
        nonlocal n
        n += 1
        await client.send_json({"id": n, "type": f"{DOMAIN}/{type_}", **kw})
        return await client.receive_json()
    return call


async def test_ws_battery_entities_accepted(ws):
    r = await ws("rooms/create", name="Bad")
    rid = r["result"]["room_id"]
    assert r["result"]["battery_entities"] == []
    r = await ws("rooms/update", room_id=rid,
                 battery_entities=["binary_sensor.fenster_bat", "sensor.fenster_bat_pct"])
    assert r["success"], r
    assert r["result"]["battery_entities"] == ["binary_sensor.fenster_bat", "sensor.fenster_bat_pct"]


async def test_ws_battery_entities_errors(ws, hass):
    rid = (await ws("rooms/create", name="Bad"))["result"]["room_id"]
    r = await ws("rooms/update", room_id=rid, battery_entities=["sensor.gibt_es_nicht"])
    assert r["error"]["code"] == "entity_not_found"
    r = await ws("rooms/update", room_id=rid, battery_entities=["climate.wz"])
    assert r["error"]["code"] == "invalid_format"


def test_battery_report_manual_and_attribute(hass):
    hass.states.async_set("binary_sensor.bat", "on",
                          {"device_class": "battery", "friendly_name": "Kontakt Batterie"})
    hass.states.async_set("sensor.bat_pct", "unavailable")
    hass.states.async_set("binary_sensor.fenster", "off",
                          {"low_battery": True, "friendly_name": "Fenster Bad"})
    hass.states.async_set("climate.trv", "heat", {"battery_level": 55})
    cfg = {"rooms": {
        "a": {"thermostats": ["climate.trv"], "window_sensors": ["binary_sensor.fenster"],
              "battery_entities": ["binary_sensor.bat", "sensor.bat_pct"]},
        "b": {"thermostats": [], "window_sensors": [], "battery_entities": ["binary_sensor.bat"]},
        "c": {"thermostats": [], "window_sensors": []},  # ohne battery_entities-Schlüssel
    }}
    report = battery_report(hass, cfg)
    by = {(b["entity_id"], b["source"]): b for b in report}
    manual = by[("binary_sensor.bat", "manual")]
    assert manual["low"] is True and manual["device"] == "Kontakt Batterie"
    assert manual["rooms"] == ["a", "b"]                     # dedupliziert, beide Räume
    assert sum(b["entity_id"] == "binary_sensor.bat" for b in report) == 1
    assert not any(b["entity_id"] == "sensor.bat_pct" for b in report)   # unavailable
    assert by[("binary_sensor.fenster", "attribute")]["low"] is True
    trv = by[("climate.trv", "attribute")]
    assert trv["level"] == 55 and trv["low"] is False
    assert report[0]["low"] is True


def test_time_normalization():
    day = validate_day([{"start": "6", "end": "8:30", "temp": 20},
                        {"start": "9.00", "end": "1030", "temp": 21},
                        {"start": " 11,5 ", "end": "24", "temp": 19}])
    assert [(s["start"], s["end"]) for s in day] == [
        ("06:00", "08:30"), ("09:00", "10:30"), ("11:05", "24:00")]
    assert validate_day([{"start": "0630", "end": "24:00", "temp": 20}])[0]["start"] == "06:30"
    for bad in ("25:00", "abc", "12:60", ""):
        with pytest.raises(vol.Invalid, match="Ungültige Uhrzeit"):
            validate_day([{"start": "00:00", "end": bad, "temp": 20}])
