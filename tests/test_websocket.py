"""WebSocket-API gegen echten HA-Core (Testharness)."""
import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smart_heating.const import DOMAIN, STORAGE_KEY

T = f"{DOMAIN}/rooms"


@pytest.fixture
async def ws(hass, hass_ws_client):
    for eid in ("climate.wz1", "climate.wz2", "climate.flur", "binary_sensor.terrasse",
                "binary_sensor.wz_fenster", "sensor.wz_temp"):
        hass.states.async_set(eid, "off" if eid.startswith("binary") else "heat")
    hass.states.async_set("climate.kaputt", "unavailable")
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    n = 0

    async def call(type_, **kw):
        nonlocal n
        n += 1
        await client.send_json({"id": n, "type": type_, **kw})
        return await client.receive_json()
    call.entry = entry
    return call


async def test_crud_roundtrip(ws, hass, hass_storage):
    r = await ws(f"{T}/create", name="Wohnzimmer", thermostats=["climate.wz1", "climate.wz2"],
                 window_sensors=["binary_sensor.wz_fenster"], temperature_sensor="sensor.wz_temp")
    assert r["success"], r
    rid = r["result"]["room_id"]
    assert r["result"]["status"]["thermostats"][0]["available"] is True

    r = await ws(f"{T}/update", room_id=rid, window_open_temp=8)
    assert r["success"] and r["result"]["window_open_temp"] == 8.0
    assert r["result"]["thermostats"] == ["climate.wz1", "climate.wz2"]

    r = await ws(f"{T}/list")
    assert [x["name"] for x in r["result"]["rooms"]] == ["Wohnzimmer"]

    await ws.entry.runtime_data.store.async_flush()
    assert rid in hass_storage[STORAGE_KEY]["data"]["rooms"]

    r = await ws(f"{T}/delete", room_id=rid)
    assert r["success"]
    r = await ws(f"{T}/list")
    assert r["result"]["rooms"] == []


async def test_unknown_entity_rejected(ws):
    r = await ws(f"{T}/create", name="X", thermostats=["climate.gibtsnicht"])
    assert not r["success"] and r["error"]["code"] == "entity_not_found"
    assert (await ws(f"{T}/list"))["result"]["rooms"] == []


async def test_registry_entity_without_state_accepted(ws, hass):
    """Gerät ist registriert, aber (z. B. nach Neustart) noch ohne State."""
    er.async_get(hass).async_get_or_create("climate", "test", "abc", suggested_object_id="spaeter")
    r = await ws(f"{T}/create", name="Gast", thermostats=["climate.spaeter"])
    assert r["success"], r
    st = r["result"]["status"]["thermostats"][0]
    assert st["missing"] is True and st["available"] is False


async def test_wrong_domain_rejected(ws):
    r = await ws(f"{T}/create", name="X", thermostats=["binary_sensor.terrasse"])
    assert r["error"]["code"] == "invalid_format"


async def test_duplicate_name_and_unknown_room(ws):
    assert (await ws(f"{T}/create", name="Bad"))["success"]
    r = await ws(f"{T}/create", name="bad")
    assert r["error"]["code"] == "duplicate_name"
    r = await ws(f"{T}/update", room_id="nix", name="Y")
    assert r["error"]["code"] == "room_not_found"
    r = await ws(f"{T}/delete", room_id="nix")
    assert r["error"]["code"] == "room_not_found"


async def test_assign_unassign_and_shared_devices(ws):
    a = (await ws(f"{T}/create", name="Esszimmer"))["result"]["room_id"]
    b = (await ws(f"{T}/create", name="Flur"))["result"]["room_id"]
    for rid in (a, b):
        r = await ws(f"{T}/assign", room_id=rid, kind="thermostat", entity_id="climate.flur")
        assert r["success"], r
        r = await ws(f"{T}/assign", room_id=rid, kind="window_sensor", entity_id="binary_sensor.terrasse")
        assert r["success"]
    assert sorted(r["result"]["status"]["thermostats"][0]["rooms"]) == sorted([a, b])

    m = (await ws(f"{DOMAIN}/devices/map"))["result"]
    assert sorted(m["thermostats"]["climate.flur"]) == sorted([a, b])
    assert sorted(m["window_sensors"]["binary_sensor.terrasse"]) == sorted([a, b])

    r = await ws(f"{T}/unassign", room_id=a, kind="thermostat", entity_id="climate.flur")
    assert r["success"] and r["result"]["thermostats"] == []
    r = await ws(f"{T}/unassign", room_id=a, kind="thermostat", entity_id="climate.flur")
    assert r["error"]["code"] == "not_assigned"
    r = await ws(f"{T}/assign", room_id=a, kind="thermostat", entity_id="climate.nix")
    assert r["error"]["code"] == "entity_not_found"


async def test_unavailable_device_visible(ws):
    r = await ws(f"{T}/create", name="Keller", thermostats=["climate.kaputt"])
    assert r["success"]
    st = r["result"]["status"]["thermostats"][0]
    assert st["state"] == "unavailable" and st["available"] is False and st["missing"] is False


async def test_existing_missing_entity_does_not_block_other_edits(ws, hass):
    rid = (await ws(f"{T}/create", name="Bad", thermostats=["climate.wz1"]))["result"]["room_id"]
    hass.states.async_remove("climate.wz1")  # Gerät verschwindet später
    r = await ws(f"{T}/update", room_id=rid, window_open_temp=6)
    assert r["success"], r


async def test_non_admin_cannot_modify(hass, hass_ws_client, hass_read_only_access_token):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": f"{T}/create", "name": "X"})
    r = await client.receive_json()
    assert r["error"]["code"] == "unauthorized"
    await client.send_json({"id": 2, "type": f"{T}/list"})
    assert (await client.receive_json())["success"]


async def test_not_loaded_error(ws, hass):
    await hass.config_entries.async_unload(ws.entry.entry_id)
    r = await ws(f"{T}/list")
    assert r["error"]["code"] == "not_loaded"


async def test_concurrent_creates_no_lost_update(ws, hass):
    import asyncio
    store = ws.entry.runtime_data.store
    from custom_components.smart_heating.rooms import create_room
    await asyncio.gather(*[
        store.async_update(lambda cfg, i=i: create_room(cfg, {"name": f"R{i}"})[0]) for i in range(10)
    ])
    assert len(store.data["rooms"]) == 10
