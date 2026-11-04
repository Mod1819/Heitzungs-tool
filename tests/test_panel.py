"""Panel: Registrierung, statische Auslieferung, Live-Abo (Modul 6)."""
import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.smart_heating.const import DOMAIN, STORAGE_KEY

TZ = ZoneInfo("Europe/Berlin")
XSS = '<img src=x onerror="window.__xss=1">'
DATA = {
    "settings": {"active_profile_id": "winter", "outdoor_enabled": True, "outdoor_sensor": "sensor.aussen"},
    "rooms": {
        "wz": {"name": "Wohnzimmer", "thermostats": ["climate.wz"], "window_sensors": ["binary_sensor.wz_f"],
               "temperature_sensor": "sensor.wz_temp"},
        "x": {"name": XSS, "thermostats": ["climate.x"]},
    },
    "profiles": {"winter": {"name": "Winter", "schedules": {"wz": {"mon": [
        {"start": "00:00", "end": "06:00", "temp": 17}, {"start": "06:00", "end": "22:00", "temp": 21},
        {"start": "22:00", "end": "24:00", "temp": 17}]}}}, "sommer": {"name": "Sommer"}},
}


async def _setup(hass, hass_storage, frontend=True):
    await hass.config.async_set_time_zone("Europe/Berlin")
    hass_storage[STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": STORAGE_KEY, "data": DATA}
    for e in ("climate.wz", "climate.x"):
        hass.states.async_set(e, "heat", {"temperature": 19, "current_temperature": 19.5, "min_temp": 5,
                                          "max_temp": 30, "target_temp_step": 0.5, "friendly_name": e})
    hass.states.async_set("binary_sensor.wz_f", "on", {"friendly_name": "Fenster WZ"})
    hass.states.async_set("sensor.aussen", "16", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.wz_temp", "20.4", {"unit_of_measurement": "°C"})
    async_mock_service(hass, "climate", "set_temperature")
    if frontend:
        hass.config.components.add("frontend")
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    with patch("homeassistant.components.panel_custom.async_register_panel", new=AsyncMock()) as reg:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, reg


async def test_panel_registered_admin_only(hass, hass_storage):
    entry, reg = await _setup(hass, hass_storage)
    reg.assert_awaited_once()
    kw = reg.await_args.kwargs
    assert kw["frontend_url_path"] == "smart-heating" and kw["require_admin"] is True
    assert kw["webcomponent_name"] == "smart-heating-panel"
    assert kw["module_url"].startswith("/smart_heating_static/smart-heating-panel.js?v=")
    with patch("homeassistant.components.frontend.async_remove_panel") as rm:
        assert await hass.config_entries.async_unload(entry.entry_id)
    rm.assert_called_once()


async def test_without_frontend_no_panel_but_setup_ok(hass, hass_storage):
    entry, reg = await _setup(hass, hass_storage, frontend=False)
    reg.assert_not_awaited()
    assert entry.runtime_data.controller.started


async def test_static_js_served(hass, hass_storage, hass_client):
    await _setup(hass, hass_storage)
    client = await hass_client()
    resp = await client.get("/smart_heating_static/smart-heating-panel.js")
    assert resp.status == 200
    body = await resp.text()
    assert 'customElements.define("smart-heating-panel"' in body
    assert (await client.get("/smart_heating_static/../manifest.json")).status in (403, 404)


@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_subscribe_live_updates(hass, hass_storage, hass_ws_client, freezer):
    await _setup(hass, hass_storage)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/subscribe"})
    assert (await ws.receive_json())["success"]
    first = (await ws.receive_json())["event"]
    assert set(first) == {"config", "status", "targets"}
    assert first["status"]["rooms"]["wz"]["locked"] is True
    assert first["status"]["rooms"]["wz"]["outdoor_reduction"] == 1.5
    if path := os.environ.get("SH_SNAPSHOT"):  # Datengrundlage für den JS-Rendertest
        await hass.async_add_executor_job(
            Path(path).write_text, json.dumps(first, ensure_ascii=False), "utf-8")

    hass.states.async_set("binary_sensor.wz_f", "off")  # Statusänderung -> Event
    await hass.async_block_till_done()
    ev = (await ws.receive_json())["event"]
    assert ev["status"]["rooms"]["wz"]["locked"] is False

    await ws.send_json({"id": 2, "type": f"{DOMAIN}/rooms/update", "room_id": "wz", "window_open_temp": 7})
    seen_result, seen_event = False, False
    while not (seen_result and seen_event):
        msg = await ws.receive_json()
        if msg.get("id") == 2 and msg["type"] == "result":
            seen_result = True
        elif msg["type"] == "event" and msg["event"]["config"]["rooms"]["wz"]["window_open_temp"] == 7:
            seen_event = True

    await ws.send_json({"id": 3, "type": "unsubscribe_events", "subscription": 1})
    msg = await ws.receive_json()
    while msg["type"] == "event":
        msg = await ws.receive_json()
    assert msg["success"]
    data = hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data
    assert data.controller._status_listeners == []


async def test_notifications_are_batched(hass, hass_storage):
    entry, _ = await _setup(hass, hass_storage)
    ctrl = entry.runtime_data.controller
    calls = []
    ctrl.async_add_status_listener(lambda: calls.append(1))
    for _ in range(5):
        ctrl.evaluate()
    await hass.async_block_till_done()
    assert len(calls) == 1


def test_now_is_berlin():
    assert datetime(2026, 11, 2, 6, 15, tzinfo=ZoneInfo("UTC")).astimezone(TZ).hour == 7
