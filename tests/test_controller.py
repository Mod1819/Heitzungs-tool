"""Thermostatansteuerung mit Fenstersperre – gegen HA-Core mit simulierten Geräten."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry, async_fire_time_changed, async_mock_service,
)

from custom_components.smart_heating.const import DOMAIN, STORAGE_KEY

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 2, 7, 15, tzinfo=TZ)  # Montag
ALLDAY = lambda t: [{"start": "00:00", "end": "24:00", "temp": t}]  # noqa: E731
WEEK = lambda day: {d: day for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}  # noqa: E731
CFG = {
    "settings": {"active_profile_id": "winter", "thermostat_changes": "adopt"},
    "rooms": {
        "wz": {"name": "Wohnzimmer", "thermostats": ["climate.wz1", "climate.wz2"],
               "window_sensors": ["binary_sensor.wz_f1", "binary_sensor.terrasse"]},
        "ez": {"name": "Esszimmer", "thermostats": ["climate.ez", "climate.flur"],
               "window_sensors": ["binary_sensor.terrasse"], "window_open_temp": 6},
        "flur": {"name": "Flur", "thermostats": ["climate.flur"], "window_sensors": ["binary_sensor.tuer"]},
    },
    "profiles": {"winter": {"name": "Winter", "schedules": {
        "wz": WEEK([{"start": "00:00", "end": "06:00", "temp": 17},
                    {"start": "06:00", "end": "08:30", "temp": 21},
                    {"start": "08:30", "end": "24:00", "temp": 18}]),
        "ez": WEEK(ALLDAY(20)), "flur": WEEK(ALLDAY(18))}}},
}
THERMOS = ("climate.wz1", "climate.wz2", "climate.ez", "climate.flur")
WINDOWS = ("binary_sensor.wz_f1", "binary_sensor.terrasse", "binary_sensor.tuer")
ATTRS = {"min_temp": 5, "max_temp": 30, "target_temp_step": 0.5, "current_temperature": 19}


class Rig:
    def __init__(self, hass, freezer, calls, entry):
        self.hass, self.freezer, self.calls, self.entry, self.seen = hass, freezer, calls, entry, 0

    def new_calls(self):
        out = {c.data["entity_id"]: c.data["temperature"] for c in self.calls[self.seen:]}
        self.seen = len(self.calls)
        return out

    async def ack(self):
        """Geräte übernehmen die zuletzt gesendeten Werte."""
        last = {c.data["entity_id"]: c.data["temperature"] for c in self.calls}
        for eid, t in last.items():
            await self.device(eid, t)
        return self.new_calls()

    async def device(self, eid, temp, state="heat", **extra):
        old = self.hass.states.get(eid)
        attrs = {**(old.attributes if old else ATTRS), "temperature": temp, **extra}
        self.hass.states.async_set(eid, state, attrs)
        await self.hass.async_block_till_done()

    async def window(self, eid, state):
        self.hass.states.async_set(eid, state)
        await self.hass.async_block_till_done()

    async def tick(self, delta=None, when=None):
        when = when or (datetime.now(TZ) + delta)
        self.freezer.move_to(when)
        async_fire_time_changed(self.hass, when)
        await self.hass.async_block_till_done()

    def room(self, rid):
        return self.entry.runtime_data.controller.status()["rooms"][rid]

    def thermo(self, eid):
        return self.entry.runtime_data.controller.status()["thermostats"][eid]


async def _prepare(hass, hass_storage, freezer, data=CFG, thermo_temp=19):
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to(NOW)
    hass_storage[STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": STORAGE_KEY, "data": data}
    for e in THERMOS:
        hass.states.async_set(e, "heat", {**ATTRS, "temperature": thermo_temp})
    for e in WINDOWS:
        hass.states.async_set(e, "off")
    calls = async_mock_service(hass, "climate", "set_temperature")
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    return calls, entry


@pytest.fixture
async def rig(hass, hass_storage, freezer):
    calls, entry = await _prepare(hass, hass_storage, freezer)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    r = Rig(hass, freezer, calls, entry)
    assert r.new_calls() == {"climate.wz1": 21, "climate.wz2": 21, "climate.ez": 20, "climate.flur": 18}
    await r.ack()
    return r


async def test_multiple_thermostats_no_repeated_calls(rig):
    assert rig.thermo("climate.wz1")["status"] == "ok"
    await rig.device("climate.wz1", 21, current_temperature=20.3)  # nur Ist-Wert
    rig.entry.runtime_data.controller.evaluate()
    await rig.hass.async_block_till_done()
    assert rig.new_calls() == {}


async def test_window_open_and_close(rig):
    await rig.window("binary_sensor.wz_f1", "on")
    assert rig.new_calls() == {"climate.wz1": 5, "climate.wz2": 5}
    assert rig.room("wz")["locked"] and rig.room("wz")["desired_temp"] == 21
    await rig.ack()
    await rig.window("binary_sensor.wz_f1", "off")
    assert rig.new_calls() == {"climate.wz1": 21, "climate.wz2": 21}


async def test_multiple_windows_all_must_close(rig):
    await rig.window("binary_sensor.wz_f1", "on")
    await rig.window("binary_sensor.terrasse", "on")
    await rig.ack()
    await rig.window("binary_sensor.wz_f1", "off")
    assert rig.new_calls() == {} and rig.room("wz")["locked"]
    await rig.window("binary_sensor.terrasse", "off")
    calls = rig.new_calls()
    assert calls["climate.wz1"] == calls["climate.wz2"] == 21


async def test_one_window_many_thermostats_and_shared_thermostat(rig):
    await rig.window("binary_sensor.terrasse", "on")
    assert rig.new_calls() == {"climate.wz1": 5, "climate.wz2": 5, "climate.ez": 6, "climate.flur": 6}
    assert rig.thermo("climate.flur")["locked"] is True
    assert rig.room("flur")["effective_temp"] == 18  # Flur selbst ungesperrt, Wert getrennt
    await rig.ack()
    await rig.window("binary_sensor.terrasse", "off")
    assert rig.new_calls() == {"climate.wz1": 21, "climate.wz2": 21, "climate.ez": 20, "climate.flur": 18}


async def test_manual_at_thermostat_during_open_window(rig):
    """Kernszenario: Fenster auf -> 5 °C, Benutzer stellt 22 °C -> bleibt gesperrt, 22 wird gemerkt."""
    await rig.window("binary_sensor.wz_f1", "on")
    await rig.ack()
    await rig.device("climate.wz1", 22)  # Bedienung am Gerät
    calls = rig.new_calls()
    assert calls == {"climate.wz1": 5}  # Sperre sofort erneut durchgesetzt
    assert rig.room("wz")["desired_temp"] == 22 and rig.room("wz")["effective_temp"] == 5
    assert rig.entry.runtime_data.store.data["overrides"]["wz"]["temp"] == 22
    await rig.ack()
    await rig.window("binary_sensor.wz_f1", "off")
    assert rig.new_calls() == {"climate.wz1": 22, "climate.wz2": 22}


async def test_manual_via_api_during_open_window(rig, hass_ws_client):
    await rig.window("binary_sensor.wz_f1", "on")
    await rig.ack()
    ws = await hass_ws_client(rig.hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/manual/set", "room_id": "wz", "temperature": 23,
                        "mode": "permanent"})
    assert (await ws.receive_json())["success"]
    await rig.hass.async_block_till_done()
    assert rig.new_calls() == {} and rig.room("wz")["effective_temp"] == 5
    await rig.window("binary_sensor.wz_f1", "off")
    assert rig.new_calls() == {"climate.wz1": 23, "climate.wz2": 23}


async def test_manual_without_window_until_next_slot(rig):
    await rig.device("climate.wz1", 23)
    assert rig.new_calls() == {"climate.wz2": 23}  # ganzer Raum folgt
    await rig.ack()
    await rig.tick(when=datetime(2026, 11, 2, 8, 30, tzinfo=TZ))
    assert rig.new_calls() == {"climate.wz1": 18, "climate.wz2": 18}


async def test_shared_thermostat_restores_lowest_after_close(rig, hass_ws_client):
    await rig.window("binary_sensor.terrasse", "on")
    await rig.ack()
    ws = await hass_ws_client(rig.hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/manual/set", "room_id": "flur", "temperature": 16,
                        "mode": "permanent"})
    assert (await ws.receive_json())["success"]
    await rig.hass.async_block_till_done()
    assert "climate.flur" not in rig.new_calls()  # weiter 6 °C (gesperrt)
    await rig.window("binary_sensor.terrasse", "off")
    assert rig.new_calls()["climate.flur"] == 16  # min(EZ 20, Flur 16)


async def test_unavailable_window_locks_and_is_visible(rig):
    await rig.window("binary_sensor.tuer", "unavailable")
    assert rig.new_calls() == {"climate.flur": 5}
    room = rig.room("flur")
    assert room["reason"] == "window_unknown" and room["sensors"]["binary_sensor.tuer"] == "unknown"
    await rig.ack()
    store = rig.entry.runtime_data.store
    await store.async_update(
        lambda c: {**c, "settings": {**c["settings"], "unknown_window_policy": "closed"}})
    await rig.hass.async_block_till_done()
    assert rig.new_calls() == {"climate.flur": 18}
    assert rig.room("flur")["window"] == "unknown"  # weiterhin sichtbar


async def test_restart_no_uncontrolled_release(hass, hass_storage, freezer):
    """Neustart: vor 'HA gestartet' nichts senden; offenes/unbekanntes Fenster -> nur Sperrwert."""
    calls, entry = await _prepare(hass, hass_storage, freezer, thermo_temp=5)
    hass.states.async_set("binary_sensor.wz_f1", "unavailable")  # Kontakt noch nicht da
    hass.set_state(CoreState.not_running)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(calls) == 0 and entry.runtime_data.controller.started is False
    hass.states.async_set("binary_sensor.tuer", "on")
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    sent = {c.data["entity_id"]: c.data["temperature"] for c in calls}
    # WZ (Kontakt unbekannt) und Flur-Thermostat (Tür offen) bleiben auf 5 -> kein Aufruf
    assert sent == {"climate.ez": 20}


async def test_echo_intermediate_values_and_confirmation(rig):
    await rig.window("binary_sensor.wz_f1", "on")
    rig.new_calls()
    await rig.device("climate.wz1", 15)  # Zwischenwert während Übertragung
    assert rig.new_calls() == {} and "wz" not in rig.entry.runtime_data.store.data["overrides"]
    await rig.device("climate.wz1", 5)
    assert rig.thermo("climate.wz1")["status"] == "ok"


async def test_retry_limit_no_endless_loop(rig):
    await rig.window("binary_sensor.wz_f1", "on")
    assert rig.new_calls() == {"climate.wz1": 5, "climate.wz2": 5}
    await rig.device("climate.wz2", 5)
    for _ in range(2):  # Gerät reagiert nicht -> 2 Wiederholungen
        await rig.tick(timedelta(seconds=31))
        assert rig.new_calls() == {"climate.wz1": 5}
    await rig.tick(timedelta(seconds=31))
    assert rig.new_calls() == {} and rig.thermo("climate.wz1")["status"] == "not_confirmed"
    rig.entry.runtime_data.controller.evaluate()
    await rig.hass.async_block_till_done()
    assert rig.new_calls() == {}
    await rig.window("binary_sensor.wz_f1", "off")
    # Gerät steht noch auf 21 (hat 5 nie übernommen) -> nichts zu senden, Status wieder ok
    assert rig.new_calls() == {"climate.wz2": 21}
    assert rig.thermo("climate.wz1")["status"] == "ok"
    await rig.device("climate.wz2", 21)
    await rig.window("binary_sensor.wz_f1", "on")  # neuer Sperrvorgang -> wird wieder versucht
    assert rig.new_calls()["climate.wz1"] == 5


async def test_change_during_pending_window_counts_as_manual(rig):
    await rig.device("climate.wz1", 21)
    await rig.window("binary_sensor.wz_f1", "on")
    rig.new_calls()
    await rig.device("climate.wz2", 5)
    await rig.device("climate.wz1", 24)  # Benutzer dreht, bevor Gerät 5 bestätigt
    await rig.tick(timedelta(seconds=31))
    assert rig.entry.runtime_data.store.data["overrides"]["wz"]["temp"] == 24
    assert rig.new_calls() == {"climate.wz1": 5} and rig.room("wz")["locked"]


async def test_unavailable_thermostat_then_back(rig):
    await rig.device("climate.wz1", 21, state="unavailable")
    await rig.window("binary_sensor.wz_f1", "on")
    assert rig.new_calls() == {"climate.wz2": 5}
    assert rig.thermo("climate.wz1")["status"] == "unavailable"
    await rig.device("climate.wz1", 21)
    assert rig.new_calls() == {"climate.wz1": 5}


async def test_hvac_off_not_touched(rig):
    await rig.device("climate.ez", 20, state="off")
    await rig.window("binary_sensor.terrasse", "on")
    assert "climate.ez" not in rig.new_calls()
    assert rig.thermo("climate.ez")["status"] == "hvac_off"


async def test_clamp_and_step(rig):
    await rig.device("climate.wz1", 21, min_temp=7, target_temp_step=1)
    await rig.window("binary_sensor.wz_f1", "on")
    assert rig.new_calls() == {"climate.wz1": 7, "climate.wz2": 5}
    await rig.ack()
    assert "wz" not in rig.entry.runtime_data.store.data["overrides"]  # 7 ist kein Handbetrieb


async def test_service_error_does_not_break_control(hass, hass_storage, freezer):
    calls, entry = await _prepare(hass, hass_storage, freezer)

    async def boom(call):
        if call.data["entity_id"] == "climate.ez":
            raise HomeAssistantError("Gerät antwortet nicht")
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", boom)
    calls.clear()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    status = entry.runtime_data.controller.status()["thermostats"]
    assert status["climate.ez"]["status"] == "service_error"
    assert {c.data["entity_id"] for c in calls} == {"climate.wz1", "climate.wz2", "climate.flur"}


async def test_status_api(rig, hass_ws_client):
    await rig.window("binary_sensor.wz_f1", "on")
    ws = await hass_ws_client(rig.hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/status"})
    r = (await ws.receive_json())["result"]
    assert r["started"] and r["rooms"]["wz"]["locked"] and r["rooms"]["wz"]["window"] == "open"
    assert r["thermostats"]["climate.wz1"]["status"] == "pending"
    assert any("Fenstersperre aktiv" in a["message"] for a in r["actions"]["wz"])


async def test_removed_thermostat_no_longer_controlled(rig):
    from custom_components.smart_heating.rooms import unassign_entity
    store = rig.entry.runtime_data.store
    await store.async_update(lambda c: unassign_entity(c, "wz", "thermostat", "climate.wz2"))
    await rig.hass.async_block_till_done()
    await rig.window("binary_sensor.wz_f1", "on")
    assert rig.new_calls() == {"climate.wz1": 5}
