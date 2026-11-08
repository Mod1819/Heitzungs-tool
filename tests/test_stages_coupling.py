"""v1.2: mehrstufige Meldungen mit Aktionen (Rollo/Skript/Szene) und gekoppelte Kontakte."""
import copy
from datetime import UTC, datetime, timedelta

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import async_capture_events, async_mock_service

from custom_components.smart_heating.const import DOMAIN
from custom_components.smart_heating.models import validate_config
from custom_components.smart_heating.targets import resolve_effective
from tests.test_controller import ATTRS, CFG, WEEK, Rig, _prepare

T0 = datetime(2026, 11, 2, 6, 15, tzinfo=UTC)
ALL20 = WEEK([{"start": "00:00", "end": "24:00", "temp": 20}])


# --- Validierung & Migration ---------------------------------------------------

def _room(**kw):
    return validate_config({"rooms": {"r": {"name": "R", **kw}}})["rooms"]["r"]


def test_stages_sorted_and_defaults():
    st = _room(notify_stages=[{"after": 1200}, {"after": 300, "notify": False, "action_entity": "script.x"}])
    assert [s["after"] for s in st["notify_stages"]] == [300, 1200]
    assert st["notify_stages"][1] == {"after": 1200, "notify": True, "action_entity": None,
                                      "cover_position": None, "texts": []}


@pytest.mark.parametrize("stages", [
    [{"after": 30}],                                               # < 1 Minute
    [{"after": 300}, {"after": 300}],                              # doppelt
    [{"after": 60 * (i + 1)} for i in range(11)],                  # > 10 Stufen
    [{"after": 300, "action_entity": "light.wz"}],                 # falsche Domain
    [{"after": 300, "action_entity": "script.x", "cover_position": 50}],  # Position ohne Rollo
    [{"after": 300, "action_entity": "cover.wz", "cover_position": 150}],
])
def test_invalid_stages(stages):
    with pytest.raises(vol.Invalid):
        _room(notify_stages=stages)


def test_migration_single_notify_after_to_stage():
    r = _room(notify_after=600)
    assert r["notify_after"] == 0 and r["notify_stages"] == [
        {"after": 600, "notify": True, "action_entity": None, "cover_position": None, "texts": []}]
    assert validate_config({"rooms": {"r": r}})["rooms"]["r"] == r  # stabil
    # vorhandene Stufen haben Vorrang
    r = _room(notify_after=600, notify_stages=[{"after": 120}])
    assert [s["after"] for s in r["notify_stages"]] == [120]


@pytest.mark.parametrize("coupled, extra", [
    ([{"entity_id": "binary_sensor.t"}, {"entity_id": "binary_sensor.t", "delay": 5}], {}),
    ([{"entity_id": "climate.t"}], {}),
    ([{"entity_id": "binary_sensor.t", "delay": -1}], {}),
    ([{"entity_id": "binary_sensor.t"}], {"window_sensors": ["binary_sensor.t"]}),  # eigener Kontakt
])
def test_invalid_coupling(coupled, extra):
    with pytest.raises(vol.Invalid):
        _room(coupled_sensors=coupled, **extra)


# --- Kopplungslogik (rein) -----------------------------------------------------

LOGIC = validate_config({"rooms": {
    "wz": {"name": "WZ", "thermostats": ["climate.wz"], "window_sensors": ["binary_sensor.terrasse"],
           "window_open_delay": 300},
    "kueche": {"name": "Küche", "thermostats": ["climate.kueche"], "window_sensors": ["binary_sensor.kf"],
               "coupled_sensors": [{"entity_id": "binary_sensor.terrasse", "delay": 900}]},
}})
D = {r: {"desired_temp": 20, "source": "schedule"} for r in ("wz", "kueche")}


def _res(terrasse, seconds, kf="closed"):
    w = {"binary_sensor.terrasse": terrasse, "binary_sensor.kf": kf}
    now = T0 + timedelta(seconds=seconds)
    return resolve_effective(LOGIC, D, w, None, {"binary_sensor.terrasse": T0}, now)


def test_coupled_room_follows_after_own_delay():
    r = _res("open", 600)["rooms"]
    assert r["wz"]["locked"] and not r["kueche"]["locked"]
    assert r["kueche"]["lock_pending_until"] == (T0 + timedelta(seconds=900)).isoformat()
    assert r["kueche"]["window"] == "closed"           # eigenes Fenster zu
    r = _res("open", 900)["rooms"]["kueche"]
    assert r["locked"] and r["reason"] == "window_coupled" and r["locked_by"] == ["binary_sensor.terrasse"]
    assert r["effective_temp"] == 5 and r["open_since"] is None  # keine eigene Öffnung -> keine Meldung


def test_coupled_unknown_does_not_lock_but_own_does():
    r = _res("unknown", 9999)["rooms"]
    assert r["wz"]["locked"] and not r["kueche"]["locked"]
    assert r["kueche"]["coupled"] == {"binary_sensor.terrasse": "unknown"}


def test_own_window_reason_wins():
    r = _res("open", 1000, kf="open")["rooms"]["kueche"]
    assert r["reason"] == "window_open"
    assert set(r["locked_by"]) == {"binary_sensor.kf", "binary_sensor.terrasse"}


# --- im laufenden System -------------------------------------------------------

def _system_cfg():
    data = copy.deepcopy(CFG)
    data["rooms"]["wz"]["window_open_delay"] = 300
    data["rooms"]["kueche"] = {
        "name": "Küche", "thermostats": ["climate.kueche"], "window_sensors": ["binary_sensor.kueche_f"],
        "coupled_sensors": [{"entity_id": "binary_sensor.terrasse", "delay": 900}],
        "notify_stages": [{"after": 300}],
    }
    data["rooms"]["diele"] = {  # kein Fenster
        "name": "Diele", "thermostats": ["climate.diele"],
        "coupled_sensors": [{"entity_id": "binary_sensor.terrasse", "delay": 900}],
    }
    data["rooms"]["bad"] = {
        "name": "Bad", "thermostats": ["climate.bad"], "window_sensors": ["binary_sensor.bad_f"],
        "notify_stages": [{"after": 300}, {"after": 600}, {"after": 1200},
                          {"after": 1800, "action_entity": "cover.bad"}],
        "notify_on_close": True,
    }
    for rid in ("kueche", "diele", "bad"):
        data["profiles"]["winter"]["schedules"][rid] = ALL20
    data["rooms"]["ez"]["window_sensors"] = []  # EZ hier nicht relevant
    data["settings"]["notify_service"] = "notify.telegram"
    return data


async def _start(hass, hass_storage, freezer, data=None):
    calls, entry = await _prepare(hass, hass_storage, freezer, data or _system_cfg())
    for e in ("climate.kueche", "climate.diele", "climate.bad"):
        hass.states.async_set(e, "heat", {**ATTRS, "temperature": 19})
    for e in ("binary_sensor.kueche_f", "binary_sensor.bad_f"):
        hass.states.async_set(e, "off")
    hass.states.async_set("cover.bad", "open")
    hass.states.async_set("script.rollo_wz", "off")
    msgs = async_mock_service(hass, "notify", "telegram")
    covers = async_mock_service(hass, "cover", "close_cover")
    opened = async_mock_service(hass, "cover", "open_cover")
    positions = async_mock_service(hass, "cover", "set_cover_position")
    scripts = async_mock_service(hass, "script", "turn_on")
    events = async_capture_events(hass, "smart_heating_window_alert")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rig = Rig(hass, freezer, calls, entry)
    rig.new_calls()
    await rig.ack()
    return rig, {"msgs": msgs, "covers": covers, "opened": opened, "positions": positions,
                 "scripts": scripts, "events": events}


async def test_terrace_door_couples_kitchen_and_hall(hass, hass_storage, freezer):
    rig, svc = await _start(hass, hass_storage, freezer)
    await rig.window("binary_sensor.terrasse", "on")
    assert rig.new_calls() == {}                                        # alle warten
    await rig.tick(timedelta(minutes=5))
    assert rig.new_calls() == {"climate.wz1": 5, "climate.wz2": 5}      # nur Wohnzimmer
    await rig.ack()
    await rig.tick(timedelta(minutes=10))                               # 15 min offen
    assert rig.new_calls() == {"climate.kueche": 5, "climate.diele": 5}
    assert rig.room("kueche")["reason"] == "window_coupled"
    assert svc["msgs"] == []                                            # Küche meldet nicht (fremder Kontakt)
    acts = rig.entry.runtime_data.controller.status()["actions"]["kueche"]
    assert any("gekoppelt" in a["message"] for a in acts)
    await rig.ack()
    await rig.window("binary_sensor.terrasse", "off")
    assert rig.new_calls() == {"climate.wz1": 21, "climate.wz2": 21,
                               "climate.kueche": 20, "climate.diele": 20}


async def test_short_terrace_opening_kitchen_unaffected(hass, hass_storage, freezer):
    rig, _ = await _start(hass, hass_storage, freezer)
    await rig.window("binary_sensor.terrasse", "on")
    await rig.tick(timedelta(minutes=8))
    assert "climate.kueche" not in rig.new_calls()
    await rig.ack()
    await rig.window("binary_sensor.terrasse", "off")
    await rig.tick(timedelta(minutes=20))
    assert "climate.kueche" not in rig.new_calls()


async def test_kitchen_own_window_independent(hass, hass_storage, freezer):
    rig, svc = await _start(hass, hass_storage, freezer)
    await rig.window("binary_sensor.kueche_f", "on")
    assert rig.new_calls() == {"climate.kueche": 5}                     # eigenes Fenster sofort
    await rig.tick(timedelta(minutes=5))
    assert len(svc["msgs"]) == 1 and "Küche" in svc["msgs"][0].data["message"]


async def test_coupled_sensor_unavailable_kitchen_keeps_heating(hass, hass_storage, freezer):
    rig, _ = await _start(hass, hass_storage, freezer)
    await rig.window("binary_sensor.terrasse", "unavailable")
    await rig.tick(timedelta(minutes=30))
    calls = rig.new_calls()
    assert calls.get("climate.wz1") == 5 and "climate.kueche" not in calls


async def test_four_stages_with_cover_no_reopen(hass, hass_storage, freezer):
    rig, svc = await _start(hass, hass_storage, freezer)
    await rig.window("binary_sensor.bad_f", "on")
    expected = {5: 1, 10: 2, 20: 3}
    elapsed = 0
    for minute, count in expected.items():
        await rig.tick(timedelta(minutes=minute - elapsed))
        elapsed = minute
        assert len(svc["msgs"]) == count, minute
    assert "(2. Erinnerung)" in svc["msgs"][1].data["message"]
    assert "(3. Erinnerung)" in svc["msgs"][2].data["message"]
    assert svc["covers"] == []
    await rig.tick(timedelta(minutes=10))                                # 30 min
    assert len(svc["msgs"]) == 4 and len(svc["covers"]) == 1
    assert svc["covers"][0].data["entity_id"] == "cover.bad"
    assert "Rollo wird geschlossen" in svc["msgs"][3].data["message"]
    assert [e.data["stage"] for e in svc["events"]] == [1, 2, 3, 4]
    await rig.tick(timedelta(hours=2))
    assert len(svc["msgs"]) == 4 and len(svc["covers"]) == 1             # nichts doppelt
    await rig.window("binary_sensor.bad_f", "off")
    assert "wieder geschlossen" in svc["msgs"][-1].data["message"]
    assert svc["opened"] == [] and svc["positions"] == []                # Rollo bleibt unten
    await rig.window("binary_sensor.bad_f", "on")                        # neue Öffnung -> von vorn
    await rig.tick(timedelta(minutes=5))
    assert svc["msgs"][-1].data["message"].startswith("🪟 Bad")


async def test_stage_action_only_script_and_position(hass, hass_storage, freezer):
    data = _system_cfg()
    data["rooms"]["bad"]["notify_stages"] = [
        {"after": 300, "notify": False, "action_entity": "script.rollo_wz"},
        {"after": 600, "notify": False, "action_entity": "cover.bad", "cover_position": 30},
    ]
    rig, svc = await _start(hass, hass_storage, freezer, data)
    await rig.window("binary_sensor.bad_f", "on")
    await rig.tick(timedelta(minutes=5))
    assert svc["msgs"] == [] and svc["scripts"][0].data == {"entity_id": "script.rollo_wz"}
    await rig.tick(timedelta(minutes=5))
    assert svc["positions"][0].data == {"entity_id": "cover.bad", "position": 30}
    assert len(svc["events"]) == 2 and svc["events"][1].data["action_entity"] == "cover.bad"


async def test_stages_survive_restart_of_integration(hass, hass_storage, freezer):
    rig, _ = await _start(hass, hass_storage, freezer)
    entry = rig.entry
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    room = entry.runtime_data.store.data["rooms"]["bad"]
    assert [s["after"] for s in room["notify_stages"]] == [300, 600, 1200, 1800]
    assert entry.runtime_data.store.data["rooms"]["kueche"]["coupled_sensors"][0]["delay"] == 900


@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_api_validation(hass, hass_storage, freezer, hass_ws_client):
    await _start(hass, hass_storage, freezer)
    ws = await hass_ws_client(hass)
    n = 0

    async def upd(**kw):
        nonlocal n
        n += 1
        await ws.send_json({"id": n, "type": f"{DOMAIN}/rooms/update", **kw})
        return await ws.receive_json()

    r = await upd(room_id="kueche", coupled_sensors=[{"entity_id": "binary_sensor.gibtsnicht", "delay": 60}])
    assert r["error"]["code"] == "entity_not_found"
    r = await upd(room_id="bad", notify_stages=[{"after": 300, "action_entity": "cover.gibtsnicht"}])
    assert r["error"]["code"] == "entity_not_found"
    r = await upd(room_id="kueche", coupled_sensors=[{"entity_id": "binary_sensor.kueche_f"}])
    assert r["error"]["code"] == "invalid_format"
    r = await upd(room_id="diele", coupled_sensors=[{"entity_id": "binary_sensor.wz_f1", "delay": 600}],
                  notify_stages=[{"after": 600, "action_entity": "cover.bad", "cover_position": 0}])
    assert r["success"], r
    assert r["result"]["coupled_sensors"] == [{"entity_id": "binary_sensor.wz_f1", "delay": 600}]
